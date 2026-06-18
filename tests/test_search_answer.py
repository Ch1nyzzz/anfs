"""FTS search, structured query, embeddings, and answer/token/cost auditing."""

import hashlib
import json
import os
import sqlite3
import tempfile

import anfs_core


def test_vector_embedding_projection_searches_visible_refs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        contract_node = writer.write("contract.md", b"contract renewal terms", [])
        writer.publish("contract.md", "artifact:contract@v1")
        support_node = writer.write("support.md", b"support ticket", [])
        writer.publish("support.md", "artifact:support@v1")
        ignored_node = writer.write("ignored.md", b"ignored dimensions", [])
        writer.publish("ignored.md", "artifact:ignored@v1")

        fs.set_node_embedding(contract_node, "toy-embedding", [1.0, 0.0])
        fs.set_node_embedding(support_node, "toy-embedding", [0.0, 1.0])
        fs.set_node_embedding(ignored_node, "toy-embedding", [1.0, 0.0, 0.0])
        assert fs.node_embedding(contract_node, "toy-embedding") == [1.0, 0.0]
        fs.set_node_embedding(contract_node, "toy-embedding", [0.8, 0.2])
        assert fs.node_embedding(contract_node, "toy-embedding") == [0.8, 0.2]

        results = fs.vector_search("toy-embedding", [1.0, 0.0], limit=10)
        assert [row[0] for row in results] == [
            "artifact:contract@v1",
            "artifact:support@v1",
        ]
        assert results[0][1] == contract_node
        assert results[0][2] > results[1][2]

        fs.set_policy_label(
            "node",
            contract_node,
            "sensitivity",
            "restricted",
            "policy_agent",
        )
        assert [
            row[0]
            for row in fs.vector_search(
                "toy-embedding",
                [1.0, 0.0],
                policy_label_excludes=["sensitivity"],
            )
        ] == ["artifact:support@v1"]
        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert [row[0] for row in fs.vector_search("toy-embedding", [1.0, 0.0])] == [
            "artifact:support@v1"
        ]

        try:
            fs.set_node_embedding(contract_node, "toy-embedding", [0.0, 0.0])
            assert False, "zero-norm embeddings should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.vector_search("toy-embedding", [], limit=10)
            assert False, "empty query embeddings should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_chunk_embedding_projection_searches_visible_chunk_ranges():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        content = b"alpha000beta1111gamma222"
        node_id = writer.write("large.md", content, [])
        writer.publish("large.md", "artifact:large@v1")
        chunk_size = 8

        try:
            fs.set_node_chunk_embedding(node_id, chunk_size, 0, "toy-embedding", [1.0, 0.0])
            assert False, "chunk embeddings should require cached chunk rows"
        except anfs_core.PolicyDeniedError:
            pass

        chunks = fs.cache_node_chunks(node_id, chunk_size)
        assert chunks == [
            (0, 0, 8, hashlib.sha256(content[0:8]).hexdigest()),
            (1, 8, 8, hashlib.sha256(content[8:16]).hexdigest()),
            (2, 16, 8, hashlib.sha256(content[16:24]).hexdigest()),
        ]
        fs.set_node_chunk_embedding(node_id, chunk_size, 0, "toy-embedding", [1.0, 0.0])
        fs.set_node_chunk_embedding(node_id, chunk_size, 1, "toy-embedding", [0.0, 1.0])
        fs.set_node_chunk_embedding(node_id, chunk_size, 2, "toy-embedding", [1.0, 0.0, 0.0])
        assert fs.node_chunk_embedding(node_id, chunk_size, 0, "toy-embedding") == [1.0, 0.0]

        results = fs.vector_search_chunks("toy-embedding", [1.0, 0.0], limit=10)
        assert [(row[0], row[2], row[3], row[4]) for row in results] == [
            ("artifact:large@v1", 0, 0, 8),
            ("artifact:large@v1", 1, 8, 8),
        ]
        assert results[0][5] > results[1][5]

        fs.set_fragment_policy_label(
            node_id,
            0,
            5,
            "sensitivity",
            "restricted",
            "policy_agent",
        )
        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert [
            (row[0], row[2], row[3], row[4])
            for row in fs.vector_search_chunks("toy-embedding", [1.0, 0.0], limit=10)
        ] == [("artifact:large@v1", 1, 8, 8)]

        fs.set_policy_label("node", node_id, "exportability", "internal", "policy_agent")
        assert [
            row[0]
            for row in fs.vector_search_chunks(
                "toy-embedding",
                [1.0, 0.0],
                limit=10,
                policy_label_excludes=["exportability"],
            )
        ] == []

        try:
            fs.vector_search_chunks("toy-embedding", [], limit=10)
            assert False, "empty chunk query embeddings should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_unified_query_api_filters_current_refs():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder = fs.open_workspace("ws:coder", "coder_agent", run_id="run:coder")
        tester = fs.open_workspace("ws:tester", "tester_agent", run_id="run:tester")

        marker = "critical_query_needle"
        patch_node = coder.write(
            "patch.md",
            f"patch body {marker}".encode("utf-8"),
            [],
            tool_call_id="tc_query_write",
        )
        coder.publish("patch.md", "artifact:query-patch@v1")
        evidence_node = tester.write(
            "test.md",
            b"PASS for critical_query_needle",
            [patch_node],
            tool_call_id="tc_query_test",
        )
        tester.publish("test.md", "artifact:query-test@v1")
        fs.approve("artifact:query-patch@v1", ["artifact:query-test@v1"], "reviewer")

        all_query_refs = fs.query(prefix="artifact:query-")
        assert {row[0] for row in all_query_refs} == {
            "artifact:query-patch@v1",
            "artifact:query-test@v1",
        }

        text_refs = fs.query(text=marker, media_type="text/plain")
        text_ref_names = {row[0] for row in text_refs}
        assert "artifact:query-patch@v1" in text_ref_names
        assert "ws:coder/patch.md" in text_ref_names
        patch_row = next(row for row in text_refs if row[0] == "artifact:query-patch@v1")
        assert patch_row[1] == patch_node
        assert patch_row[3] == "approved"
        assert patch_row[5] == "artifact"
        assert patch_row[6] == "text/plain"
        assert patch_row[8] is not None
        assert patch_row[9] == "approve"
        assert marker in patch_row[10]

        assert [row[0] for row in fs.query(state="approved")] == [
            "artifact:query-patch@v1"
        ]
        assert "artifact:query-test@v1" in {
            row[0] for row in fs.query(agent_id="tester_agent")
        }
        assert "artifact:query-test@v1" in {row[0] for row in fs.query(run_id="run:tester")}
        assert "artifact:query-patch@v1" in {
            row[0] for row in fs.query(event_kind="approve")
        }
        assert [row[0] for row in fs.query(policy="lineage_approval", decision="allow")] == [
            "artifact:query-patch@v1"
        ]
        assert [
            row[0]
            for row in fs.query(reason_code="lineage_evidence_covers_target")
        ] == ["artifact:query-patch@v1"]
        assert "artifact:query-patch@v1" in {
            row[0] for row in fs.query(created_after_ms=patch_row[7])
        }

        try:
            fs.query(limit=0)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid query limit should be rejected")

        try:
            fs.query(created_after_ms=10, created_before_ms=1)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("invalid query time range should be rejected")


