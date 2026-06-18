"""Ref approval/rejection lifecycle, state machine, and review policy events."""

import hashlib
import json
import os
import sqlite3
import tempfile

import anfs_core


def test_valid_approval_and_ref_audit():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")

        fs.approve(
            target_ref="artifact:patch@v1",
            evidence_refs=["artifact:test_result@v1"],
            agent_id="reviewer_agent",
        )

        ref_info = fs.get_ref("artifact:patch@v1")
        assert ref_info[3] == "approved"

        with sqlite3.connect(db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM ref_events WHERE ref_name = ?",
                ("artifact:patch@v1",),
            ).fetchone()[0]
            assert count == 2


def test_reject_ref_records_lifecycle_event_and_audit():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")

        patch_node = coder_ws.write("src/db.py", b"print('bad')", [])
        coder_ws.publish("src/db.py", "artifact:patch@bad")
        fs.reject_ref(
            "artifact:patch@bad",
            "reviewer_agent",
            reason="breaks retry behavior",
            run_id="run:review",
            tool_call_id="tc_reject",
        )

        ref_info = fs.get_ref("artifact:patch@bad")
        assert ref_info[1] == patch_node
        assert ref_info[3] == "rejected"

        history = fs.ref_history("artifact:patch@bad")
        assert [row[2] for row in history] == ["publish", "reject"]
        assert [row[7] for row in history] == ["published", "rejected"]
        assert history[1][3] == "reviewer_agent"

        reject_event = fs.event(history[1][1])
        assert reject_event[1] == "reject"
        assert reject_event[2] == "reviewer_agent"
        assert reject_event[3] == "run:review"
        assert reject_event[4] == "tc_reject"
        assert json.loads(reject_event[6]) == {
            "target_ref": "artifact:patch@bad",
            "reason": "breaks retry behavior",
        }
        reject_edges = {(edge[0], edge[2], edge[3]) for edge in reject_event[8]}
        assert ("input", "target", "artifact:patch@bad") in reject_edges
        assert ("output", "rejected_target", "artifact:patch@bad") in reject_edges

        fs.archive_ref("artifact:patch@bad", "retention_agent")
        assert fs.get_ref("artifact:patch@bad")[3] == "archived"


def test_reject_ref_enforces_state_machine():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.approve("artifact:patch@v1", ["artifact:test_result@v1"], "reviewer_agent")

        try:
            fs.reject_ref("artifact:patch@v1", "reviewer_agent")
        except anfs_core.InvalidStateTransitionError:
            pass
        else:
            raise AssertionError("approved refs must not transition to rejected")

        assert fs.get_ref("artifact:patch@v1")[3] == "approved"
        assert [row[2] for row in fs.ref_history("artifact:patch@v1")] == [
            "publish",
            "approve",
        ]


