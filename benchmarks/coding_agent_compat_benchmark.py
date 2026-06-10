#!/usr/bin/env python3
"""C4 compatibility: a real coding agent, ANFS worktree vs native directory.

A real model (deepseek-v4-flash) fixes small buggy Python modules given the
failing test. Each agent fix is applied two ways and the pytest outcome is
compared:

  - native : the fix is written to a plain directory and tested there.
  - anfs   : the buggy repo is published into ANFS, checked out, materialized
             to a worktree; the SAME agent fix is written there, committed back
             with commit_worktree, then re-materialized to a FRESH directory
             and tested. A pass therefore proves the fix survived the full
             ANFS round-trip (commit -> re-materialize) byte-faithfully.

The compatibility claim is no regression: the ANFS arm must reach the same
pass/fail as native on every task, with integrity clean. The agent is the
load generator producing realistic edits; the variable under test is the IO
path, so each task uses ONE agent call applied to both arms to isolate it.

Needs network + DEEPSEEK_API_KEY. Not a CI test (nondeterministic, external);
the deterministic round-trip guarantee is in tests/test_worktree_roundtrip.py.

Run:
    python benchmarks/coding_agent_compat_benchmark.py
    python benchmarks/coding_agent_compat_benchmark.py --json docs/benchmark_snapshots/coding_agent_compat.json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

import anfs_core  # noqa: E402
from deepseek_client import chat, load_api_key  # noqa: E402

BASE = "resource:repo/main@v1"

# Small self-contained bug-fix tasks: each module has one logic bug the test
# catches. Difficulty is irrelevant — the compatibility metric is the DELTA
# between arms, not absolute pass rate.
TASKS = [
    {
        "name": "off_by_one_sum",
        "module": "calc.py",
        "buggy": "def sum_to(n):\n    total = 0\n    for i in range(1, n):\n        total += i\n    return total\n",
        "test": "from calc import sum_to\n\ndef test_sum_to():\n    assert sum_to(5) == 15\n    assert sum_to(1) == 1\n",
    },
    {
        "name": "wrong_comparison",
        "module": "grade.py",
        "buggy": "def is_pass(score):\n    return score > 60\n",
        "test": "from grade import is_pass\n\ndef test_is_pass():\n    assert is_pass(60) is True\n    assert is_pass(59) is False\n",
    },
    {
        "name": "empty_edge_case",
        "module": "stats.py",
        "buggy": "def mean(xs):\n    return sum(xs) / len(xs)\n",
        "test": "from stats import mean\n\ndef test_mean():\n    assert mean([2, 4]) == 3\n    assert mean([]) == 0\n",
    },
    {
        "name": "string_misuse",
        "module": "text.py",
        "buggy": "def initials(name):\n    return name[0]\n",
        "test": "from text import initials\n\ndef test_initials():\n    assert initials('ada lovelace') == 'AL'\n    assert initials('grace') == 'G'\n",
    },
    {
        "name": "missing_return",
        "module": "fib.py",
        "buggy": "def fib(n):\n    a, b = 0, 1\n    for _ in range(n):\n        a, b = b, a + b\n",
        "test": "from fib import fib\n\ndef test_fib():\n    assert fib(0) == 0\n    assert fib(7) == 13\n",
    },
]


def agent_fix(api_key, task):
    """One real agent call: return the corrected module text."""
    prompt = (
        f"A pytest test is failing against a buggy module. Return ONLY the "
        f"complete corrected contents of {task['module']} — no explanation, no "
        f"markdown fences.\n\n# {task['module'].replace('.py', '')}_test.py\n"
        f"{task['test']}\n\n# {task['module']} (buggy)\n{task['buggy']}"
    )
    out = chat(
        api_key,
        [
            {"role": "system", "content": "You are a precise Python bug-fixer."},
            {"role": "user", "content": prompt},
        ],
    )
    # Strip accidental markdown fences if the model added them.
    fence = re.match(r"\s*```[a-zA-Z]*\n(.*)\n```\s*$", out, re.S)
    return fence.group(1) if fence else out.strip() + "\n"


def run_pytest(work_dir, test_name):
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_name, "-q"],
        cwd=work_dir, capture_output=True, text=True,
    )
    return proc.returncode == 0


def native_arm(task, fix_text):
    with tempfile.TemporaryDirectory() as work:
        open(os.path.join(work, task["module"]), "w").write(fix_text)
        test_name = task["module"].replace(".py", "") + "_test.py"
        open(os.path.join(work, test_name), "w").write(task["test"])
        return run_pytest(work, test_name)


def anfs_arm(task, fix_text):
    test_name = task["module"].replace(".py", "") + "_test.py"
    with tempfile.TemporaryDirectory() as tmpdir:
        fs = anfs_core.AnfsEngine(
            os.path.join(tmpdir, "anfs.db"), os.path.join(tmpdir, "objs")
        )
        importer = fs.open_workspace("ws:importer", "importer_agent")
        importer.write(task["module"], task["buggy"].encode(), [])
        importer.write(test_name, task["test"].encode(), [])
        importer.publish(task["module"], f"{BASE}/{task['module']}", "resource")
        importer.publish(test_name, f"{BASE}/{test_name}", "resource")

        fs.checkout("ws:coder", "coder_agent", BASE)
        work = os.path.join(tmpdir, "work")
        fs.materialize_workspace("ws:coder", work)

        # agent writes its fix into the worktree
        open(os.path.join(work, task["module"]), "w").write(fix_text)
        fs.commit_worktree("ws:coder", work, "coder_agent", require_manifest_match=True)

        # re-materialize to a FRESH dir: the test runs on round-tripped content
        fresh = os.path.join(tmpdir, "fresh")
        fs.materialize_workspace("ws:coder", fresh)
        passed = run_pytest(fresh, test_name)
        integrity_clean = fs.verify_integrity() == []
        return passed, integrity_clean


def main():
    parser = argparse.ArgumentParser(description="C4 coding-agent compatibility")
    parser.add_argument("--json", help="write report to this path")
    args = parser.parse_args()
    api_key = load_api_key()

    rows, native_pass, anfs_pass, mismatches, integrity_ok = [], 0, 0, 0, 0
    for task in TASKS:
        fix = agent_fix(api_key, task)
        n_ok = native_arm(task, fix)
        a_ok, clean = anfs_arm(task, fix)
        native_pass += n_ok
        anfs_pass += a_ok
        integrity_ok += clean
        if n_ok != a_ok:
            mismatches += 1
        rows.append({"task": task["name"], "native": n_ok, "anfs": a_ok, "integrity_clean": clean})
        print(f"{task['name']:<20} native={'pass' if n_ok else 'fail'}  "
              f"anfs={'pass' if a_ok else 'fail'}  integrity={'ok' if clean else 'DIRTY'}")

    n = len(TASKS)
    print(f"\nnative pass: {native_pass}/{n}   anfs pass: {anfs_pass}/{n}")
    print(f"arm mismatches (regressions): {mismatches}   integrity clean: {integrity_ok}/{n}")
    verdict = mismatches == 0 and integrity_ok == n
    print(
        "\nVERDICT: "
        + (
            "PASS — ANFS round-trip reaches the same outcome as native on every "
            "task (no compatibility regression), integrity clean."
            if verdict
            else "FAIL — ANFS arm diverged from native or integrity not clean."
        )
    )

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump(
                {"model": "deepseek-v4-flash", "tasks": n,
                 "native_pass": native_pass, "anfs_pass": anfs_pass,
                 "mismatches": mismatches, "rows": rows},
                h, indent=2,
            )
        print(f"wrote {args.json}")

    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
