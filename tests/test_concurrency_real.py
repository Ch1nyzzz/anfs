"""W2/C2 real-parallelism: the merge race invariant is a kernel claim.

Spawns real OS processes that race to merge into a shared ANFS database and
asserts the kernel invariant under genuine contention: exactly one writer of a
contended file commits, the rest get a clean RefConflictError with their data
preserved, disjoint concurrent work all commits, integrity stays clean. This
is the real-process upgrade of the deterministic concurrency_merge benchmark;
it is what caught merge_workspace surfacing a raw SQLITE_BUSY (fixed by taking
the write lock with TransactionBehavior::Immediate). See
benchmarks/concurrency_real_benchmark.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))

from concurrency_real_benchmark import run_real_processes  # noqa: E402

AGENTS = 4


def test_real_processes_serialize_contended_merges_with_no_silent_loss():
    r = run_real_processes(AGENTS, "contention")
    assert r["errors"] == 0, r["error_detail"]
    assert r["merged"] == 1
    assert r["conflicts"] == AGENTS - 1
    assert r["all_preserved"]
    assert r["integrity_clean"]


def test_real_processes_commit_disjoint_concurrent_work():
    r = run_real_processes(AGENTS, "disjoint")
    assert r["errors"] == 0, r["error_detail"]
    assert r["merged"] == AGENTS
    assert r["conflicts"] == 0
    assert r["integrity_clean"]
