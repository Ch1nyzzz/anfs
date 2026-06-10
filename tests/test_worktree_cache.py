"""Worktree materialization/commit and cached working sets."""

import hashlib
import json
import multiprocessing
import os
import sqlite3
import tempfile

import anfs_core

from _helpers import _collect_queue_results, _concurrent_checked_worktree_commit_worker


def test_persistent_node_chunk_cache_is_rebuildable_and_verified():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        content = (b"0123456789abcdef" * 8192) + b"tail"
        node_id = ws.write("large.bin", content, [])
        chunk_size = 32768

        derived = fs.node_chunks(node_id, chunk_size)
        assert fs.cached_node_chunks(node_id, chunk_size) == []
        cached = fs.cache_node_chunks(node_id, chunk_size)
        assert cached == derived
        assert fs.cached_node_chunks(node_id, chunk_size) == derived
        assert fs.cache_node_chunks(node_id, chunk_size) == derived

        with sqlite3.connect(db_path) as conn:
            header_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM node_chunk_indexes
                WHERE node_id = ? AND chunk_size = ?
                """,
                (node_id, chunk_size),
            ).fetchone()[0]
            row_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM node_chunk_index
                WHERE node_id = ? AND chunk_size = ?
                """,
                (node_id, chunk_size),
            ).fetchone()[0]
        assert header_count == 1
        assert row_count == len(derived)
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                UPDATE node_chunk_index
                SET sha256 = '0000000000000000000000000000000000000000000000000000000000000000'
                WHERE node_id = ? AND chunk_size = ? AND chunk_index = 0
                """,
                (node_id, chunk_size),
            )

        issues = fs.verify_integrity()
        assert any(
            f"node_chunk_index for {node_id} chunk_size {chunk_size} chunk 0 sha256 mismatch"
            in issue
            for issue in issues
        )


def test_materialize_ref_view_at_event_writes_replay_directory():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        replay_dir = os.path.join(tmpdir, "replay")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer_ws = fs.open_workspace("ws:importer", "importer_agent")

        importer_ws.write("src/a.py", b"print('a')", [])
        importer_ws.publish("src/a.py", "resource:repo/main@v1/src/a.py", "resource")
        importer_ws.write("src/b.py", b"print('b')", [])
        importer_ws.publish("src/b.py", "resource:repo/main@v1/src/b.py", "resource")
        b_publish_event = fs.ref_history("resource:repo/main@v1/src/b.py")[0][1]

        materialized = {
            row[1]: (row[0], row[3])
            for row in fs.materialize_ref_view_at_event(
                b_publish_event,
                replay_dir,
                prefix="resource:repo/main@v1",
            )
        }

        assert materialized == {
            "src/a.py": ("resource:repo/main@v1/src/a.py", len(b"print('a')")),
            "src/b.py": ("resource:repo/main@v1/src/b.py", len(b"print('b')")),
        }
        with open(os.path.join(replay_dir, "src", "a.py"), "rb") as file:
            assert file.read() == b"print('a')"
        with open(os.path.join(replay_dir, "src", "b.py"), "rb") as file:
            assert file.read() == b"print('b')"
        with open(
            os.path.join(replay_dir, ".anfs-replay-manifest.json"),
            "r",
            encoding="utf-8",
        ) as file:
            replay_manifest = json.load(file)
        assert replay_manifest["schema"] == "anfs.replay_manifest.v1"
        assert replay_manifest["event_id"] == b_publish_event
        assert replay_manifest["prefix"] == "resource:repo/main@v1"
        assert replay_manifest["include_inactive"] is False
        assert {
            file["relative_path"]: (file["ref_name"], file["size"])
            for file in replay_manifest["files"]
        } == materialized
        with open(os.path.join(replay_dir, "src", "a.py"), "wb") as file:
            file.write(b"existing")
        stale_path = os.path.join(replay_dir, "stale.txt")
        with open(stale_path, "wb") as file:
            file.write(b"stale")
        try:
            fs.materialize_ref_view_at_event(
                b_publish_event,
                replay_dir,
                prefix="resource:repo/main@v1",
            )
            assert False, "materialization should reject existing targets by default"
        except anfs_core.PolicyDeniedError:
            pass
        with open(os.path.join(replay_dir, "src", "a.py"), "rb") as file:
            assert file.read() == b"existing"
        fs.materialize_ref_view_at_event(
            b_publish_event,
            replay_dir,
            prefix="resource:repo/main@v1",
            overwrite=True,
        )
        with open(os.path.join(replay_dir, "src", "a.py"), "rb") as file:
            assert file.read() == b"print('a')"
        assert not os.path.exists(stale_path)

        fs.archive_ref("resource:repo/main@v1/src/b.py", "maintainer_agent")
        archive_event = fs.ref_history("resource:repo/main@v1/src/b.py")[-1][1]
        active_replay_dir = os.path.join(tmpdir, "active-replay")
        active_materialized = {
            row[1]
            for row in fs.materialize_ref_view_at_event(
                archive_event,
                active_replay_dir,
                prefix="resource:repo/main@v1",
            )
        }
        assert active_materialized == {"src/a.py"}
        assert os.path.exists(os.path.join(active_replay_dir, "src", "a.py"))
        assert not os.path.exists(os.path.join(active_replay_dir, "src", "b.py"))
        with open(
            os.path.join(active_replay_dir, ".anfs-replay-manifest.json"),
            "r",
            encoding="utf-8",
        ) as file:
            active_replay_manifest = json.load(file)
        assert [file["relative_path"] for file in active_replay_manifest["files"]] == [
            "src/a.py"
        ]

        direct_replay_dir = os.path.join(tmpdir, "direct-replay")
        fs.materialize_ref_view_at_event(
            b_publish_event,
            direct_replay_dir,
            prefix="resource:repo/main@v1",
            atomic=False,
        )
        direct_stale_path = os.path.join(direct_replay_dir, "stale.txt")
        with open(direct_stale_path, "wb") as file:
            file.write(b"stale")
        fs.materialize_ref_view_at_event(
            b_publish_event,
            direct_replay_dir,
            prefix="resource:repo/main@v1",
            overwrite=True,
            atomic=False,
        )
        assert not os.path.exists(direct_stale_path)


def test_worktree_materialize_and_commit_roundtrip_existing_fs_edits():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")
        blocked_dir = os.path.join(tmpdir, "blocked")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        maintainer = fs.open_workspace("ws:maintainer", "maintainer_agent")
        app_v1 = maintainer.write("app.py", b"print('v1')\n", [])
        maintainer.publish("app.py", "resource:repo/main@v1/app.py", "resource")
        remove_node = maintainer.write("remove.txt", b"remove me\n", [])
        maintainer.publish("remove.txt", "resource:repo/main@v1/remove.txt", "resource")

        fs.checkout(
            "ws:coder",
            "coder_agent",
            base="resource:repo/main@v1",
            tool_call_id="tc_checkout_worktree",
        )
        fs.set_policy_label(
            "node",
            app_v1,
            "retention",
            "blocked",
            "policy_agent",
        )
        try:
            fs.materialize_workspace(
                "ws:coder",
                blocked_dir,
                policy_label_excludes=["retention"],
            )
            assert False, "worktree materialization should reject excluded labels"
        except anfs_core.PolicyDeniedError:
            pass
        assert not os.path.exists(blocked_dir)

        fs.set_policy_label("node", app_v1, "retention", None, "policy_agent")
        rows = fs.materialize_workspace("ws:coder", worktree_dir)
        assert {
            (path, ref_name, node_id)
            for path, ref_name, node_id, _size in rows
        } == {
            ("app.py", "ws:coder/app.py", app_v1),
            ("remove.txt", "ws:coder/remove.txt", remove_node),
        }
        assert os.path.exists(os.path.join(worktree_dir, ".anfs-worktree-manifest.json"))
        with open(os.path.join(worktree_dir, "app.py"), "wb") as file:
            file.write(b"print('v2')\n")
        os.remove(os.path.join(worktree_dir, "remove.txt"))
        os.makedirs(os.path.join(worktree_dir, "docs"))
        with open(os.path.join(worktree_dir, "docs", "new.md"), "wb") as file:
            file.write(b"new doc\n")

        fs.set_policy_label(
            "ref",
            "ws:coder/app.py",
            "retention",
            "blocked",
            "policy_agent",
        )
        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_blocked_worktree_commit",
                policy_label_excludes=["retention"],
            )
            assert False, "worktree commit should reject excluded current refs"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_label("ref", "ws:coder/app.py", "retention", None, "policy_agent")
        committed = fs.commit_worktree(
            "ws:coder",
            worktree_dir,
            "coder_agent",
            run_id="run:worktree",
            tool_call_id="tc_worktree_commit",
            policy_label_excludes=["retention"],
        )
        statuses = {path: (status, node_id) for path, status, node_id in committed}
        assert statuses["app.py"][0] == "modified"
        assert statuses["docs/new.md"][0] == "added"
        assert statuses["remove.txt"] == ("deleted", None)
        app_v2 = statuses["app.py"][1]
        assert bytes(fs.read_node(app_v2)) == b"print('v2')\n"
        assert app_v1 in fs.lineage_nodes(app_v2, "ancestors")
        assert fs.get_ref("ws:coder/remove.txt")[3] == "deleted"
        worktree_commit_events = fs.events(kind="worktree_commit")
        assert len(worktree_commit_events) == 1
        worktree_commit_event = fs.event(worktree_commit_events[0][1])
        worktree_payload = json.loads(worktree_commit_event[6])
        assert worktree_payload["workspace"] == "ws:coder"
        assert worktree_payload["delete_missing"] is True
        assert worktree_payload["policy_label_excludes"] == ["retention"]
        assert worktree_payload["result_count"] == 3
        assert worktree_payload["changed_count"] == 3
        assert worktree_payload["edge_count"] == 4
        assert {
            (row["path"], row["status"], row["old_node_id"], row["new_node_id"])
            for row in worktree_payload["results"]
        } == {
            ("app.py", "modified", app_v1, app_v2),
            ("docs/new.md", "added", None, statuses["docs/new.md"][1]),
            ("remove.txt", "deleted", remove_node, None),
        }
        assert {
            (direction, node_id, role.split(":")[0], logical_path)
            for direction, node_id, role, logical_path in worktree_commit_event[8]
        } == {
            ("input", app_v1, "worktree_input", "app.py"),
            ("output", app_v2, "worktree_modified", "app.py"),
            ("output", statuses["docs/new.md"][1], "worktree_added", "docs/new.md"),
            ("input", remove_node, "worktree_deleted", "remove.txt"),
        }

        merged = fs.merge_workspace(
            "resource:repo/main@v1",
            "ws:coder",
            "merge_agent",
            tool_call_id="tc_merge_worktree",
        )
        merged_by_path = {path: (status, ref_name, node_id) for path, status, ref_name, node_id in merged}
        assert merged_by_path["app.py"] == (
            "modified",
            "resource:repo/main@v1/app.py",
            app_v2,
        )
        assert merged_by_path["docs/new.md"][0:2] == (
            "added",
            "resource:repo/main@v1/docs/new.md",
        )
        assert merged_by_path["remove.txt"] == (
            "deleted",
            "resource:repo/main@v1/remove.txt",
            None,
        )
        assert fs.verify_integrity() == []


def test_concurrent_checked_worktree_commits_allow_only_one_materialized_base():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_a = os.path.join(tmpdir, "worktree-a")
        worktree_b = os.path.join(tmpdir, "worktree-b")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer = fs.open_workspace("ws:importer", "importer_agent")
        base_a = importer.write("a.txt", b"a base\n", [])
        importer.publish("a.txt", "resource:repo/main@v1/a.txt", "resource")
        base_b = importer.write("b.txt", b"b base\n", [])
        importer.publish("b.txt", "resource:repo/main@v1/b.txt", "resource")
        fs.checkout("ws:coder", "coder_agent", base="resource:repo/main@v1")

        fs.materialize_workspace("ws:coder", worktree_a)
        fs.materialize_workspace("ws:coder", worktree_b)
        with open(os.path.join(worktree_a, "a.txt"), "wb") as file:
            file.write(b"a from worker 0\n")
        with open(os.path.join(worktree_b, "b.txt"), "wb") as file:
            file.write(b"b from worker 1\n")

        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=_concurrent_checked_worktree_commit_worker,
                args=(
                    db_path,
                    objs_dir,
                    "ws:coder",
                    worktree_dir,
                    worker_idx,
                    queue,
                ),
            )
            for worker_idx, worktree_dir in enumerate((worktree_a, worktree_b))
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(20)

        results = _collect_queue_results(queue, len(processes))

        assert [process.exitcode for process in processes] == [0, 0]
        assert sorted(result[0] for result in results) == [
            "committed",
            "conflict",
        ], results
        committed = [result for result in results if result[0] == "committed"][0]
        conflict = [result for result in results if result[0] == "conflict"][0]
        assert "worktree base for" in conflict[2]
        assert "is stale" in conflict[2]

        committed_worker = committed[1]
        committed_rows = committed[2]
        statuses = {path: (status, node_id) for path, status, node_id in committed_rows}
        assert len(fs.events(kind="worktree_commit")) == 1
        if committed_worker == 0:
            assert statuses["a.txt"][0] == "modified"
            assert statuses["b.txt"][0] == "unchanged"
            assert bytes(fs.read_node(fs.get_ref("ws:coder/a.txt")[1])) == b"a from worker 0\n"
            assert bytes(fs.read_node(fs.get_ref("ws:coder/b.txt")[1])) == b"b base\n"
            assert fs.get_ref("ws:coder/b.txt")[1] == base_b
            assert base_a in fs.lineage_nodes(fs.get_ref("ws:coder/a.txt")[1], "ancestors")
        else:
            assert statuses["a.txt"][0] == "unchanged"
            assert statuses["b.txt"][0] == "modified"
            assert bytes(fs.read_node(fs.get_ref("ws:coder/a.txt")[1])) == b"a base\n"
            assert bytes(fs.read_node(fs.get_ref("ws:coder/b.txt")[1])) == b"b from worker 1\n"
            assert fs.get_ref("ws:coder/a.txt")[1] == base_a
            assert base_b in fs.lineage_nodes(fs.get_ref("ws:coder/b.txt")[1], "ancestors")
        assert fs.verify_integrity() == []


def test_worktree_adapter_reserves_root_manifest_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        ws.write(".anfs-worktree-manifest.json", b"user payload\n", [])
        ws.write("app.py", b"print('v1')\n", [])

        try:
            fs.materialize_workspace("ws:coder", worktree_dir)
            assert False, "worktree materialization should reject reserved manifest path"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe worktree relative path .anfs-worktree-manifest.json" in str(exc)

        os.makedirs(worktree_dir)
        with open(os.path.join(worktree_dir, ".anfs-worktree-manifest.json"), "wb") as file:
            file.write(b'{"schema":"local-tool-metadata"}')
        with open(os.path.join(worktree_dir, "app.py"), "wb") as file:
            file.write(b"print('v2')\n")

        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_reserved_manifest_worktree_commit",
            )
            assert False, "worktree commit should reject current reserved manifest path"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe worktree relative path .anfs-worktree-manifest.json" in str(exc)
        assert fs.events(kind="worktree_commit", tool_call_id="tc_reserved_manifest_worktree_commit") == []

        clean_fs = anfs_core.AnfsEngine(
            os.path.join(tmpdir, "clean.db"),
            os.path.join(tmpdir, "clean-objs"),
        )
        clean_ws = clean_fs.open_workspace("ws:clean", "coder_agent")
        clean_node = clean_ws.write("app.py", b"print('v1')\n", [])
        clean_dir = os.path.join(tmpdir, "clean-worktree")
        clean_fs.materialize_workspace("ws:clean", clean_dir)
        with open(os.path.join(clean_dir, ".anfs-worktree-manifest.json"), "wb") as file:
            file.write(b'{"schema":"local-tool-metadata"}')
        committed = clean_fs.commit_worktree(
            "ws:clean",
            clean_dir,
            "coder_agent",
            tool_call_id="tc_local_manifest_metadata_ignored",
        )
        assert committed == [("app.py", "unchanged", clean_node)]
        try:
            clean_ws.read(".anfs-worktree-manifest.json")
            assert False, "local adapter manifest metadata should not be imported as a user file"
        except anfs_core.RefNotFoundError:
            pass
        assert clean_fs.verify_integrity() == []
        assert fs.verify_integrity() == []


def test_cached_materialized_workspace_reuses_and_verifies_working_set():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        cache_root = os.path.join(tmpdir, "cache")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        ws.write("app.py", b"print('v1')\n", [])
        ws.write("docs/readme.md", b"hello\n", [])

        cache_key, output_dir, file_count, reused = fs.cache_materialized_workspace(
            "ws:coder",
            cache_root,
            cache_key="main",
        )
        assert (cache_key, file_count, reused) == ("main", 2, False)
        assert output_dir == os.path.join(cache_root, "main")
        assert os.path.exists(os.path.join(output_dir, ".anfs-worktree-manifest.json"))
        with open(os.path.join(output_dir, "app.py"), "rb") as file:
            assert file.read() == b"print('v1')\n"

        cache_key2, output_dir2, file_count2, reused2 = fs.cache_materialized_workspace(
            "ws:coder",
            cache_root,
            cache_key="main",
        )
        assert (cache_key2, output_dir2, file_count2, reused2) == (
            cache_key,
            output_dir,
            file_count,
            True,
        )
        listed = fs.cached_working_sets("ws:coder")
        assert len(listed) == 1
        assert listed[0][0:3] == ("main", "ws:coder", output_dir)
        assert listed[0][4] == 2
        assert fs.cached_working_sets("ws:other") == []
        assert fs.verify_integrity() == []

        ws.write("app.py", b"print('v2')\n", [fs.get_ref("ws:coder/app.py")[1]])
        try:
            fs.cache_materialized_workspace("ws:coder", cache_root, cache_key="main")
            assert False, "stale cached working set should require overwrite"
        except anfs_core.PolicyDeniedError:
            pass

        cache_key3, output_dir3, file_count3, reused3 = fs.cache_materialized_workspace(
            "ws:coder",
            cache_root,
            cache_key="main",
            overwrite=True,
        )
        assert (cache_key3, output_dir3, file_count3, reused3) == (
            "main",
            output_dir,
            2,
            False,
        )
        with open(os.path.join(output_dir, "app.py"), "rb") as file:
            assert file.read() == b"print('v2')\n"
        assert fs.verify_integrity() == []

        with open(os.path.join(output_dir, "app.py"), "wb") as file:
            file.write(b"tampered\n")
        issues = fs.verify_integrity()
        assert any(
            "materialized_working_set main file app.py bytes do not match node" in issue
            for issue in issues
        )


def test_worktree_commit_rolls_back_all_file_mutations_on_late_failure():
    def table_counts(db_path):
        with sqlite3.connect(db_path) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "events",
                    "event_edges",
                    "event_sequence",
                    "ref_events",
                    "refs",
                    "nodes",
                    "blobs",
                )
            }

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        original_node = ws.write("a.txt", b"old\n", [])
        fs.materialize_workspace("ws:coder", worktree_dir)
        with open(os.path.join(worktree_dir, "a.txt"), "wb") as file:
            file.write(b"new\n")
        bad_content = b"bad orphan object candidate\n" * 4096
        bad_hash = hashlib.sha256(bad_content).hexdigest()
        bad_object_path = os.path.join(objs_dir, bad_hash[:2], bad_hash[2:4], bad_hash)
        with open(os.path.join(worktree_dir, "bad.txt"), "wb") as file:
            file.write(bad_content)

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                CREATE TRIGGER fail_bad_worktree_write
                BEFORE INSERT ON events
                WHEN NEW.kind = 'write'
                  AND NEW.workspace_id = 'ws:coder'
                  AND NEW.payload_json = 'bad.txt'
                BEGIN
                  SELECT RAISE(ABORT, 'forced bad worktree write');
                END
                """
            )

        before = table_counts(db_path)
        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_atomic_worktree_fail",
            )
            assert False, "worktree commit should fail on injected late write"
        except Exception as exc:
            assert "forced bad worktree write" in str(exc)

        assert table_counts(db_path) == before
        assert fs.get_ref("ws:coder/a.txt")[1] == original_node
        try:
            fs.get_ref("ws:coder/bad.txt")
            assert False, "failed worktree commit should not create bad.txt ref"
        except anfs_core.RefNotFoundError:
            pass
        assert fs.events(kind="worktree_commit") == []
        assert fs.verify_integrity() == []
        assert os.path.exists(bad_object_path)

        plan_rows = {row[0]: row for row in fs.compaction_plan()}
        assert plan_rows["orphan_object_files"][1] == 1
        assert plan_rows["orphan_object_bytes"][1] == len(bad_content)

        dry_candidates, dry_bytes, dry_removed, dry_run = fs.clean_orphan_objects()
        assert (dry_candidates, dry_bytes, dry_removed, dry_run) == (
            1,
            len(bad_content),
            0,
            True,
        )
        assert os.path.exists(bad_object_path)

        run_candidates, run_bytes, removed, ran_dry = fs.clean_orphan_objects(
            dry_run=False
        )
        assert (run_candidates, run_bytes, removed, ran_dry) == (
            1,
            len(bad_content),
            1,
            False,
        )
        assert not os.path.exists(bad_object_path)
        assert fs.clean_orphan_objects() == (0, 0, 0, True)

        with sqlite3.connect(db_path) as conn:
            conn.execute("DROP TRIGGER fail_bad_worktree_write")

        committed = fs.commit_worktree(
            "ws:coder",
            worktree_dir,
            "coder_agent",
            tool_call_id="tc_atomic_worktree_success",
        )
        statuses = {path: status for path, status, _node_id in committed}
        assert statuses == {"a.txt": "modified", "bad.txt": "added"}
        assert bytes(fs.read_node(fs.get_ref("ws:coder/a.txt")[1])) == b"new\n"
        assert bytes(fs.read_node(fs.get_ref("ws:coder/bad.txt")[1])) == bad_content
        assert len(fs.events(kind="worktree_commit")) == 1
        assert fs.verify_integrity() == []


