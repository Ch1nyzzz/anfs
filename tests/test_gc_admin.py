"""GC pins, retention policies, snapshots, vacuum/compaction admin operations."""

import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import anfs_core


def test_compaction_plan_reports_operational_pressure_without_mutating():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        keep_node = ws.write("keep.txt", b"keep", [])
        fs.pin_ref("ws:coder/keep.txt", "retain active draft", "ops_agent")

        garbage_bytes = b"deleted file-backed payload" * 4096
        ws.write("deleted.bin", garbage_bytes, [])
        ws.delete("deleted.bin")

        before_candidates = fs.gc_candidates(True)
        before_issues = fs.verify_integrity()
        plan = fs.compaction_plan()

        assert fs.verify_integrity() == before_issues
        assert fs.gc_candidates(True) == before_candidates
        assert bytes(fs.read_node(keep_node)) == b"keep"

        rows = {area: (count, action, scope) for area, count, action, scope in plan}
        assert rows["inactive_refs"][0] >= 1
        assert rows["unreachable_blobs"][0] >= 1
        assert rows["unreachable_blob_bytes"][0] >= len(garbage_bytes)
        assert "collect_garbage" in rows["unreachable_blobs"][1]
        assert rows["unreachable_blobs"][2] == "object_bytes"
        assert rows["active_gc_pins"][0] == 1
        assert rows["active_retention_policies"][0] == 0
        assert rows["active_retention_policies"][2] == "gc_policy"
        assert rows["canonical_events"][0] >= 3
        assert rows["ref_audit_history"][0] >= 2
        assert rows["derived_projection_rows"][2] == "derived"
        assert "VACUUM" in rows["sqlite_freelist_pages"][1] or rows[
            "sqlite_freelist_pages"
        ][0] == 0


def test_vacuum_database_orchestrates_sqlite_freelist_without_touching_kernel_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        node_id = ws.write("keep.txt", b"keep", [])

        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE vacuum_pressure(payload BLOB)")
            payload = b"x" * 4096
            conn.executemany(
                "INSERT INTO vacuum_pressure(payload) VALUES (?)",
                [(payload,) for _ in range(128)],
            )
            conn.execute("DROP TABLE vacuum_pressure")

        before_plan_rows = {row[0]: row for row in fs.compaction_plan()}
        before_freelist = before_plan_rows["sqlite_freelist_pages"][1]
        assert before_freelist > 0

        dry_before, dry_after, dry_ran = fs.vacuum_database()
        assert dry_before == before_freelist
        assert dry_after == before_freelist
        assert dry_ran is False
        assert bytes(fs.read_node(node_id)) == b"keep"

        run_before, run_after, ran = fs.vacuum_database(dry_run=False)
        assert run_before == before_freelist
        assert ran is True
        assert run_after < run_before
        assert fs.verify_integrity() == []
        assert bytes(fs.read_node(node_id)) == b"keep"

        after_plan_rows = {row[0]: row for row in fs.compaction_plan()}
        assert after_plan_rows["sqlite_freelist_pages"][1] == run_after


def test_compact_inline_blobs_moves_storage_without_changing_nodes_or_refs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        content = b"small inline payload"
        node_id = ws.write("small.txt", content, [])
        ws.publish("small.txt", "artifact:small@v1")

        with sqlite3.connect(db_path) as conn:
            blob_hash, storage_kind, storage_uri, inline_content = conn.execute(
                """
                SELECT b.hash, b.storage_kind, b.storage_uri, b.inline_content
                FROM nodes n
                JOIN blobs b ON b.hash = n.blob_hash
                WHERE n.node_id = ?
                """,
                (node_id,),
            ).fetchone()
        assert storage_kind == "inline"
        assert storage_uri is None
        assert inline_content == content

        plan_before = {row[0]: row for row in fs.compaction_plan()}
        assert plan_before["inline_blobs"][1] >= 1
        assert plan_before["inline_blob_bytes"][1] >= len(content)

        dry_candidates, dry_bytes, dry_compacted, dry_run = fs.compact_inline_blobs()
        assert dry_run is True
        assert dry_candidates >= 1
        assert dry_bytes >= len(content)
        assert dry_compacted == 0
        assert bytes(fs.read_node(node_id)) == content
        assert fs.get_ref("artifact:small@v1")[1] == node_id

        run_candidates, run_bytes, run_compacted, ran_dry = fs.compact_inline_blobs(
            dry_run=False,
            limit=1,
        )
        assert ran_dry is False
        assert run_candidates == 1
        assert run_bytes >= len(content)
        assert run_compacted == 1

        with sqlite3.connect(db_path) as conn:
            storage_kind, storage_uri, inline_content = conn.execute(
                "SELECT storage_kind, storage_uri, inline_content FROM blobs WHERE hash = ?",
                (blob_hash,),
            ).fetchone()
            try:
                conn.execute(
                    "UPDATE blobs SET size = size + 1 WHERE hash = ?",
                    (blob_hash,),
                )
            except sqlite3.DatabaseError as exc:
                assert "blob CAS identity is immutable" in str(exc)
            else:
                raise AssertionError("blob hash/size identity update should be rejected")

        assert storage_kind == "file"
        assert storage_uri is not None
        assert inline_content is None
        assert Path(storage_uri).read_bytes() == content
        assert bytes(fs.read_node(node_id)) == content
        assert fs.get_ref("artifact:small@v1")[1] == node_id
        assert fs.verify_integrity() == []


