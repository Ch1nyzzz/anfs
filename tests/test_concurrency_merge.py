"""W2 concurrent-shared-state: conflict surfacing is a kernel claim.

Locks in the proof that ANFS surfaces every contention between concurrent
writers with zero silent data loss, while a plain shared directory
(last-writer-wins) silently drops updates — and that ANFS does not achieve
this by rejecting disjoint work. See benchmarks/concurrency_merge_benchmark.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))

from concurrency_merge_benchmark import (  # noqa: E402
    base_files,
    run_anfs,
    run_control,
)

AGENTS = 4
FILES = base_files(8)


def test_anfs_surfaces_contention_with_zero_silent_loss():
    anfs = run_anfs(AGENTS, FILES, "contention")
    control = run_control(AGENTS, FILES, "contention")

    # ANFS: every loser is told (conflict), nothing silently dropped.
    assert anfs["silent_lost_updates"] == 0
    assert anfs["surfaced_conflicts"] == AGENTS - 1
    assert anfs["loser_data_preserved"]
    assert anfs["integrity_clean"]

    # Control: the plain directory loses the overwritten updates with no signal.
    assert control["silent_lost_updates"] == AGENTS - 1
    assert control["surfaced_conflicts"] == 0


def test_anfs_merges_disjoint_concurrent_work_without_false_conflicts():
    anfs = run_anfs(AGENTS, FILES, "disjoint")
    assert anfs["surfaced_conflicts"] == 0
    assert anfs["merged_agents"] == AGENTS
    assert anfs["silent_lost_updates"] == 0
    assert anfs["integrity_clean"]
