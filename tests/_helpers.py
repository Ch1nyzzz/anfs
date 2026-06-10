"""Shared helpers for the ANFS test suite.

Multiprocessing workers live here (not in conftest.py) so spawn-based
child processes can import them by module path.
"""

import queue as queue_module
import time

import anfs_core


def _drain_queue_nowait(queue):
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except queue_module.Empty:
            return items


def _collect_queue_results(queue, expected_count, timeout=5):
    items = []
    for _ in range(expected_count):
        try:
            items.append(queue.get(timeout=timeout))
        except queue_module.Empty:
            break
    return items


def _concurrent_event_writer(db_path, objs_dir, worker_idx, writes_per_worker, queue):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace(
            f"ws:worker-{worker_idx}",
            f"worker_agent_{worker_idx}",
            run_id=f"run:worker-{worker_idx}",
        )
        for item_idx in range(writes_per_worker):
            content = f"worker={worker_idx}; item={item_idx}".encode("utf-8")
            ws.write(
                f"file-{item_idx}.txt",
                content,
                [],
            )
            read_back = bytes(ws.read(f"file-{item_idx}.txt"))
            if read_back != content:
                queue.put(
                    f"worker {worker_idx} read mismatch for item {item_idx}: {read_back!r}"
                )
        grep_rows = ws.grep(f"worker={worker_idx}", "")
        if len(grep_rows) != writes_per_worker:
            queue.put(
                f"worker {worker_idx} grep mismatch: {len(grep_rows)} != {writes_per_worker}"
            )
    except Exception as exc:
        queue.put(f"{type(exc).__name__}: {exc}")


def _concurrent_stale_approval_worker(
    db_path,
    objs_dir,
    worker_idx,
    target_ref,
    evidence_ref,
    expected_version,
    queue,
):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.approve(
            target_ref,
            [evidence_ref],
            f"reviewer_agent_{worker_idx}",
            run_id=f"run:stale-review-{worker_idx}",
            tool_call_id=f"tc_stale_review_{worker_idx}",
            expected_version=expected_version,
        )
        queue.put(("approved", worker_idx, ""))
    except anfs_core.RefConflictError as exc:
        queue.put(("conflict", worker_idx, str(exc)))
    except Exception as exc:
        queue.put(("error", worker_idx, f"{type(exc).__name__}: {exc}"))


def _concurrent_checked_worktree_commit_worker(
    db_path,
    objs_dir,
    workspace,
    worktree_dir,
    worker_idx,
    queue,
):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        committed = fs.commit_worktree(
            workspace,
            worktree_dir,
            f"coder_agent_{worker_idx}",
            run_id=f"run:checked-worktree-{worker_idx}",
            tool_call_id=f"tc_checked_worktree_{worker_idx}",
            require_manifest_match=True,
        )
        queue.put(("committed", worker_idx, committed))
    except anfs_core.RefConflictError as exc:
        queue.put(("conflict", worker_idx, str(exc)))
    except Exception as exc:
        queue.put(("error", worker_idx, f"{type(exc).__name__}: {exc}"))
