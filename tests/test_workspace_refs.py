"""Workspace writes, checkout/fork/merge, ref lineage, events, and concurrency."""

import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile
import time

import anfs_core

from _helpers import _drain_queue_nowait, _collect_queue_results, _concurrent_event_writer, _concurrent_stale_approval_worker


def test_ref_expected_version_guards_lifecycle_decisions():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")

        coder_ws.write("src/db.py", b"print('bad')", [])
        coder_ws.publish("src/db.py", "artifact:patch@bad")
        published_version = fs.get_ref("artifact:patch@bad")[4]

        fs.reject_ref(
            "artifact:patch@bad",
            "reviewer_agent",
            expected_version=published_version,
        )
        rejected_version = fs.get_ref("artifact:patch@bad")[4]
        assert rejected_version == published_version + 1

        try:
            fs.archive_ref(
                "artifact:patch@bad",
                "retention_agent",
                expected_version=published_version,
            )
        except anfs_core.RefConflictError as exc:
            assert "version mismatch" in str(exc)
        else:
            raise AssertionError("stale archive decision should raise RefConflictError")

        assert fs.get_ref("artifact:patch@bad")[3] == "rejected"
        assert [row[2] for row in fs.ref_history("artifact:patch@bad")] == [
            "publish",
            "reject",
        ]

        fs.archive_ref(
            "artifact:patch@bad",
            "retention_agent",
            expected_version=rejected_version,
        )
        assert fs.get_ref("artifact:patch@bad")[3] == "archived"


def test_blob_dedup_file_storage_and_readback():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        content = b"x" * (70 * 1024)
        node_a = ws.write("large-a.bin", content, [])
        node_b = ws.write("large-b.bin", content, [])

        assert bytes(fs.read_node(node_a)) == content
        assert bytes(fs.read_node(node_b)) == content

        with sqlite3.connect(db_path) as conn:
            blob_count = conn.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
            storage_kind = conn.execute(
                "SELECT storage_kind FROM blobs LIMIT 1"
            ).fetchone()[0]

        assert blob_count == 1
        assert storage_kind == "file"


def test_node_range_reads_and_chunk_index_for_inline_and_file_blobs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        inline_node = ws.write("small.txt", b"abcdef", [])
        assert bytes(fs.read_node_range(inline_node, 1, 3)) == b"bcd"
        assert bytes(fs.read_node_range(inline_node, 6, 10)) == b""
        assert fs.node_chunks(inline_node, 2) == [
            (0, 0, 2, hashlib.sha256(b"ab").hexdigest()),
            (1, 2, 2, hashlib.sha256(b"cd").hexdigest()),
            (2, 4, 2, hashlib.sha256(b"ef").hexdigest()),
        ]

        large = (b"0123456789abcdef" * 5000) + b"tail"
        large_node = ws.write("large.bin", large, [])
        with sqlite3.connect(db_path) as conn:
            storage_kind = conn.execute(
                """
                SELECT b.storage_kind
                FROM nodes n
                JOIN blobs b ON b.hash = n.blob_hash
                WHERE n.node_id = ?
                """,
                (large_node,),
            ).fetchone()[0]
        assert storage_kind == "file"
        assert bytes(fs.read_node_range(large_node, 10, 20)) == large[10:30]
        assert bytes(fs.read_node_range(large_node, len(large) - 2, 20)) == large[-2:]
        chunks = fs.node_chunks(large_node, 32768)
        assert chunks[0] == (
            0,
            0,
            32768,
            hashlib.sha256(large[:32768]).hexdigest(),
        )
        last_index, last_offset, last_size, last_hash = chunks[-1]
        assert last_index == len(chunks) - 1
        assert last_offset + last_size == len(large)
        assert last_hash == hashlib.sha256(large[last_offset:]).hexdigest()

        try:
            fs.read_node_range(large_node, -1, 1)
            assert False, "negative node range offset should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.read_node_range(large_node, 0, 0)
            assert False, "non-positive node range length should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.node_chunks(large_node, 0)
            assert False, "non-positive chunk size should be rejected"
        except anfs_core.PolicyDeniedError:
            pass


