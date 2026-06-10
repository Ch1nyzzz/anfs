"""Event/run/archive bundle export+import, signatures, and replay checkpoints."""

import hashlib
import json
import os
import sqlite3
import tempfile

import anfs_core


def test_export_run_bundle_includes_all_run_events_and_causal_inputs():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "run-bundle")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        coder_ws = source_fs.open_workspace(
            "ws:coder",
            "coder_agent",
            run_id="run:coder",
        )
        tester_ws = source_fs.open_workspace(
            "ws:tester",
            "tester_agent",
            run_id="run:tester",
        )

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        note_node = coder_ws.write("notes/debug.md", b"unpublished debugging note", [])
        test_node = tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")

        bundle_path, event_count, node_count, blob_count = source_fs.export_run_bundle(
            "run:coder",
            bundle_dir,
        )

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)

        assert bundle["schema"] == "anfs.event_bundle.v1"
        assert [event["kind"] for event in bundle["events"]] == [
            "write",
            "publish",
            "write",
        ]
        assert {event["run_id"] for event in bundle["events"]} == {"run:coder"}
        assert {node["node_id"] for node in bundle["nodes"]} == {patch_node, note_node}
        assert test_node not in {node["node_id"] for node in bundle["nodes"]}
        assert event_count == 3
        assert node_count == 2
        assert blob_count == 2

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported = imported_fs.import_event_bundle(bundle_path)
        assert imported == (bundle["root_event_id"], event_count, node_count, blob_count)
        assert bytes(imported_fs.read_node(patch_node)) == b"print('v1')"
        assert bytes(imported_fs.read_node(note_node)) == b"unpublished debugging note"
        try:
            imported_fs.read_node(test_node)
            assert False, "run bundle should not include future events from other runs"
        except anfs_core.NodeNotFoundError:
            pass

        try:
            source_fs.export_run_bundle("run:missing", os.path.join(tmpdir, "missing"))
            assert False, "missing run id should raise EventNotFoundError"
        except anfs_core.EventNotFoundError:
            pass