def test_gc_roots_and_candidates_are_reachability_based():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        published_content = b"published"
        draft_content = b"draft"
        deleted_content = b"deleted"

        published_node = ws.write("published.txt", published_content, [])
        ws.publish("published.txt", "artifact:published@v1")
        draft_node = ws.write("draft.txt", draft_content, [])
        ws.write("deleted.txt", deleted_content, [])
        ws.delete("deleted.txt")

        roots = fs.gc_roots(True)
        root_refs = {root[0] for root in roots}
        assert "artifact:published@v1" in root_refs
        assert "ws:coder/draft.txt" in root_refs
        assert "ws:coder/deleted.txt" not in root_refs

        candidates_with_workspaces = {candidate[0] for candidate in fs.gc_candidates(True)}
        candidates_without_workspaces = {
            candidate[0] for candidate in fs.gc_candidates(False)
        }

        published_hash = hashlib.sha256(published_content).hexdigest()
        draft_hash = hashlib.sha256(draft_content).hexdigest()
        deleted_hash = hashlib.sha256(deleted_content).hexdigest()

        assert published_hash not in candidates_with_workspaces
        assert published_hash not in candidates_without_workspaces
        assert draft_hash not in candidates_with_workspaces
        assert draft_hash in candidates_without_workspaces
        assert deleted_hash in candidates_with_workspaces
        assert deleted_hash in candidates_without_workspaces

        assert bytes(fs.read_node(published_node)) == published_content
        assert bytes(fs.read_node(draft_node)) == draft_content


def test_gc_collected_file_blob_can_be_recreated_by_same_content():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        content = b"recreated" * (10 * 1024)
        blob_hash = hashlib.sha256(content).hexdigest()
        first_node = ws.write("garbage.bin", content, [])
        ws.delete("garbage.bin")

        first_collect = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=False,
                agent_id="gc_agent",
            )
        }
        assert first_collect[blob_hash] == "collected_file"
        try:
            fs.read_node(first_node)
            assert False, "first collected node should not read back"
        except anfs_core.StorageCorruptionError:
            pass

        recreated_node = ws.write("garbage-again.bin", content, [])
        assert bytes(fs.read_node(recreated_node)) == content
        assert fs.verify_integrity() == []

        ws.delete("garbage-again.bin")
        assert blob_hash in {row[0] for row in fs.gc_candidates(True)}
        second_collect = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=False,
                agent_id="gc_agent",
            )
        }
        assert second_collect[blob_hash] == "collected_file"
        with sqlite3.connect(db_path) as conn:
            gc_blob_event_count = conn.execute(
                "SELECT COUNT(*) FROM gc_blob_events WHERE hash = ?",
                (blob_hash,),
            ).fetchone()[0]
        assert gc_blob_event_count == 2


def test_gc_retention_window_filters_recent_unreachable_blobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        content = b"recent-unreachable" * (8 * 1024)
        blob_hash = hashlib.sha256(content).hexdigest()
        node_id = ws.write("recent.bin", content, [])
        ws.delete("recent.bin")

        with sqlite3.connect(db_path) as conn:
            storage_uri = conn.execute(
                "SELECT storage_uri FROM blobs WHERE hash = ?",
                (blob_hash,),
            ).fetchone()[0]

        assert blob_hash in {row[0] for row in fs.gc_candidates(True)}
        assert blob_hash not in {
            row[0] for row in fs.gc_candidates(True, older_than_ms=0)
        }
        assert blob_hash not in {
            row[0]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=True,
                agent_id="gc_agent",
                older_than_ms=0,
            )
        }

        no_op = fs.collect_garbage(
            include_workspaces=True,
            dry_run=False,
            agent_id="gc_agent",
            older_than_ms=0,
        )
        assert no_op == []
        assert os.path.exists(storage_uri)
        assert bytes(fs.read_node(node_id)) == content

        collected = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=False,
                agent_id="gc_agent",
                older_than_ms=9_999_999_999_999,
            )
        }
        assert collected[blob_hash] == "collected_file"
        assert not os.path.exists(storage_uri)
        assert fs.verify_integrity() == []


