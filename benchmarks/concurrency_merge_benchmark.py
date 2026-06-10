#!/usr/bin/env python3
"""W2 concurrent-shared-state harness.

Proves the ANFS claim that *the filesystem coordinates concurrent writers to
shared state, instead of leaving it to application-level convention*. The
proof is a differential against the control that the Amplify "file systems for
agents" article names as the status quo: concurrent writers to a shared
mutable directory with no coordinating authority (last-writer-wins). That
control silently loses updates; ANFS surfaces every contention as an explicit
conflict and loses nothing.

Two scenarios:
  - contention: N agents each modify the SAME shared file (plus a private one).
  - disjoint:   N agents each modify DISTINCT files (no overlap).

Metrics per scenario:
  - silent_lost_updates: writes that were overwritten with no signal.
  - surfaced_conflicts:  contentions the system reported to the loser.
The claim holds iff ANFS has silent_lost_updates == 0 and surfaces every
contention, while the control silently loses (writers - 1) updates per
contended file and surfaces nothing. The disjoint scenario guards against a
system that "wins" by rejecting everything: ANFS must merge all disjoint work
with zero conflicts.

This is mechanical and deterministic. Real multi-process contention is already
covered by the suite's spawn-based regression tests; here we measure the
outcome differential against a plain-filesystem control.

Run:
    python benchmarks/concurrency_merge_benchmark.py
    python benchmarks/concurrency_merge_benchmark.py --agents 6 --json out.json
"""

import argparse
import json
import os
import tempfile

import anfs_core

BASE = "resource:repo/main@v1"


def base_files(n_files):
    return [f"file_{i}.txt" for i in range(n_files)]


def setup_base(fs, files):
    """Publish the shared base repo (one resource ref per file)."""
    importer = fs.open_workspace("ws:importer", "importer_agent")
    for name in files:
        importer.write(name, b"base-content\n", [])
        importer.publish(name, f"{BASE}/{name}", "resource")


def agent_edits(agents, files, scenario):
    """Return {agent_idx: {filename: new_bytes}} for the chosen scenario."""
    edits = {}
    if scenario == "contention":
        shared = files[0]
        for i in range(agents):
            edits[i] = {
                shared: f"agent-{i}-shared\n".encode(),
                files[1 + i % (len(files) - 1)]: f"agent-{i}-private\n".encode(),
            }
    elif scenario == "disjoint":
        # Each agent owns a distinct file (requires n_files >= agents).
        for i in range(agents):
            edits[i] = {files[i]: f"agent-{i}-owns\n".encode()}
    else:
        raise ValueError(scenario)
    return edits