def test_checkout_projects_base_refs_copy_on_write():
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

        coder_ws = fs.checkout(
            workspace="ws:coder",
            agent_id="coder_agent",
            base="resource:repo/main@v1",
        )
        checked_out = fs.get_ref("ws:coder/src/db.py")
        assert checked_out[1] == base_node
        assert bytes(fs.read_node(checked_out[1])) == b"print('base')"

        changed_node = coder_ws.write("src/db.py", b"print('changed')", [])
        assert changed_node != base_node
        assert fs.get_ref("ws:coder/src/db.py")[1] == changed_node
        assert fs.get_ref("resource:repo/main@v1/src/db.py")[1] == base_node

        reviewer_ws = fs.checkout(
            workspace="ws:reviewer",
            agent_id="reviewer_agent",
            base="resource:repo/main@v1",
        )
        _ = reviewer_ws
        assert fs.get_ref("ws:reviewer/src/db.py")[1] == base_node

        with sqlite3.connect(db_path) as conn:
            checkout_edges = conn.execute(
                """
                SELECT COUNT(*)
                FROM event_edges ee
                JOIN events e ON e.event_id = ee.event_id
                WHERE e.kind = 'checkout'
                """
            ).fetchone()[0]
            assert checkout_edges >= 4


def test_workspace_branch_names_are_validated_at_public_boundaries():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        alpha = fs.open_workspace("ws:alpha-1", "alpha_agent")
        alpha.write("src/a.md", b"alpha", [])

        invalid_names = [
            "",
            "coder",
            "resource:repo",
            "ws:",
            "ws:/alpha",
            "ws:alpha/beta",
            "ws:alpha beta",
            "ws:.alpha",
            "ws:alpha_",
        ]
        for name in invalid_names:
            try:
                fs.open_workspace(name, "bad_agent")
                assert False, f"open_workspace should reject {name!r}"
            except anfs_core.PolicyDeniedError:
                pass

        try:
            fs.checkout(
                "ws:bad/name",
                "bad_agent",
                tool_call_id="tc_bad_checkout_branch",
            )
            assert False, "checkout should reject unsafe workspace names"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe workspace name ws:bad/name" in str(exc)
        assert fs.events(kind="checkout", tool_call_id="tc_bad_checkout_branch") == []

        try:
            fs.materialize_workspace("ws:bad/name", worktree_dir)
            assert False, "worktree materialization should reject unsafe workspace names"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe workspace name ws:bad/name" in str(exc)

        assert fs.verify_integrity() == []


def test_collect_garbage_deletes_unreachable_file_blobs_with_audit():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        live_content = b"live" * (20 * 1024)
        garbage_content = b"garbage" * (12 * 1024)
        live_node = ws.write("live.bin", live_content, [])
        ws.publish("live.bin", "artifact:live@v1")
        garbage_node = ws.write("garbage.bin", garbage_content, [])
        ws.delete("garbage.bin")

        garbage_hash = hashlib.sha256(garbage_content).hexdigest()
        live_hash = hashlib.sha256(live_content).hexdigest()
        with sqlite3.connect(db_path) as conn:
            garbage_uri = conn.execute(
                "SELECT storage_uri FROM blobs WHERE hash = ?",
                (garbage_hash,),
            ).fetchone()[0]
            live_uri = conn.execute(
                "SELECT storage_uri FROM blobs WHERE hash = ?",
                (live_hash,),
            ).fetchone()[0]

        dry_run = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=True,
                agent_id="gc_agent",
            )
        }
        assert dry_run[garbage_hash] == "would_collect_file"
        assert live_hash not in dry_run
        assert os.path.exists(garbage_uri)

        collected = {
            row[0]: row[3]
            for row in fs.collect_garbage(
                include_workspaces=True,
                dry_run=False,
                agent_id="gc_agent",
            )
        }
        assert collected[garbage_hash] == "collected_file"
        assert not os.path.exists(garbage_uri)
        assert os.path.exists(live_uri)
        assert fs.verify_integrity() == []
        assert garbage_hash not in {row[0] for row in fs.gc_candidates(True)}
        assert bytes(fs.read_node(live_node)) == live_content

        try:
            fs.read_node(garbage_node)
        except anfs_core.StorageCorruptionError:
            pass
        else:
            raise AssertionError("collected unreachable node should not read back")

        with sqlite3.connect(db_path) as conn:
            gc_events = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'gc_collect'"
            ).fetchone()[0]
            gc_blob_events = conn.execute(
                "SELECT COUNT(*) FROM gc_blob_events WHERE hash = ?",
                (garbage_hash,),
            ).fetchone()[0]
        assert gc_events == 1
        assert gc_blob_events == 1


