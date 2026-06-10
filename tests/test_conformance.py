"""End-to-end demo and the executable agent-native filesystem conformance proof (docs/08)."""

import os
import shutil
import tempfile
from pathlib import Path

import anfs_core


def test_killer_demo():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)

        coder_ws = fs.checkout(workspace="ws:coder", agent_id="coder_agent")
        tester_ws = fs.checkout(workspace="ws:tester", agent_id="tester_agent")

        v1_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")

        tester_ws.write("test.log", b"PASS", [v1_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")

        coder_ws.write("src/db.py", b"print('v2_bug')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v2")

        try:
            fs.approve(
                target_ref="artifact:patch@v2",
                evidence_refs=["artifact:test_result@v1"],
                agent_id="reviewer_agent",
            )
        except anfs_core.LineageMismatchError:
            pass
        else:
            raise AssertionError(
                "CRITICAL FAILURE: Rust core failed to enforce lineage DAG"
            )


def test_agent_native_fs_conformance_preserves_kernel_invariants_with_existing_tools():
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        db_path = root / "anfs.db"
        objs_dir = root / "objs"
        worktree_dir = root / "worktree"
        blocked_dir = root / "blocked"
        cache_root = root / "cache"

        fs = anfs_core.AnfsEngine(str(db_path), str(objs_dir))
        importer = fs.open_workspace("ws:importer", "importer_agent")

        app_v1 = importer.write("src/app.py", b"print('v1')\n", [])
        importer.publish("src/app.py", "resource:repo/main@v1/src/app.py", "resource")
        customer_json = (
            b'{"customer":{"name":"Ada","ssn":"123-45-6789"},'
            b'"notes":"renewal priority"}'
        )
        customer_node = importer.write("data/customer.json", customer_json, [])
        importer.publish(
            "data/customer.json",
            "resource:repo/main@v1/data/customer.json",
            "resource",
        )
        fs.set_json_field_policy_label(
            customer_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
        )
        large_blob = (b"0123456789abcdef" * 8192) + b"tail"
        large_node = importer.write("data/large.bin", large_blob, [])
        importer.publish(
            "data/large.bin",
            "resource:repo/main@v1/data/large.bin",
            "resource",
        )
        importer.write("docs/todo.md", b"old task\n", [])
        importer.publish("docs/todo.md", "resource:repo/main@v1/docs/todo.md", "resource")

        coder = fs.checkout(
            "ws:coder",
            "coder_agent",
            base="resource:repo/main@v1",
            run_id="run:agent-native",
        )
        rows = fs.materialize_workspace("ws:coder", str(worktree_dir))
        assert {row[0] for row in rows} == {
            "data/customer.json",
            "data/large.bin",
            "docs/todo.md",
            "src/app.py",
        }

        assert (worktree_dir / "src" / "app.py").read_text() == "print('v1')\n"
        (worktree_dir / "src" / "app.py").write_text("print('v2')\n")
        (worktree_dir / "docs" / "todo.md").unlink()
        (worktree_dir / "docs" / "new.md").write_text("new task\n")
        shutil.copyfile(
            worktree_dir / "data" / "customer.json",
            worktree_dir / "data" / "customer-copy.json",
        )

        committed = fs.commit_worktree(
            "ws:coder",
            str(worktree_dir),
            "coder_agent",
            run_id="run:agent-native",
            tool_call_id="tc_existing_fs_commit",
        )
        statuses = {path: (status, node_id) for path, status, node_id in committed}
        assert statuses["src/app.py"][0] == "modified"
        assert statuses["docs/todo.md"] == ("deleted", None)
        assert statuses["docs/new.md"][0] == "added"
        assert statuses["data/customer-copy.json"][0] == "added"
        app_v2 = statuses["src/app.py"][1]
        assert bytes(fs.read_node(app_v2)) == b"print('v2')\n"
        assert app_v1 in fs.lineage_nodes(app_v2, "ancestors")

        new_doc_node = statuses["docs/new.md"][1]
        search = coder.search("new task", scope="draft", tool_call_id="tc_search")
        assert {node_id for node_id, _snippet in search} == {new_doc_node}
        search_event = fs.event(fs.events(kind="search", tool_call_id="tc_search")[0][1])
        assert search_event[8] == [
            ("input", new_doc_node, "search_result:0", "ws:coder/docs/new.md")
        ]
        query_rows = coder.query(
            prefix="ws:coder/data/",
            text="renewal",
            tool_call_id="tc_query",
        )
        assert {row[0] for row in query_rows} == {
            "ws:coder/data/customer.json",
            "ws:coder/data/customer-copy.json",
        }

        chunks = fs.cache_node_chunks(large_node, 32768)
        assert fs.cached_node_chunks(large_node, 32768) == chunks
        assert bytes(fs.read_node_range(large_node, 0, 16)) == b"0123456789abcdef"
        cache_key, output_dir, file_count, reused = fs.cache_materialized_workspace(
            "ws:coder",
            str(cache_root),
            cache_key="agent-native",
        )
        assert (cache_key, output_dir, file_count, reused) == (
            "agent-native",
            str(cache_root / "agent-native"),
            5,
            False,
        )
        assert fs.cache_materialized_workspace(
            "ws:coder",
            str(cache_root),
            cache_key="agent-native",
        )[3] is True

        customer_copy_node = statuses["data/customer-copy.json"][1]
        assert fs.fragment_policy_labels(node_id=customer_copy_node)[0][0] == customer_copy_node
        fs.set_policy_rule(
            "pii",
            value="true",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.materialize_workspace("ws:coder", str(blocked_dir))
            assert False, "denied JSON field fragments should block whole-node projection"
        except anfs_core.PolicyDeniedError:
            pass
        assert not blocked_dir.exists()
        assert coder.search("123-45-6789", scope="draft") == []

        merged = fs.merge_workspace(
            "resource:repo/main@v1",
            "ws:coder",
            "merge_agent",
            tool_call_id="tc_agent_native_merge",
        )
        merged_by_path = {path: status for path, status, _ref_name, _node_id in merged}
        assert merged_by_path["src/app.py"] == "modified"
        assert merged_by_path["docs/new.md"] == "added"
        assert merged_by_path["docs/todo.md"] == "deleted"
        assert fs.verify_integrity() == []
