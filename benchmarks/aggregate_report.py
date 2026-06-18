#!/usr/bin/env python3
"""Aggregate the per-repo C6 layer-1 snapshots into one cross-language table.

Reads docs/benchmark_snapshots/structural_*.json (each written by
structural_accuracy_benchmark.py) and prints the unified accuracy + efficiency
table across all measured codegraph tasks. Pure reporting, no repos needed.
"""

import glob
import json
import os
import sys

SNAP_DIR = os.path.join(os.path.dirname(__file__), "..", "docs", "benchmark_snapshots")


def main():
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "structural_*.json")))
    rows = []
    for f in files:
        with open(f) as h:
            d = json.load(h)
        if "anfs_recall" in d:
            rows.append(d)
    if not rows:
        print("no structural_*.json snapshots found")
        return 1

    rows.sort(key=lambda d: d["lang"])
    print("C6 layer-1 — structural retrieval vs compiler (SCIP) oracle, by task\n")
    print(f"  {'repo':<12}{'lang':<11}{'files':>6}{'fns':>6}   "
          f"{'ANFS rec':>8}{'ANFS prec':>10}   {'grep rec':>8}{'grep prec':>10}   {'ANFS/grep ops':>14}")
    for d in rows:
        print(f"  {d['repo']:<12}{d['lang']:<11}{d['corpus_files']:>6}{d['evaluated']:>6}   "
              f"{d['anfs_recall']:>7.1f}%{d['anfs_precision']:>9.1f}%   "
              f"{d['grep_recall']:>7.1f}%{d['grep_precision']:>9.1f}%   "
              f"{d.get('anfs_ops', 1):>6.1f}/{d.get('grep_ops', 0):<6.1f}")

    # Where does ANFS win/tie each axis?
    print("\nper-axis verdict (ANFS vs grep):")
    for d in rows:
        rec = "ANFS≥grep" if d["anfs_recall"] >= d["grep_recall"] - 1 else "grep>ANFS"
        prec = "ANFS≥grep" if d["anfs_precision"] >= d["grep_precision"] - 1 else "grep>ANFS"
        print(f"  {d['repo']:<12} recall: {rec:<10}  precision: {prec}")

    print("\nNB: ANFS always = 1 op/symbol (attributed caller set in one call); grep "
          "needs 1 grep + reads to attribute. All 7 codegraph repos measured "
          "(Swift via sourcekit-lsp, as no SCIP indexer exists for it).")

    # ---- token efficiency (codegraph-style: agent answers, count tokens) ----
    tfiles = sorted(glob.glob(os.path.join(SNAP_DIR, "tokens_*.json")))
    trows = []
    for f in tfiles:
        with open(f) as h:
            d = json.load(h)
        if "arms" in d and "baseline" in d["arms"]:
            trows.append(d)
    if trows:
        trows.sort(key=lambda d: d["lang"])
        print("\n\nC5 token efficiency — agent answers the codegraph question, "
              "with ANFS tools vs baseline (read/grep). codegraph's headline metric.\n")
        print(f"  {'repo':<12}{'lang':<11}{'base tok':>10}{'anfs tok':>10}{'tok saved':>10}"
              f"{'base calls':>11}{'anfs calls':>11}   {'answered b/a':>12}")
        for d in trows:
            b, a = d["arms"]["baseline"], d["arms"]["anfs"]
            saved = 100.0 * (b["total_tokens"] - a["total_tokens"]) / b["total_tokens"] if b["total_tokens"] else 0
            print(f"  {d['repo']:<12}{d['lang']:<11}{b['total_tokens']:>10.0f}{a['total_tokens']:>10.0f}"
                  f"{saved:>9.0f}%{b['tool_calls']:>11.0f}{a['tool_calls']:>11.0f}   "
                  f"{b['success']}/{d['runs']} vs {a['success']}/{d['runs']}")
        print("\nNB: 'answered' = run finished with an answer (not cut off at max_steps). "
              "A low anfs 'answered' means token savings are partly from non-completion — "
              "report honestly, do not credit as pure efficiency.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
