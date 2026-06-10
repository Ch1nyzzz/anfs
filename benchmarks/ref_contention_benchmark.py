#!/usr/bin/env python3
import argparse
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time
from pathlib import Path

import anfs_core


def millis(start):
    return (time.perf_counter() - start) * 1000.0


def approve_worker(
    db_path,
    objs_dir,
    worker_idx,
    round_idx,
    target_ref,
    evidence_ref,
    expected_version,
    queue,
):
    start = time.perf_counter()
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.approve(
            target_ref,
            [evidence_ref],
            f"reviewer_agent_{worker_idx}",
            run_id=f"run:ref-contention:{round_idx}:{worker_idx}",
            tool_call_id=f"approve:{round_idx}:{worker_idx}",
            expected_version=expected_version,
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "status": "approved",
                "elapsed_ms": millis(start),
                "detail": "",
            }
        )
    except anfs_core.RefConflictError as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "status": "conflict",
                "elapsed_ms": millis(start),
                "detail": str(exc),
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "status": "error",
                "elapsed_ms": millis(start),
                "detail": f"{type(exc).__name__}: {exc}",
            }
        )


def sqlite_sequence_status(db_path):
    with sqlite3.connect(db_path) as conn:
        seqs = [
            row[0]
            for row in conn.execute(
                "SELECT seq FROM event_sequence ORDER BY seq"
            ).fetchall()
        ]
        kind_counts = dict(
            conn.execute(
                "SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY kind"
            ).fetchall()
        )
    return {
        "event_count": len(seqs),
        "max_seq": max(seqs, default=0),
        "sequence_contiguous": seqs == list(range(1, len(seqs) + 1)),
        "event_kinds": kind_counts,
    }


def run_round(db_path, objs_dir, round_idx, workers, timeout_s):
    fs = anfs_core.AnfsEngine(db_path, objs_dir)
    coder_ws = fs.open_workspace(
        f"ws:coder-{round_idx}",
        "coder_agent",
        run_id=f"run:setup:{round_idx}:coder",
    )
    tester_ws = fs.open_workspace(
        f"ws:tester-{round_idx}",
        "tester_agent",
        run_id=f"run:setup:{round_idx}:tester",
    )

    patch_path = f"round-{round_idx}/src/db.py"
    patch_ref = f"artifact:patch@contention-{round_idx}"
    evidence_path = f"round-{round_idx}/test.log"
    evidence_ref = f"artifact:test_result@contention-{round_idx}"
    patch_node = coder_ws.write(
        patch_path,
        f"print('round {round_idx}')\n".encode("utf-8"),
        [],
        tool_call_id=f"write-patch:{round_idx}",
    )
    coder_ws.publish(patch_path, patch_ref)
    tester_ws.write(
        evidence_path,
        f"PASS round {round_idx}\n".encode("utf-8"),
        [patch_node],
        tool_call_id=f"write-test:{round_idx}",
    )
    tester_ws.publish(evidence_path, evidence_ref)
    expected_version = fs.get_ref(patch_ref)[4]

    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=approve_worker,
            args=(
                db_path,
                objs_dir,
                worker_idx,
                round_idx,
                patch_ref,
                evidence_ref,
                expected_version,
                queue,
            ),
        )
        for worker_idx in range(workers)
    ]
    start = time.perf_counter()
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout_s)

    results = []
    while not queue.empty():
        results.append(queue.get())
    results.sort(key=lambda row: row["worker_idx"])
    exitcodes = [process.exitcode for process in processes]
    statuses = [result["status"] for result in results]

    final_fs = anfs_core.AnfsEngine(db_path, objs_dir)
    final_ref = final_fs.get_ref(patch_ref)
    history = final_fs.ref_history(patch_ref)
    approve_history = [row for row in history if row[2] == "approve"]
    conflict_details = [
        result["detail"] for result in results if result["status"] == "conflict"
    ]
    passed = (
        exitcodes == [0] * workers
        and len(results) == workers
        and statuses.count("approved") == 1
        and statuses.count("conflict") == workers - 1
        and statuses.count("error") == 0
        and final_ref[3] == "approved"
        and final_ref[4] == expected_version + 1
        and [row[2] for row in history] == ["publish", "approve"]
        and len(approve_history) == 1
        and all(
            "version mismatch" in detail or "changed concurrently" in detail
            for detail in conflict_details
        )
    )
    return {
        "round": round_idx,
        "workers": workers,
        "elapsed_ms": millis(start),
        "expected_version": expected_version,
        "approved": statuses.count("approved"),
        "conflicts": statuses.count("conflict"),
        "errors": statuses.count("error"),
        "exitcodes": exitcodes,
        "final_state": final_ref[3],
        "final_ref_version": final_ref[4],
        "history_kinds": [row[2] for row in history],
        "worker_elapsed_ms": [result["elapsed_ms"] for result in results],
        "failure_details": [
            result["detail"] for result in results if result["status"] == "error"
        ],
        "passed": passed,
    }


def run_benchmark(args):
    root_start = time.perf_counter()
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        db_path = str(root / "anfs.db")
        objs_dir = str(root / "objects")
        anfs_core.AnfsEngine(db_path, objs_dir)

        rounds = [
            run_round(db_path, objs_dir, round_idx, args.workers, args.timeout_s)
            for round_idx in range(args.rounds)
        ]
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        integrity_issues = fs.verify_integrity()
        sequence = sqlite_sequence_status(db_path)

    passed = (
        all(row["passed"] for row in rounds)
        and not integrity_issues
        and sequence["sequence_contiguous"]
    )
    return {
        "schema": "anfs.ref_contention_benchmark.v1",
        "config": {
            "workers": args.workers,
            "rounds": args.rounds,
            "timeout_s": args.timeout_s,
        },
        "summary": {
            "passed": passed,
            "elapsed_ms": millis(root_start),
            "rounds_passed": sum(1 for row in rounds if row["passed"]),
            "rounds_total": len(rounds),
            "total_approved": sum(row["approved"] for row in rounds),
            "total_conflicts": sum(row["conflicts"] for row in rounds),
            "total_errors": sum(row["errors"] for row in rounds),
            "integrity_issue_count": len(integrity_issues),
            **sequence,
        },
        "rounds": rounds,
        "integrity_issues": integrity_issues,
    }


def parse_args():
    parser = argparse.ArgumentParser(
        description="ANFS shared-ref stale-version contention benchmark"
    )
    parser.add_argument("--workers", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--timeout-s", type=int, default=60)
    parser.add_argument("--output-json")
    args = parser.parse_args()
    if args.workers < 2:
        raise SystemExit("--workers must be at least 2")
    if args.rounds <= 0:
        raise SystemExit("--rounds must be positive")
    if args.timeout_s <= 0:
        raise SystemExit("--timeout-s must be positive")
    return args


def main():
    args = parse_args()
    result = run_benchmark(args)
    text = json.dumps(result, indent=2, sort_keys=True)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(text + "\n", encoding="utf-8")
    print(text)
    if not result["summary"]["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