def test_manifest_publish_approval_requires_all_children_covered():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        db_node = coder_ws.write("src/db.py", b"print('db fix')", [])
        api_node = coder_ws.write("src/api.py", b"print('api fix')", [])
        manifest_node = coder_ws.publish_manifest(
            ["src/api.py", "src/db.py"],
            "artifact:patch_bundle@v1",
        )

        children = fs.manifest_children(manifest_node)
        assert children == [("src/api.py", api_node), ("src/db.py", db_node)]
        child_records = fs.manifest_child_records(manifest_node)
        assert child_records == [
            (
                "src/api.py",
                api_node,
                "child",
                "text/plain",
                hashlib.sha256(b"print('api fix')").hexdigest(),
                len(b"print('api fix')"),
            ),
            (
                "src/db.py",
                db_node,
                "child",
                "text/plain",
                hashlib.sha256(b"print('db fix')").hexdigest(),
                len(b"print('db fix')"),
            ),
        ]

        tester_ws.write("partial.log", b"PARTIAL PASS", [db_node])
        tester_ws.publish("partial.log", "artifact:test_result_partial@v1")
        try:
            fs.approve(
                target_ref="artifact:patch_bundle@v1",
                evidence_refs=["artifact:test_result_partial@v1"],
                agent_id="reviewer_agent",
            )
        except anfs_core.LineageMismatchError:
            pass
        else:
            raise AssertionError("partial manifest evidence should not approve bundle")

        tester_ws.write("full.log", b"PASS", [db_node, api_node])
        tester_ws.publish("full.log", "artifact:test_result_full@v1")
        fs.approve(
            target_ref="artifact:patch_bundle@v1",
            evidence_refs=["artifact:test_result_full@v1"],
            agent_id="reviewer_agent",
        )
        assert fs.get_ref("artifact:patch_bundle@v1")[3] == "approved"

        candidates_without_workspaces = {
            candidate[0] for candidate in fs.gc_candidates(False)
        }
        assert hashlib.sha256(b"print('db fix')").hexdigest() not in candidates_without_workspaces
        assert hashlib.sha256(b"print('api fix')").hexdigest() not in candidates_without_workspaces
        manifest_doc = json.loads(bytes(fs.read_node(manifest_node)))
        assert manifest_doc["schema"] == "anfs.manifest.v1"
        assert manifest_doc["children"][0]["digest"] == hashlib.sha256(
            b"print('api fix')"
        ).hexdigest()
        assert manifest_doc["children"][0]["media_type"] == "text/plain"
        assert manifest_doc["children"][0]["role"] == "child"
        assert manifest_doc["children"][0]["size"] == len(b"print('api fix')")


def test_materialize_ref_view_rejects_backslash_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        replay_dir = os.path.join(tmpdir, "replay")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:importer", "importer_agent")

        ws.write("safe.py", b"print('safe')", [])
        ws.publish("safe.py", r"resource:repo/main@v1/src/..\evil.py", "resource")
        publish_event = fs.ref_history(r"resource:repo/main@v1/src/..\evil.py")[0][1]

        try:
            fs.materialize_ref_view_at_event(
                publish_event,
                replay_dir,
                prefix="resource:repo/main@v1",
            )
            assert False, "materialization should reject backslash path segments"
        except anfs_core.PolicyDeniedError:
            pass
        assert not os.path.exists(replay_dir)


def test_materialize_ref_view_rejects_duplicate_relative_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        replay_dir = os.path.join(tmpdir, "replay")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:importer", "importer_agent")

        ws.write("root.txt", b"root", [])
        ws.publish("root.txt", "resource:repo/main@v1", "resource")
        ws.write("child.txt", b"child", [])
        ws.publish("child.txt", "resource:repo/main@v1/__ref__", "resource")
        child_publish_event = fs.ref_history("resource:repo/main@v1/__ref__")[0][1]

        try:
            fs.materialize_ref_view_at_event(
                child_publish_event,
                replay_dir,
                prefix="resource:repo/main@v1",
            )
            assert False, "materialization should reject duplicate relative paths"
        except anfs_core.PolicyDeniedError:
            pass
        assert not os.path.exists(replay_dir)



