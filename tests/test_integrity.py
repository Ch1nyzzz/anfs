"""verify_integrity invariants, corruption detection, repair and migration plans."""

import hashlib
import json
import os
import sqlite3
import tempfile

import anfs_core


def test_integrity_verifier_detects_checkout_base_snapshot_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        base_node = importer_ws.write("src/db.py", b"print('base')", [])
        importer_ws.publish(
            "src/db.py",
            "resource:repo/main@v1/src/db.py",
            "resource",
        )
        fs.checkout(
            workspace="ws:coder",
            agent_id="coder_agent",
            base="resource:repo/main@v1",
        )
        assert fs.verify_integrity() == []

        bad_event = "event:bad-workspace-base"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            checkout_event = conn.execute(
                """
                SELECT checkout_event_id
                FROM workspace_base_refs
                WHERE workspace_id = 'ws:coder'
                  AND base_ref = 'resource:repo/main@v1'
                  AND logical_path = 'src/db.py'
                """
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO workspace_base_refs
                    (workspace_id, base_ref, logical_path, source_ref, node_id, checkout_event_id, created_at)
                VALUES ('ws:bad-source', 'resource:repo/main@v1', 'src/db.py',
                        'resource:repo/main@v1/src/other.py', ?, ?, 0)
                """,
                (base_node, checkout_event),
            )
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'write', 'agent', NULL, NULL, 'ws:coder', NULL, 0)
                """,
                (bad_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event, next_seq),
            )
            conn.execute(
                """
                INSERT INTO workspace_base_refs
                    (workspace_id, base_ref, logical_path, source_ref, node_id, checkout_event_id, created_at)
                VALUES ('ws:bad', 'resource:repo/main@v1', 'src/db.py',
                        'resource:repo/main@v1/src/db.py', ?, ?, 0)
                """,
                (base_node, bad_event),
            )

        issues = fs.verify_integrity()
        assert any(
            "workspace_base_ref ws:bad-source/resource:repo/main@v1/src/db.py must have exactly one matching input base_source edge"
            in issue
            for issue in issues
        )
        assert any(
            f"workspace_base_ref ws:bad/resource:repo/main@v1/src/db.py must reference checkout event {bad_event}; found write"
            in issue
            for issue in issues
        )
        assert any(
            "workspace_base_ref ws:bad/resource:repo/main@v1/src/db.py must have exactly one matching output workspace_view edge"
            in issue
            for issue in issues
        )


def test_integrity_verifier_clean_and_missing_file_blob():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        assert fs.verify_integrity() == []
        with sqlite3.connect(db_path) as conn:
            migrations = conn.execute(
                "SELECT version, name FROM schema_migrations ORDER BY version"
            ).fetchall()
        assert migrations == [(1, "anfs_kernel_schema_v1")]

        ws.write("large.bin", b"z" * (70 * 1024), [])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            storage_uri = conn.execute(
                "SELECT storage_uri FROM blobs WHERE storage_kind = 'file'"
            ).fetchone()[0]

        os.remove(storage_uri)
        issues = fs.verify_integrity()
        assert any("unreadable" in issue for issue in issues)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (999, "future_schema", 0),
            )
        issues = fs.verify_integrity()
        assert any("future schema version 999" in issue for issue in issues)


