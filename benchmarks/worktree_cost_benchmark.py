#!/usr/bin/env python3
"""C4 cost: what does the ANFS guarantee cost an agent session?

A coding agent does its work on a materialized worktree — an ordinary
directory — so its inner edit loop runs at native filesystem speed (the
worktree IS a real directory; ANFS is not in the path). ANFS overhead is paid
only at the session BOUNDARIES: materialize once at checkout, commit + verify
once at the end. This harness measures that boundary cost against a raw-FS
baseline that does the same file work with no provenance, conflict detection,
or integrity.

The honest message is the amortization: the price of provenance + integrity +
conflict detection is a bounded per-session cost, not a per-edit tax.

Run:
    python benchmarks/worktree_cost_benchmark.py
    python benchmarks/worktree_cost_benchmark.py --json docs/benchmark_snapshots/worktree_cost.json
"""

import argparse
import json
import os
import shutil
import tempfile
import time

import anfs_core

BASE = "resource:repo/main@v1"


def perf():
    return time.perf_counter()


def make_repo_files(n_files):
    return {
        f"src/mod_{i:03d}.py": (f"# module {i}\n" + "x = 1\n" * 20).encode()
        for i in range(n_files)
    }


def edit_dir(root, files, n_edits):
    """Apply n_edits ordinary file modifications inside a real directory."""
    names = sorted(files)[:n_edits]
    for name in names:
        path = os.path.join(root, name)
        with open(path, "ab") as h:
            h.write(b"# edited by agent\n")


def run_anfs(n_files, n_edits, trials):
    files = make_repo_files(n_files)
    phases = {"materialize": [], "inner_edits": [], "commit": [], "integrity": []}
    with tempfile.TemporaryDirectory() as tmpdir:
        for t in range(trials):
            fs = anfs_core.AnfsEngine(
                os.path.join(tmpdir, f"anfs_{t}.db"), os.path.join(tmpdir, f"objs_{t}")
            )
            importer = fs.open_workspace("ws:importer", "importer_agent")
            for name, content in files.items():
                importer.write(name, content, [])
                importer.publish(name, f"{BASE}/{name}", "resource")
            fs.checkout("ws:coder", "coder_agent", BASE)

            work = os.path.join(tmpdir, f"work_{t}")
            s = perf()
            fs.materialize_workspace("ws:coder", work)
            phases["materialize"].append(perf() - s)

            s = perf()
            edit_dir(work, files, n_edits)  # ordinary file IO, ANFS not in path
            phases["inner_edits"].append(perf() - s)

            s = perf()
            fs.commit_worktree("ws:coder", work, "coder_agent", require_manifest_match=True)
            phases["commit"].append(perf() - s)

            s = perf()
            integrity = fs.verify_integrity()
            phases["integrity"].append(perf() - s)
            assert integrity == [], integrity
    return {k: 1000 * sum(v) / len(v) for k, v in phases.items()}  # ms, mean


def run_native(n_files, n_edits, trials):
    files = make_repo_files(n_files)
    setup, inner = [], []
    with tempfile.TemporaryDirectory() as tmpdir:
        for t in range(trials):
            work = os.path.join(tmpdir, f"native_{t}")
            s = perf()
            for name, content in files.items():
                path = os.path.join(work, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as h:
                    h.write(content)
            setup.append(perf() - s)

            s = perf()
            edit_dir(work, files, n_edits)
            inner.append(perf() - s)
            shutil.rmtree(work)
    return {
        "setup": 1000 * sum(setup) / len(setup),
        "inner_edits": 1000 * sum(inner) / len(inner),
    }


def main():
    parser = argparse.ArgumentParser(description="C4 worktree boundary-cost benchmark")
    parser.add_argument("--trials", type=int, default=5)
    parser.add_argument("--json", help="write report to this path")
    args = parser.parse_args()

    sizes = [(50, 10), (200, 40)]
    report = {"trials": args.trials, "cases": []}
    print(f"trials/case: {args.trials}   (times in ms, mean)\n")
    for n_files, n_edits in sizes:
        anfs = run_anfs(n_files, n_edits, args.trials)
        native = run_native(n_files, n_edits, args.trials)
        boundary = anfs["materialize"] + anfs["commit"] + anfs["integrity"]
        case = {
            "files": n_files, "edits": n_edits,
            "anfs": anfs, "native": native,
            "anfs_boundary_ms": boundary,
        }
        report["cases"].append(case)
        print(f"[{n_files} files, {n_edits} edits]")
        print(f"  ANFS boundary (paid once/session): {boundary:.1f} ms")
        print(f"    materialize {anfs['materialize']:.1f}  commit {anfs['commit']:.1f}  "
              f"integrity {anfs['integrity']:.1f}")
        print(f"  inner edit loop:  ANFS {anfs['inner_edits']:.2f} ms  vs  "
              f"native {native['inner_edits']:.2f} ms  (same — worktree is a real dir)")
        print()

    print("takeaway: ANFS taxes the session boundary, not the per-edit inner "
          "loop (which runs on an ordinary directory at native speed).")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump(report, h, indent=2)
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