def test_worktree_checked_commit_rejects_paths_removed_or_added_after_materialization():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        disappeared_dir = os.path.join(tmpdir, "disappeared")
        appeared_dir = os.path.join(tmpdir, "appeared")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer = fs.open_workspace("ws:importer", "importer_agent")
        importer.write("a.txt", b"a base\n", [])
        importer.publish("a.txt", "resource:repo/main@v1/a.txt", "resource")
        fs.checkout("ws:coder", "coder_agent", base="resource:repo/main@v1")

        fs.materialize_workspace("ws:coder", disappeared_dir)
        with open(os.path.join(disappeared_dir, "a.txt"), "wb") as file:
            file.write(b"a local edit after disappearing base\n")
        live_workspace = fs.open_workspace("ws:coder", "other_agent")
        live_workspace.delete("a.txt")

        disappeared_rows = {
            row[0]: row for row in fs.worktree_readiness("ws:coder", disappeared_dir)
        }
        assert disappeared_rows["manifest_lease"][1] is False
        assert "current workspace ref is missing" in disappeared_rows["manifest_lease"][2]
        assert disappeared_rows["checked_commit_ready"][1] is False
        try:
            fs.commit_worktree(
                "ws:coder",
                disappeared_dir,
                "coder_agent",
                tool_call_id="tc_disappeared_checked_commit",
                require_manifest_match=True,
            )
            assert False, "checked worktree commit should reject a disappeared base ref"
        except anfs_core.RefConflictError as exc:
            assert "current workspace ref is missing" in str(exc)
        assert fs.events(kind="worktree_commit", tool_call_id="tc_disappeared_checked_commit") == []
        assert fs.get_ref("ws:coder/a.txt")[3] == "deleted"

        fs.materialize_workspace("ws:coder", appeared_dir)
        live_workspace.write("new.txt", b"new current path\n", [])
        appeared_rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", appeared_dir)}
        assert appeared_rows["manifest_lease"][1] is False
        assert "current workspace has new path new.txt" in appeared_rows["manifest_lease"][2]
        assert appeared_rows["checked_commit_ready"][1] is False
        try:
            fs.commit_worktree(
                "ws:coder",
                appeared_dir,
                "coder_agent",
                tool_call_id="tc_appeared_checked_commit",
                require_manifest_match=True,
            )
            assert False, "checked worktree commit should reject a newly appeared base ref"
        except anfs_core.RefConflictError as exc:
            assert "current workspace has new path new.txt" in str(exc)
        assert fs.events(kind="worktree_commit", tool_call_id="tc_appeared_checked_commit") == []
        assert bytes(live_workspace.read("new.txt")) == b"new current path\n"
        assert fs.verify_integrity() == []


def test_worktree_readiness_and_commit_reject_symlinks_without_following_them():
    if not hasattr(os, "symlink"):
        return

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")
        outside_path = os.path.join(tmpdir, "outside-secret.txt")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer = fs.open_workspace("ws:importer", "importer_agent")
        importer.write("app.py", b"print('v1')\n", [])
        importer.publish("app.py", "resource:repo/main@v1/app.py", "resource")
        fs.checkout("ws:coder", "coder_agent", base="resource:repo/main@v1")
        fs.materialize_workspace("ws:coder", worktree_dir)

        with open(outside_path, "wb") as file:
            file.write(b"outside secret should not be imported\n")
        try:
            os.symlink(outside_path, os.path.join(worktree_dir, "leak.txt"))
        except OSError:
            return

        rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", worktree_dir)}
        assert rows["input_dir"][1] is True
        assert rows["manifest_lease"][1] is True
        assert rows["supported_entries"][1] is False
        assert "unsupported worktree symlink entry" in rows["supported_entries"][2]
        assert rows["local_changes"][1] is False
        assert rows["checked_commit_ready"][1] is False

        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_symlink_worktree_commit",
                require_manifest_match=True,
            )
            assert False, "worktree commit should reject symlink entries"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsupported worktree symlink entry" in str(exc)

        assert fs.events(kind="worktree_commit", tool_call_id="tc_symlink_worktree_commit") == []
        assert fs.verify_integrity() == []


def test_worktree_readiness_and_commit_reject_backslash_paths():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer = fs.open_workspace("ws:importer", "importer_agent")
        importer.write("app.py", b"print('v1')\n", [])
        importer.publish("app.py", "resource:repo/main@v1/app.py", "resource")
        fs.checkout("ws:coder", "coder_agent", base="resource:repo/main@v1")
        fs.materialize_workspace("ws:coder", worktree_dir)

        with open(os.path.join(worktree_dir, "bad\\name.txt"), "wb") as file:
            file.write(b"ambiguous cross-platform path\n")

        rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", worktree_dir)}
        assert rows["input_dir"][1] is True
        assert rows["manifest_lease"][1] is True
        assert rows["supported_entries"][1] is False
        assert "unsafe worktree relative path" in rows["supported_entries"][2]
        assert "bad" in rows["supported_entries"][2]
        assert "name.txt" in rows["supported_entries"][2]
        assert rows["local_changes"][1] is False
        assert rows["checked_commit_ready"][1] is False

        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_backslash_worktree_commit",
                require_manifest_match=True,
            )
            assert False, "worktree commit should reject backslash paths"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe worktree relative path bad\\name.txt" in str(exc)

        assert fs.events(kind="worktree_commit", tool_call_id="tc_backslash_worktree_commit") == []
        assert fs.verify_integrity() == []


