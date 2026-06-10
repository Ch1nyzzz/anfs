#!/usr/bin/env python3
"""C3 provenance/replay differential: ANFS lineage vs git history.

Claim: every artifact's derivation lineage is reconstructable and the exact
state at any point is replayable. The control is git, the realistic status quo
for versioning agent work. git is honest competition for *file history* — but
it records "commit C changed file F", not "artifact A was derived from sources
B and C". An agent that synthesizes a report from three documents leaves no
structural trace in git of which documents it used; in ANFS that is a recorded
derivation edge.

Scenario: import 3 source documents, then an agent derives an intermediate
artifact from two of them, then a final artifact from the intermediate plus the
third (write(path, content, [source_node_ids]) records the derivation).

Two metrics:
  - derivation recovery: given the final artifact, what upstream sources can be
    proven? ANFS returns the full ancestor closure; git's structure yields the
    file's own commit history only (zero derivation edges).
  - replay: ANFS reconstructs the exact namespace view at any *event* boundary
    (per-mutation), finer than git's commit granularity.

Run:
    python benchmarks/provenance_benchmark.py
    python benchmarks/provenance_benchmark.py --json docs/benchmark_snapshots/provenance.json
"""

import argparse
import json
import os
import shutil
import subprocess
import tempfile

import anfs_core


def build_scenario(fs):
    """Import 3 sources, derive an intermediate, then a final artifact.

    Returns (nodes, true_ancestors, replay_events).
    """
    importer = fs.open_workspace("ws:importer", "importer_agent")
    src = {}
    for name, text in [
        ("src1.md", b"# quarterly revenue\nrevenue rose 12 percent\n"),
        ("src2.md", b"# headcount\nengineering grew to 240 staff\n"),
        ("src3.md", b"# risks\nsupply chain remains volatile\n"),
    ]:
        node = importer.write(name, text, [])
        importer.publish(name, f"artifact:{name.split('.')[0]}@v1")
        src[name] = node

    reader = fs.open_workspace("ws:analyst", "analyst_agent")
    # intermediate artifact derived from src1 + src2
    a1 = reader.write(
        "brief.md", b"# brief\nrevenue up, headcount up\n",
        [src["src1.md"], src["src2.md"]],
    )
    reader.publish("brief.md", "artifact:brief@v1")
    # final artifact derived from the intermediate + src3
    final = reader.write(
        "report.md", b"# report\ngrowth with supply risk\n",
        [a1, src["src3.md"]],
    )
    reader.publish("report.md", "artifact:report@v1")

    nodes = {"src1": src["src1.md"], "src2": src["src2.md"], "src3": src["src3.md"],
             "brief": a1, "report": final}
    # ground-truth derivation closure of the final artifact
    true_ancestors = {nodes["src1"], nodes["src2"], nodes["src3"], nodes["brief"]}
    replay_events = {
        "after_src2_publish": fs.ref_history("artifact:src2@v1")[0][1],
        "after_report_publish": fs.ref_history("artifact:report@v1")[0][1],
    }
    return nodes, true_ancestors, replay_events


def anfs_recovery(fs, nodes, true_ancestors):
    closure = set(fs.lineage_nodes(nodes["report"], direction="ancestors"))
    recovered = true_ancestors & closure
    return {
        "true_edges": len(true_ancestors),
        "recovered": len(recovered),
        "coverage": len(recovered) / len(true_ancestors),
    }


def anfs_replay(fs, nodes, replay_events):
    def view(event_id):
        return {
            row[0]: row[3]  # ref_name -> state
            for row in fs.ref_view_at_event(event_id, prefix="artifact:")
        }

    after_src2 = view(replay_events["after_src2_publish"])
    after_report = view(replay_events["after_report_publish"])
    # At the src2-publish event only src1/src2 exist; brief/report do not yet.
    early_ok = set(after_src2) == {"artifact:src1@v1", "artifact:src2@v1"}
    late_ok = set(after_report) == {
        "artifact:src1@v1", "artifact:src2@v1", "artifact:src3@v1",
        "artifact:brief@v1", "artifact:report@v1",
    }
    return {"event_granular_early": early_ok, "full_view_late": late_ok}