def test_workspace_query_records_context_event_with_result_edges():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        writer.write("policy.md", b"Refund policy query audit marker", [])
        writer.publish("policy.md", "artifact:audited-query@v1")

        reader = fs.open_workspace("ws:reader", "reader_agent", run_id="run:reader")
        rows = reader.query(
            prefix="artifact:audited-",
            text="marker",
            state="published",
            tool_call_id="tc_structured_query",
        )
        assert [row[0] for row in rows] == ["artifact:audited-query@v1"]
        assert rows[0][10] is not None
        assert "marker" in rows[0][10]

        query_events = fs.events(kind="query")
        assert len(query_events) == 1
        assert query_events[0][3] == "reader_agent"
        assert query_events[0][4] == "ws:reader"
        assert query_events[0][6:] == (1, 0)
        query_event = fs.event(query_events[0][1])
        payload = json.loads(query_event[6])
        assert payload["prefix"] == "artifact:audited-"
        assert payload["text"] == "marker"
        assert payload["state"] == "published"
        assert payload["result_count"] == 1
        assert payload["results"][0]["ref_name"] == "artifact:audited-query@v1"
        assert payload["results"][0]["node_id"] == rows[0][1]
        assert query_event[3] == "run:reader"
        assert query_event[4] == "tc_structured_query"
        assert query_event[8] == [
            (
                "input",
                rows[0][1],
                "query_result:0",
                "artifact:audited-query@v1",
            )
        ]
        assert [row[0] for row in fs.events(tool_call_id="tc_structured_query")] == [
            query_events[0][0]
        ]