def test_import_event_bundle_rejects_invalid_source_sequence_metadata():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        ws = source_fs.open_workspace("ws:coder", "coder_agent")
        ws.write("src/db.py", b"print('v1')", [])
        ws.publish("src/db.py", "artifact:patch@v1")
        publish_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, _event_count, _node_count, _blob_count = source_fs.export_event_bundle(
            publish_event,
            bundle_dir,
        )

        with open(bundle_path, "r", encoding="utf-8") as file:
            base_bundle = json.load(file)

        cases = []

        duplicate_seq = json.loads(json.dumps(base_bundle))
        duplicate_seq.pop("bundle_checksum", None)
        duplicate_seq["events"][1]["source_seq"] = duplicate_seq["events"][0]["source_seq"]
        cases.append(("duplicate-source-seq.json", duplicate_seq))

        mixed_seq = json.loads(json.dumps(base_bundle))
        mixed_seq.pop("bundle_checksum", None)
        mixed_seq["events"][0].pop("source_seq")
        cases.append(("mixed-source-seq.json", mixed_seq))

        root_not_max = json.loads(json.dumps(base_bundle))
        root_not_max.pop("bundle_checksum", None)
        root_event = next(
            event for event in root_not_max["events"] if event["event_id"] == publish_event
        )
        root_event["source_seq"] = 1
        for event in root_not_max["events"]:
            if event["event_id"] != publish_event:
                event["source_seq"] += 10
        cases.append(("root-not-max-source-seq.json", root_not_max))

        for filename, bundle in cases:
            tampered_path = os.path.join(bundle_dir, filename)
            with open(tampered_path, "w", encoding="utf-8") as file:
                json.dump(bundle, file, indent=2)

            imported_db_path = os.path.join(tmpdir, f"{filename}.db")
            imported_objs_dir = os.path.join(tmpdir, f"{filename}-objs")
            imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
            try:
                imported_fs.import_event_bundle(tampered_path)
                assert False, f"{filename} should be rejected"
            except anfs_core.StorageCorruptionError:
                pass
            with sqlite3.connect(imported_db_path) as conn:
                assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_import_event_bundle_rejects_tampered_metadata_checksum():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        ws = source_fs.open_workspace("ws:coder", "coder_agent")
        ws.write("src/db.py", b"print('v1')", [])
        ws.publish("src/db.py", "artifact:patch@v1")
        publish_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, _event_count, _node_count, _blob_count = source_fs.export_event_bundle(
            publish_event,
            bundle_dir,
        )

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)
        bundle["events"][0]["kind"] = "tampered_write"
        with open(bundle_path, "w", encoding="utf-8") as file:
            json.dump(bundle, file, indent=2)

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        try:
            imported_fs.import_event_bundle(bundle_path)
            assert False, "tampered bundle metadata should be rejected"
        except anfs_core.StorageCorruptionError:
            pass