def test_observability_ref_history_and_event_lookup():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write(
            "src/db.py",
            b"print('v1')",
            [],
            tool_call_id="coder_tool_1",
        )
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
        )

        history = fs.ref_history("artifact:patch@v1")
        assert [row[2] for row in history] == ["publish", "approve"]
        assert [row[7] for row in history] == ["published", "approved"]
        assert history[0][5] == patch_node
        assert history[1][4] == patch_node
        assert history[1][5] == patch_node
        assert history[1][3] == "reviewer_agent"

        publish_event = fs.event(history[0][1])
        assert publish_event[1] == "publish"
        assert publish_event[2] == "coder_agent"
        assert publish_event[5] == "ws:coder"
        publish_edges = {(edge[0], edge[2], edge[3]) for edge in publish_event[8]}
        assert ("input", "workspace_node", "src/db.py") in publish_edges
        assert ("output", "published_ref", "artifact:patch@v1") in publish_edges

        approve_event = fs.event(history[1][1])
        assert approve_event[1] == "approve"
        approve_edges = {(edge[0], edge[2], edge[3]) for edge in approve_event[8]}
        assert ("input", "target", None) in approve_edges
        assert ("input", "evidence", "artifact:test_result@v1") in approve_edges
        assert ("output", "approved_target", "artifact:patch@v1") in approve_edges

        try:
            fs.event("event:missing")
        except anfs_core.EventNotFoundError:
            pass
        else:
            raise AssertionError("missing event should raise EventNotFoundError")


def test_event_sequence_is_explicit_and_monotonic():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node = ws.write("src/db.py", b"print('v1')", [])
        ws.publish("src/db.py", "artifact:patch@v1")
        tester = fs.open_workspace("ws:tester", "tester_agent")
        tester.write("test.log", b"PASS", [node])
        tester.publish("test.log", "artifact:test_result@v1")

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT e.kind, es.seq
                FROM events e
                JOIN event_sequence es ON es.event_id = e.event_id
                ORDER BY es.seq
                """
            ).fetchall()
            event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

        assert len(rows) == event_count
        assert [seq for _, seq in rows] == list(range(1, len(rows) + 1))
        assert [kind for kind, _ in rows] == ["write", "publish", "write", "publish"]


def test_concurrent_engine_handles_allocate_unique_event_sequences():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        anfs_core.AnfsEngine(db_path, objs_dir)

        worker_count = 4
        writes_per_worker = 10
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_concurrent_event_writer,
                args=(db_path, objs_dir, worker_idx, writes_per_worker, queue),
            )
            for worker_idx in range(worker_count)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)

        failures = _drain_queue_nowait(queue)
        assert failures == []
        assert [process.exitcode for process in processes] == [0] * worker_count

        expected_events = worker_count * ((writes_per_worker * 2) + 1)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT es.seq, e.kind, e.workspace_id
                FROM event_sequence es
                JOIN events e ON e.event_id = es.event_id
                ORDER BY es.seq
                """
            ).fetchall()
            allocation_count = conn.execute(
                "SELECT COUNT(*) FROM event_sequence_allocations"
            ).fetchone()[0]
            kind_counts = dict(
                conn.execute(
                    """
                    SELECT kind, COUNT(*)
                    FROM events
                    GROUP BY kind
                    """
                ).fetchall()
            )

        assert len(rows) == expected_events
        assert [seq for seq, _kind, _workspace in rows] == list(
            range(1, expected_events + 1)
        )
        assert kind_counts == {
            "read_ref": worker_count * writes_per_worker,
            "search": worker_count,
            "write": worker_count * writes_per_worker,
        }
        assert {kind for _seq, kind, _workspace in rows} == {
            "read_ref",
            "search",
            "write",
        }
        assert {
            workspace for _seq, _kind, workspace in rows
        } == {f"ws:worker-{idx}" for idx in range(worker_count)}
        assert allocation_count == expected_events
        assert anfs_core.AnfsEngine(db_path, objs_dir).verify_integrity() == []


def test_concurrent_stale_ref_version_allows_only_one_shared_ref_update():
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
        expected_version = fs.get_ref("artifact:patch@v1")[4]

        worker_count = 8
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_concurrent_stale_approval_worker,
                args=(
                    db_path,
                    objs_dir,
                    worker_idx,
                    "artifact:patch@v1",
                    "artifact:test_result@v1",
                    expected_version,
                    queue,
                ),
            )
            for worker_idx in range(worker_count)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)

        results = _collect_queue_results(queue, worker_count)

        assert [process.exitcode for process in processes] == [0] * worker_count
        assert sorted(result[0] for result in results) == (
            ["approved"] + ["conflict"] * (worker_count - 1)
        )
        assert all(
            "version mismatch" in detail or "changed concurrently" in detail
            for status, _worker_idx, detail in results
            if status == "conflict"
        )

        final_fs = anfs_core.AnfsEngine(db_path, objs_dir)
        final_ref = final_fs.get_ref("artifact:patch@v1")
        assert final_ref[3] == "approved"
        assert final_ref[4] == expected_version + 1
        assert [row[2] for row in final_fs.ref_history("artifact:patch@v1")] == [
            "publish",
            "approve",
        ]
        with sqlite3.connect(db_path) as conn:
            approve_count = conn.execute(
                "SELECT COUNT(*) FROM events WHERE kind = 'approve'"
            ).fetchone()[0]
            seqs = [
                row[0]
                for row in conn.execute(
                    "SELECT seq FROM event_sequence ORDER BY seq"
                ).fetchall()
            ]
        assert approve_count == 1
        assert seqs == list(range(1, len(seqs) + 1))
        assert final_fs.verify_integrity() == []