def test_search_records_context_event_with_result_edges():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer_ws = fs.open_workspace("ws:writer", "writer_agent")
        search_ws = fs.open_workspace(
            "ws:searcher",
            "search_agent",
            run_id="run:search",
        )

        policy_node = writer_ws.write(
            "refund.md",
            b"Refund policy: database pool refunds are reviewed weekly.",
            [],
        )
        writer_ws.publish("refund.md", "artifact:refund_policy@v1")
        policy_ref = fs.get_ref("artifact:refund_policy@v1")

        results = search_ws.search("refund", "published")
        assert policy_node in {node_id for node_id, _snippet in results}

        search_events = fs.events(kind="search", run_id="run:search")
        assert len(search_events) == 1
        assert search_events[0][6:] == (1, 0)

        event = fs.event(search_events[0][1])
        assert event[1] == "search"
        assert event[2] == "search_agent"
        assert event[3] == "run:search"
        assert event[5] == "ws:searcher"
        payload = json.loads(event[6])
        assert payload == {
            "query": "refund",
            "scope": "published",
            "policy_label_excludes": [],
            "purpose": None,
            "result_count": 1,
            "results": [
                {
                    "ref_name": "artifact:refund_policy@v1",
                    "node_id": policy_node,
                    "state": "published",
                    "ref_version": policy_ref[4],
                }
            ],
        }
        assert event[8] == [
            (
                "input",
                policy_node,
                "search_result:0",
                "artifact:refund_policy@v1",
            )
        ]


def test_draft_search_is_scoped_to_current_workspace():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        alpha_ws = fs.open_workspace("ws:alpha", "alpha_agent", run_id="run:alpha")
        beta_ws = fs.open_workspace("ws:beta", "beta_agent", run_id="run:beta")

        alpha_node = alpha_ws.write(
            "notes.txt",
            b"private draft needle_unique belongs to alpha",
            [],
        )
        beta_node = beta_ws.write(
            "notes.txt",
            b"private draft needle_unique belongs to beta",
            [],
        )

        alpha_results = alpha_ws.search("needle_unique", "draft", tool_call_id="tc_alpha_search")
        beta_results = beta_ws.search("needle_unique", "draft", tool_call_id="tc_beta_search")

        assert {node_id for node_id, _snippet in alpha_results} == {alpha_node}
        assert {node_id for node_id, _snippet in beta_results} == {beta_node}

        alpha_event = fs.events(kind="search", run_id="run:alpha")[0]
        alpha_record = fs.event(alpha_event[1])
        assert alpha_record[4] == "tc_alpha_search"
        alpha_payload = json.loads(alpha_record[6])
        assert alpha_payload == {
            "query": "needle_unique",
            "scope": "draft",
            "policy_label_excludes": [],
            "purpose": None,
            "result_count": 1,
            "results": [
                {
                    "ref_name": "ws:alpha/notes.txt",
                    "node_id": alpha_node,
                    "state": "draft",
                    "ref_version": fs.get_ref("ws:alpha/notes.txt")[4],
                }
            ],
        }
        assert alpha_record[8] == [
            (
                "input",
                alpha_node,
                "search_result:0",
                "ws:alpha/notes.txt",
            )
        ]

        alpha_ws.publish("notes.txt", "artifact:alpha_notes@v1")
        beta_published_results = beta_ws.search("needle_unique", "published")
        assert {node_id for node_id, _snippet in beta_published_results} == {alpha_node}


