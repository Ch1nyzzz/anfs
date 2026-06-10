"""POSIX-style facade: stat/chmod/chown/utime/access/locks and path edits."""

import json
import os
import sqlite3
import tempfile

import anfs_core


def test_workspace_delete_is_logical_ref_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        ws.write("scratch.txt", b"temporary", [])
        ws.delete("scratch.txt")

        ref_info = fs.get_ref("ws:coder/scratch.txt")
        assert ref_info[3] == "deleted"

        with sqlite3.connect(db_path) as conn:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            ref_events = conn.execute(
                "SELECT COUNT(*) FROM ref_events WHERE ref_name = ?",
                ("ws:coder/scratch.txt",),
            ).fetchone()[0]

        assert node_count == 1
        assert ref_events == 2


def test_consume_event_records_ref_version_and_state():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        producer_ws = fs.open_workspace("ws:producer", "producer_agent")
        consumer_ws = fs.open_workspace(
            "ws:consumer",
            "consumer_agent",
            run_id="run:consume",
        )

        node_id = producer_ws.write("artifact.txt", b"payload", [])
        producer_ws.publish("artifact.txt", "artifact:payload@v1")
        ref_before_consume = fs.get_ref("artifact:payload@v1")

        consumed_node = consumer_ws.consume(
            "artifact:payload@v1",
            purpose="test input",
            tool_call_id="tc_consume_payload",
        )
        assert consumed_node == node_id
        fs.archive_ref("artifact:payload@v1", "retention_agent")

        consume_event = fs.event(fs.events(kind="consume", run_id="run:consume")[0][1])
        assert consume_event[1] == "consume"
        assert consume_event[2] == "consumer_agent"
        assert consume_event[4] == "tc_consume_payload"
        assert consume_event[5] == "ws:consumer"
        assert json.loads(consume_event[6]) == {
            "ref_name": "artifact:payload@v1",
            "node_id": node_id,
            "state": "published",
            "ref_version": ref_before_consume[4],
            "purpose": "test input",
        }
        assert consume_event[8] == [
            (
                "input",
                node_id,
                "consumed",
                "artifact:payload@v1",
            )
        ]
        assert fs.get_ref("artifact:payload@v1")[3] == "archived"