def test_ref_view_checkpoint_records_and_verifies_replay_proof():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        a_node = importer_ws.write("a.txt", b"a", [])
        importer_ws.publish("a.txt", "resource:repo/main@v1/a.txt", "resource")
        b_node = importer_ws.write("b.txt", b"b", [])
        importer_ws.publish("b.txt", "resource:repo/main@v1/b.txt", "resource")
        fs.archive_ref("resource:repo/main@v1/b.txt", "retention_agent")
        archive_event = fs.ref_history("resource:repo/main@v1/b.txt")[-1][1]

        checkpoint = fs.create_ref_view_checkpoint(
            event_id=archive_event,
            prefix="resource:repo/main@v1/",
            include_inactive=True,
            agent_id="checkpoint_agent",
        )
        (
            checkpoint_id,
            target_event_id,
            target_seq,
            prefix,
            include_inactive,
            ref_count,
            checksum,
            agent_id,
            _created_at,
        ) = checkpoint
        assert target_event_id == archive_event
        assert target_seq > 0
        assert prefix == "resource:repo/main@v1"
        assert include_inactive is True
        assert ref_count == 2
        assert len(checksum) == 64
        assert agent_id == "checkpoint_agent"
        assert fs.ref_view_checkpoints() == [checkpoint]

        checkpoint_event = fs.event(checkpoint_id)
        assert checkpoint_event[1] == "ref_view_checkpoint"
        assert checkpoint_event[2] == "checkpoint_agent"
        payload = json.loads(checkpoint_event[6])
        assert payload["schema"] == "anfs.ref_view_checkpoint.v1"
        assert payload["target_event_id"] == archive_event
        assert payload["ref_count"] == 2
        assert payload["checksum"] == checksum

        verification = fs.verify_ref_view_checkpoint(checkpoint_id)
        assert verification == (checkpoint_id, True, checksum, checksum, 2, 2)
        view = {
            row[0]: (row[1], row[3])
            for row in fs.ref_view_at_event(
                archive_event,
                prefix="resource:repo/main@v1",
                include_inactive=True,
            )
        }
        assert view == {
            "resource:repo/main@v1/a.txt": (a_node, "published"),
            "resource:repo/main@v1/b.txt": (b_node, "archived"),
        }
        assert fs.verify_integrity() == []

        tampered_checksum = "0" * 64
        with sqlite3.connect(db_path) as conn:
            try:
                conn.execute(
                    """
                    UPDATE ref_view_checkpoints
                    SET checksum = ?
                    WHERE checkpoint_id = ?
                    """,
                    (tampered_checksum, checkpoint_id),
                )
            except sqlite3.IntegrityError as exc:
                assert "ref_view_checkpoints are immutable" in str(exc)
            else:
                raise AssertionError("ref view checkpoints should be immutable")

            bad_checkpoint_id = "event:bad-ref-view-checkpoint"
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'ref_view_checkpoint', 'bad_agent', NULL, NULL, NULL, '{}', 0)
                """,
                (bad_checkpoint_id,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_checkpoint_id, next_seq),
            )
            conn.execute(
                """
                INSERT INTO ref_view_checkpoints
                    (checkpoint_id, target_event_id, target_seq, prefix, include_inactive,
                     ref_count, checksum, agent_id, created_at)
                VALUES (?, ?, ?, ?, 1, 2, ?, 'bad_agent', 0)
                """,
                (
                    bad_checkpoint_id,
                    archive_event,
                    target_seq,
                    "resource:repo/main@v1",
                    tampered_checksum,
                ),
            )

        tampered = fs.verify_ref_view_checkpoint(bad_checkpoint_id)
        assert tampered[0] == bad_checkpoint_id
        assert tampered[1] is False
        assert tampered[2] == tampered_checksum
        assert tampered[3] == checksum
        assert tampered[4] == 2
        assert tampered[5] == 2
        issues = fs.verify_integrity()
        assert any(
            f"ref_view_checkpoint {bad_checkpoint_id} mismatch" in issue
            for issue in issues
        )


def test_archival_readiness_plan_requires_full_archive_and_valid_checkpoint():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        bundle_dir = os.path.join(tmpdir, "history-archive")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:importer", "importer_agent")

        node = ws.write("a.txt", b"a", [])
        ws.publish("a.txt", "resource:repo/main@v1/a.txt", "resource")
        publish_event = fs.ref_history("resource:repo/main@v1/a.txt")[-1][1]
        checkpoint = fs.create_ref_view_checkpoint(
            event_id=publish_event,
            prefix="resource:repo/main@v1",
            agent_id="checkpoint_agent",
        )
        checkpoint_id = checkpoint[0]
        bundle_path, event_count, _node_count, _blob_count = fs.export_history_archive(
            bundle_dir,
            signing_key="archive-secret",
            signer_id="ops",
        )

        plan = fs.archival_readiness_plan(
            checkpoint_id,
            bundle_path,
            signature_key="archive-secret",
            require_signature=True,
        )
        rows = {row[0]: row for row in plan}
        assert rows["history_archive_bundle"][1] is True
        assert rows["history_archive_bundle"][3] == event_count
        assert "covers" in rows["history_archive_bundle"][2]
        assert rows["ref_view_checkpoint"] == (
            "ref_view_checkpoint",
            True,
            f"ref-view checkpoint {checkpoint_id} verifies",
            1,
        )
        assert rows["physical_archival_preconditions"][1] is True

        ws.write("b.txt", b"b", [node])
        stale_plan = fs.archival_readiness_plan(
            checkpoint_id,
            bundle_path,
            signature_key="archive-secret",
            require_signature=True,
        )
        stale_rows = {row[0]: row for row in stale_plan}
        assert stale_rows["history_archive_bundle"][1] is False
        assert "mismatch" in stale_rows["history_archive_bundle"][2]
        assert stale_rows["ref_view_checkpoint"][1] is True
        assert stale_rows["physical_archival_preconditions"][1] is False

        unsigned_plan = fs.archival_readiness_plan(
            checkpoint_id,
            bundle_path,
            require_signature=True,
        )
        unsigned_rows = {row[0]: row for row in unsigned_plan}
        assert unsigned_rows["history_archive_bundle"][1] is False
        assert "signature_key is required" in unsigned_rows["history_archive_bundle"][2]


def test_export_event_bundle_writes_causal_subgraph_and_objects():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        bundle_dir = os.path.join(tmpdir, "bundle")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        test_node = tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.approve("artifact:patch@v1", ["artifact:test_result@v1"], "reviewer_agent")
        approve_event = fs.ref_history("artifact:patch@v1")[-1][1]

        bundle_path, event_count, node_count, blob_count = fs.export_event_bundle(
            approve_event,
            bundle_dir,
        )

        assert bundle_path == os.path.join(bundle_dir, "bundle.json")
        assert event_count >= 3
        assert node_count == 2
        assert blob_count == 2

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)

        assert bundle["schema"] == "anfs.event_bundle.v1"
        assert bundle["root_event_id"] == approve_event
        assert len(bundle["bundle_checksum"]) == 64
        assert all(char in "0123456789abcdef" for char in bundle["bundle_checksum"])
        source_seqs = [event["source_seq"] for event in bundle["events"]]
        assert source_seqs == sorted(source_seqs)
        assert bundle["events"][-1]["event_id"] == approve_event
        event_kinds = {event["kind"] for event in bundle["events"]}
        assert {"write", "publish", "approve"}.issubset(event_kinds)
        node_ids = {node["node_id"] for node in bundle["nodes"]}
        assert node_ids == {patch_node, test_node}
        edge_roles = {edge["role"] for edge in bundle["event_edges"]}
        assert {"source", "result", "target", "evidence"}.issubset(edge_roles)
        ref_event_refs = {ref_event["ref_name"] for ref_event in bundle["ref_events"]}
        assert "artifact:patch@v1" in ref_event_refs
        assert "artifact:test_result@v1" in ref_event_refs

        for blob in bundle["blobs"]:
            assert blob["object_path"] is not None
            object_path = os.path.join(bundle_dir, blob["object_path"])
            assert os.path.exists(object_path)
            with open(object_path, "rb") as file:
                assert hashlib.sha256(file.read()).hexdigest() == blob["hash"]


def test_export_history_archive_exports_full_event_ref_history_without_mutating():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "history-archive")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        left_ws = source_fs.open_workspace("ws:left", "left_agent")
        right_ws = source_fs.open_workspace("ws:right", "right_agent")

        left_node = left_ws.write("left.txt", b"left branch", [])
        left_ws.publish("left.txt", "artifact:left@v1")
        right_node = right_ws.write("right.txt", b"right branch", [])
        right_ws.publish("right.txt", "artifact:right@v1")
        source_fs.archive_ref("artifact:left@v1", "retention_agent")

        before_events = source_fs.events(limit=1000)
        before_left_history = source_fs.ref_history("artifact:left@v1")
        before_right_history = source_fs.ref_history("artifact:right@v1")

        bundle_path, event_count, node_count, blob_count = source_fs.export_history_archive(
            bundle_dir,
            signing_key="archive-secret",
            signer_id="ops",
        )

        after_events = source_fs.events(limit=1000)
        assert after_events == before_events
        assert source_fs.ref_history("artifact:left@v1") == before_left_history
        assert source_fs.ref_history("artifact:right@v1") == before_right_history
        assert source_fs.verify_integrity() == []

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)

        source_event_ids = [row[1] for row in before_events]
        assert bundle["root_event_id"] == source_event_ids[-1]
        assert [event["event_id"] for event in bundle["events"]] == source_event_ids
        assert [event["source_seq"] for event in bundle["events"]] == [
            row[0] for row in before_events
        ]
        assert {node["node_id"] for node in bundle["nodes"]} == {left_node, right_node}
        assert event_count == len(before_events)
        assert node_count == 2
        assert blob_count == 2
        assert bundle["bundle_signature"]["scheme"] == "hmac-sha256"
        assert bundle["bundle_signature"]["signer_id"] == "ops"

        ref_states = {
            (ref_event["ref_name"], ref_event["new_state"])
            for ref_event in bundle["ref_events"]
        }
        assert ("artifact:left@v1", "published") in ref_states
        assert ("artifact:left@v1", "archived") in ref_states
        assert ("artifact:right@v1", "published") in ref_states

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported = imported_fs.import_event_bundle(
            bundle_path,
            signature_key="archive-secret",
            require_signature=True,
        )
        assert imported == (bundle["root_event_id"], event_count, node_count, blob_count)
        assert bytes(imported_fs.read_node(left_node)) == b"left branch"
        assert bytes(imported_fs.read_node(right_node)) == b"right branch"


def test_import_event_bundle_roundtrips_causal_subgraph():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")
        replay_dir = os.path.join(tmpdir, "imported-replay")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        coder_ws = source_fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = source_fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        test_node = tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        source_fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
        )
        approve_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, event_count, node_count, blob_count = source_fs.export_event_bundle(
            approve_event,
            bundle_dir,
        )

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported = imported_fs.import_event_bundle(bundle_path)
        assert imported == (approve_event, event_count, node_count, blob_count)
        assert bytes(imported_fs.read_node(patch_node)) == b"print('v1')"
        assert bytes(imported_fs.read_node(test_node)) == b"PASS"

        imported_event = imported_fs.event(approve_event)
        assert imported_event[1] == "approve"
        imported_view = {
            row[0]: (row[1], row[3])
            for row in imported_fs.ref_view_at_event(
                approve_event,
                prefix="artifact:",
            )
        }
        assert imported_view == {
            "artifact:patch@v1": (patch_node, "approved"),
            "artifact:test_result@v1": (test_node, "published"),
        }

        materialized = {
            row[1]: row[3]
            for row in imported_fs.materialize_ref_view_at_event(
                approve_event,
                replay_dir,
                prefix="artifact:",
            )
        }
        assert materialized == {
            "patch@v1": len(b"print('v1')"),
            "test_result@v1": len(b"PASS"),
        }
        with open(os.path.join(replay_dir, "patch@v1"), "rb") as file:
            assert file.read() == b"print('v1')"
        with open(os.path.join(replay_dir, "test_result@v1"), "rb") as file:
            assert file.read() == b"PASS"

        # Import is idempotent for the same bundle.
        assert imported_fs.import_event_bundle(bundle_path) == imported
        with sqlite3.connect(imported_db_path) as conn:
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            sequence_count = conn.execute("SELECT COUNT(*) FROM event_sequence").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM node_fts").fetchone()[0]
        assert sequence_count == event_count
        assert fts_count == node_count
        assert imported_fs.verify_integrity() == []
        imported_events = imported_fs.events(limit=100)
        assert len(imported_events) == event_count
        assert imported_events[-1][1] == approve_event


def test_import_event_bundle_uses_source_sequence_order():
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
        expected_order = [
            event["event_id"]
            for event in sorted(bundle["events"], key=lambda event: event["source_seq"])
        ]
        bundle.pop("bundle_checksum", None)
        bundle["events"] = list(reversed(bundle["events"]))
        reversed_path = os.path.join(bundle_dir, "reversed-events.json")
        with open(reversed_path, "w", encoding="utf-8") as file:
            json.dump(bundle, file, indent=2)

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported_fs.import_event_bundle(reversed_path)
        imported_order = [event[1] for event in imported_fs.events(limit=100)]
        assert imported_order == expected_order
        assert imported_order[-1] == publish_event
        assert imported_fs.verify_integrity() == []


def test_event_bundle_hmac_signature_can_be_required_on_import():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        signed_bundle_dir = os.path.join(tmpdir, "signed-bundle")
        unsigned_bundle_dir = os.path.join(tmpdir, "unsigned-bundle")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")
        wrong_key_db_path = os.path.join(tmpdir, "wrong-key.db")
        wrong_key_objs_dir = os.path.join(tmpdir, "wrong-key-objs")
        unsigned_db_path = os.path.join(tmpdir, "unsigned.db")
        unsigned_objs_dir = os.path.join(tmpdir, "unsigned-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        ws = source_fs.open_workspace("ws:coder", "coder_agent")
        ws.write("src/db.py", b"print('signed')", [])
        ws.publish("src/db.py", "artifact:patch@signed")
        publish_event = source_fs.ref_history("artifact:patch@signed")[-1][1]

        bundle_path, _event_count, _node_count, _blob_count = source_fs.export_event_bundle(
            publish_event,
            signed_bundle_dir,
            signing_key="bundle-secret",
            signer_id="source:test",
        )
        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)
        signature = bundle["bundle_signature"]
        assert signature["scheme"] == "hmac-sha256"
        assert signature["signer_id"] == "source:test"
        assert signature["payload_sha256"] == bundle["bundle_checksum"]
        assert len(signature["signature"]) == 64
        assert all(char in "0123456789abcdef" for char in signature["signature"])

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        imported = imported_fs.import_event_bundle(
            bundle_path,
            signature_key="bundle-secret",
            require_signature=True,
        )
        assert imported[0] == publish_event
        assert imported_fs.verify_integrity() == []

        wrong_key_fs = anfs_core.AnfsEngine(wrong_key_db_path, wrong_key_objs_dir)
        try:
            wrong_key_fs.import_event_bundle(
                bundle_path,
                signature_key="wrong-secret",
                require_signature=True,
            )
            assert False, "signed bundle import should reject the wrong key"
        except anfs_core.StorageCorruptionError:
            pass

        try:
            wrong_key_fs.import_event_bundle(bundle_path, require_signature=True)
            assert False, "require_signature should require a verification key"
        except anfs_core.PolicyDeniedError:
            pass

        unsigned_bundle_path, _event_count, _node_count, _blob_count = (
            source_fs.export_event_bundle(
                publish_event,
                unsigned_bundle_dir,
            )
        )
        unsigned_fs = anfs_core.AnfsEngine(unsigned_db_path, unsigned_objs_dir)
        try:
            unsigned_fs.import_event_bundle(
                unsigned_bundle_path,
                signature_key="bundle-secret",
                require_signature=True,
            )
            assert False, "require_signature should reject unsigned bundles"
        except anfs_core.StorageCorruptionError:
            pass


def test_import_metadata_only_bundle_reuses_existing_cas_blob():
    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle-no-blobs")
        imported_db_path = os.path.join(tmpdir, "imported.db")
        imported_objs_dir = os.path.join(tmpdir, "imported-objs")

        content = b"print('v1')"
        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        source_ws = source_fs.open_workspace("ws:coder", "coder_agent")
        source_node = source_ws.write("src/db.py", content, [])
        source_ws.publish("src/db.py", "artifact:patch@v1")
        publish_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, event_count, node_count, blob_count = source_fs.export_event_bundle(
            publish_event,
            bundle_dir,
            False,
        )

        with open(bundle_path, "r", encoding="utf-8") as file:
            bundle = json.load(file)
        assert bundle["blobs"][0]["object_path"] is None
        assert not os.path.exists(os.path.join(bundle_dir, "objects"))

        imported_fs = anfs_core.AnfsEngine(imported_db_path, imported_objs_dir)
        seed_ws = imported_fs.open_workspace("ws:seed", "seed_agent")
        seed_ws.write("same-content.py", content, [])

        imported = imported_fs.import_event_bundle(bundle_path)
        assert imported == (publish_event, event_count, node_count, blob_count)
        assert bytes(imported_fs.read_node(source_node)) == content
        assert imported_fs.verify_integrity() == []