def test_events_api_lists_by_sequence_with_pagination_and_filtering():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        one_node = ws.write("one.txt", b"one", [])
        ws.publish("one.txt", "artifact:one@v1")
        ws.write("two.txt", b"two", [])
        ws.publish("two.txt", "artifact:two@v1")
        test_node = tester_ws.write("test.log", b"PASS", [one_node])

        first_page = fs.events(limit=2)
        assert [(row[0], row[2]) for row in first_page] == [(1, "write"), (2, "publish")]
        assert first_page[0][6:] == (0, 1)
        assert first_page[1][6:] == (1, 1)

        second_page = fs.events(after_seq=first_page[-1][0], limit=10)
        assert [(row[0], row[2]) for row in second_page] == [
            (3, "write"),
            (4, "publish"),
            (5, "write"),
        ]
        assert second_page[-1][6:] == (1, 1)

        publishes = fs.events(kind="publish")
        assert [(row[0], row[2], row[4]) for row in publishes] == [
            (2, "publish", "ws:coder"),
            (4, "publish", "ws:coder"),
        ]

        coder_events = fs.events(agent_id="coder_agent")
        assert [row[0] for row in coder_events] == [1, 2, 3, 4]
        tester_events = fs.events(workspace_id="ws:tester")
        assert [(row[0], row[3], row[4]) for row in tester_events] == [
            (5, "tester_agent", "ws:tester")
        ]
        assert [(row[0], row[2]) for row in fs.node_events(one_node)] == [
            (1, "write"),
            (2, "publish"),
            (5, "write"),
        ]
        assert [(row[0], row[2]) for row in fs.node_events(one_node, direction="output")] == [
            (1, "write"),
            (2, "publish"),
        ]
        assert [(row[0], row[2]) for row in fs.node_events(one_node, direction="input")] == [
            (2, "publish"),
            (5, "write"),
        ]
        assert [(row[0], row[2]) for row in fs.node_events(one_node, role="source")] == [
            (5, "write")
        ]
        assert [(row[0], row[2]) for row in fs.node_events(one_node, after_seq=1)] == [
            (2, "publish"),
            (5, "write"),
        ]
        assert set(fs.lineage_nodes(test_node)) == {one_node, test_node}
        assert set(fs.lineage_nodes(test_node, direction="ancestors")) == {
            one_node,
            test_node,
        }
        assert set(fs.lineage_nodes(one_node, direction="descendants")) == {
            one_node,
            test_node,
        }
        ancestor_edges = {
            (row[2], row[3], row[4], row[6], row[7])
            for row in fs.lineage_graph(test_node)
        }
        assert ancestor_edges == {
            ("publish", one_node, "published_ref", one_node, "workspace_node"),
            ("write", test_node, "result", one_node, "source"),
        }
        descendant_edges = {
            (row[2], row[3], row[4], row[6], row[7])
            for row in fs.lineage_graph(one_node, direction="descendants")
        }
        assert descendant_edges == {
            ("publish", one_node, "workspace_node", one_node, "published_ref"),
            ("write", one_node, "source", test_node, "result"),
        }

        time.sleep(0.01)
        searcher = fs.open_workspace("ws:searcher", "search_agent")
        searcher.search("needle_xyz", "published", tool_call_id="tc_search_list")
        search_event = fs.events(kind="search")[0]
        assert [row[0] for row in fs.events(payload_contains="needle_xyz")] == [
            search_event[0]
        ]
        assert [row[0] for row in fs.events(tool_call_id="tc_search_list")] == [
            search_event[0]
        ]
        assert [
            row[0]
            for row in fs.events(kind="search", tool_call_id="tc_search_list")
        ] == [search_event[0]]
        assert fs.events(tool_call_id="tc_missing") == []
        assert [row[0] for row in fs.events(payload_contains="published")] == [
            search_event[0]
        ]
        assert [row[0] for row in fs.events(created_after_ms=search_event[5])] == [
            search_event[0]
        ]
        assert [row[0] for row in fs.events(created_before_ms=search_event[5] - 1)] == [
            1,
            2,
            3,
            4,
            5,
        ]

        try:
            fs.events(limit=0)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid event list limit should be rejected")

        try:
            fs.events(created_after_ms=10, created_before_ms=1)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid event time range should be rejected")

        try:
            fs.node_events(one_node, direction="sideways")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid node event direction should be rejected")

        try:
            fs.node_events("node:missing")
        except anfs_core.NodeNotFoundError:
            pass
        else:
            raise AssertionError("missing node event lookup should be rejected")

        try:
            fs.lineage_nodes(one_node, direction="sideways")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid lineage direction should be rejected")

        try:
            fs.lineage_nodes("node:missing")
        except anfs_core.NodeNotFoundError:
            pass
        else:
            raise AssertionError("missing lineage node lookup should be rejected")

        try:
            fs.lineage_graph(one_node, direction="sideways")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid lineage graph direction should be rejected")

        try:
            fs.lineage_graph("node:missing")
        except anfs_core.NodeNotFoundError:
            pass
        else:
            raise AssertionError("missing lineage graph lookup should be rejected")