def test_posix_facade_cp_mv_and_rm_preserve_ref_audit():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:posix-mutate")

        source_node = ws.write("a.txt", b"copy me", [], tool_call_id="tc_write_a")
        copied_node = ws.cp("a.txt", "b.txt", tool_call_id="tc_cp")
        assert copied_node == source_node
        assert bytes(ws.read("b.txt")) == b"copy me"

        ws.mkdir("dest", tool_call_id="tc_mkdir_dest")
        copied_into_dir = ws.cp("a.txt", "dest", tool_call_id="tc_cp_into_dir")
        assert copied_into_dir == source_node
        assert bytes(ws.read("dest/a.txt")) == b"copy me"

        ws.mkdir("move-dest", tool_call_id="tc_mkdir_move_dest")
        moved_into_dir = ws.mv(
            "dest/a.txt", "move-dest", tool_call_id="tc_mv_into_dir"
        )
        assert moved_into_dir == source_node
        assert bytes(ws.read("move-dest/a.txt")) == b"copy me"
        assert fs.get_ref("ws:coder/dest/a.txt")[3] == "deleted"

        moved_node = ws.mv("b.txt", "c.txt", tool_call_id="tc_mv")
        assert moved_node == source_node
        assert bytes(ws.read("c.txt")) == b"copy me"
        assert fs.get_ref("ws:coder/b.txt")[3] == "deleted"
        assert not ws.exists("b.txt")
        assert not ws.is_file("b.txt")
        assert not ws.is_dir("b.txt")

        ranged_node = ws.write_range(
            "c.txt", 5, b"range", tool_call_id="tc_write_range"
        )
        assert ranged_node != source_node
        assert bytes(ws.read("c.txt")) == b"copy range"
        assert bytes(ws.read("a.txt")) == b"copy me"
        appended_node = ws.write_range("c.txt", len(b"copy range"), b"\n")
        assert appended_node != ranged_node
        assert bytes(ws.read("c.txt")) == b"copy range\n"
        zero_fill_offset = len(b"copy range\n") + 3
        zero_filled_node = ws.write_range(
            "c.txt", zero_fill_offset, b"x", tool_call_id="tc_write_range_zero_fill"
        )
        assert zero_filled_node != appended_node
        assert bytes(ws.read("c.txt")) == b"copy range\n\0\0\0x"
        ws.mkdir("dir")
        try:
            ws.write_range("dir", 0, b"x")
            assert False, "write_range should reject directory paths"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.append("dir", b"x")
            assert False, "append should reject directory paths"
        except anfs_core.PolicyDeniedError:
            pass
        range_write_event = fs.event(
            fs.events(
                kind="write_range",
                run_id="run:posix-mutate",
                tool_call_id="tc_write_range",
            )[0][1]
        )
        range_write_payload = json.loads(range_write_event[6])
        assert range_write_payload["path"] == "c.txt"
        assert range_write_payload["ref_name"] == "ws:coder/c.txt"
        assert range_write_payload["old_node_id"] == source_node
        assert range_write_payload["offset"] == 5
        assert range_write_payload["write_length"] == 5
        assert range_write_payload["old_length"] == len(b"copy me")
        assert range_write_payload["new_length"] == len(b"copy range")
        assert set(range_write_event[8]) == {
            ("input", source_node, "source", None),
            ("output", ranged_node, "result", "c.txt"),
        }
        zero_fill_event = fs.event(
            fs.events(
                kind="write_range",
                run_id="run:posix-mutate",
                tool_call_id="tc_write_range_zero_fill",
            )[0][1]
        )
        zero_fill_payload = json.loads(zero_fill_event[6])
        assert zero_fill_payload["path"] == "c.txt"
        assert zero_fill_payload["ref_name"] == "ws:coder/c.txt"
        assert zero_fill_payload["old_node_id"] == appended_node
        assert zero_fill_payload["offset"] == zero_fill_offset
        assert zero_fill_payload["write_length"] == 1
        assert zero_fill_payload["zero_fill_length"] == 3
        assert zero_fill_payload["old_length"] == len(b"copy range\n")
        assert zero_fill_payload["new_length"] == len(b"copy range\n\0\0\0x")
        assert set(zero_fill_event[8]) == {
            ("input", appended_node, "source", None),
            ("output", zero_filled_node, "result", "c.txt"),
        }
        first_log_node = ws.append("log.txt", b"first", tool_call_id="tc_append_first")
        assert bytes(ws.read("log.txt")) == b"first"
        second_log_node = ws.append("log.txt", b"\nsecond", tool_call_id="tc_append_second")
        assert second_log_node != first_log_node
        assert bytes(ws.read("log.txt")) == b"first\nsecond"
        append_event = fs.event(
            fs.events(
                kind="append",
                run_id="run:posix-mutate",
                tool_call_id="tc_append_second",
            )[0][1]
        )
        append_payload = json.loads(append_event[6])
        assert append_payload["path"] == "log.txt"
        assert append_payload["ref_name"] == "ws:coder/log.txt"
        assert append_payload["old_node_id"] == first_log_node
        assert append_payload["append_length"] == len(b"\nsecond")
        assert append_payload["old_length"] == len(b"first")
        assert append_payload["new_length"] == len(b"first\nsecond")
        assert set(append_event[8]) == {
            ("input", first_log_node, "source", None),
            ("output", second_log_node, "result", "log.txt"),
        }
        no_op_truncate_node = ws.truncate(
            "log.txt", len(b"first\nsecond"), tool_call_id="tc_truncate_noop"
        )
        assert no_op_truncate_node == second_log_node
        assert (
            fs.events(
                kind="truncate",
                run_id="run:posix-mutate",
                tool_call_id="tc_truncate_noop",
            )
            == []
        )
        shrink_log_node = ws.truncate(
            "log.txt", len(b"first"), tool_call_id="tc_truncate_shrink"
        )
        assert shrink_log_node != second_log_node
        assert bytes(ws.read("log.txt")) == b"first"
        extend_log_node = ws.truncate(
            "log.txt", len(b"first\0\0\0"), tool_call_id="tc_truncate_extend"
        )
        assert extend_log_node != shrink_log_node
        assert bytes(ws.read("log.txt")) == b"first\0\0\0"
        try:
            ws.truncate("missing.txt", 0)
            assert False, "truncate should reject missing paths"
        except anfs_core.RefNotFoundError:
            pass
        try:
            ws.truncate("dir", 0)
            assert False, "truncate should reject directory paths"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.truncate("log.txt", -1)
            assert False, "truncate should reject negative lengths"
        except anfs_core.PolicyDeniedError:
            pass
        truncate_event = fs.event(
            fs.events(
                kind="truncate",
                run_id="run:posix-mutate",
                tool_call_id="tc_truncate_shrink",
            )[0][1]
        )
        truncate_payload = json.loads(truncate_event[6])
        assert truncate_payload["path"] == "log.txt"
        assert truncate_payload["ref_name"] == "ws:coder/log.txt"
        assert truncate_payload["old_node_id"] == second_log_node
        assert truncate_payload["old_length"] == len(b"first\nsecond")
        assert truncate_payload["new_length"] == len(b"first")
        assert set(truncate_event[8]) == {
            ("input", second_log_node, "source", None),
            ("output", shrink_log_node, "result", "log.txt"),
        }

        try:
            ws.read("b.txt")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("mv source path should be logically deleted")

        ws.rm("c.txt", tool_call_id="tc_rm")
        assert fs.get_ref("ws:coder/c.txt")[3] == "deleted"

        ws.mkdir("sticky", tool_call_id="tc_mkdir_sticky")
        ws.chown("sticky", 100, 100)
        ws.chmod("sticky", 0o1777, tool_call_id="tc_chmod_sticky")
        owned_by_other = ws.write("sticky/owned-by-other.txt", b"other", [])
        ws.chown("sticky/owned-by-other.txt", 300, 300)
        try:
            ws.rm("sticky/owned-by-other.txt", uid=200, gid=200)
            assert False, "sticky directory should block non-owner deletion"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(ws.read("sticky/owned-by-other.txt")) == b"other"
        ws.rm("sticky/owned-by-other.txt", uid=300, gid=300, tool_call_id="tc_rm_sticky_owner")
        assert fs.get_ref("ws:coder/sticky/owned-by-other.txt")[3] == "deleted"

        source_for_move = ws.write("move-source.txt", b"move source", [])
        protected_target = ws.write("sticky/protected-target.txt", b"protected", [])
        ws.chown("sticky/protected-target.txt", 300, 300)
        try:
            ws.mv("move-source.txt", "sticky/protected-target.txt", uid=200, gid=200)
            assert False, "sticky directory should block non-owner replacement"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(ws.read("move-source.txt")) == b"move source"
        assert bytes(ws.read("sticky/protected-target.txt")) == b"protected"
        moved_into_sticky = ws.mv(
            "move-source.txt",
            "sticky/protected-target.txt",
            uid=100,
            gid=100,
            tool_call_id="tc_mv_sticky_parent_owner",
        )
        assert moved_into_sticky == source_for_move
        assert bytes(ws.read("sticky/protected-target.txt")) == b"move source"
        assert fs.get_ref("ws:coder/move-source.txt")[3] == "deleted"
        assert protected_target != source_for_move

        assert [row[2] for row in fs.ref_history("ws:coder/b.txt")] == [
            "write",
            "delete_ref",
        ]
        assert [row[2] for row in fs.ref_history("ws:coder/c.txt")] == [
            "write",
            "write_range",
            "write_range",
            "write_range",
            "delete_ref",
        ]
        assert [row[2] for row in fs.ref_history("ws:coder/log.txt")] == [
            "append",
            "append",
            "truncate",
            "truncate",
        ]
        assert fs.verify_integrity() == []