def test_posix_facade_path_read_ls_stat_find_and_grep_are_audited():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:posix")

        assert ws.mkdir("src", tool_call_id="tc_mkdir") == "src/"
        empty_node = ws.touch("src/empty.txt", tool_call_id="tc_touch")
        assert bytes(ws.read("src/empty.txt")) == b""
        empty_stat = ws.stat("src/empty.txt")
        assert empty_stat[0] == "src/empty.txt"
        assert empty_stat[1] == "file"
        assert empty_stat[3] == empty_node
        assert empty_stat[7] == 0
        empty_ref_before = fs.get_ref("ws:coder/src/empty.txt")
        assert ws.touch("src/empty.txt", tool_call_id="tc_touch_existing") == empty_node
        assert fs.get_ref("ws:coder/src/empty.txt")[4] == empty_ref_before[4]
        assert fs.events(kind="write", tool_call_id="tc_touch_existing") == []
        try:
            ws.touch("src", tool_call_id="tc_touch_dir")
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("touch should reject directory paths")

        app_node = ws.write(
            "src/app.py",
            b"print('hello')\nneedle = 'found'\n",
            [],
            tool_call_id="tc_write",
        )
        app_content = b"print('hello')\nneedle = 'found'\n"
        ws.write("src/readme.md", b"notes only\n", [], tool_call_id="tc_write_notes")

        assert bytes(ws.read("src/app.py", purpose="inspect", tool_call_id="tc_read")) == (
            app_content
        )
        assert bytes(ws.cat("src/app.py")) == app_content
        needle_offset = app_content.index(b"needle")
        assert bytes(
            ws.read_range("src/app.py", needle_offset, 6, tool_call_id="tc_read_range")
        ) == b"needle"
        assert bytes(ws.read_range("src/app.py", len(app_content) + 10, 8)) == b""

        other_ws = fs.open_workspace("ws:coder", "other_agent", run_id="run:other")
        locked_node = ws.write("src/locked.txt", b"locked v1\n", [])
        lock_id = ws.acquire_lock(
            "src/locked.txt", ttl_ms=60_000, tool_call_id="tc_lock_file"
        )
        lock_rows = ws.lock_info("src/locked.txt")
        assert lock_rows == [
            (
                "src/locked.txt",
                lock_id,
                "exclusive",
                "coder_agent",
                "run:posix",
                lock_rows[0][5],
                lock_rows[0][6],
            )
        ]
        assert lock_rows[0][5] > 0
        assert lock_rows[0][6] >= lock_rows[0][5]
        try:
            other_ws.write("src/locked.txt", b"blocked by lock\n", [])
        except anfs_core.PolicyDeniedError as exc:
            assert "exclusive lock" in str(exc)
        else:
            raise AssertionError("exclusive path lock should block another agent write")
        same_owner_node = ws.write("src/locked.txt", b"locked v2\n", [locked_node])
        assert same_owner_node != locked_node
        try:
            other_ws.release_lock("src/locked.txt", lock_id)
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("exclusive path lock should not be releasable by another agent")
        assert ws.release_lock(
            "src/locked.txt", lock_id, tool_call_id="tc_release_file_lock"
        )
        assert not ws.release_lock("src/locked.txt", lock_id)
        other_ws.write("src/locked.txt", b"other after release\n", [])

        dir_lock_id = ws.acquire_lock("src", ttl_ms=60_000, tool_call_id="tc_lock_dir")
        try:
            other_ws.touch("src/blocked-by-dir-lock.txt")
        except anfs_core.PolicyDeniedError as exc:
            assert "exclusive lock" in str(exc)
        else:
            raise AssertionError("exclusive directory lock should block child creation")
        assert ws.release_lock("src", dir_lock_id, tool_call_id="tc_release_dir_lock")
        other_ws.touch("src/blocked-by-dir-lock.txt")
        lock_event = fs.event(
            fs.events(kind="acquire_lock", run_id="run:posix", tool_call_id="tc_lock_file")[
                0
            ][1]
        )
        assert json.loads(lock_event[6])["lock_id"] == lock_id

        root_entries = {entry[0]: entry for entry in ws.ls("")}
        assert root_entries["src"][1] == "directory"
        assert ws.exists("")
        assert ws.is_dir("")
        assert not ws.is_file("")
        assert ws.exists("src")
        assert ws.is_dir("src")
        assert not ws.is_file("src")

        src_entries = {entry[0]: entry for entry in ws.ls("src")}
        assert src_entries["app.py"][1] == "file"
        assert src_entries["app.py"][2] == "ws:coder/src/app.py"
        assert src_entries["app.py"][3] == app_node
        assert ws.exists("src/app.py")
        assert ws.is_file("src/app.py")
        assert not ws.is_dir("src/app.py")
        assert not ws.exists("src/missing.py")
        assert not ws.is_file("src/missing.py")
        assert not ws.is_dir("src/missing.py")

        dir_stat = ws.stat("src")
        assert dir_stat[0] == "src"
        assert dir_stat[1] == "directory"
        assert dir_stat[2] == "ws:coder/src/"
        root_posix_stat = ws.stat_posix("")
        assert root_posix_stat[0] == ""
        assert root_posix_stat[1] == "directory"
        assert root_posix_stat[8] == 0o040755
        assert root_posix_stat[9] == 2
        assert root_posix_stat[10] == 0
        assert root_posix_stat[11] == 0
        assert root_posix_stat[12] > 0
        assert root_posix_stat[14] == root_posix_stat[13]
        assert root_posix_stat[15] == root_posix_stat[13]
        dir_posix_stat = ws.stat_posix("src")
        assert dir_posix_stat[0] == "src"
        assert dir_posix_stat[1] == "directory"
        assert dir_posix_stat[2] == "ws:coder/src/"
        assert dir_posix_stat[8] == 0o040755
        assert dir_posix_stat[9] == 2

        file_stat = ws.stat("src/app.py")
        assert file_stat[0] == "src/app.py"
        assert file_stat[1] == "file"
        assert file_stat[2] == "ws:coder/src/app.py"
        assert file_stat[3] == app_node
        assert file_stat[4] == "draft"
        assert file_stat[7] == len(b"print('hello')\nneedle = 'found'\n")
        file_posix_stat = ws.stat_posix("src/app.py")
        assert file_posix_stat[:8] == file_stat
        assert file_posix_stat[8] == 0o100644
        assert file_posix_stat[9] == 1
        assert file_posix_stat[10] == 0
        assert file_posix_stat[11] == 0
        assert file_posix_stat[12] > 0
        assert file_posix_stat[13] > 0
        assert file_posix_stat[14] == file_posix_stat[13]
        assert file_posix_stat[15] == file_posix_stat[13]
        assert ws.access("src/app.py", 0)
        assert ws.access("src/app.py", 4)
        assert ws.access("src/app.py", 2)
        assert ws.access("src/app.py", 6)
        assert not ws.access("src/app.py", 1)
        assert ws.access("src", 1)
        assert ws.access("", 7)
        assert not ws.access("src/missing.py", 0)
        assert not ws.access("src/missing.py", 4)
        try:
            ws.access("src/app.py", 8)
            assert False, "access should reject unsupported mode bits"
        except anfs_core.PolicyDeniedError:
            pass
        ws.chmod("src/app.py", 0o755, tool_call_id="tc_chmod_exec")
        chmod_exec_stat = ws.stat_posix("src/app.py")
        assert chmod_exec_stat[8] == 0o100755
        assert chmod_exec_stat[14] == file_posix_stat[14]
        assert chmod_exec_stat[15] >= chmod_exec_stat[14]
        assert ws.access("src/app.py", 1)
        chmod_event = fs.event(
            fs.events(kind="chmod", run_id="run:posix", tool_call_id="tc_chmod_exec")[0][1]
        )
        chmod_payload = json.loads(chmod_event[6])
        assert chmod_payload["path"] == "src/app.py"
        assert chmod_payload["kind"] == "file"
        assert chmod_payload["old_mode"] == 0o644
        assert chmod_payload["new_mode"] == 0o755
        assert chmod_event[8] == [("input", app_node, "target", "src/app.py")]
        ws.chown("src/app.py", 501, 20, tool_call_id="tc_chown")
        chown_stat = ws.stat_posix("src/app.py")
        assert chown_stat[8] == 0o100755
        assert chown_stat[10] == 501
        assert chown_stat[11] == 20
        assert chown_stat[14] == chmod_exec_stat[14]
        assert chown_stat[15] >= chmod_exec_stat[15]
        chown_event = fs.event(
            fs.events(kind="chown", run_id="run:posix", tool_call_id="tc_chown")[0][1]
        )
        chown_payload = json.loads(chown_event[6])
        assert chown_payload["path"] == "src/app.py"
        assert chown_payload["kind"] == "file"
        assert chown_payload["old_uid"] == 0
        assert chown_payload["old_gid"] == 0
        assert chown_payload["new_uid"] == 501
        assert chown_payload["new_gid"] == 20
        assert chown_event[8] == [("input", app_node, "target", "src/app.py")]
        ws.chown("src/app.py", -1, 21)
        chown_preserve_uid_stat = ws.stat_posix("src/app.py")
        assert chown_preserve_uid_stat[10] == 501
        assert chown_preserve_uid_stat[11] == 21
        ws.utime("src/app.py", 111, 222, tool_call_id="tc_utime")
        utime_stat = ws.stat_posix("src/app.py")
        assert utime_stat[8] == 0o100755
        assert utime_stat[10] == 501
        assert utime_stat[11] == 21
        assert utime_stat[13] == 111
        assert utime_stat[14] == 222
        assert utime_stat[15] >= file_posix_stat[15]
        utime_event = fs.event(
            fs.events(kind="utime", run_id="run:posix", tool_call_id="tc_utime")[0][1]
        )
        utime_payload = json.loads(utime_event[6])
        assert utime_payload["path"] == "src/app.py"
        assert utime_payload["kind"] == "file"
        assert utime_payload["old_atime_ms"] == chmod_exec_stat[13]
        assert utime_payload["old_mtime_ms"] == chmod_exec_stat[14]
        assert utime_payload["new_atime_ms"] == 111
        assert utime_payload["new_mtime_ms"] == 222
        assert utime_event[8] == [("input", app_node, "target", "src/app.py")]
        ws.chmod("src/app.py", 0o600)
        chmod_after_utime_stat = ws.stat_posix("src/app.py")
        assert chmod_after_utime_stat[8] == 0o100600
        assert chmod_after_utime_stat[10] == 501
        assert chmod_after_utime_stat[11] == 21
        assert chmod_after_utime_stat[13] == 111
        assert chmod_after_utime_stat[14] == 222
        assert ws.access("src/app.py", 4)
        assert ws.access("src/app.py", 2)
        assert not ws.access("src/app.py", 1)
        ws.chmod("src/app.py", 0o640, tool_call_id="tc_chmod_group_access")
        assert ws.access("src/app.py", 4, 501, 999)
        assert ws.access("src/app.py", 2, 501, 999)
        assert ws.access("src/app.py", 4, 600, 21)
        assert ws.access("src/app.py", 4, 600, 999, [21])
        assert not ws.access("src/app.py", 2, 600, 21)
        assert not ws.access("src/app.py", 4, 600, 999)
        ws.chmod("src/app.py", 0o000, tool_call_id="tc_chmod_root_access")
        assert ws.access("src/app.py", 4, 0, 0)
        assert ws.access("src/app.py", 2, 0, 0)
        assert not ws.access("src/app.py", 1, 0, 0)
        ws.chmod("src/app.py", 0o001)
        assert ws.access("src/app.py", 1, 0, 0)
        ws.chmod("src/app.py", 0o640)
        try:
            ws.access("src/app.py", 4, -1, 21)
            assert False, "access should reject negative effective uid"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.access("src/app.py", 4, 501)
            assert False, "access should require uid and gid together"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.access("src/app.py", 4, 600, 999, [-1])
            assert False, "access should reject negative supplementary groups"
        except anfs_core.PolicyDeniedError:
            pass
        ws.chmod("src/app.py", 0o7640, tool_call_id="tc_chmod_special_bits")
        special_mode_stat = ws.stat_posix("src/app.py")
        assert special_mode_stat[8] == 0o107640
        assert ws.access("src/app.py", 4, 501, 999)
        assert ws.access("src/app.py", 2, 501, 999)
        assert ws.access("src/app.py", 4, 600, 21)
        assert not ws.access("src/app.py", 2, 600, 21)
        assert not ws.access("src/app.py", 1, 0, 0)
        special_mode_event = fs.event(
            fs.events(
                kind="chmod", run_id="run:posix", tool_call_id="tc_chmod_special_bits"
            )[0][1]
        )
        special_mode_payload = json.loads(special_mode_event[6])
        assert special_mode_payload["old_mode"] == 0o640
        assert special_mode_payload["new_mode"] == 0o7640
        ws.chmod("src/app.py", 0o600)
        ws.utime("src/app.py", tool_call_id="tc_utime_now")
        utime_now_stat = ws.stat_posix("src/app.py")
        assert utime_now_stat[13] >= chmod_after_utime_stat[15]
        assert utime_now_stat[14] == utime_now_stat[13]
        assert utime_now_stat[15] >= utime_now_stat[14]
        ws.chmod("src", 0o700, tool_call_id="tc_chmod_dir")
        assert ws.stat_posix("src")[8] == 0o040700
        assert ws.access("src", 1)
        ws.chmod("src", 0o3700, tool_call_id="tc_chmod_dir_special_bits")
        assert ws.stat_posix("src")[8] == 0o043700
        assert ws.access("src", 1)
        ws.chown("src", 1000, 100, tool_call_id="tc_chown_dir")
        dir_chown_stat = ws.stat_posix("src")
        assert dir_chown_stat[10] == 1000
        assert dir_chown_stat[11] == 100
        inherited_file_node = ws.write(
            "src/inherited.txt", b"inherited gid", [], tool_call_id="tc_write_setgid_file"
        )
        inherited_file_stat = ws.stat_posix("src/inherited.txt")
        assert inherited_file_stat[3] == inherited_file_node
        assert inherited_file_stat[8] == 0o100644
        assert inherited_file_stat[10] == 0
        assert inherited_file_stat[11] == 100
        ws.chown("src/inherited.txt", 2000, 200, tool_call_id="tc_chown_inherited_file")
        overwritten_inherited_node = ws.write(
            "src/inherited.txt",
            b"overwritten gid",
            [inherited_file_node],
            tool_call_id="tc_write_setgid_file_overwrite",
        )
        overwritten_inherited_stat = ws.stat_posix("src/inherited.txt")
        assert overwritten_inherited_stat[3] == overwritten_inherited_node
        assert overwritten_inherited_stat[10] == 2000
        assert overwritten_inherited_stat[11] == 200
        touched_inherited_node = ws.touch(
            "src/touched-inherited.txt", tool_call_id="tc_touch_setgid_file"
        )
        touched_inherited_stat = ws.stat_posix("src/touched-inherited.txt")
        assert touched_inherited_stat[3] == touched_inherited_node
        assert touched_inherited_stat[8] == 0o100644
        assert touched_inherited_stat[11] == 100
        ws.mkdir("src/setgid-child", tool_call_id="tc_mkdir_setgid_child")
        setgid_child_stat = ws.stat_posix("src/setgid-child")
        assert setgid_child_stat[8] == 0o042755
        assert setgid_child_stat[10] == 0
        assert setgid_child_stat[11] == 100
        ws.utime("src", 333, 444, tool_call_id="tc_utime_dir")
        dir_utime_stat = ws.stat_posix("src")
        assert dir_utime_stat[10] == 1000
        assert dir_utime_stat[11] == 100
        assert dir_utime_stat[13] == 333
        assert dir_utime_stat[14] == 444
        linked_node = ws.link(
            "src/app.py", "links/app-link.py", tool_call_id="tc_link_app"
        )
        assert linked_node == app_node
        assert bytes(ws.read("links/app-link.py")) == app_content
        linked_source_stat = ws.stat_posix("src/app.py")
        linked_path_stat = ws.stat_posix("links/app-link.py")
        assert linked_source_stat[3] == linked_path_stat[3] == app_node
        assert linked_source_stat[9] == linked_path_stat[9] == 2
        assert linked_source_stat[12] == linked_path_stat[12]
        assert linked_source_stat[8] == linked_path_stat[8] == 0o100600
        assert linked_source_stat[10] == linked_path_stat[10] == 501
        assert linked_source_stat[11] == linked_path_stat[11] == 21
        assert linked_source_stat[13] == linked_path_stat[13]
        assert linked_source_stat[14] == linked_path_stat[14]
        link_event = fs.event(
            fs.events(kind="link", run_id="run:posix", tool_call_id="tc_link_app")[0][1]
        )
        link_payload = json.loads(link_event[6])
        assert link_payload["source_ref"] == "ws:coder/src/app.py"
        assert link_payload["destination_ref"] == "ws:coder/links/app-link.py"
        assert link_payload["destination_path"] == "links/app-link.py"
        assert link_payload["node_id"] == app_node
        assert set(link_event[8]) == {
            ("input", app_node, "source", "ws:coder/src/app.py"),
            ("output", app_node, "result", "links/app-link.py"),
        }
        ws.chmod("links/app-link.py", 0o640, tool_call_id="tc_chmod_link")
        chmod_link_event = fs.event(
            fs.events(kind="chmod", run_id="run:posix", tool_call_id="tc_chmod_link")[0][1]
        )
        assert set(json.loads(chmod_link_event[6])["affected_paths"]) == {
            "src/app.py",
            "links/app-link.py",
        }
        assert ws.stat_posix("src/app.py")[8] == 0o100640
        assert ws.stat_posix("links/app-link.py")[8] == 0o100640
        ws.chown("src/app.py", 600, 30, tool_call_id="tc_chown_link")
        chown_link_event = fs.event(
            fs.events(kind="chown", run_id="run:posix", tool_call_id="tc_chown_link")[0][1]
        )
        assert set(json.loads(chown_link_event[6])["affected_paths"]) == {
            "src/app.py",
            "links/app-link.py",
        }
        assert ws.stat_posix("src/app.py")[10:12] == (600, 30)
        assert ws.stat_posix("links/app-link.py")[10:12] == (600, 30)
        ws.utime("links/app-link.py", 777, 888, tool_call_id="tc_utime_link")
        utime_link_event = fs.event(
            fs.events(kind="utime", run_id="run:posix", tool_call_id="tc_utime_link")[0][1]
        )
        assert set(json.loads(utime_link_event[6])["affected_paths"]) == {
            "src/app.py",
            "links/app-link.py",
        }
        assert ws.stat_posix("src/app.py")[13:15] == (777, 888)
        assert ws.stat_posix("links/app-link.py")[13:15] == (777, 888)
        relinked_node = ws.write_range("links/app-link.py", 0, b"PRINT")
        assert relinked_node != app_node
        assert bytes(ws.read("src/app.py")) == app_content
        assert bytes(ws.read("links/app-link.py")).startswith(b"PRINT")
        relinked_source_stat = ws.stat_posix("src/app.py")
        relinked_path_stat = ws.stat_posix("links/app-link.py")
        assert relinked_source_stat[9] == 1
        assert relinked_path_stat[9] == 1
        assert relinked_source_stat[12] != relinked_path_stat[12]
        try:
            ws.link("src/app.py", "src/readme.md")
            assert False, "link should reject existing destination paths"
        except anfs_core.RefConflictError:
            pass
        try:
            ws.link("src", "links/src-link")
            assert False, "link should reject directory sources"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.link("src/app.py", "artifact:repo/app-link.py")
            assert False, "link should reject explicit destination refs"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.chmod("src/app.py", 0o10000)
            assert False, "chmod should reject mode bits outside POSIX permissions"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.utime("src/app.py", -1, 0)
            assert False, "utime should reject negative timestamps"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.chown("src/app.py", -2, 0)
            assert False, "chown should reject unsupported uid values"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.chown("src/app.py", -1, -1)
            assert False, "chown should reject no-op ownership updates"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.chmod("artifact:repo/app.py", 0o644)
            assert False, "chmod should reject explicit refs"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.utime("artifact:repo/app.py", 1, 2)
            assert False, "utime should reject explicit refs"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.chown("artifact:repo/app.py", 1, 2)
            assert False, "chown should reject explicit refs"
        except anfs_core.PolicyDeniedError:
            pass

        found = ws.find("src", "*.py", tool_call_id="tc_find")
        assert found == [("src/app.py", "file", "ws:coder/src/app.py", app_node)]

        grep_results = ws.grep("needle", "src", tool_call_id="tc_grep")
        assert grep_results == [
            ("src/app.py", 2, "needle = 'found'", app_node, "ws:coder/src/app.py")
        ]

        search_events = [fs.event(row[1]) for row in fs.events(kind="search", run_id="run:posix")]
        payloads = [json.loads(event[6]) for event in search_events]
        assert [payload["tool"] for payload in payloads] == ["find", "grep"]
        assert payloads[0]["path"] == "src/"
        assert payloads[0]["result_count"] == 1
        assert payloads[1]["query"] == "needle"
        assert payloads[1]["result_count"] == 1
        assert search_events[1][8] == [
            ("input", app_node, "search_result:0", "ws:coder/src/app.py")
        ]

        read_event = fs.event(
            fs.events(kind="read_ref", run_id="run:posix", tool_call_id="tc_read")[0][1]
        )
        assert read_event[4] == "tc_read"
        assert json.loads(read_event[6])["ref_name"] == "ws:coder/src/app.py"

        range_event = fs.event(
            fs.events(kind="read_range", run_id="run:posix", tool_call_id="tc_read_range")[0][1]
        )
        range_payload = json.loads(range_event[6])
        assert range_event[4] == "tc_read_range"
        assert range_payload["path"] == "src/app.py"
        assert range_payload["ref_name"] == "ws:coder/src/app.py"
        assert range_payload["node_id"] == app_node
        assert range_payload["offset"] == needle_offset
        assert range_payload["length"] == 6
        assert range_payload["actual_length"] == 6
        assert range_event[8] == [
            ("input", app_node, "read_range", "ws:coder/src/app.py")
        ]

        fs.set_fragment_policy_label(
            app_node,
            needle_offset,
            6,
            "contains",
            "secret",
            "policy_agent",
        )
        fs.set_policy_rule(
            "contains",
            value="secret",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert not ws.access("src/app.py", 4)
        assert not ws.access("src/app.py", 4, 0, 0)
        assert ws.access("src/app.py", 2)
        assert bytes(ws.read_range("src/app.py", 0, 5)) == b"print"
        try:
            ws.read_range("src/app.py", needle_offset, 6)
            assert False, "path range read should block only overlapping labeled bytes"
        except anfs_core.PolicyDeniedError:
            pass

        assert fs.verify_integrity() == []


def test_posix_find_and_grep_are_uncapped_by_default_with_optional_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:posix-uncapped")

        ws.mkdir("docs")
        for idx in range(120):
            ws.write(f"docs/file-{idx:03}.md", b"needle\n", [])

        assert len(ws.find("docs", "*.md", tool_call_id="tc_find_all")) == 120
        assert len(ws.find("docs", "*.md", tool_call_id="tc_find_limited", limit=7)) == 7
        assert len(ws.grep("needle", "docs", tool_call_id="tc_grep_all")) == 120
        assert len(ws.grep("needle", "docs", tool_call_id="tc_grep_limited", limit=9)) == 9

        search_events = [fs.event(row[1]) for row in fs.events(kind="search", run_id="run:posix-uncapped")]
        payloads = [json.loads(event[6]) for event in search_events]
        assert [payload["result_count"] for payload in payloads] == [120, 7, 120, 9]
        assert fs.verify_integrity() == []