def git_recovery(true_edge_count):
    """Replicate the scenario as git commits; measure derivation recovery."""
    if not shutil.which("git"):
        return None
    with tempfile.TemporaryDirectory() as repo:
        env = {**os.environ, "GIT_AUTHOR_NAME": "a", "GIT_AUTHOR_EMAIL": "a@a",
               "GIT_COMMITTER_NAME": "a", "GIT_COMMITTER_EMAIL": "a@a"}

        def git(*a, check=True):
            return subprocess.run(["git", *a], cwd=repo, env=env, check=check,
                                  capture_output=True, text=True)

        git("init", "-q")
        git("config", "commit.gpgsign", "false")
        # import: one commit (the realistic granularity)
        for name in ("src1.md", "src2.md", "src3.md"):
            open(os.path.join(repo, name), "w").write(name)
        git("add", "-A")
        git("commit", "-q", "-m", "import sources")
        # derive intermediate, then final — committed like an agent would
        open(os.path.join(repo, "brief.md"), "w").write("brief")
        git("add", "-A")
        git("commit", "-q", "-m", "add brief")
        open(os.path.join(repo, "report.md"), "w").write("report")
        git("add", "-A")
        git("commit", "-q", "-m", "add report")

        # What can git prove about report.md's derivation? Its file history
        # yields only the commits that touched report.md, and those commits'
        # changed files = {report.md}. No edge to brief/src3.
        log = git("log", "--follow", "--name-only", "--format=", "--", "report.md")
        touched = {ln.strip() for ln in log.stdout.splitlines() if ln.strip()}
        derivation_sources = touched - {"report.md"}  # none
        return {
            "true_edges": true_edge_count,
            "recovered": len(derivation_sources),
            "coverage": len(derivation_sources) / true_edge_count,
            "note": "git --follow yields report.md's own commit history; no derived-from edges",
        }


def run():
    with tempfile.TemporaryDirectory() as tmpdir:
        fs = anfs_core.AnfsEngine(
            os.path.join(tmpdir, "anfs.db"), os.path.join(tmpdir, "objs")
        )
        nodes, true_ancestors, replay_events = build_scenario(fs)
        anfs = anfs_recovery(fs, nodes, true_ancestors)
        replay = anfs_replay(fs, nodes, replay_events)
        integrity = fs.verify_integrity()
        git = git_recovery(len(true_ancestors))
        return {"anfs": anfs, "replay": replay, "git": git,
                "integrity_clean": integrity == []}


def main():
    parser = argparse.ArgumentParser(description="C3 provenance/replay differential")
    parser.add_argument("--json", help="write report to this path")
    args = parser.parse_args()
    report = run()

    a = report["anfs"]
    print("=== Derivation recovery (final artifact -> upstream sources) ===\n")
    print(f"  ANFS : {a['recovered']}/{a['true_edges']} sources  "
          f"({a['coverage'] * 100:.0f}% coverage)")
    if report["git"]:
        g = report["git"]
        print(f"  git  : {g['recovered']}/{g['true_edges']} sources  "
              f"({g['coverage'] * 100:.0f}% coverage)  — {g['note']}")
    print("\n=== Replay (event-granular namespace reconstruction) ===\n")
    print(f"  view at src2-publish event = only src1,src2: {report['replay']['event_granular_early']}")
    print(f"  view at report-publish event = all 5 artifacts: {report['replay']['full_view_late']}")
    print(f"\nintegrity clean: {report['integrity_clean']}")

    verdict = (
        a["coverage"] == 1.0
        and (report["git"] is None or report["git"]["coverage"] == 0.0)
        and report["replay"]["event_granular_early"]
        and report["replay"]["full_view_late"]
        and report["integrity_clean"]
    )
    print(
        "\nVERDICT: "
        + (
            "PASS — ANFS reconstructs the full derivation closure and replays "
            "any event boundary; git's structure recovers no derivation edges."
            if verdict
            else "INSPECT — see numbers above."
        )
    )

    if args.json:
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as h:
            json.dump(report, h, indent=2)
        print(f"wrote {args.json}")

    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