def test_posix_facade_directory_cp_mv_and_rm_are_recursive():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:posix-tree")

        assert ws.mkdir("src", tool_call_id="tc_mkdir_src") == "src/"
        assert ws.mkdir("src/nested", tool_call_id="tc_mkdir_nested") == "src/nested/"
        src_dir_node = ws.stat("src")[3]
        nested_dir_node = ws.stat("src/nested")[3]
        app_node = ws.write("src/app.py", b"needle in app\n", [], tool_call_id="tc_write_app")
        cfg_node = ws.write(
            "src/nested/config.txt",
            b"nested needle\n",
            [],
            tool_call_id="tc_write_cfg",
        )

        copied_root = ws.cp("src", "copy", tool_call_id="tc_cp_dir")
        assert copied_root == src_dir_node
        assert bytes(ws.read("copy/app.py")) == b"needle in app\n"
        assert bytes(ws.read("copy/nested/config.txt")) == b"nested needle\n"
        assert ws.stat("copy")[1] == "directory"
        assert ws.stat("copy/nested")[3] == nested_dir_node
        assert ws.stat("copy/app.py")[3] == app_node
        assert ws.stat("copy/nested/config.txt")[3] == cfg_node

        ws.mkdir("archives", tool_call_id="tc_mkdir_archives")
        copied_under_existing_dir = ws.cp(
            "src", "archives", tool_call_id="tc_cp_dir_into_existing_dir"
        )
        assert copied_under_existing_dir == src_dir_node
        assert bytes(ws.read("archives/src/app.py")) == b"needle in app\n"
        assert bytes(ws.read("archives/src/nested/config.txt")) == b"nested needle\n"
        assert ws.stat("archives/src")[3] == src_dir_node

        ws.mkdir("moves", tool_call_id="tc_mkdir_moves")
        moved_under_existing_dir = ws.mv(
            "archives/src", "moves", tool_call_id="tc_mv_dir_into_existing_dir"
        )
        assert moved_under_existing_dir == src_dir_node
        assert bytes(ws.read("moves/src/app.py")) == b"needle in app\n"
        assert bytes(ws.read("moves/src/nested/config.txt")) == b"nested needle\n"
        assert fs.get_ref("ws:coder/archives/src/")[3] == "deleted"
        assert fs.get_ref("ws:coder/archives/src/app.py")[3] == "deleted"

        copy_grep = ws.grep("needle", "copy", tool_call_id="tc_grep_copy")
        assert {
            (path, line, text, node_id)
            for path, line, text, node_id, _ref_name in copy_grep
        } == {
            ("copy/app.py", 1, "needle in app", app_node),
            ("copy/nested/config.txt", 1, "nested needle", cfg_node),
        }

        moved_root = ws.mv("copy", "moved", tool_call_id="tc_mv_dir")
        assert moved_root == src_dir_node
        assert bytes(ws.read("moved/app.py")) == b"needle in app\n"
        assert bytes(ws.read("moved/nested/config.txt")) == b"nested needle\n"

        for ref_name in [
            "ws:coder/copy/",
            "ws:coder/copy/app.py",
            "ws:coder/copy/nested/",
            "ws:coder/copy/nested/config.txt",
        ]:
            assert fs.get_ref(ref_name)[3] == "deleted"

        try:
            ws.mv("moved", "moved/child", tool_call_id="tc_bad_mv")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("directory mv into its own subtree must be rejected")

        ws.rm("moved", tool_call_id="tc_rm_dir")
        for ref_name in [
            "ws:coder/moved/",
            "ws:coder/moved/app.py",
            "ws:coder/moved/nested/",
            "ws:coder/moved/nested/config.txt",
        ]:
            assert fs.get_ref(ref_name)[3] == "deleted"

        assert fs.verify_integrity() == []
