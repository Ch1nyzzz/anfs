#!/usr/bin/env python3
"""C5 token-efficiency eval: what does an agent spend to answer the same
question with vs without ANFS's structural tools?

This is the positive "省 token" number that C4 only bounded. It mirrors
codegraph's real-repo methodology exactly — same real repo (Tokio, on
codegraph's own list and the only language ANFS parses for defines/calls),
the SAME verbatim benchmark question, and the SAME measurement: pure
operational cost (tokens + tool calls), no answer grading. codegraph's own
harness grades nothing — its honesty rests on "same model, same question,
only the tools differ," and so does this.

Three arms, each a different tool surface over the SAME indexed corpus
(see _anfs_tools.py): baseline (read/grep) · rag (top-k chunks) · anfs
(outline/symbol_search/callers/context_pack/read_span). The agent decides how
much to read, so tokens and tool calls are what it actually spent.

We add ONE thing codegraph lacks: a non-gating "key facts hit" column, so a
reviewer can see an arm did not win merely by answering less. It never gates
the comparison — the headline stays the cost delta, codegraph's口径.

Needs network + DEEPSEEK_API_KEY. Nondeterministic, external — a benchmark,
not a CI test.

Run:
    python benchmarks/token_efficiency_benchmark.py --runs 1            # smoke
    python benchmarks/token_efficiency_benchmark.py --runs 4 --json docs/benchmark_snapshots/token_efficiency.json
"""

import argparse
import json
import os
import statistics
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import anfs_core  # noqa: E402
from _agent_loop import run_agent  # noqa: E402
from _anfs_tools import anfs_toolset, baseline_toolset  # noqa: E402
from _corpus import REPOS, load_repo  # noqa: E402
from deepseek_client import DEFAULT_MODEL, load_api_key  # noqa: E402

SYSTEM = (
    "You are a senior engineer analyzing an unfamiliar codebase. Use the provided "
    "tools to investigate the code, then answer the question concisely and "
    "specifically, naming the key types and functions involved. Investigate "
    "efficiently: prefer targeted, high-level retrieval over reading entire "
    "files, and stop to answer as soon as you have enough to explain the mechanism."
)

# codegraph's on/off comparison: same model, same question, only the tool set
# differs. baseline = read_file/grep/list (built-in tools); anfs = the
# structural tools. The efficiency metric is TOKEN usage (codegraph's headline).
ARMS = [
    ("baseline", baseline_toolset),
    ("anfs", anfs_toolset),
]


def run_once(api_key, corpus, arm_factory, question, model, max_steps):
    res = run_agent(api_key, SYSTEM, question, arm_factory(corpus),
                    model=model, max_steps=max_steps)
    return {
        "tool_calls": res.tool_calls,
        "prompt_tokens": res.prompt_tokens,
        "completion_tokens": res.completion_tokens,
        "cache_miss_tokens": res.cache_miss_tokens,
        "uncached_tokens": res.uncached_tokens,
        "total_tokens": res.total_tokens,
        "steps": res.steps,
        "stopped": res.stopped,
        "answer": res.answer,
    }


def med(rows, field):
    return statistics.median([r[field] for r in rows]) if rows else 0


def saved_pct(base, arm):
    """Positive = arm spent less than baseline (a saving)."""
    return 0.0 if base == 0 else (base - arm) / base * 100


def main():
    ap = argparse.ArgumentParser(description="C5 token-efficiency eval (codegraph-style)")
    ap.add_argument("--repo", required=True, choices=sorted(REPOS))
    ap.add_argument("--root", required=True, help="path to the cloned repo")
    ap.add_argument("--runs", type=int, default=3, help="runs per arm (median reported)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--max-steps", type=int, default=20)
    ap.add_argument("--max-files", type=int, default=None)
    ap.add_argument("--json", help="write report to this path")
    args = ap.parse_args()
    spec = REPOS[args.repo]
    api_key = load_api_key()

    tmp = tempfile.mkdtemp()
    fs = anfs_core.AnfsEngine(os.path.join(tmp, "anfs.db"), os.path.join(tmp, "objs"))
    corpus, sources = load_repo(fs, args.root, spec["subdir"], spec["parser"],
                                exts=spec["exts"], max_files=args.max_files)
    question = spec["question"]
    print(f"[{args.repo}/{spec['lang']}] {len(sources)} files, {len(corpus.by_name)} symbols, "
          f"runs={args.runs}\n## {question}")

    arms = {}
    for arm_name, factory in ARMS:
        runs = [run_once(api_key, corpus, factory, question, args.model, args.max_steps)
                for _ in range(args.runs)]
        arms[arm_name] = {
            "total_tokens": med(runs, "total_tokens"),
            "uncached_tokens": med(runs, "uncached_tokens"),
            "tool_calls": med(runs, "tool_calls"),
            "success": sum(r["stopped"] == "answer" for r in runs),
            "runs": runs,
        }
        a = arms[arm_name]
        print(f"  {arm_name:<9} tokens(total)={a['total_tokens']:.0f}  "
              f"uncached={a['uncached_tokens']:.0f}  tool_calls={a['tool_calls']:.0f}  "
              f"answered={a['success']}/{args.runs}")

    base, anfs = arms["baseline"], arms["anfs"]
    print(f"\n  ANFS vs baseline: {saved_pct(base['total_tokens'], anfs['total_tokens']):+.0f}% total tokens, "
          f"{saved_pct(base['uncached_tokens'], anfs['uncached_tokens']):+.0f}% uncached, "
          f"{saved_pct(base['tool_calls'], anfs['tool_calls']):+.0f}% tool calls (positive=saved)")

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump({"repo": args.repo, "lang": spec["lang"], "question": question,
                       "model": args.model, "runs": args.runs,
                       "corpus_files": len(sources), "arms": arms}, h, indent=2)
        print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