def test_gc_batch_limit_bounds_candidate_and_collection_size():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        blobs = []
        for idx in range(3):
            content = f"batch-garbage-{idx}".encode("utf-8") * (8 * 1024)
            blob_hash = hashlib.sha256(content).hexdigest()
            ws.write(f"garbage-{idx}.bin", content, [])
            ws.delete(f"garbage-{idx}.bin")
            blobs.append(blob_hash)

        expected_order = sorted(blobs)
        assert [row[0] for row in fs.gc_candidates(True, limit=2)] == expected_order[:2]

        first_batch = fs.collect_garbage(
            include_workspaces=True,
            dry_run=False,
            agent_id="gc_agent",
            limit=2,
        )
        assert [row[0] for row in first_batch] == expected_order[:2]
        assert [row[3] for row in first_batch] == ["collected_file", "collected_file"]
        assert [row[0] for row in fs.gc_candidates(True)] == expected_order[2:]

        second_batch = fs.collect_garbage(
            include_workspaces=True,
            dry_run=False,
            agent_id="gc_agent",
            limit=2,
        )
        assert [row[0] for row in second_batch] == expected_order[2:]
        assert fs.gc_candidates(True) == []
        assert fs.verify_integrity() == []

        try:
            fs.gc_candidates(True, limit=0)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid gc candidate limit should be rejected")

        try:
            fs.collect_garbage(limit=10001)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid gc collection limit should be rejected")


def test_gc_pin_preserves_deleted_ref_until_unpinned():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        retained_content = b"retain-me" * (9 * 1024)
        retained_hash = hashlib.sha256(retained_content).hexdigest()
        retained_node = ws.write("retained.bin", retained_content, [])
        ws.delete("retained.bin")

        pin_id = fs.pin_ref(
            "ws:coder/retained.bin",
            reason="retain deleted draft for audit",
            agent_id="policy_agent",
        )

        roots = fs.gc_roots(True)
        assert ("ws:coder/retained.bin", retained_node, "pinned") in roots
        active_pins = fs.gc_pins()
        assert len(active_pins) == 1
        assert active_pins[0][:5] == (
            pin_id,
            "pin",
            "ws:coder/retained.bin",
            retained_node,
            "retain deleted draft for audit",
        )
        assert retained_hash not in {row[0] for row in fs.gc_candidates(True)}
        assert retained_hash not in {
            row[0]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=True,
                agent_id="gc_agent",
            )
        }

        fs.unpin_gc_root(pin_id, agent_id="policy_agent")

        assert fs.gc_pins() == []
        pin_history = fs.gc_pins(active_only=False)
        assert [row[1] for row in pin_history] == ["pin", "unpin"]
        assert retained_hash in {row[0] for row in fs.gc_candidates(True)}

        collected = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=False,
                agent_id="gc_agent",
            )
        }
        assert collected[retained_hash] == "collected_file"
        assert fs.verify_integrity() == []

        try:
            fs.unpin_gc_root(pin_id, agent_id="policy_agent")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("inactive gc pin should not unpin twice")