def test_import_event_bundle_rejects_internally_inconsistent_bundle():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        ws = source_fs.open_workspace("ws:coder", "coder_agent")
        ws.write("src/db.py", b"print('v1')", [])
        ws.publish("src/db.py", "artifact:patch@v1")
        publish_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, _event_count, _node_count, _blob_count = source_fs.export_event_bundle(
            publish_event,
            bundle_dir,
        )

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)

        missing_root_bundle = json.loads(json.dumps(bundle))
        missing_root_bundle.pop("bundle_checksum", None)
        missing_root_bundle["events"] = [
            event
            for event in missing_root_bundle["events"]
            if event["event_id"] != publish_event
        ]
        missing_root_path = os.path.join(bundle_dir, "missing-root.json")
        with open(missing_root_path, "w", encoding="utf-8") as file:
            json.dump(missing_root_bundle, file, indent=2)

        imported_root_db = os.path.join(tmpdir, "imported-root.db")
        imported_root_objs = os.path.join(tmpdir, "imported-root-objs")
        imported_root_fs = anfs_core.AnfsEngine(imported_root_db, imported_root_objs)
        try:
            imported_root_fs.import_event_bundle(missing_root_path)
            assert False, "bundle missing root event should be rejected"
        except anfs_core.StorageCorruptionError:
            pass
        with sqlite3.connect(imported_root_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0

        missing_node_bundle = json.loads(json.dumps(bundle))
        missing_node_bundle.pop("bundle_checksum", None)
        removed_node = missing_node_bundle["event_edges"][0]["node_id"]
        missing_node_bundle["nodes"] = [
            node
            for node in missing_node_bundle["nodes"]
            if node["node_id"] != removed_node
        ]
        missing_node_path = os.path.join(bundle_dir, "missing-node.json")
        with open(missing_node_path, "w", encoding="utf-8") as file:
            json.dump(missing_node_bundle, file, indent=2)

        imported_node_db = os.path.join(tmpdir, "imported-node.db")
        imported_node_objs = os.path.join(tmpdir, "imported-node-objs")
        imported_node_fs = anfs_core.AnfsEngine(imported_node_db, imported_node_objs)
        try:
            imported_node_fs.import_event_bundle(missing_node_path)
            assert False, "bundle edge referencing missing node should be rejected"
        except anfs_core.StorageCorruptionError:
            pass
        with sqlite3.connect(imported_node_db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_import_event_bundle_rejects_conflicting_existing_edges_and_ref_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        coder_ws = source_fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = source_fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        source_fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
        )
        approve_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, _event_count, _node_count, _blob_count = source_fs.export_event_bundle(
            approve_event,
            bundle_dir,
        )

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported_fs.import_event_bundle(bundle_path)

        with open(bundle_path, "r", encoding="utf-8") as file:
            original_bundle = json.load(file)

        edge_conflict_bundle = json.loads(json.dumps(original_bundle))
        edge_conflict_bundle["bundle_checksum"] = None
        edge_conflict_bundle["event_edges"][0]["logical_path"] = "__tampered_path__"
        edge_conflict_path = os.path.join(bundle_dir, "edge-conflict.json")
        with open(edge_conflict_path, "w", encoding="utf-8") as file:
            json.dump(edge_conflict_bundle, file, indent=2)

        try:
            imported_fs.import_event_bundle(edge_conflict_path)
            assert False, "conflicting imported event edge should be rejected"
        except anfs_core.RefConflictError:
            pass

        ref_event_conflict_bundle = json.loads(json.dumps(original_bundle))
        ref_event_conflict_bundle["bundle_checksum"] = None
        ref_event_conflict_bundle["ref_events"][0]["new_state"] = "__tampered_state__"
        ref_event_conflict_path = os.path.join(bundle_dir, "ref-event-conflict.json")
        with open(ref_event_conflict_path, "w", encoding="utf-8") as file:
            json.dump(ref_event_conflict_bundle, file, indent=2)

        try:
            imported_fs.import_event_bundle(ref_event_conflict_path)
            assert False, "conflicting imported ref event should be rejected"
        except anfs_core.RefConflictError:
            pass


def test_merge_workspace_rejects_conflicts_without_overwriting_base():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        importer_ws.write("conflict.txt", b"old", [])
        importer_ws.publish("conflict.txt", "resource:repo/main@v1/conflict.txt", "resource")
        coder_ws = fs.checkout("ws:coder", "coder_agent", "resource:repo/main@v1")
        coder_ws.write("conflict.txt", b"workspace", [])

        current_base = importer_ws.write("current.txt", b"current", [])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE refs
                SET node_id = ?, ref_version = ref_version + 1
                WHERE ref_name = ?
                """,
                (current_base, "resource:repo/main@v1/conflict.txt"),
            )

        try:
            fs.merge_workspace("resource:repo/main@v1", "ws:coder", "merge_agent")
        except anfs_core.RefConflictError:
            pass
        else:
            raise AssertionError("merge should reject conflicting workspace")

        assert fs.get_ref("resource:repo/main@v1/conflict.txt")[1] == current_base

        with sqlite3.connect(db_path) as conn:
            merge_events = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'merge_workspace'"
            ).fetchone()[0]
            merge_policy_events = conn.execute(
                """
                SELECT event_id, payload_json
                FROM events
                WHERE kind = 'policy_decision'
                  AND json_extract(payload_json, '$.policy') = 'workspace_merge'
                """
            ).fetchall()

        assert merge_events == 0
        assert len(merge_policy_events) == 1
        with sqlite3.connect(db_path) as conn:
            policy_edges = conn.execute(
                """
                SELECT role, node_id, logical_path
                FROM event_edges
                WHERE event_id = ?
                ORDER BY role
                """,
                (merge_policy_events[0][0],),
            ).fetchall()

        merge_policy_payload = json.loads(merge_policy_events[0][1])
        assert merge_policy_payload["decision"] == "deny"
        assert merge_policy_payload["reason_code"] == "workspace_conflicts"
        assert merge_policy_payload["reason"] == "workspace_conflicts"
        assert merge_policy_payload["target_ref"] == "resource:repo/main@v1"
        assert merge_policy_payload["workspace"] == "ws:coder"
        assert merge_policy_payload["conflict_count"] == 1
        assert merge_policy_payload["change_count"] == 0
        assert merge_policy_payload["conflicts"][0]["path"] == "conflict.txt"
        assert {
            (role, logical_path) for role, _node_id, logical_path in policy_edges
        } == {
            ("merge_conflict:0:checkout", "conflict.txt"),
            ("merge_conflict:0:current_base", "conflict.txt"),
            ("merge_conflict:0:workspace", "conflict.txt"),
        }


if __name__ == "__main__":
    test_killer_demo()
    test_valid_approval_and_ref_audit()
    test_reject_ref_records_lifecycle_event_and_audit()
    test_reject_ref_enforces_state_machine()
    test_ref_expected_version_guards_lifecycle_decisions()
    test_stale_approval_precondition_blocks_policy_event()
    test_self_approval_is_policy_denied_with_audit_event()
    test_self_rejection_is_policy_denied_with_audit_event()
    test_blob_dedup_file_storage_and_readback()
    test_node_range_reads_and_chunk_index_for_inline_and_file_blobs()
    test_persistent_node_chunk_cache_is_rebuildable_and_verified()
    test_workspace_delete_is_logical_ref_state()
    test_checkout_projects_base_refs_copy_on_write()
    test_fork_workspace_copies_draft_refs_with_typed_lineage_edges()
    test_workspace_branch_names_are_validated_at_public_boundaries()
    test_integrity_verifier_detects_checkout_base_snapshot_mismatch()
    test_integrity_verifier_clean_and_missing_file_blob()
    test_schema_status_and_strict_future_schema_guard()
    test_integrity_verifier_detects_blob_size_mismatch()
    test_integrity_verifier_detects_missing_fts_row()
    test_integrity_verifier_detects_corrupt_fts_rows()
    test_rebuild_derived_indexes_repairs_fts_and_clears_chunk_cache()
    test_repair_plan_classifies_derived_and_canonical_issues_without_mutating()
    test_compaction_plan_reports_operational_pressure_without_mutating()
    test_vacuum_database_orchestrates_sqlite_freelist_without_touching_kernel_state()
    test_compact_inline_blobs_moves_storage_without_changing_nodes_or_refs()
    test_integrity_verifier_detects_ref_audit_divergence()
    test_integrity_verifier_detects_ref_audit_missing_nodes()
    test_integrity_verifier_detects_ref_audit_event_edge_mismatch()
    test_integrity_verifier_detects_ref_audit_chain_discontinuity()
    test_gc_roots_and_candidates_are_reachability_based()
    test_collect_garbage_deletes_unreachable_file_blobs_with_audit()
    test_gc_collected_file_blob_can_be_recreated_by_same_content()
    test_gc_retention_window_filters_recent_unreachable_blobs()
    test_retention_policy_automation_runs_gc_with_audited_rule()
    test_gc_batch_limit_bounds_candidate_and_collection_size()
    test_gc_pin_preserves_deleted_ref_until_unpinned()
    test_integrity_verifier_detects_gc_pin_event_linkage_mismatch()
    test_policy_labels_are_append_only_and_queryable()
    test_query_policy_label_excludes_filter_active_ref_and_node_labels()
    test_policy_registry_interprets_label_values_for_visibility()
    test_policy_expression_rules_compose_active_labels_for_visibility()
    test_integrity_verifier_detects_policy_expression_rule_mismatch()
    test_purpose_policy_rules_gate_read_and_consume_without_breaking_plain_reads()
    test_purpose_capability_rules_authorize_explicit_purpose_without_breaking_plain_reads()
    test_operation_capability_rules_gate_lifecycle_operations()
    test_integrity_verifier_detects_purpose_policy_rule_mismatch()
    test_integrity_verifier_detects_capability_audit_mismatch()
    test_policy_labels_support_multiple_active_values_per_label()
    test_fragment_policy_labels_gate_ranges_and_whole_node_projection()
    test_json_field_policy_labels_map_semantic_paths_to_fragments()
    test_fragment_policy_labels_propagate_to_exact_copy_nodes()
    test_derived_outputs_inherit_source_policy_labels_conservatively()
    test_partial_output_attribution_propagates_fragment_policy_to_output_span()
    test_auto_attribution_propagates_unique_exact_fragment_matches_only()
    test_auto_attribution_propagates_unique_normalized_json_string_scalar()
    test_auto_attribution_propagates_integer_normalized_json_number_scalar()
    test_auto_attribution_propagates_json_bool_null_and_float_scalars()
    test_auto_attribution_propagates_normalized_json_array_and_object_values()
    test_markdown_frontmatter_policy_labels_map_semantic_fields_to_fragments()
    test_markdown_body_section_policy_labels_map_headings_to_fragments()
    test_search_policy_label_excludes_filter_context_results()
    test_vector_embedding_projection_searches_visible_refs()
    test_chunk_embedding_projection_searches_visible_chunk_ranges()
    test_integrity_verifier_detects_corrupt_embedding_projection()
    test_integrity_verifier_detects_corrupt_chunk_embedding_projection()
    test_integrity_verifier_detects_policy_label_event_mismatch()
    test_manifest_publish_approval_requires_all_children_covered()
    test_integrity_verifier_detects_manifest_child_metadata_mismatch()
    test_snapshot_namespace_publishes_manifest_ref_for_active_view()
    test_integrity_verifier_detects_manifest_event_child_edge_mismatch()
    test_policy_decision_log_records_approve_allow_and_deny()
    test_observability_ref_history_and_event_lookup()
    test_event_sequence_is_explicit_and_monotonic()
    test_integrity_verifier_detects_invalid_event_edge_direction()
    test_integrity_verifier_detects_event_edge_shape_mismatch()
    test_integrity_verifier_detects_access_event_edge_shape_mismatch()
    test_integrity_verifier_detects_lifecycle_event_edge_shape_mismatch()
    test_integrity_verifier_detects_workspace_event_edge_shape_mismatch()
    test_integrity_verifier_detects_policy_merge_and_gc_event_mismatch()
    test_integrity_verifier_detects_merge_ref_audit_edge_linkage_mismatch()
    test_concurrent_engine_handles_allocate_unique_event_sequences()
    test_concurrent_stale_ref_version_allows_only_one_shared_ref_update()
    test_events_api_lists_by_sequence_with_pagination_and_filtering()
    test_unified_query_api_filters_current_refs()
    test_workspace_query_records_context_event_with_result_edges()
    test_run_id_propagates_to_workspace_and_engine_events()
    test_tool_call_id_propagates_to_workspace_events()
    test_consume_event_records_ref_version_and_state()
    test_read_ref_enforces_workspace_draft_boundary_and_records_version()
    test_tool_call_id_propagates_to_engine_events()
    test_run_metadata_lifecycle_and_listing()
    test_integrity_verifier_detects_run_audit_divergence()
    test_integrity_verifier_detects_run_event_linkage_mismatch()
    test_integrity_verifier_detects_run_audit_chain_discontinuity()
    test_search_records_context_event_with_result_edges()
    test_draft_search_is_scoped_to_current_workspace()
    test_posix_facade_path_read_ls_stat_find_and_grep_are_audited()
    test_posix_facade_cp_mv_and_rm_preserve_ref_audit()
    test_posix_facade_directory_cp_mv_and_rm_are_recursive()
    test_posix_find_and_grep_are_uncapped_by_default_with_optional_limit()
    test_export_run_bundle_includes_all_run_events_and_causal_inputs()
    test_ref_view_at_event_reconstructs_namespace_view()
    test_ref_view_checkpoint_records_and_verifies_replay_proof()
    test_archival_readiness_plan_requires_full_archive_and_valid_checkpoint()
    test_replay_views_exclude_active_policy_labels()
    test_materialize_ref_view_at_event_writes_replay_directory()
    test_materialize_ref_view_rejects_backslash_paths()
    test_materialize_ref_view_rejects_duplicate_relative_paths()
    test_export_event_bundle_writes_causal_subgraph_and_objects()
    test_export_history_archive_exports_full_event_ref_history_without_mutating()
    test_bundle_export_policy_label_excludes_block_labeled_refs_and_nodes()
    test_bundle_import_policy_label_excludes_block_destination_labels_without_partial_writes()
    test_worktree_materialize_and_commit_roundtrip_existing_fs_edits()
    test_worktree_checked_commit_rejects_stale_materialized_base()
    test_worktree_checked_commit_rejects_paths_removed_or_added_after_materialization()
    test_concurrent_checked_worktree_commits_allow_only_one_materialized_base()
    test_worktree_readiness_and_commit_reject_symlinks_without_following_them()
    test_worktree_readiness_and_commit_reject_backslash_paths()
    test_worktree_adapter_reserves_root_manifest_path()
    test_agent_native_fs_conformance_preserves_kernel_invariants_with_existing_tools()
    test_cached_materialized_workspace_reuses_and_verifies_working_set()
    test_worktree_commit_rolls_back_all_file_mutations_on_late_failure()
    test_integrity_verifier_detects_worktree_commit_event_mismatch()
    test_import_event_bundle_roundtrips_causal_subgraph()
    test_import_event_bundle_uses_source_sequence_order()
    test_import_event_bundle_rejects_invalid_source_sequence_metadata()
    test_import_event_bundle_rejects_tampered_metadata_checksum()
    test_event_bundle_hmac_signature_can_be_required_on_import()
    test_import_event_bundle_rejects_internally_inconsistent_bundle()
    test_import_metadata_only_bundle_reuses_existing_cas_blob()
    test_import_event_bundle_rejects_conflicting_existing_edges_and_ref_events()
    test_workspace_diff_reports_added_modified_deleted_unchanged()
    test_workspace_conflicts_use_checkout_base_snapshot()
    test_merge_workspace_applies_non_conflicting_changes_atomically()
    test_merge_workspace_accepts_snapshot_manifest_base()
    test_merge_workspace_from_snapshot_detects_current_base_conflicts()
    test_merge_workspace_rejects_conflicts_without_overwriting_base()
    print("ANFS killer demo passed.")
