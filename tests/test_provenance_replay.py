"""C3 provenance/replay: derivation closure + event-granular replay are kernel claims.

Locks in the ANFS side of the provenance differential: from a final artifact,
the full derivation closure is recoverable (100% of the true upstream sources),
the namespace view is reconstructable at any event boundary, and integrity is
clean. The git contrast (zero derivation-edge recovery) lives in
benchmarks/provenance_benchmark.py.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))

from provenance_benchmark import run  # noqa: E402


def test_anfs_recovers_full_derivation_closure_and_replays_event_boundaries():
    report = run()
    assert report["anfs"]["coverage"] == 1.0
    assert report["anfs"]["recovered"] == report["anfs"]["true_edges"]
    assert report["replay"]["event_granular_early"]
    assert report["replay"]["full_view_late"]
    assert report["integrity_clean"]


def test_git_structure_recovers_no_derivation_edges():
    # The contrast that motivates C3: git is honest file history, but its
    # structure carries no derived-from relation.
    report = run()
    if report["git"] is not None:  # git binary present
        assert report["git"]["coverage"] == 0.0
