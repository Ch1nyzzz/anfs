"""W3 privacy-leak red-team: the kernel-label differential is a kernel claim.

Locks in the proof that field-level policy labels block sensitive data on
every content-bearing retrieval surface, while the no-label control (a plain
filesystem / vanilla RAG) leaks on all of them. See
benchmarks/leak_redteam_benchmark.py for the full harness and rationale.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "benchmarks"))

from leak_redteam_benchmark import (  # noqa: E402
    BUILTIN_CORPUS,
    CONTENT_SURFACES,
    run_mode,
    summarize,
)


def test_kernel_labels_block_every_content_surface_the_control_leaks_on():
    off_surface, off_integrity = run_mode(
        BUILTIN_CORPUS, label_fields=False, activate_rules=False
    )
    on_surface, on_integrity = run_mode(
        BUILTIN_CORPUS, label_fields=True, activate_rules=True
    )
    off = summarize(off_surface)
    on = summarize(on_surface)

    # Differential: control leaks on the content surfaces, kernel blocks all.
    assert off["content_leaks"] == off["content_probes"] > 0
    assert on["content_leaks"] == 0

    # Discovery surfaces (search/query) must also stop surfacing the node.
    for surface in ("search_discovery", "query_discovery"):
        assert off_surface[surface]["leaks"] == off_surface[surface]["probes"] > 0
        assert on_surface[surface]["leaks"] == 0

    assert off_integrity == []
    assert on_integrity == []


def test_every_content_surface_is_exercised():
    # Guard against a probe silently disappearing and hiding a regression.
    off_surface, _ = run_mode(BUILTIN_CORPUS, label_fields=False, activate_rules=False)
    assert CONTENT_SURFACES.issubset(set(off_surface))