def test_snapshot_namespace_publishes_manifest_ref_for_active_view():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:snapshot")

        a_node = ws.write("src/a.py", b"print('a')", [])
        b_node = ws.write("src/b.py", b"print('b')", [])
        ws.write("src/deleted.py", b"deleted", [])
        ws.delete("src/deleted.py")

        snapshot_node = fs.snapshot_namespace(
            "ws:coder",
            "resource:repo/snapshot@v1",
            "snapshot_agent",
            run_id="run:snapshot",
        )

        assert fs.get_ref("resource:repo/snapshot@v1")[1:4] == (
            snapshot_node,
            "resource",
            "published",
        )
        assert fs.manifest_children(snapshot_node) == [
            ("src/a.py", a_node),
            ("src/b.py", b_node),
        ]
        records = fs.manifest_child_records(snapshot_node)
        assert [record[2] for record in records] == ["snapshot_child", "snapshot_child"]

        snapshot_doc = json.loads(bytes(fs.read_node(snapshot_node)))
        assert snapshot_doc["schema"] == "anfs.manifest.v1"
        assert snapshot_doc["kind"] == "resource"
        assert [child["path"] for child in snapshot_doc["children"]] == [
            "src/a.py",
            "src/b.py",
        ]

        snapshot_events = fs.events(kind="snapshot_namespace", run_id="run:snapshot")
        assert len(snapshot_events) == 1
        event = fs.event(snapshot_events[0][1])
        assert event[1] == "snapshot_namespace"
        edge_roles = {(edge[0], edge[2], edge[3]) for edge in event[8]}
        assert ("input", "snapshot_child:0", "src/a.py") in edge_roles
        assert ("input", "snapshot_child:1", "src/b.py") in edge_roles
        assert ("output", "snapshot_manifest", "resource:repo/snapshot@v1") in edge_roles

        candidates_without_workspaces = {
            candidate[0] for candidate in fs.gc_candidates(False)
        }
        assert hashlib.sha256(b"print('a')").hexdigest() not in candidates_without_workspaces
        assert hashlib.sha256(b"print('b')").hexdigest() not in candidates_without_workspaces
        assert hashlib.sha256(b"deleted").hexdigest() in candidates_without_workspaces
        assert fs.verify_integrity() == []

        replay_ws = fs.checkout(
            workspace="ws:replay",
            agent_id="replay_agent",
            base="resource:repo/snapshot@v1",
        )
        _ = replay_ws
        assert fs.get_ref("ws:replay/src/a.py")[1] == a_node
        assert fs.get_ref("ws:replay/src/b.py")[1] == b_node
        try:
            fs.get_ref("ws:replay/src/deleted.py")
        except anfs_core.RefNotFoundError:
            pass
        else:
            raise AssertionError("snapshot checkout should exclude deleted refs")
        with sqlite3.connect(db_path) as conn:
            checkout_rows = conn.execute(
                """
                SELECT logical_path, source_ref, node_id
                FROM workspace_base_refs
                WHERE workspace_id = ?
                ORDER BY logical_path
                """,
                ("ws:replay",),
            ).fetchall()
        assert checkout_rows == [
            ("src/a.py", "resource:repo/snapshot@v1", a_node),
            ("src/b.py", "resource:repo/snapshot@v1", b_node),
        ]

        try:
            fs.snapshot_namespace("ws:missing", "resource:empty@v1", "snapshot_agent")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("empty namespace snapshot should be rejected")