def test_schema_status_and_strict_future_schema_guard():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        status = fs.schema_status()
        assert len(status) == 1
        assert status[0][0] == 1
        assert status[0][1] == "anfs_kernel_schema_v1"
        assert status[0][2] > 0
        assert status[0][3] == "current"
        assert fs.schema_migration_plan() == [
            (1, "anfs_kernel_schema_v1", "current", "none")
        ]
        assert fs.apply_schema_migrations() == (0, 0, True)
        assert fs.apply_schema_migrations(dry_run=False) == (0, 0, False)
        fs.require_schema_current()

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (1,))

        assert fs.schema_status() == [
            (1, "anfs_kernel_schema_v1", 0, "missing_current")
        ]
        assert fs.schema_migration_plan() == [
            (1, "anfs_kernel_schema_v1", "missing_current", "record_current")
        ]
        assert fs.apply_schema_migrations() == (1, 0, True)
        assert fs.schema_status() == [
            (1, "anfs_kernel_schema_v1", 0, "missing_current")
        ]
        assert fs.apply_schema_migrations(dry_run=False) == (1, 1, False)
        status = fs.schema_status()
        assert len(status) == 1
        assert status[0][0] == 1
        assert status[0][1] == "anfs_kernel_schema_v1"
        assert status[0][2] > 0
        assert status[0][3] == "current"
        fs.require_schema_current()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE schema_migrations SET name = ? WHERE version = ?",
                ("wrong_current_name", 1),
            )

        assert (
            1,
            "wrong_current_name",
            "current_name_mismatch",
            "manual_repair_required",
        ) in fs.schema_migration_plan()
        try:
            fs.apply_schema_migrations(dry_run=False)
        except anfs_core.StorageCorruptionError as exc:
            assert "schema migration plan is not safely executable" in str(exc)
            assert "1:wrong_current_name:current_name_mismatch" in str(exc)
        else:
            raise AssertionError("current schema name mismatch must not be auto-repaired")

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE schema_migrations SET name = ? WHERE version = ?",
                ("anfs_kernel_schema_v1", 1),
            )
        fs.require_schema_current()

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (999, "future_schema", 0),
            )

        status = fs.schema_status()
        assert (999, "future_schema", 0, "future_unsupported") in status
        assert (
            999,
            "future_schema",
            "future_unsupported",
            "unsupported_future",
        ) in fs.schema_migration_plan()
        try:
            fs.apply_schema_migrations()
        except anfs_core.StorageCorruptionError as exc:
            assert "schema migration plan is not safely executable" in str(exc)
            assert "999:future_schema:future_unsupported" in str(exc)
        else:
            raise AssertionError("future schema must not have an executable migration plan")
        try:
            fs.require_schema_current()
        except anfs_core.StorageCorruptionError as exc:
            assert "schema guard failed" in str(exc)
            assert "999:future_schema:future_unsupported" in str(exc)
        else:
            raise AssertionError("future schema must fail explicit schema guard")
        assert any("future schema version 999" in issue for issue in fs.verify_integrity())

        try:
            anfs_core.AnfsEngine(db_path, objs_dir)
        except anfs_core.StorageCorruptionError as exc:
            assert "newer than supported kernel schema version" in str(exc)
        else:
            raise AssertionError("opening a future schema database must fail")

    with tempfile.TemporaryDirectory() as tmpdir:
        future_db_path = os.path.join(tmpdir, "future.db")
        future_objs_dir = os.path.join(tmpdir, "objs")
        with sqlite3.connect(future_db_path) as conn:
            conn.execute(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at INTEGER NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)",
                (999, "future_schema", 0),
            )

        try:
            anfs_core.AnfsEngine(future_db_path, future_objs_dir)
        except anfs_core.StorageCorruptionError:
            pass
        else:
            raise AssertionError("future-only schema database must fail before init")

        with sqlite3.connect(future_db_path) as conn:
            tables = {
                name
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        assert tables == {"schema_migrations"}


def test_integrity_verifier_detects_blob_size_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        assert fs.verify_integrity() == []

        content = b"small"
        digest = hashlib.sha256(content).hexdigest()
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO blobs
                    (hash, size, storage_kind, storage_uri, inline_content)
                VALUES (?, ?, 'inline', NULL, ?)
                """,
                (digest, len(content) + 1, content),
            )

        issues = fs.verify_integrity()
        assert any("inline blob size mismatch" in issue for issue in issues)


def test_integrity_verifier_detects_missing_fts_row():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("small.txt", b"small searchable text", [])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))

        issues = fs.verify_integrity()
        assert any(
            f"node {node_id} with media_type text/plain is missing node_fts row"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_corrupt_fts_rows():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("small.txt", b"small searchable text", [])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))
            conn.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?, ?)",
                (node_id, "stale searchable text"),
            )
            conn.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?, ?)",
                (node_id, "small searchable text"),
            )
            conn.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?, ?)",
                ("node:missing", "orphan text"),
            )

        issues = fs.verify_integrity()
        assert any(
            f"node {node_id} with media_type text/plain has stale node_fts body"
            in issue
            for issue in issues
        )
        assert any(
            f"node_fts has 2 rows for node {node_id}" in issue for issue in issues
        )
        assert any(
            "node_fts row references missing node node:missing" in issue
            for issue in issues
        )


def test_rebuild_derived_indexes_repairs_fts_and_clears_chunk_cache():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        text_node = ws.write("small.txt", b"small searchable text", [])
        large_node = ws.write("large.bin", b"0123456789abcdef" * 8192, [])
        chunk_size = 32768
        cached_chunks = fs.cache_node_chunks(large_node, chunk_size)
        assert fs.cached_node_chunks(large_node, chunk_size) == cached_chunks
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM node_fts WHERE node_id = ?", (text_node,))
            conn.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?, ?)",
                ("node:missing", "orphan text"),
            )
            conn.execute(
                """
                UPDATE node_chunk_index
                SET sha256 = '0000000000000000000000000000000000000000000000000000000000000000'
                WHERE node_id = ? AND chunk_size = ? AND chunk_index = 0
                """,
                (large_node, chunk_size),
            )

        issues = fs.verify_integrity()
        assert any(
            f"node {text_node} with media_type text/plain is missing node_fts row"
            in issue
            for issue in issues
        )
        assert any(
            "node_fts row references missing node node:missing" in issue
            for issue in issues
        )
        assert any("sha256 mismatch" in issue for issue in issues)

        fts_rows, chunk_indexes_cleared = fs.rebuild_derived_indexes()
        assert fts_rows >= 1
        assert chunk_indexes_cleared == 1
        assert fs.verify_integrity() == []
        assert ws.search("small searchable", scope="draft")
        assert fs.cached_node_chunks(large_node, chunk_size) == []
        assert fs.cache_node_chunks(large_node, chunk_size) == cached_chunks
        assert fs.verify_integrity() == []


def test_repair_plan_classifies_derived_and_canonical_issues_without_mutating():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        node_id = ws.write("small.txt", b"small searchable text", [])
        assert fs.repair_plan() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute("DELETE FROM node_fts WHERE node_id = ?", (node_id,))

        plan = fs.repair_plan()
        assert fs.verify_integrity() != []
        assert any(
            issue.startswith(f"node {node_id} with media_type text/plain is missing node_fts row")
            and classification == "derived_index"
            and "rebuild_derived_indexes" in action
            and mutable_scope == "derived"
            for issue, classification, action, mutable_scope in plan
        )
        assert fs.verify_integrity() != [], "repair_plan must not mutate derived rows"
        fs.rebuild_derived_indexes()
        assert fs.repair_plan() == []

        with sqlite3.connect(db_path) as conn:
            bad_event_id = "event:bad-repair-plan-ref"
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'write', 'coder_agent', NULL, NULL, 'ws:coder', 'small.txt', 0)
                """,
                (bad_event_id,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event_id, next_seq),
            )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES ('ws:coder/small.txt', ?, NULL, 'node:missing', NULL, 'draft')
                """,
                (bad_event_id,),
            )

        plan = fs.repair_plan()
        assert any(
            "references missing node" in issue
            and classification == "canonical_audit"
            and mutable_scope == "canonical"
            and "manual canonical repair plan required" in action
            for issue, classification, action, mutable_scope in plan
        )


def test_integrity_verifier_detects_ref_audit_divergence():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        ws.write("patch.diff", b"diff", [])
        ws.publish("patch.diff", "artifact:patch@v1")
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE refs SET state = 'approved' WHERE ref_name = ?",
                ("artifact:patch@v1",),
            )

        issues = fs.verify_integrity()
        assert any(
            "ref artifact:patch@v1 current state diverges from latest audit event"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_ref_audit_missing_nodes():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("patch.diff", b"diff", [])
        assert fs.verify_integrity() == []

        bad_event = "event:bad-ref-audit-node"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'publish', 'agent', NULL, NULL, 'ws:coder', NULL, 0)
                """,
                (bad_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event, next_seq),
            )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES ('artifact:bad-ref-audit@v1', ?, 'node:missing-old', 'node:missing-new', 'draft', 'published')
                """,
                (bad_event,),
            )

        issues = fs.verify_integrity()
        assert any(
            f"ref_event {bad_event} for artifact:bad-ref-audit@v1 old_node_id node:missing-old references missing node"
            in issue
            for issue in issues
        )
        assert any(
            f"ref_event {bad_event} for artifact:bad-ref-audit@v1 new_node_id node:missing-new references missing node"
            in issue
            for issue in issues
        )
        assert bytes(fs.read_node(node_id)) == b"diff"


def test_integrity_verifier_detects_ref_audit_event_edge_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("patch.diff", b"diff", [])
        assert fs.verify_integrity() == []

        bad_event = "event:bad-ref-audit-edge"
        bad_ref = "artifact:bad-ref-audit-edge@v1"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'publish', 'agent', NULL, NULL, 'ws:coder', ?, 0)
                """,
                (bad_event, bad_ref),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event, next_seq),
            )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'workspace_node', 'patch.diff')
                """,
                (bad_event, node_id),
            )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES (?, ?, NULL, ?, NULL, 'published')
                """,
                (bad_ref, bad_event, node_id),
            )

        issues = fs.verify_integrity()
        assert any(
            f"ref_event {bad_event} for {bad_ref} must have exactly one matching output published_ref edge for node {node_id}; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_ref_audit_chain_discontinuity():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        first_node = ws.write("patch.diff", b"first", [])
        second_node = ws.write("patch.diff", b"second", [])
        assert fs.verify_integrity() == []

        bad_first_event = "event:bad-ref-first"
        bad_second_event = "event:bad-ref-second"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            for event_id, seq in [
                (bad_first_event, next_seq),
                (bad_second_event, next_seq + 1),
            ]:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, 'write', 'agent', NULL, NULL, 'ws:coder', NULL, 0)
                    """,
                    (event_id,),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES ('artifact:bad-chain@v1', ?, ?, ?, 'draft', 'published')
                """,
                (bad_first_event, first_node, first_node),
            )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES ('artifact:bad-chain@v1', ?, ?, ?, 'draft', 'published')
                """,
                (bad_second_event, second_node, second_node),
            )

        issues = fs.verify_integrity()
        assert any(
            f"ref_event {bad_first_event} for artifact:bad-chain@v1 is first audit row"
            in issue
            for issue in issues
        )
        assert any(
            f"ref_event {bad_second_event} for artifact:bad-chain@v1 old=(Some(\"{second_node}\"), Some(\"draft\")) does not match previous new=(Some(\"{first_node}\"), Some(\"published\"))"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_gc_pin_event_linkage_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("retained.bin", b"retained" * (10 * 1024), [])
        ws.publish("retained.bin", "artifact:retained@v1")
        assert fs.verify_integrity() == []

        wrong_kind_event = "event:bad-gc-pin-kind"
        missing_edge_event = "event:bad-gc-pin-edge"
        orphan_event = "event:bad-gc-pin-orphan"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (wrong_kind_event, "write", next_seq),
                (missing_edge_event, "gc_pin", next_seq + 1),
                (orphan_event, "gc_pin", next_seq + 2),
            ]
            for event_id, kind, seq in malformed_events:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', NULL, NULL, NULL, NULL, 0)
                    """,
                    (event_id, kind),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
            conn.execute(
                """
                INSERT INTO gc_pin_events
                    (pin_id, event_id, action, ref_name, node_id, reason, created_at)
                VALUES ('pin:wrong-kind', ?, 'pin', 'artifact:retained@v1', ?, NULL, 0)
                """,
                (wrong_kind_event, node_id),
            )
            conn.execute(
                """
                INSERT INTO gc_pin_events
                    (pin_id, event_id, action, ref_name, node_id, reason, created_at)
                VALUES ('pin:missing-edge', ?, 'pin', 'artifact:retained@v1', ?, NULL, 0)
                """,
                (missing_edge_event, node_id),
            )

        issues = fs.verify_integrity()
        assert any(
            f"gc_pin_event {wrong_kind_event} for pin:wrong-kind action pin must reference event kind gc_pin; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"gc_pin_event {wrong_kind_event} for pin:wrong-kind action pin must have exactly one input gc_pin_root edge matching ref artifact:retained@v1 and node {node_id}; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"gc_pin_event {missing_edge_event} for pin:missing-edge action pin must have exactly one input gc_pin_root edge matching ref artifact:retained@v1 and node {node_id}; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"gc_pin event {orphan_event} must have exactly one gc_pin_events audit row; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_corrupt_embedding_projection():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        node_id = writer.write("contract.md", b"contract renewal terms", [])
        fs.set_node_embedding(node_id, "toy-embedding", [1.0, 0.0])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE node_embeddings
                SET norm = 9.0
                WHERE node_id = ? AND model = ?
                """,
                (node_id, "toy-embedding"),
            )

        issues = fs.verify_integrity()
        assert any(
            f"node_embeddings row for {node_id} model toy-embedding has stale norm"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_corrupt_chunk_embedding_projection():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        node_id = writer.write("large.md", b"alpha000beta1111", [])
        fs.cache_node_chunks(node_id, 8)
        fs.set_node_chunk_embedding(node_id, 8, 0, "toy-embedding", [1.0, 0.0])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE node_chunk_embeddings
                SET norm = 9.0
                WHERE node_id = ? AND chunk_size = ? AND chunk_index = ? AND model = ?
                """,
                (node_id, 8, 0, "toy-embedding"),
            )

        issues = fs.verify_integrity()
        assert any(
            f"node_chunk_embeddings row for {node_id} chunk_size 8 chunk 0 model toy-embedding has stale norm"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_manifest_child_metadata_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        child_node = ws.write("src/db.py", b"print('db fix')", [])
        assert fs.verify_integrity() == []

        manifest = {
            "schema": "anfs.manifest.v1",
            "kind": "artifact",
            "children": [
                {
                    "path": "src/db.py",
                    "node_id": child_node,
                    "role": "child",
                    "media_type": "application/octet-stream",
                    "digest": "0" * 64,
                    "size": 999,
                }
            ],
        }
        manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode()
        manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
        bad_manifest_node = "node:bad-manifest"
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO blobs
                    (hash, size, storage_kind, storage_uri, inline_content)
                VALUES (?, ?, 'inline', NULL, ?)
                """,
                (manifest_hash, len(manifest_bytes), manifest_bytes),
            )
            conn.execute(
                """
                INSERT INTO nodes
                    (node_id, blob_hash, kind, media_type, metadata_json, created_at)
                VALUES (?, ?, 'artifact', ?, '{}', 0)
                """,
                (
                    bad_manifest_node,
                    manifest_hash,
                    "application/vnd.anfs.manifest+json",
                ),
            )
            conn.execute(
                "INSERT INTO node_fts (node_id, body) VALUES (?, ?)",
                (bad_manifest_node, manifest_bytes.decode()),
            )

        issues = fs.verify_integrity()
        assert any(
            f"manifest node {bad_manifest_node} child src/db.py digest mismatch"
            in issue
            for issue in issues
        )
        assert any(
            f"manifest node {bad_manifest_node} child src/db.py media_type mismatch"
            in issue
            for issue in issues
        )
        assert any(
            f"manifest node {bad_manifest_node} child src/db.py size mismatch"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_manifest_event_child_edge_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        ws.write("src/a.py", b"print('a')", [])
        ws.write("src/b.py", b"print('b')", [])
        manifest_node = ws.publish_manifest(
            ["src/a.py", "src/b.py"],
            "artifact:bundle@v1",
        )
        snapshot_node = fs.snapshot_namespace(
            "ws:coder",
            "resource:snapshot@v1",
            "snapshot_agent",
        )
        assert fs.verify_integrity() == []

        bad_manifest_event = "event:bad-manifest-edges"
        bad_snapshot_event = "event:bad-snapshot-edges"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (bad_manifest_event, "publish_manifest", manifest_node, "manifest", next_seq),
                (
                    bad_snapshot_event,
                    "snapshot_namespace",
                    snapshot_node,
                    "snapshot_manifest",
                    next_seq + 1,
                ),
            ]
            for event_id, kind, node_id, output_role, seq in malformed_events:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', NULL, NULL, 'ws:coder', NULL, 0)
                    """,
                    (event_id, kind),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
                conn.execute(
                    """
                    INSERT INTO event_edges
                        (event_id, direction, node_id, role, logical_path)
                    VALUES (?, 'output', ?, ?, 'artifact:bad@v1')
                    """,
                    (event_id, node_id, output_role),
                )

        issues = fs.verify_integrity()
        assert any(
            f"publish_manifest event {bad_manifest_event} child edge count 0 does not match manifest child count 2"
            in issue
            for issue in issues
        )
        assert any(
            f"publish_manifest event {bad_manifest_event} manifest child src/a.py"
            in issue
            and "must have exactly one matching input manifest_child edge; found 0" in issue
            for issue in issues
        )
        assert any(
            f"snapshot_namespace event {bad_snapshot_event} child edge count 0 does not match manifest child count 2"
            in issue
            for issue in issues
        )
        assert any(
            f"snapshot_namespace event {bad_snapshot_event} manifest child src/b.py"
            in issue
            and "must have exactly one matching input snapshot_child edge; found 0" in issue
            for issue in issues
        )


def test_integrity_verifier_detects_invalid_event_edge_direction():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            event_id = conn.execute(
                "SELECT event_id FROM events WHERE kind = 'write'"
            ).fetchone()[0]
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'sideways', ?, 'bad_edge', ?)
                """,
                (event_id, node_id, "src/db.py"),
            )

        issues = fs.verify_integrity()
        assert any(
            f"event_edge {event_id}/{node_id}/bad_edge has invalid direction sideways"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_event_edge_shape_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        bad_event_id = "event:bad-publish"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'publish', 'coder_agent', NULL, NULL, 'ws:coder', 'artifact:bad@v1', 0)
                """,
                (bad_event_id,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event_id, next_seq),
            )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'workspace_node', 'src/db.py')
                """,
                (bad_event_id, node_id),
            )

        issues = fs.verify_integrity()
        assert any(
            f"publish event {bad_event_id} must have exactly one output published_ref edge; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_access_event_edge_shape_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        ws.write("src/db.py", b"print('v1')", [])
        ws.publish("src/db.py", "artifact:patch@v1")
        assert fs.verify_integrity() == []

        bad_consume = "event:bad-consume"
        bad_read = "event:bad-read"
        bad_search = "event:bad-search"
        bad_answer = "event:bad-answer"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (
                    bad_consume,
                    "consume",
                    '{"ref_name":"artifact:patch@v1","result_count":0}',
                    next_seq,
                ),
                (
                    bad_read,
                    "read_ref",
                    '{"ref_name":"artifact:patch@v1","result_count":0}',
                    next_seq + 1,
                ),
                (
                    bad_search,
                    "search",
                    '{"query":"print","scope":"published","result_count":1}',
                    next_seq + 2,
                ),
                (
                    bad_answer,
                    "answer",
                    json.dumps(
                        {
                            "logical_path": "answer.md",
                            "citation_count": 1,
                            "retrieval_event_ids": ["event:missing-retrieval"],
                            "citations": [
                                {
                                    "ref_name": "artifact:patch@v1",
                                    "node_id": fs.get_ref("artifact:patch@v1")[1],
                                }
                            ],
                        }
                    ),
                    next_seq + 3,
                ),
            ]
            for event_id, kind, payload, seq in malformed_events:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', NULL, NULL, 'ws:coder', ?, 0)
                    """,
                    (event_id, kind, payload),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )

        issues = fs.verify_integrity()
        assert any(
            f"consume event {bad_consume} must have exactly one input consumed edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"read_ref event {bad_read} must have exactly one input read_ref edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"search event {bad_search} result_count 1 does not match search_result edge count 0"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_answer} citation_count 1 does not match answer_citation edge count 0"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_answer} must have exactly one output result edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_answer} references missing retrieval event event:missing-retrieval"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_answer} citation artifact:patch@v1/{fs.get_ref('artifact:patch@v1')[1]} is not covered by retrieval events"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_lifecycle_event_edge_shape_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        bad_manifest = "event:bad-manifest"
        bad_delete = "event:bad-delete"
        bad_reject = "event:bad-reject"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (bad_manifest, "publish_manifest", next_seq),
                (bad_delete, "delete_ref", next_seq + 1),
                (bad_reject, "reject", next_seq + 2),
            ]
            for event_id, kind, seq in malformed_events:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', NULL, NULL, 'ws:coder', NULL, 0)
                    """,
                    (event_id, kind),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'manifest_child:0', 'src/db.py')
                """,
                (bad_manifest, node_id),
            )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'target', 'artifact:patch@v1')
                """,
                (bad_reject, node_id),
            )

        issues = fs.verify_integrity()
        assert any(
            f"publish_manifest event {bad_manifest} must have exactly one output manifest edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"delete_ref event {bad_delete} must have at least one input deleted_ref edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"reject event {bad_reject} must have exactly one output rejected_target edge; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_workspace_event_edge_shape_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        bad_checkout = "event:bad-checkout"
        bad_archive = "event:bad-archive"
        bad_snapshot = "event:bad-snapshot"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (bad_checkout, "checkout", "resource:repo/main@v1", next_seq),
                (bad_archive, "archive_ref", "artifact:patch@v1", next_seq + 1),
                (
                    bad_snapshot,
                    "snapshot_namespace",
                    '{"prefix":"resource:repo/main@v1","snapshot_ref":"resource:snapshot@v1","child_count":1}',
                    next_seq + 2,
                ),
            ]
            for event_id, kind, payload, seq in malformed_events:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', NULL, NULL, 'ws:coder', ?, 0)
                    """,
                    (event_id, kind, payload),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'snapshot_child:0', 'src/db.py')
                """,
                (bad_snapshot, node_id),
            )

        issues = fs.verify_integrity()
        assert any(
            f"checkout event {bad_checkout} with base must have at least one input base_source edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"archive_ref event {bad_archive} must have exactly one input archived_ref edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"snapshot_namespace event {bad_snapshot} must have exactly one output snapshot_manifest edge; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_merge_ref_audit_edge_linkage_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        bad_merge = "event:bad-merge-ref-link"
        audited_ref = "resource:repo/main@v1/src/db.py"
        wrong_ref = "resource:repo/main@v1/other.py"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'merge_workspace', 'agent', NULL, NULL, 'ws:coder', 'resource:repo/main@v1', 0)
                """,
                (bad_merge,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_merge, next_seq),
            )
            conn.execute(
                """
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES (?, ?, NULL, ?, NULL, 'published')
                """,
                (audited_ref, bad_merge, node_id),
            )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'merge_input:0', 'src/db.py')
                """,
                (bad_merge, node_id),
            )
            conn.execute(
                """
                INSERT INTO event_edges
                    (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'output', ?, 'merge_output:0', ?)
                """,
                (bad_merge, node_id, wrong_ref),
            )

        issues = fs.verify_integrity()
        assert any(
            f"merge_workspace ref_event {bad_merge} for {audited_ref} must have exactly one matching output merge_output edge for node {node_id}; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_run_audit_divergence():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.start_run(
            "run:coder",
            agent_id="coder_agent",
            workspace_id="ws:coder",
            metadata_json='{"task":"fix-db"}',
        )
        fs.finish_run(
            "run:coder",
            "succeeded",
            metadata_json='{"task":"fix-db","status":"done"}',
        )
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                "UPDATE runs SET state = 'failed' WHERE run_id = ?",
                ("run:coder",),
            )

        issues = fs.verify_integrity()
        assert any(
            "run run:coder current state diverges from latest audit event" in issue
            for issue in issues
        )


def test_integrity_verifier_detects_run_event_linkage_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.start_run(
            "run:coder",
            agent_id="coder_agent",
            workspace_id="ws:coder",
            metadata_json='{"task":"fix-db"}',
        )
        assert fs.verify_integrity() == []

        wrong_kind_event = "event:bad-run-kind"
        orphan_event = "event:bad-run-orphan"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'write', 'agent', 'run:other', NULL, NULL, '{"run_id":"run:other","state":"active"}', 0)
                """,
                (wrong_kind_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (wrong_kind_event, next_seq),
            )
            conn.execute(
                """
                INSERT INTO run_events
                    (run_id, event_id, old_state, new_state, old_metadata_json, new_metadata_json)
                VALUES ('run:coder', ?, NULL, 'active', NULL, NULL)
                """,
                (wrong_kind_event,),
            )
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'run_start', 'agent', 'run:coder', NULL, NULL, '{"run_id":"run:coder","state":"active"}', 0)
                """,
                (orphan_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (orphan_event, next_seq + 1),
            )

        issues = fs.verify_integrity()
        assert any(
            f"run_event {wrong_kind_event} for run:coder transition None -> active must reference event kind run_start; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"run_event {wrong_kind_event} for run:coder must reference event with matching run_id; found run:other"
            in issue
            for issue in issues
        )
        assert any(
            f"run_event {wrong_kind_event} for run:coder has payload run_id mismatch"
            in issue
            for issue in issues
        )
        assert any(
            f"run_start event {orphan_event} must have exactly one run_events audit row; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_run_audit_chain_discontinuity():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.start_run(
            "run:coder",
            agent_id="coder_agent",
            workspace_id="ws:coder",
            metadata_json='{"task":"fix-db"}',
        )
        assert fs.verify_integrity() == []

        bad_first_event = "event:bad-run-first"
        bad_second_event = "event:bad-run-second"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            for event_id, kind, payload_state, seq in [
                (bad_first_event, "run_start", "active", next_seq),
                (bad_second_event, "run_finish", "succeeded", next_seq + 1),
            ]:
                conn.execute(
                    """
                    INSERT INTO events
                        (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                    VALUES (?, ?, 'agent', 'run:bad-chain', NULL, NULL, ?, 0)
                    """,
                    (
                        event_id,
                        kind,
                        json.dumps({"run_id": "run:bad-chain", "state": payload_state}),
                    ),
                )
                conn.execute(
                    "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                    (event_id, seq),
                )
            conn.execute(
                """
                INSERT INTO runs
                    (run_id, agent_id, workspace_id, state, metadata_json, created_at, updated_at, ended_at)
                VALUES ('run:bad-chain', 'agent', NULL, 'succeeded', '{"final":true}', 0, 0, 0)
                """
            )
            conn.execute(
                """
                INSERT INTO run_events
                    (run_id, event_id, old_state, new_state, old_metadata_json, new_metadata_json)
                VALUES ('run:bad-chain', ?, 'active', 'active', '{"old":true}', '{"task":"start"}')
                """,
                (bad_first_event,),
            )
            conn.execute(
                """
                INSERT INTO run_events
                    (run_id, event_id, old_state, new_state, old_metadata_json, new_metadata_json)
                VALUES ('run:bad-chain', ?, 'active', 'succeeded', '{"wrong":true}', '{"final":true}')
                """,
                (bad_second_event,),
            )

        issues = fs.verify_integrity()
        assert any(
            f"run_event {bad_first_event} for run:bad-chain is first audit row"
            in issue
            for issue in issues
        )
        assert any(
            f"run_event {bad_second_event} for run:bad-chain old=(Some(\"active\"), Some(\"{{\\\"wrong\\\":true}}\")) does not match previous new=(Some(\"active\"), Some(\"{{\\\"task\\\":\\\"start\\\"}}\"))"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_worktree_commit_event_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        node_id = ws.write("app.py", b"print('v1')\n", [])
        assert fs.verify_integrity() == []

        bad_event = "event:bad-worktree-commit"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'worktree_commit', 'coder_agent', NULL, NULL, 'ws:coder', ?, 0)
                """,
                (
                    bad_event,
                    json.dumps(
                        {
                            "workspace": "ws:coder",
                            "result_count": 2,
                            "changed_count": 1,
                            "edge_count": 1,
                            "results": [
                                {
                                    "path": "app.py",
                                    "status": "modified",
                                    "old_node_id": node_id,
                                    "new_node_id": node_id,
                                }
                            ],
                        }
                    ),
                ),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event, next_seq),
            )

        issues = fs.verify_integrity()
        assert any(
            f"worktree_commit event {bad_event} result_count 2 does not match results length 1"
            in issue
            for issue in issues
        )
        assert any(
            f"worktree_commit event {bad_event} edge_count 1 does not match worktree edge count 0"
            in issue
            for issue in issues
        )
        assert any(
            f"worktree_commit event {bad_event} changed_count 1 does not match changed worktree edge count 0"
            in issue
            for issue in issues
        )
