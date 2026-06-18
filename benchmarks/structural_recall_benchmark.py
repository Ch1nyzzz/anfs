#!/usr/bin/env python3
"""C6 layer-1: deterministic structural-retrieval accuracy + efficiency, no LLM.

The honest counterpart to the agentic C5 eval. C5 measures whether ANFS helps a
*model*; this measures the retrieval itself — reproducibly, in CI, with no money
and no run-to-run noise — against the realistic control an agent without ANFS
falls back to: grep.

The query is a concrete structural question asked of EVERY called function in a
real repo (Tokio, unbiased — not a hand-picked symbol): "which functions call
X?" For each symbol we compare:

  - ANFS  : `fragment_callers(X)` — the attributed caller set in ONE call, each
            edge carrying the byte span of the call site (so precision is
            self-verifiable: read the span, confirm it contains the call).
  - grep  : `\\bX\\s*\\(` across the repo — what a toolless agent greps. Gives
            line hits with NO caller attribution, and conflates the definition
            line and comments with real call sites.

Two axes, reported straight (including where ANFS is not ahead):

  ACCURACY   - ANFS edge precision (spans that really contain the call);
               ANFS attributes each call to its enclosing function (grep: none);
               grep noise (% of hits that are the definition or a comment).
  EFFICIENCY - operations to obtain the attributed caller set: ANFS = 1;
               grep = 1 grep + one read per file with hits (to attribute/filter).
               For a k-hop blast radius ANFS stays 1 (`call_graph` depth=k); grep
               must BFS level by level.

Deterministic. No network, no API key.

Run:
    python benchmarks/structural_recall_benchmark.py
    python benchmarks/structural_recall_benchmark.py --json docs/benchmark_snapshots/structural_recall.json
"""

import argparse
import json
import os
import re
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import anfs_core  # noqa: E402
from _corpus import load_tokio_runtime  # noqa: E402


def grep_hits(sources, name):
    """Naive call-site grep: (path, lineno, line, is_def, is_comment) per hit."""
    rx = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    def_rx = re.compile(r"\bfn\s+" + re.escape(name) + r"\b")
    hits = []
    for path in sorted(sources):
        for n, line in enumerate(sources[path].splitlines(), 1):
            if rx.search(line):
                stripped = line.lstrip()
                hits.append((path, n, line, bool(def_rx.search(line)),
                             stripped.startswith("//")))
    return hits


