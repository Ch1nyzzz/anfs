#!/usr/bin/env python3
"""C6 layer-1: structural-retrieval accuracy + efficiency against a COMPILER
oracle (rust-analyzer SCIP) — no LLM, deterministic, CI-able.

The honest accuracy test the grep-only differential could not be: a real repo,
a real structural question ("which sites call X?"), and a real ground truth
(rust-analyzer's type-resolved references, not us, not grep). ANFS and grep are
BOTH scored for recall/precision against the oracle. The comparison is scoped
to the files ANFS actually indexed, so all three see the same code.

  ACCURACY   - recall  = |found ∩ oracle| / |oracle|     (did it find the calls?)
               precision = |found ∩ oracle| / |found|    (are its hits real?)
  EFFICIENCY - operations to get the attributed caller set: ANFS 1; grep 1+files.

Sites are compared at (file, 1-based line) granularity; symbols are grouped by
simple name (the ANFS-native view, see scip_oracle.py).

Run (needs a SCIP index built by rust-analyzer for the corpus repo):
    rust-analyzer scip . --output index.scip          # in the repo
    python benchmarks/structural_accuracy_benchmark.py --scip <path/to/index.scip>
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "oracle"))

import anfs_core  # noqa: E402
from _corpus import REPOS, load_repo  # noqa: E402
from scip_oracle import reference_sites_by_name  # noqa: E402


def grep_call_lines(sources, name):
    """grep call sites a toolless agent finds: (path, line), minus the
    definition line and comment lines (the obvious false positives)."""
    rx = re.compile(r"\b" + re.escape(name) + r"\s*\(")
    def_rx = re.compile(r"\bfn\s+" + re.escape(name) + r"\b")
    lines, files = set(), set()
    for path in sources:
        for n, line in enumerate(sources[path].splitlines(), 1):
            if rx.search(line):
                files.add(path)
                if not def_rx.search(line) and not line.lstrip().startswith("//"):
                    lines.add((path, n))
    return lines, len(files)


def anfs_call_lines(fs, corpus, source_bytes, name):
    """ANFS call sites for `name`: (path, line) from each caller edge's span.

    ANFS evidence offsets are UTF-8 BYTE offsets, so the line must be counted on
    the bytes, not on a decoded str (byte != char index once non-ASCII appears).
    """
    lines = set()
    for _frag, _kind, _sname, src_node, ev_s, _ev_e in fs.fragment_callers(name):
        path = corpus.path_of(src_node)
        if path in source_bytes:
            line = source_bytes[path][:ev_s].count(b"\n") + 1
            lines.add((path, line))
    return lines


def main():
    ap = argparse.ArgumentParser(description="C6 layer-1 accuracy vs SCIP oracle")
    ap.add_argument("--repo", required=True, choices=sorted(REPOS), help="codegraph repo key")
    ap.add_argument("--root", required=True, help="path to the cloned repo")
    ap.add_argument("--scip", help="path to the SCIP index (omit for sourcekit-lsp oracle)")
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--examples", type=int, default=10)
    ap.add_argument("--json", help="write report to this path")
    args = ap.parse_args()
    spec = REPOS[args.repo]

    import tempfile
    tmp = tempfile.mkdtemp()
    fs = anfs_core.AnfsEngine(os.path.join(tmp, "anfs.db"), os.path.join(tmp, "objs"))
    corpus, sources = load_repo(fs, args.root, spec["subdir"], spec["parser"],
                                exts=spec["exts"], max_files=args.max_files)
    corpus_paths = set(sources)
    source_bytes = {p: t.encode("utf-8") for p, t in sources.items()}

    if spec["scip"] == "sourcekit-lsp":
        from sourcekit_oracle import reference_sites_by_name as sk_refs
        oracle = sk_refs(args.root, sorted(corpus_paths))
    else:
        oracle = reference_sites_by_name(args.scip)
    # Scope the oracle to the indexed corpus: only call sites in files ANFS saw.
    oracle = {name: {s for s in sites if s[0] in corpus_paths}
              for name, sites in oracle.items()}

    # Unbiased symbol set: every function in the corpus the oracle says is called
    # at least once within the corpus (so there is ground truth to score against).
    callable_kinds = {"function", "method", "constructor"}
    corpus_fns = {name for name, defs in corpus.by_name.items()
                  if any(k in callable_kinds for _p, _f, k, _s, _e in defs)}
    targets = sorted(n for n in corpus_fns if oracle.get(n))

    agg = {k: 0 for k in ("oracle", "anfs_hit", "anfs_total", "grep_hit", "grep_total",
                          "anfs_ops", "grep_ops")}
    rows = []
    for name in targets:
        truth = oracle[name]
        anfs = anfs_call_lines(fs, corpus, source_bytes, name)
        grep, grep_files = grep_call_lines(sources, name)
        agg["oracle"] += len(truth)
        agg["anfs_hit"] += len(anfs & truth)
        agg["anfs_total"] += len(anfs)
        agg["grep_hit"] += len(grep & truth)
        agg["grep_total"] += len(grep)
        agg["anfs_ops"] += 1
        agg["grep_ops"] += 1 + grep_files
        rows.append({"symbol": name, "oracle": len(truth),
                     "anfs_recall": len(anfs & truth), "anfs_total": len(anfs),
                     "grep_recall": len(grep & truth), "grep_total": len(grep)})

    def pct(num, den):
        return 100.0 * num / den if den else 0.0

    n = len(targets)
    if n == 0:
        print(f"[{args.repo}/{spec['lang']}] no called functions resolved by the oracle "
              f"in-corpus — cannot score (oracle empty or path mismatch).")
        return 1
    anfs_recall = pct(agg["anfs_hit"], agg["oracle"])
    anfs_prec = pct(agg["anfs_hit"], agg["anfs_total"])
    grep_recall = pct(agg["grep_hit"], agg["oracle"])
    grep_prec = pct(agg["grep_hit"], agg["grep_total"])

    print(f"[{args.repo}/{spec['lang']}] corpus: {len(sources)} files | oracle: {spec['scip']} SCIP")
    print(f"evaluated {n} called functions (oracle has >=1 in-corpus call site)\n")
    print("ACCURACY vs compiler oracle (micro-averaged over call sites)")
    print(f"  {'':<6}{'recall':>9}{'precision':>11}")
    print(f"  {'ANFS':<6}{anfs_recall:>8.1f}%{anfs_prec:>10.1f}%   "
          f"({agg['anfs_hit']}/{agg['oracle']} found, {agg['anfs_total']} reported)")
    print(f"  {'grep':<6}{grep_recall:>8.1f}%{grep_prec:>10.1f}%   "
          f"({agg['grep_hit']}/{agg['oracle']} found, {agg['grep_total']} reported)")
    print("\nEFFICIENCY (operations to get the attributed caller set)")
    print(f"  ANFS : {agg['anfs_ops'] / n:.1f} op/symbol   grep : {agg['grep_ops'] / n:.1f} op/symbol")

    worst = sorted(rows, key=lambda r: r["oracle"] - r["anfs_recall"], reverse=True)[:args.examples]
    print(f"\nlargest ANFS recall gaps (oracle vs anfs_found):")
    print(f"  {'symbol':<22}{'oracle':>7}{'anfs':>6}{'grep':>6}")
    for r in worst:
        print(f"  {r['symbol']:<22}{r['oracle']:>7}{r['anfs_recall']:>6}{r['grep_recall']:>6}")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump({"repo": args.repo, "lang": spec["lang"], "oracle": spec["scip"],
                       "corpus_files": len(sources), "evaluated": n,
                       "anfs_recall": anfs_recall, "anfs_precision": anfs_prec,
                       "grep_recall": grep_recall, "grep_precision": grep_prec,
                       "anfs_ops": agg["anfs_ops"] / n, "grep_ops": agg["grep_ops"] / n,
                       "aggregate": agg, "rows": rows}, h, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