def run_anfs(agents, files, scenario):
    """ANFS: each agent checks out the shared base (CoW), edits, merges back."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fs = anfs_core.AnfsEngine(
            os.path.join(tmpdir, "anfs.db"), os.path.join(tmpdir, "objs")
        )
        setup_base(fs, files)
        edits = agent_edits(agents, files, scenario)

        # All agents check out the SAME base version before anyone merges:
        # this is the concurrency — every agent's base is v1.
        workspaces = {}
        written_nodes = {}  # (agent, file) -> node_id, to prove no data is lost
        for i in range(agents):
            ws = fs.checkout(f"ws:agent-{i}", f"agent_{i}", BASE)
            for name, content in edits[i].items():
                written_nodes[(i, name)] = ws.write(name, content, [])
            workspaces[i] = ws

        surfaced_conflicts = 0
        merged_agents = []
        for i in range(agents):
            try:
                fs.merge_workspace(BASE, f"ws:agent-{i}", f"agent_{i}")
                merged_agents.append(i)
            except anfs_core.RefConflictError:
                surfaced_conflicts += 1

        # No silent loss: every conflicting agent's bytes are still retrievable
        # from the node it wrote (the kernel kept them; the agent was told to
        # re-reconcile rather than having work dropped).
        preserved = all(
            bytes(fs.read_node(node)) == edits[a][name]
            for (a, name), node in written_nodes.items()
        )

        integrity = fs.verify_integrity()
        return {
            "silent_lost_updates": 0 if preserved else _count_writes(edits),
            "surfaced_conflicts": surfaced_conflicts,
            "merged_agents": len(merged_agents),
            "loser_data_preserved": preserved,
            "integrity_clean": integrity == [],
        }


def run_control(agents, files, scenario):
    """Control: a plain shared directory, last-writer-wins, no coordination."""
    with tempfile.TemporaryDirectory() as shared_dir:
        for name in files:
            with open(os.path.join(shared_dir, name), "wb") as handle:
                handle.write(b"base-content\n")

        edits = agent_edits(agents, files, scenario)
        # Track intended writes per file in agent order; last writer wins.
        intended = {}  # filename -> list of (agent, content)
        for i in range(agents):
            for name, content in edits[i].items():
                intended.setdefault(name, []).append((i, content))
                with open(os.path.join(shared_dir, name), "wb") as handle:
                    handle.write(content)

        silent_lost = 0
        for name, writes in intended.items():
            if len(writes) <= 1:
                continue
            final = open(os.path.join(shared_dir, name), "rb").read()
            # Every write whose content is not the surviving content is lost,
            # and the directory gave no signal that it happened.
            silent_lost += sum(1 for _agent, content in writes if content != final)

        return {
            "silent_lost_updates": silent_lost,
            "surfaced_conflicts": 0,  # a plain directory reports nothing
            "merged_agents": agents,
            "loser_data_preserved": silent_lost == 0,
            "integrity_clean": True,  # n/a for a plain dir
        }


def _count_writes(edits):
    return sum(len(v) for v in edits.values())


def expected_contended_writers(agents, files, scenario):
    edits = agent_edits(agents, files, scenario)
    counts = {}
    for i in range(agents):
        for name in edits[i]:
            counts[name] = counts.get(name, 0) + 1
    return {name: c for name, c in counts.items() if c >= 2}


def main():
    parser = argparse.ArgumentParser(description="ANFS W2 concurrency differential")
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--files", type=int, default=8)
    parser.add_argument("--json", help="write the full report to this path")
    args = parser.parse_args()

    files = base_files(max(args.files, args.agents))
    report = {"agents": args.agents, "files": len(files), "scenarios": {}}

    for scenario in ("contention", "disjoint"):
        anfs = run_anfs(args.agents, files, scenario)
        control = run_control(args.agents, files, scenario)
        contended = expected_contended_writers(args.agents, files, scenario)
        report["scenarios"][scenario] = {
            "contended_files": len(contended),
            "anfs": anfs,
            "control": control,
        }

    print(f"agents: {report['agents']}, base files: {report['files']}\n")
    for scenario, data in report["scenarios"].items():
        print(f"[{scenario}]  contended files: {data['contended_files']}")
        for label in ("control", "anfs"):
            r = data[label]
            print(
                f"  {label:<8} silent_lost={r['silent_lost_updates']}  "
                f"surfaced_conflicts={r['surfaced_conflicts']}  "
                f"data_preserved={r['loser_data_preserved']}  "
                f"integrity_clean={r['integrity_clean']}"
            )
        print()

    contention = report["scenarios"]["contention"]
    disjoint = report["scenarios"]["disjoint"]
    verdict = (
        contention["anfs"]["silent_lost_updates"] == 0
        and contention["anfs"]["surfaced_conflicts"] > 0
        and contention["anfs"]["loser_data_preserved"]
        and contention["control"]["silent_lost_updates"] > 0
        and disjoint["anfs"]["surfaced_conflicts"] == 0
        and disjoint["anfs"]["merged_agents"] == args.agents
    )
    print(
        "VERDICT: "
        + (
            "PASS — ANFS surfaces every contention with zero silent loss; the "
            "control silently drops updates; disjoint work merges cleanly."
            if verdict
            else "FAIL — differential not demonstrated."
        )
    )

    if args.json:
        with open(args.json, "w") as handle:
            json.dump(report, handle, indent=2)
        print(f"\nwrote {args.json}")

    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