def test_run_id_propagates_to_workspace_and_engine_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace(
            "ws:coder",
            "coder_agent",
            run_id="run:coder",
        )
        tester_ws = fs.open_workspace(
            "ws:tester",
            "tester_agent",
            run_id="run:tester",
        )

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
            run_id="run:review",
        )

        coder_events = fs.events(run_id="run:coder")
        assert [row[2] for row in coder_events] == ["write", "publish"]
        assert {fs.event(row[1])[3] for row in coder_events} == {"run:coder"}

        tester_events = fs.events(run_id="run:tester")
        assert [row[2] for row in tester_events] == ["write", "publish"]
        assert {fs.event(row[1])[3] for row in tester_events} == {"run:tester"}

        review_events = fs.events(run_id="run:review")
        assert [row[2] for row in review_events] == ["policy_decision", "approve"]
        assert {fs.event(row[1])[3] for row in review_events} == {"run:review"}


def test_tool_call_id_propagates_to_workspace_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:tools")

        ws.write("a.txt", b"alpha", [], tool_call_id="tc_write")
        ws.publish("a.txt", "artifact:a@v1", tool_call_id="tc_publish")
        ws.consume("artifact:a@v1", purpose="inspect", tool_call_id="tc_consume")
        ws.write("b.txt", b"beta", [], tool_call_id="tc_write_b")
        ws.write("c.txt", b"gamma", [], tool_call_id="tc_write_c")
        ws.publish_manifest(
            ["b.txt", "c.txt"],
            "artifact:bundle@v1",
            tool_call_id="tc_manifest",
        )
        ws.search("alpha", "published", tool_call_id="tc_search")
        ws.write("tmp.txt", b"tmp", [], tool_call_id="tc_tmp")
        ws.delete("tmp.txt", tool_call_id="tc_delete")

        tool_calls = {
            fs.event(row[1])[1]: fs.event(row[1])[4]
            for row in fs.events(run_id="run:tools")
            if fs.event(row[1])[4] is not None
        }
        assert tool_calls == {
            "write": "tc_tmp",
            "publish": "tc_publish",
            "consume": "tc_consume",
            "publish_manifest": "tc_manifest",
            "search": "tc_search",
            "delete_ref": "tc_delete",
        }
        assert [row[2] for row in fs.events(tool_call_id="tc_search")] == ["search"]
        assert [row[2] for row in fs.events(kind="delete_ref", tool_call_id="tc_delete")] == [
            "delete_ref"
        ]


def test_read_ref_enforces_workspace_draft_boundary_and_records_version():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        alpha_ws = fs.open_workspace("ws:alpha", "alpha_agent", run_id="run:alpha-read")
        beta_ws = fs.open_workspace("ws:beta", "beta_agent", run_id="run:beta-read")

        draft_node = alpha_ws.write("secret.txt", b"private draft", [])
        draft_ref = fs.get_ref("ws:alpha/secret.txt")
        assert bytes(
            alpha_ws.read_ref(
                "ws:alpha/secret.txt",
                purpose="self inspect",
                tool_call_id="tc_alpha_read",
            )
        ) == b"private draft"

        try:
            beta_ws.read_ref("ws:alpha/secret.txt", purpose="cross workspace")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("other workspaces must not read private draft refs")

        alpha_ws.publish("secret.txt", "artifact:secret@v1")
        published_ref = fs.get_ref("artifact:secret@v1")
        assert bytes(
            beta_ws.read_ref(
                "artifact:secret@v1",
                purpose="published input",
                tool_call_id="tc_beta_read",
            )
        ) == b"private draft"

        alpha_event = fs.event(fs.events(kind="read_ref", run_id="run:alpha-read")[0][1])
        assert alpha_event[1] == "read_ref"
        assert alpha_event[2] == "alpha_agent"
        assert alpha_event[4] == "tc_alpha_read"
        assert json.loads(alpha_event[6]) == {
            "ref_name": "ws:alpha/secret.txt",
            "node_id": draft_node,
            "state": "draft",
            "ref_version": draft_ref[4],
            "purpose": "self inspect",
        }
        assert alpha_event[8] == [
            (
                "input",
                draft_node,
                "read_ref",
                "ws:alpha/secret.txt",
            )
        ]

        beta_events = fs.events(kind="read_ref", run_id="run:beta-read")
        assert len(beta_events) == 1
        beta_event = fs.event(beta_events[0][1])
        assert beta_event[4] == "tc_beta_read"
        assert json.loads(beta_event[6]) == {
            "ref_name": "artifact:secret@v1",
            "node_id": draft_node,
            "state": "published",
            "ref_version": published_ref[4],
            "purpose": "published input",
        }