def test_workspace_conflicts_use_checkout_base_snapshot():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        base_keep = importer_ws.write("keep.txt", b"keep", [])
        base_conflict = importer_ws.write("conflict.txt", b"old conflict", [])
        base_changed_only = importer_ws.write("base_changed_only.txt", b"old base", [])
        importer_ws.publish("keep.txt", "resource:repo/main@v1/keep.txt", "resource")
        importer_ws.publish(
            "conflict.txt",
            "resource:repo/main@v1/conflict.txt",
            "resource",
        )
        importer_ws.publish(
            "base_changed_only.txt",
            "resource:repo/main@v1/base_changed_only.txt",
            "resource",
        )

        coder_ws = fs.checkout(
            workspace="ws:coder",
            agent_id="coder_agent",
            base="resource:repo/main@v1",
        )
        workspace_conflict = coder_ws.write("conflict.txt", b"workspace conflict", [])

        current_conflict = importer_ws.write("current_conflict.txt", b"current conflict", [])
        current_base_only = importer_ws.write("current_base_only.txt", b"current base", [])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE refs
                SET node_id = ?, ref_version = ref_version + 1
                WHERE ref_name = ?
                """,
                (current_conflict, "resource:repo/main@v1/conflict.txt"),
            )
            conn.execute(
                """
                UPDATE refs
                SET node_id = ?, ref_version = ref_version + 1
                WHERE ref_name = ?
                """,
                (current_base_only, "resource:repo/main@v1/base_changed_only.txt"),
            )

        conflicts = {
            path: (checkout_node, current_node, workspace_node)
            for path, checkout_node, current_node, workspace_node in fs.workspace_conflicts(
                "resource:repo/main@v1",
                "ws:coder",
            )
        }

        assert conflicts == {
            "conflict.txt": (base_conflict, current_conflict, workspace_conflict)
        }
        assert fs.get_ref("ws:coder/keep.txt")[1] == base_keep
        assert fs.get_ref("ws:coder/base_changed_only.txt")[1] == base_changed_only


def test_merge_workspace_accepts_snapshot_manifest_base():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        keep_node = importer_ws.write("keep.txt", b"keep", [])
        modify_base = importer_ws.write("modify.txt", b"old", [])
        importer_ws.write("delete.txt", b"delete", [])
        importer_ws.publish("keep.txt", "resource:repo/main@v1/keep.txt", "resource")
        importer_ws.publish("modify.txt", "resource:repo/main@v1/modify.txt", "resource")
        importer_ws.publish("delete.txt", "resource:repo/main@v1/delete.txt", "resource")

        snapshot_node = fs.snapshot_namespace(
            "resource:repo/main@v1",
            "resource:repo/main@snapshot",
            "snapshot_agent",
        )
        coder_ws = fs.checkout(
            "ws:snapshot-coder",
            "coder_agent",
            "resource:repo/main@snapshot",
        )
        modify_workspace = coder_ws.write("modify.txt", b"new", [])
        add_workspace = coder_ws.write("add.txt", b"add", [])
        coder_ws.delete("delete.txt")

        diff = {
            path: (status, base_node, workspace_node)
            for path, status, base_node, workspace_node in fs.workspace_diff(
                "resource:repo/main@snapshot",
                "ws:snapshot-coder",
            )
        }
        assert diff["keep.txt"] == ("unchanged", keep_node, keep_node)
        assert diff["modify.txt"] == ("modified", modify_base, modify_workspace)
        assert diff["add.txt"] == ("added", None, add_workspace)
        assert diff["delete.txt"][0] == "deleted"
        assert fs.workspace_conflicts(
            "resource:repo/main@snapshot",
            "ws:snapshot-coder",
        ) == []

        merged = {
            path: (status, ref_name, node_id)
            for path, status, ref_name, node_id in fs.merge_workspace(
                "resource:repo/main@snapshot",
                "ws:snapshot-coder",
                "merge_agent",
            )
        }

        assert merged == {
            "add.txt": ("added", "resource:repo/main@v1/add.txt", add_workspace),
            "delete.txt": ("deleted", "resource:repo/main@v1/delete.txt", None),
            "modify.txt": ("modified", "resource:repo/main@v1/modify.txt", modify_workspace),
        }
        assert fs.get_ref("resource:repo/main@snapshot")[1] == snapshot_node
        assert fs.get_ref("resource:repo/main@v1/keep.txt")[1] == keep_node
        assert fs.get_ref("resource:repo/main@v1/modify.txt")[1] == modify_workspace
        assert fs.get_ref("resource:repo/main@v1/add.txt")[1] == add_workspace
        assert fs.get_ref("resource:repo/main@v1/delete.txt")[3] == "archived"

        with sqlite3.connect(db_path) as conn:
            output_paths = {
                row[0]
                for row in conn.execute(
                    """
                    SELECT logical_path
                    FROM event_edges
                    WHERE event_id IN (
                        SELECT event_id FROM events WHERE kind = 'merge_workspace'
                    )
                      AND direction = 'output'
                    """
                )
            }
        assert "resource:repo/main@v1/add.txt" in output_paths
        assert "resource:repo/main@v1/modify.txt" in output_paths
        assert "resource:repo/main@snapshot/add.txt" not in output_paths


def test_merge_workspace_from_snapshot_detects_current_base_conflicts():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        importer_ws.write("conflict.txt", b"old", [])
        importer_ws.publish("conflict.txt", "resource:repo/main@v1/conflict.txt", "resource")
        fs.snapshot_namespace(
            "resource:repo/main@v1",
            "resource:repo/main@snapshot",
            "snapshot_agent",
        )

        coder_ws = fs.checkout(
            "ws:snapshot-conflict",
            "coder_agent",
            "resource:repo/main@snapshot",
        )
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

        conflicts = {
            path: (checkout_node, current_node, workspace_node)
            for path, checkout_node, current_node, workspace_node in fs.workspace_conflicts(
                "resource:repo/main@snapshot",
                "ws:snapshot-conflict",
            )
        }
        assert conflicts["conflict.txt"][1] == current_base

        try:
            fs.merge_workspace(
                "resource:repo/main@snapshot",
                "ws:snapshot-conflict",
                "merge_agent",
            )
        except anfs_core.RefConflictError:
            pass
        else:
            raise AssertionError("snapshot merge should reject current-base conflicts")

        assert fs.get_ref("resource:repo/main@v1/conflict.txt")[1] == current_base
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM events WHERE kind = 'merge_workspace'"
                ).fetchone()[0]
                == 0
            )
            policy_payload = conn.execute(
                """
                SELECT payload_json
                FROM events
                WHERE kind = 'policy_decision'
                  AND json_extract(payload_json, '$.policy') = 'workspace_merge'
                """
            ).fetchone()[0]
        assert json.loads(policy_payload)["decision"] == "deny"