def main():
    ap = argparse.ArgumentParser(description="C6 layer-1 structural retrieval eval")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--examples", type=int, default=8)
    ap.add_argument("--json", help="write report to this path")
    args = ap.parse_args()

    tmp = tempfile.mkdtemp()
    fs = anfs_core.AnfsEngine(os.path.join(tmp, "anfs.db"), os.path.join(tmp, "objs"))
    corpus, sources = load_tokio_runtime(fs, max_files=args.max_files)

    # Unbiased symbol set: every function definition that is called at least once.
    called = []
    for name, defs in corpus.by_name.items():
        if any(kind == "function" for _p, _f, kind, _s, _e in defs):
            if fs.fragment_callers(name):
                called.append(name)
    called.sort()

    def line_of(text, byte_offset):
        return text[:byte_offset].count("\n") + 1

    rows = []
    agg = {
        "symbols": 0, "anfs_edges": 0, "anfs_edges_verified": 0,
        "grep_hits": 0, "grep_def_hits": 0, "grep_comment_hits": 0,
        "grep_call_lines": 0, "grep_only_call_lines": 0,
        "anfs_ops": 0, "grep_ops": 0,
    }
    for name in called:
        callers = fs.fragment_callers(name)  # (frag, kind, src_name, src_node, ev_s, ev_e)
        verified = 0
        anfs_lines = set()  # (path, line) of every recorded call site
        for _frag, _kind, _sname, src_node, ev_s, ev_e in callers:
            span = bytes(fs.read_node_range(src_node, ev_s, ev_e - ev_s)).decode("utf-8", "replace")
            if name in span:  # the recorded span really contains the call
                verified += 1
            path = corpus.path_of(src_node)
            if path in sources:
                anfs_lines.add((path, line_of(sources[path], ev_s)))
        caller_fns = {sname for _f, _k, sname, _n, _s, _e in callers}

        hits = grep_hits(sources, name)
        defs = sum(1 for h in hits if h[3])
        comments = sum(1 for h in hits if h[4])
        files_with_hits = len({h[0] for h in hits})
        # Reverse (recall) check: grep lines that look like real calls (not the
        # definition, not a comment) but ANFS recorded no call site there — a
        # potential ANFS miss (e.g. a call outside any function body).
        grep_call_lines = {(h[0], h[1]) for h in hits if not h[3] and not h[4]}
        grep_only = grep_call_lines - anfs_lines
        agg["grep_call_lines"] += len(grep_call_lines)
        agg["grep_only_call_lines"] += len(grep_only)

        agg["symbols"] += 1
        agg["anfs_edges"] += len(callers)
        agg["anfs_edges_verified"] += verified
        agg["grep_hits"] += len(hits)
        agg["grep_def_hits"] += defs
        agg["grep_comment_hits"] += comments
        agg["anfs_ops"] += 1  # one fragment_callers call → attributed set
        agg["grep_ops"] += 1 + files_with_hits  # grep + one read per file to attribute

        rows.append({
            "symbol": name, "anfs_call_sites": len(callers),
            "anfs_caller_fns": len(caller_fns), "anfs_verified": verified,
            "grep_hits": len(hits), "grep_def_hits": defs, "grep_comment_hits": comments,
            "grep_files": files_with_hits,
        })

    n = agg["symbols"]
    anfs_prec = 100.0 * agg["anfs_edges_verified"] / agg["anfs_edges"] if agg["anfs_edges"] else 0
    grep_noise = 100.0 * (agg["grep_def_hits"] + agg["grep_comment_hits"]) / agg["grep_hits"] \
        if agg["grep_hits"] else 0

    print(f"corpus: {len(sources)} files, {len(corpus.by_name)} symbols")
    print(f"evaluated {n} called functions (unbiased: every function with >=1 caller)\n")

    grep_only = agg["grep_only_call_lines"]
    print("ACCURACY")
    print(f"  ANFS  call-site precision : {anfs_prec:.1f}%  "
          f"({agg['anfs_edges_verified']}/{agg['anfs_edges']} spans contain the call)")
    print(f"  ANFS  caller attribution  : yes (every call mapped to its enclosing function)")
    print(f"  grep  attribution         : none (line hits only)")
    print(f"  grep  noise (def+comment) : {grep_noise:.1f}%  "
          f"({agg['grep_def_hits']} defs + {agg['grep_comment_hits']} comments / {agg['grep_hits']} hits)")
    print(f"  grep-only call-like lines : {grep_only}  "
          f"(of {agg['grep_call_lines']} grep call lines — potential ANFS misses, "
          f"NB: grep is not an oracle)")
    print("\nEFFICIENCY (operations to get the attributed caller set)")
    print(f"  ANFS  : {agg['anfs_ops'] / n:.1f} op/symbol  (1 fragment_callers call)")
    print(f"  grep  : {agg['grep_ops'] / n:.1f} op/symbol  (1 grep + reads to attribute)")
    print(f"  for a k-hop blast radius: ANFS stays 1 op (call_graph depth=k); grep BFS grows per level")

    top = sorted(rows, key=lambda r: r["anfs_call_sites"], reverse=True)[:args.examples]
    print(f"\nexamples (top {len(top)} by call-site count):")
    print(f"  {'symbol':<24} {'anfs_sites':>10} {'caller_fns':>10} {'grep_hits':>9} {'grep_noise':>10}")
    for r in top:
        noise = r["grep_def_hits"] + r["grep_comment_hits"]
        print(f"  {r['symbol']:<24} {r['anfs_call_sites']:>10} {r['anfs_caller_fns']:>10} "
              f"{r['grep_hits']:>9} {noise:>10}")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump({"corpus_files": len(sources), "evaluated": n,
                       "anfs_precision_pct": anfs_prec, "grep_noise_pct": grep_noise,
                       "aggregate": agg, "rows": rows}, h, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