def test_tool_call_id_propagates_to_engine_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        base_node = importer_ws.write("file.txt", b"old", [])
        importer_ws.publish("file.txt", "resource:repo/main@v1/file.txt", "resource")
        fs.snapshot_namespace(
            "resource:repo/main@v1",
            "resource:repo/main@snapshot",
            "snapshot_agent",
            run_id="run:engine",
            tool_call_id="tc_snapshot",
        )

        coder_ws = fs.checkout(
            "ws:coder",
            "coder_agent",
            "resource:repo/main@v1",
            run_id="run:engine",
            tool_call_id="tc_checkout",
        )
        coder_ws.write("add.txt", b"add", [])
        fs.merge_workspace(
            "resource:repo/main@v1",
            "ws:coder",
            "merge_agent",
            run_id="run:engine",
            tool_call_id="tc_merge",
        )

        patch_ws = fs.open_workspace("ws:patch", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")
        patch_node = patch_ws.write("src/db.py", b"print('v1')", [])
        patch_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
            run_id="run:engine",
            tool_call_id="tc_approve",
        )
        fs.archive_ref(
            "artifact:patch@v1",
            "retention_agent",
            run_id="run:engine",
            tool_call_id="tc_archive",
        )

        deny_ws = fs.checkout(
            "ws:deny",
            "coder_agent",
            "resource:repo/main@v1",
            run_id="run:engine",
        )
        deny_ws.write("file.txt", b"workspace", [])
        current_base = importer_ws.write("current.txt", b"current", [])
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE refs
                SET node_id = ?, ref_version = ref_version + 1
                WHERE ref_name = ?
                """,
                (current_base, "resource:repo/main@v1/file.txt"),
            )

        try:
            fs.merge_workspace(
                "resource:repo/main@v1",
                "ws:deny",
                "merge_agent",
                run_id="run:engine",
                tool_call_id="tc_merge_deny",
            )
        except anfs_core.RefConflictError:
            pass
        else:
            raise AssertionError("conflicting merge should raise RefConflictError")

        event_records = [fs.event(row[1]) for row in fs.events(run_id="run:engine")]
        tool_calls = [
            (event[1], event[4])
            for event in event_records
            if event[4] is not None
        ]

        assert ("checkout", "tc_checkout") in tool_calls
        assert ("snapshot_namespace", "tc_snapshot") in tool_calls
        assert ("merge_workspace", "tc_merge", "resource:repo/main@v1") in [
            (event[1], event[4], event[6]) for event in event_records
        ]
        assert ("approve", "tc_approve", "artifact:patch@v1") in [
            (event[1], event[4], event[6]) for event in event_records
        ]
        assert ("archive_ref", "tc_archive", "artifact:patch@v1") in [
            (event[1], event[4], event[6]) for event in event_records
        ]

        policy_decisions = [
            (event[4], json.loads(event[6]))
            for event in event_records
            if event[1] == "policy_decision" and event[4] is not None
        ]
        assert ("tc_approve", {"policy": "lineage_approval", "decision": "allow"}) in [
            (tool_call_id, {"policy": payload["policy"], "decision": payload["decision"]})
            for tool_call_id, payload in policy_decisions
        ]
        assert ("tc_merge", {"policy": "workspace_merge", "decision": "allow"}) in [
            (tool_call_id, {"policy": payload["policy"], "decision": payload["decision"]})
            for tool_call_id, payload in policy_decisions
        ]
        assert ("tc_merge_deny", {"policy": "workspace_merge", "decision": "deny"}) in [
            (tool_call_id, {"policy": payload["policy"], "decision": payload["decision"]})
            for tool_call_id, payload in policy_decisions
        ]
        assert fs.get_ref("resource:repo/main@v1/file.txt")[1] == current_base
        assert fs.get_ref("resource:repo/main@v1/add.txt")[3] == "published"
        assert base_node != current_base


def test_run_metadata_lifecycle_and_listing():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        started = fs.start_run(
            "run:coder",
            agent_id="coder_agent",
            workspace_id="ws:coder",
            metadata_json='{"task":"fix-db"}',
        )
        assert started[0] == "run:coder"
        assert started[1] == "coder_agent"
        assert started[2] == "ws:coder"
        assert started[3] == "active"
        assert started[4] == '{"task":"fix-db"}'
        assert started[7] is None

        coder_ws = fs.open_workspace(
            "ws:coder",
            "coder_agent",
            run_id="run:coder",
        )
        coder_ws.write("src/db.py", b"print('v1')", [])

        active_runs = fs.runs(state="active")
        assert [row[0] for row in active_runs] == ["run:coder"]
        assert fs.runs(agent_id="coder_agent")[0][0] == "run:coder"
        assert fs.runs(workspace_id="ws:coder")[0][0] == "run:coder"

        finished = fs.finish_run(
            "run:coder",
            "succeeded",
            metadata_json='{"task":"fix-db","status":"done"}',
        )
        assert finished[3] == "succeeded"
        assert finished[4] == '{"task":"fix-db","status":"done"}'
        assert finished[7] is not None
        assert fs.get_run("run:coder")[3] == "succeeded"
        assert fs.runs(state="active") == []
        assert [row[2] for row in fs.events(run_id="run:coder")] == [
            "run_start",
            "write",
            "run_finish",
        ]

        try:
            fs.finish_run("run:coder", "failed")
            assert False, "finished run should not be finishable again"
        except anfs_core.InvalidStateTransitionError:
            pass

        try:
            fs.get_run("run:missing")
            assert False, "missing run should raise RunNotFoundError"
        except anfs_core.RunNotFoundError:
            pass

        with sqlite3.connect(db_path) as conn:
            run_events = conn.execute(
                """
                SELECT e.kind, re.old_state, re.new_state
                FROM run_events re
                JOIN events e ON e.event_id = re.event_id
                JOIN event_sequence es ON es.event_id = e.event_id
                WHERE re.run_id = ?
                ORDER BY es.seq
                """,
                ("run:coder",),
            ).fetchall()
        assert run_events == [
            ("run_start", None, "active"),
            ("run_finish", "active", "succeeded"),
        ]
        assert fs.verify_integrity() == []


def test_ref_view_at_event_reconstructs_namespace_view():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        a_node = importer_ws.write("a.txt", b"a", [])
        importer_ws.publish("a.txt", "resource:repo/main@v1/a.txt", "resource")
        a_publish_event = fs.ref_history("resource:repo/main@v1/a.txt")[0][1]

        b_node = importer_ws.write("b.txt", b"b", [])
        importer_ws.publish("b.txt", "resource:repo/main@v1/b.txt", "resource")
        b_publish_event = fs.ref_history("resource:repo/main@v1/b.txt")[0][1]

        fs.archive_ref("resource:repo/main@v1/b.txt", "maintainer_agent")
        b_archive_event = fs.ref_history("resource:repo/main@v1/b.txt")[-1][1]

        view_after_a = {
            row[0]: (row[1], row[2], row[3])
            for row in fs.ref_view_at_event(
                a_publish_event,
                prefix="resource:repo/main@v1",
            )
        }
        assert view_after_a == {
            "resource:repo/main@v1/a.txt": (a_node, "resource", "published")
        }

        view_after_b = {
            row[0]: (row[1], row[2], row[3])
            for row in fs.ref_view_at_event(
                b_publish_event,
                prefix="resource:repo/main@v1",
            )
        }
        assert view_after_b == {
            "resource:repo/main@v1/a.txt": (a_node, "resource", "published"),
            "resource:repo/main@v1/b.txt": (b_node, "resource", "published"),
        }

        active_after_archive = {
            row[0]: (row[1], row[2], row[3])
            for row in fs.ref_view_at_event(
                b_archive_event,
                prefix="resource:repo/main@v1",
            )
        }
        assert active_after_archive == {
            "resource:repo/main@v1/a.txt": (a_node, "resource", "published")
        }

        full_after_archive = {
            row[0]: (row[1], row[2], row[3])
            for row in fs.ref_view_at_event(
                b_archive_event,
                prefix="resource:repo/main@v1",
                include_inactive=True,
            )
        }
        assert full_after_archive == {
            "resource:repo/main@v1/a.txt": (a_node, "resource", "published"),
            "resource:repo/main@v1/b.txt": (b_node, "resource", "archived"),
        }


def test_workspace_diff_reports_added_modified_deleted_unchanged():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        base_keep = importer_ws.write("keep.txt", b"keep", [])
        base_modify = importer_ws.write("modify.txt", b"old", [])
        base_delete = importer_ws.write("delete.txt", b"delete", [])
        importer_ws.publish_manifest(
            ["delete.txt", "keep.txt", "modify.txt"],
            "resource:repo/main@v1",
            "resource",
        )
        importer_ws.publish("keep.txt", "resource:repo/main@v1/keep.txt", "resource")
        importer_ws.publish("modify.txt", "resource:repo/main@v1/modify.txt", "resource")
        importer_ws.publish("delete.txt", "resource:repo/main@v1/delete.txt", "resource")

        coder_ws = fs.checkout(
            workspace="ws:coder",
            agent_id="coder_agent",
            base="resource:repo/main@v1",
        )
        modified_node = coder_ws.write("modify.txt", b"new", [])
        added_node = coder_ws.write("add.txt", b"add", [])
        coder_ws.delete("delete.txt")

        diff = {
            path: (status, base_node, workspace_node)
            for path, status, base_node, workspace_node in fs.workspace_diff(
                "resource:repo/main@v1",
                "ws:coder",
            )
        }

        assert diff["keep.txt"] == ("unchanged", base_keep, base_keep)
        assert diff["modify.txt"] == ("modified", base_modify, modified_node)
        assert diff["delete.txt"] == ("deleted", base_delete, None)
        assert diff["add.txt"] == ("added", None, added_node)


def test_merge_workspace_applies_non_conflicting_changes_atomically():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        keep_node = importer_ws.write("keep.txt", b"keep", [])
        modify_base = importer_ws.write("modify.txt", b"old", [])
        delete_base = importer_ws.write("delete.txt", b"delete", [])
        importer_ws.publish("keep.txt", "resource:repo/main@v1/keep.txt", "resource")
        importer_ws.publish("modify.txt", "resource:repo/main@v1/modify.txt", "resource")
        importer_ws.publish("delete.txt", "resource:repo/main@v1/delete.txt", "resource")

        coder_ws = fs.checkout("ws:coder", "coder_agent", "resource:repo/main@v1")
        modify_workspace = coder_ws.write("modify.txt", b"new", [])
        add_workspace = coder_ws.write("add.txt", b"add", [])
        coder_ws.delete("delete.txt")

        merged = {
            path: (status, ref_name, node_id)
            for path, status, ref_name, node_id in fs.merge_workspace(
                "resource:repo/main@v1",
                "ws:coder",
                "merge_agent",
            )
        }

        assert merged == {
            "add.txt": ("added", "resource:repo/main@v1/add.txt", add_workspace),
            "delete.txt": ("deleted", "resource:repo/main@v1/delete.txt", None),
            "modify.txt": ("modified", "resource:repo/main@v1/modify.txt", modify_workspace),
        }
        assert fs.get_ref("resource:repo/main@v1/keep.txt")[1] == keep_node
        assert fs.get_ref("resource:repo/main@v1/modify.txt")[1] == modify_workspace
        assert fs.get_ref("resource:repo/main@v1/add.txt")[1] == add_workspace
        assert fs.get_ref("resource:repo/main@v1/delete.txt")[3] == "archived"

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
            ref_event_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM ref_events
                WHERE event_id IN (
                    SELECT event_id FROM events WHERE kind = 'merge_workspace'
                )
                """
            ).fetchone()[0]
            assert merge_events == 1
            assert ref_event_count == 3
            assert conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE node_id = ?",
                (modify_base,),
            ).fetchone()[0] == 1

        assert len(merge_policy_events) == 1
        merge_policy_payload = json.loads(merge_policy_events[0][1])
        assert merge_policy_payload["decision"] == "allow"
        assert merge_policy_payload["reason_code"] == "no_workspace_conflicts"
        assert merge_policy_payload["reason"] == "no_workspace_conflicts"
        assert merge_policy_payload["target_ref"] == "resource:repo/main@v1"
        assert merge_policy_payload["workspace"] == "ws:coder"
        assert merge_policy_payload["conflict_count"] == 0
        assert merge_policy_payload["change_count"] == 3
        with sqlite3.connect(db_path) as conn:
            policy_edge_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM event_edges
                WHERE event_id = ?
                  AND role LIKE 'merge_change:%'
                """,
                (merge_policy_events[0][0],),
            ).fetchone()[0]
        assert policy_edge_count >= 3