def test_worktree_checked_commit_rejects_stale_materialized_base():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        importer = fs.open_workspace("ws:importer", "importer_agent")
        app_v1 = importer.write("app.py", b"print('v1')\n", [])
        importer.publish("app.py", "resource:repo/main@v1/app.py", "resource")
        fs.checkout("ws:coder", "coder_agent", base="resource:repo/main@v1")

        missing_rows = {
            row[0]: row
            for row in fs.worktree_readiness(
                "ws:coder",
                os.path.join(tmpdir, "missing-worktree"),
            )
        }
        assert missing_rows["input_dir"][1] is False
        assert missing_rows["checked_commit_ready"][1] is False

        fs.materialize_workspace("ws:coder", worktree_dir)
        ready_rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", worktree_dir)}
        assert ready_rows["input_dir"][1] is True
        assert ready_rows["manifest_lease"][1] is True
        assert ready_rows["local_changes"][2] == "added=0 modified=0 deleted=0 unchanged=1"
        assert ready_rows["checked_commit_ready"][1] is True

        with open(os.path.join(worktree_dir, "app.py"), "wb") as file:
            file.write(b"print('local edit')\n")
        edited_rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", worktree_dir)}
        assert edited_rows["manifest_lease"][1] is True
        assert edited_rows["local_changes"][2] == "added=0 modified=1 deleted=0 unchanged=0"
        assert edited_rows["checked_commit_ready"][1] is True

        live_workspace = fs.open_workspace("ws:coder", "other_agent")
        app_external = live_workspace.write("app.py", b"print('external')\n", [])
        stale_rows = {row[0]: row for row in fs.worktree_readiness("ws:coder", worktree_dir)}
        assert stale_rows["manifest_lease"][1] is False
        assert "worktree base for app.py is stale" in stale_rows["manifest_lease"][2]
        assert stale_rows["local_changes"][2] == "added=0 modified=1 deleted=0 unchanged=0"
        assert stale_rows["checked_commit_ready"][1] is False

        try:
            fs.commit_worktree(
                "ws:coder",
                worktree_dir,
                "coder_agent",
                tool_call_id="tc_stale_checked_commit",
                require_manifest_match=True,
            )
            assert False, "checked worktree commit should reject a stale base"
        except anfs_core.RefConflictError as exc:
            assert "worktree base for app.py is stale" in str(exc)
        assert bytes(live_workspace.read("app.py")) == b"print('external')\n"
        assert fs.events(kind="worktree_commit", tool_call_id="tc_stale_checked_commit") == []

        fs.materialize_workspace("ws:coder", worktree_dir, overwrite=True)
        with open(os.path.join(worktree_dir, "app.py"), "wb") as file:
            file.write(b"print('fresh edit')\n")
        committed = fs.commit_worktree(
            "ws:coder",
            worktree_dir,
            "coder_agent",
            tool_call_id="tc_fresh_checked_commit",
            require_manifest_match=True,
        )
        statuses = {path: (status, node_id) for path, status, node_id in committed}
        assert statuses["app.py"][0] == "modified"
        app_fresh = statuses["app.py"][1]
        assert bytes(fs.read_node(app_fresh)) == b"print('fresh edit')\n"
        assert app_external in fs.lineage_nodes(app_fresh, "ancestors")

        worktree_commit_event = fs.event(
            fs.events(kind="worktree_commit", tool_call_id="tc_fresh_checked_commit")[0][1]
        )
        payload = json.loads(worktree_commit_event[6])
        assert payload["require_manifest_match"] is True
        assert fs.verify_integrity() == []
