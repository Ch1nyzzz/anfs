import hashlib
import json
import multiprocessing
import os
import queue as queue_module
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path

import anfs_core


def _drain_queue_nowait(queue):
    items = []
    while True:
        try:
            items.append(queue.get_nowait())
        except queue_module.Empty:
            return items


def _collect_queue_results(queue, expected_count, timeout=5):
    items = []
    for _ in range(expected_count):
        try:
            items.append(queue.get(timeout=timeout))
        except queue_module.Empty:
            break
    return items


def _concurrent_event_writer(db_path, objs_dir, worker_idx, writes_per_worker, queue):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace(
            f"ws:worker-{worker_idx}",
            f"worker_agent_{worker_idx}",
            run_id=f"run:worker-{worker_idx}",
        )
        for item_idx in range(writes_per_worker):
            content = f"worker={worker_idx}; item={item_idx}".encode("utf-8")
            ws.write(
                f"file-{item_idx}.txt",
                content,
                [],
            )
            read_back = bytes(ws.read(f"file-{item_idx}.txt"))
            if read_back != content:
                queue.put(
                    f"worker {worker_idx} read mismatch for item {item_idx}: {read_back!r}"
                )
        grep_rows = ws.grep(f"worker={worker_idx}", "")
        if len(grep_rows) != writes_per_worker:
            queue.put(
                f"worker {worker_idx} grep mismatch: {len(grep_rows)} != {writes_per_worker}"
            )
    except Exception as exc:
        queue.put(f"{type(exc).__name__}: {exc}")


def _concurrent_stale_approval_worker(
    db_path,
    objs_dir,
    worker_idx,
    target_ref,
    evidence_ref,
    expected_version,
    queue,
):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        fs.approve(
            target_ref,
            [evidence_ref],
            f"reviewer_agent_{worker_idx}",
            run_id=f"run:stale-review-{worker_idx}",
            tool_call_id=f"tc_stale_review_{worker_idx}",
            expected_version=expected_version,
        )
        queue.put(("approved", worker_idx, ""))
    except anfs_core.RefConflictError as exc:
        queue.put(("conflict", worker_idx, str(exc)))
    except Exception as exc:
        queue.put(("error", worker_idx, f"{type(exc).__name__}: {exc}"))


def _concurrent_checked_worktree_commit_worker(
    db_path,
    objs_dir,
    workspace,
    worktree_dir,
    worker_idx,
    queue,
):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        committed = fs.commit_worktree(
            workspace,
            worktree_dir,
            f"coder_agent_{worker_idx}",
            run_id=f"run:checked-worktree-{worker_idx}",
            tool_call_id=f"tc_checked_worktree_{worker_idx}",
            require_manifest_match=True,
        )
        queue.put(("committed", worker_idx, committed))
    except anfs_core.RefConflictError as exc:
        queue.put(("conflict", worker_idx, str(exc)))
    except Exception as exc:
        queue.put(("error", worker_idx, f"{type(exc).__name__}: {exc}"))


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


def test_stale_approval_precondition_blocks_policy_event():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        patch_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        published_version = fs.get_ref("artifact:patch@v1")[4]
        tester_ws.write("test.log", b"PASS", [patch_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")
        fs.reject_ref("artifact:patch@v1", "reviewer_agent")

        try:
            fs.approve(
                "artifact:patch@v1",
                ["artifact:test_result@v1"],
                "reviewer_agent",
                expected_version=published_version,
            )
        except anfs_core.RefConflictError as exc:
            assert "version mismatch" in str(exc)
        else:
            raise AssertionError("stale approval decision should raise RefConflictError")

        assert fs.get_ref("artifact:patch@v1")[3] == "rejected"
        assert fs.policy_decisions("artifact:patch@v1") == []
        assert [row[2] for row in fs.ref_history("artifact:patch@v1")] == [
            "publish",
            "reject",
        ]


def test_self_approval_is_policy_denied_with_audit_event():
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

        try:
            fs.approve(
                "artifact:patch@v1",
                ["artifact:test_result@v1"],
                "coder_agent",
                run_id="run:self-review",
                tool_call_id="tc_self_approve",
            )
        except anfs_core.PolicyDeniedError as exc:
            assert "cannot approve" in str(exc)
        else:
            raise AssertionError("producer must not approve its own artifact")

        assert fs.get_ref("artifact:patch@v1")[3] == "published"
        decisions = fs.policy_decisions(
            target_ref="artifact:patch@v1",
            policy="review_separation",
            decision="deny",
            reason_code="self_review_denied",
        )
        assert len(decisions) == 1
        payload = json.loads(decisions[0][1])
        assert payload["target_ref"] == "artifact:patch@v1"
        assert payload["target_node_id"] == patch_node
        assert payload["evidence_refs"] == []
        event = fs.event(decisions[0][0])
        assert event[2] == "coder_agent"
        assert event[3] == "run:self-review"
        assert event[4] == "tc_self_approve"

        fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
        )
        assert fs.get_ref("artifact:patch@v1")[3] == "approved"


def test_self_rejection_is_policy_denied_with_audit_event():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")

        patch_node = coder_ws.write("src/db.py", b"print('bad')", [])
        coder_ws.publish("src/db.py", "artifact:patch@bad")

        try:
            fs.reject_ref(
                "artifact:patch@bad",
                "coder_agent",
                reason="self reject",
                run_id="run:self-review",
                tool_call_id="tc_self_reject",
            )
        except anfs_core.PolicyDeniedError as exc:
            assert "cannot reject" in str(exc)
        else:
            raise AssertionError("producer must not reject its own artifact")

        assert fs.get_ref("artifact:patch@bad")[3] == "published"
        decisions = fs.policy_decisions(
            target_ref="artifact:patch@bad",
            policy="review_separation",
            decision="deny",
            reason_code="self_review_denied",
        )
        assert len(decisions) == 1
        payload = json.loads(decisions[0][1])
        assert payload["target_ref"] == "artifact:patch@bad"
        assert payload["target_node_id"] == patch_node
        event = fs.event(decisions[0][0])
        assert event[2] == "coder_agent"
        assert event[4] == "tc_self_reject"

        fs.reject_ref("artifact:patch@bad", "reviewer_agent")
        assert fs.get_ref("artifact:patch@bad")[3] == "rejected"


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


def test_fork_workspace_copies_draft_refs_with_typed_lineage_edges():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        alpha = fs.open_workspace("ws:alpha", "alpha_agent", run_id="run:alpha")
        node_a = alpha.write("src/a.md", b"alpha a v1", [])
        node_b = alpha.write("src/b.md", b"alpha b v1", [])

        beta = fs.fork_workspace(
            "ws:alpha",
            "ws:beta",
            "beta_agent",
            run_id="run:fork",
            tool_call_id="tc_fork",
        )
        assert bytes(beta.read("src/a.md")) == b"alpha a v1"
        assert bytes(beta.read("src/b.md")) == b"alpha b v1"

        alpha.write("src/a.md", b"alpha a v2", [])
        beta.write("src/b.md", b"beta b v2", [])
        assert bytes(alpha.read("src/a.md")) == b"alpha a v2"
        assert bytes(alpha.read("src/b.md")) == b"alpha b v1"
        assert bytes(beta.read("src/a.md")) == b"alpha a v1"
        assert bytes(beta.read("src/b.md")) == b"beta b v2"

        fork_event = fs.event(fs.events(kind="fork_workspace", run_id="run:fork")[0][1])
        assert fork_event[1] == "fork_workspace"
        assert fork_event[2] == "beta_agent"
        assert fork_event[4] == "tc_fork"
        assert fork_event[5] == "ws:beta"
        assert json.loads(fork_event[6]) == {
            "source_workspace": "ws:alpha",
            "target_workspace": "ws:beta",
        }
        assert {
            (direction, node_id, logical_path)
            for direction, node_id, _role, logical_path in fork_event[8]
        } == {
            ("input", node_a, "ws:alpha/src/a.md"),
            ("input", node_b, "ws:alpha/src/b.md"),
            ("output", node_a, "ws:beta/src/a.md"),
            ("output", node_b, "ws:beta/src/b.md"),
        }
        assert fs.verify_integrity() == []

        try:
            fs.fork_workspace("ws:alpha", "ws:beta", "beta_agent")
            assert False, "forking into an existing workspace ref should conflict"
        except anfs_core.RefConflictError:
            pass
        try:
            fs.fork_workspace("ws:alpha", "ws:alpha", "alpha_agent")
            assert False, "fork source and target must differ"
        except anfs_core.PolicyDeniedError:
            pass


def test_workspace_branch_names_are_validated_at_public_boundaries():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        worktree_dir = os.path.join(tmpdir, "worktree")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        alpha = fs.open_workspace("ws:alpha-1", "alpha_agent")
        alpha.write("src/a.md", b"alpha", [])

        valid = fs.fork_workspace("ws:alpha-1", "ws:beta_2", "beta_agent")
        assert bytes(valid.read("src/a.md")) == b"alpha"

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
            fs.fork_workspace(
                "ws:alpha-1",
                "ws:bad/name",
                "bad_agent",
                tool_call_id="tc_bad_fork_branch",
            )
            assert False, "fork should reject unsafe target workspace names"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe workspace name ws:bad/name" in str(exc)
        assert fs.events(kind="fork_workspace", tool_call_id="tc_bad_fork_branch") == []

        try:
            fs.materialize_workspace("ws:bad/name", worktree_dir)
            assert False, "worktree materialization should reject unsafe workspace names"
        except anfs_core.PolicyDeniedError as exc:
            assert "unsafe workspace name ws:bad/name" in str(exc)

        assert fs.verify_integrity() == []


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


def test_retention_policy_automation_runs_gc_with_audited_rule():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")
        contents = [
            b"retention garbage a" * 4096,
            b"retention garbage b" * 4096,
        ]
        hashes = [hashlib.sha256(content).hexdigest() for content in contents]
        for index, content in enumerate(contents):
            ws.write(f"deleted-{index}.bin", content, [])
            ws.delete(f"deleted-{index}.bin")

        with sqlite3.connect(db_path) as conn:
            storage_paths = {
                row[0]: row[1]
                for row in conn.execute(
                    "SELECT hash, storage_uri FROM blobs WHERE hash IN (?, ?)",
                    hashes,
                )
            }
        assert all(os.path.exists(storage_paths[digest]) for digest in hashes)

        fs.set_retention_policy(
            "default",
            include_workspaces=True,
            min_age_ms=0,
            limit=1,
            agent_id="policy_agent",
        )
        active = fs.retention_policies()
        assert len(active) == 1
        assert active[0][0:5] == ("default", "enabled", True, 0, 1)

        dry_run = fs.run_retention_policy("default")
        assert len(dry_run) == 1
        assert dry_run[0][3] == "would_collect_file"
        assert os.path.exists(storage_paths[dry_run[0][0]])

        collected = fs.run_retention_policy(
            "default",
            dry_run=False,
            agent_id="retention_agent",
        )
        assert len(collected) == 1
        assert collected[0][3] == "collected_file"
        assert not os.path.exists(storage_paths[collected[0][0]])
        remaining = set(hashes) - {collected[0][0]}
        assert remaining == {row[0] for row in fs.gc_candidates(True)}

        gc_event = fs.event(fs.events(kind="gc_collect")[-1][1])
        payload = json.loads(gc_event[6])
        assert payload["retention_policy"] == "default"
        assert payload["limit"] == 1
        assert payload["older_than_ms"] is not None

        fs.set_retention_policy("default", enabled=False, agent_id="policy_agent")
        assert fs.retention_policies() == []
        assert [row[1] for row in fs.retention_policies(active_only=False)] == [
            "enabled",
            "disabled",
        ]
        try:
            fs.run_retention_policy("default")
            assert False, "disabled retention policy should not run"
        except anfs_core.PolicyDeniedError:
            pass

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


def test_policy_labels_are_append_only_and_queryable():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:analyst", "analyst_agent")
        node_id = ws.write("contract.md", b"Confidential contract", [])
        ws.publish("contract.md", "artifact:contract@v1")

        fs.set_policy_label(
            "ref",
            "artifact:contract@v1",
            "sensitivity",
            "confidential",
            "policy_agent",
            run_id="run:policy",
            tool_call_id="tc_label_ref",
        )
        fs.set_policy_label(
            "node",
            node_id,
            "exportability",
            "internal_only",
            "policy_agent",
            tool_call_id="tc_label_node",
        )
        active = fs.policy_labels()
        assert {
            (row[0], row[1], row[2], row[3])
            for row in active
        } == {
            ("ref", "artifact:contract@v1", "sensitivity", "confidential"),
            ("node", node_id, "exportability", "internal_only"),
        }
        assert fs.policy_labels(
            subject_type="ref",
            subject_id="artifact:contract@v1",
            label="sensitivity",
        )[0][3] == "confidential"

        label_events = fs.events(kind="policy_label")
        assert len(label_events) == 2
        assert [row[0] for row in fs.events(tool_call_id="tc_label_ref")] == [
            label_events[0][0]
        ]
        event = fs.event(label_events[0][1])
        payload = json.loads(event[6])
        assert payload == {
            "subject_type": "ref",
            "subject_id": "artifact:contract@v1",
            "label": "sensitivity",
            "value": "confidential",
            "action": "set",
        }
        assert event[3] == "run:policy"
        assert event[4] == "tc_label_ref"
        assert event[8] == [
            (
                "input",
                node_id,
                "policy_label_subject",
                "artifact:contract@v1",
            )
        ]

        fs.set_policy_label(
            "ref",
            "artifact:contract@v1",
            "sensitivity",
            None,
            "policy_agent",
        )
        assert fs.policy_labels(subject_type="ref") == []
        label_history = fs.policy_labels(subject_type="ref", active_only=False)
        assert [row[3] for row in label_history] == ["confidential", None]
        assert fs.verify_integrity() == []

        try:
            fs.set_policy_label(
                "workspace",
                "artifact:contract@v1",
                "sensitivity",
                "confidential",
                "policy_agent",
            )
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("unsupported policy label subject should be rejected")

        try:
            fs.set_policy_label(
                "ref",
                "artifact:missing@v1",
                "sensitivity",
                "confidential",
                "policy_agent",
            )
        except anfs_core.RefNotFoundError:
            pass
        else:
            raise AssertionError("missing policy label ref subject should be rejected")


def test_query_policy_label_excludes_filter_active_ref_and_node_labels():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        blocked_node = ws.write("blocked.md", b"query policy label gate blocked", [])
        ws.publish("blocked.md", "artifact:blocked@v1")
        allowed_node = ws.write("allowed.md", b"query policy label gate allowed", [])
        ws.publish("allowed.md", "artifact:allowed@v1")

        assert {
            row[0]
            for row in fs.query(prefix="artifact:", text="query", state="published")
        } == {"artifact:allowed@v1", "artifact:blocked@v1"}

        fs.set_policy_label(
            "ref",
            "artifact:blocked@v1",
            "sensitivity",
            "pii",
            "policy_agent",
        )
        filtered = fs.query(
            prefix="artifact:",
            text="query",
            state="published",
            policy_label_excludes=["sensitivity"],
        )
        assert [row[0] for row in filtered] == ["artifact:allowed@v1"]

        fs.set_policy_label("ref", "artifact:blocked@v1", "sensitivity", None, "policy_agent")
        fs.set_policy_label("node", allowed_node, "exportability", "blocked", "policy_agent")
        filtered_by_node = fs.query(
            prefix="artifact:",
            text="query",
            state="published",
            policy_label_excludes=["exportability"],
        )
        assert [row[0] for row in filtered_by_node] == ["artifact:blocked@v1"]

        reader = fs.open_workspace("ws:reader", "reader_agent", run_id="run:reader")
        audited_rows = reader.query(
            prefix="artifact:",
            text="query",
            state="published",
            policy_label_excludes=["exportability"],
            tool_call_id="tc_policy_filtered_query",
        )
        assert [row[0] for row in audited_rows] == ["artifact:blocked@v1"]
        query_event = fs.event(fs.events(kind="query")[0][1])
        payload = json.loads(query_event[6])
        assert payload["policy_label_excludes"] == ["exportability"]
        assert payload["result_count"] == 1
        assert query_event[8] == [
            ("input", blocked_node, "query_result:0", "artifact:blocked@v1")
        ]

        try:
            fs.query(policy_label_excludes=[""])
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("empty policy label excludes should be rejected")


def test_policy_registry_interprets_label_values_for_visibility():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:analyst", "analyst_agent")
        public_node = ws.write("public.md", b"public report", [])
        ws.publish("public.md", "artifact:public@v1")
        restricted_node = ws.write("restricted.md", b"restricted report", [])
        ws.publish("restricted.md", "artifact:restricted@v1")

        fs.set_policy_label(
            "ref",
            "artifact:restricted@v1",
            "sensitivity",
            "restricted",
            "policy_agent",
        )
        fs.set_policy_label(
            "ref",
            "artifact:public@v1",
            "sensitivity",
            "public",
            "policy_agent",
        )
        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="ref",
            agent_id="policy_agent",
            tool_call_id="tc_policy_rule",
        )

        active_rules = fs.policy_rules()
        assert len(active_rules) == 1
        assert active_rules[0][0:5] == (
            "visibility",
            "ref",
            "sensitivity",
            "restricted",
            "deny",
        )
        assert fs.events(kind="policy_rule")[0][1] == active_rules[0][5]
        assert fs.events(tool_call_id="tc_policy_rule")[0][1] == active_rules[0][5]

        rows = fs.query(prefix="artifact:")
        assert {row[0] for row in rows} == {"artifact:public@v1"}
        assert rows[0][1] == public_node

        reader = fs.open_workspace("ws:reader", "reader_agent")
        search = reader.search("report", "published")
        assert search == [(public_node, "public [report]")]
        try:
            reader.answer("answer.md", b"answer", ["artifact:restricted@v1"])
            assert False, "policy registry deny rule should block restricted citation"
        except anfs_core.PolicyDeniedError:
            pass

        fs.clear_policy_rule(
            "sensitivity",
            value="restricted",
            scope="visibility",
            subject_type="ref",
            agent_id="policy_agent",
        )
        assert fs.policy_rules() == []
        history = fs.policy_rules(active_only=False)
        assert [row[4] for row in history] == ["deny", None]
        assert {row[0] for row in fs.query(prefix="artifact:")} == {
            "artifact:public@v1",
            "artifact:restricted@v1",
        }

        fs.set_policy_rule(
            "sensitivity",
            effect="deny",
            scope="visibility",
            subject_type="*",
            agent_id="policy_agent",
        )
        assert fs.query(prefix="artifact:") == []
        fs.clear_policy_rule("sensitivity", agent_id="policy_agent")

        try:
            fs.set_policy_rule(
                "sensitivity",
                value="restricted",
                effect="allow",
                agent_id="policy_agent",
            )
            assert False, "unsupported policy rule effect should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.set_policy_rule(
                "sensitivity",
                value="*",
                effect="deny",
                agent_id="policy_agent",
            )
            assert False, "literal wildcard policy values should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        assert bytes(fs.read_node(restricted_node)) == b"restricted report"
        assert fs.verify_integrity() == []


def test_policy_expression_rules_compose_active_labels_for_visibility():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:analyst", "analyst_agent")
        public_node = ws.write("public.md", b"public report", [])
        ws.publish("public.md", "artifact:public@v1")
        restricted_node = ws.write("restricted.md", b"restricted report", [])
        ws.publish("restricted.md", "artifact:restricted@v1")
        combined_node = ws.write("combined.md", b"combined report", [])
        ws.publish("combined.md", "artifact:combined@v1")

        fs.set_policy_label("node", restricted_node, "sensitivity", "restricted", "policy_agent")
        fs.set_policy_label("node", combined_node, "sensitivity", "restricted", "policy_agent")
        fs.set_policy_label("node", combined_node, "exportability", "external", "policy_agent")
        expression = json.dumps(
            {
                "all": [
                    {"label": "sensitivity", "value": "restricted"},
                    {"label": "exportability", "value": "external"},
                ]
            },
            separators=(",", ":"),
        )
        fs.set_policy_expression_rule(
            expression,
            effect="deny",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
            tool_call_id="tc_policy_expression",
        )

        active_rules = fs.policy_expression_rules()
        assert len(active_rules) == 1
        assert active_rules[0][0:4] == ("visibility", "node", expression, "deny")
        assert fs.events(kind="policy_expression_rule")[0][1] == active_rules[0][4]
        assert fs.events(tool_call_id="tc_policy_expression")[0][1] == active_rules[0][4]
        assert {row[0] for row in fs.query(prefix="artifact:")} == {
            "artifact:public@v1",
            "artifact:restricted@v1",
        }

        reader = fs.open_workspace("ws:reader", "reader_agent")
        assert {node for node, _snippet in reader.search("report", "published")} == {
            public_node,
            restricted_node,
        }

        fs.clear_policy_expression_rule(
            expression,
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert fs.policy_expression_rules() == []
        assert [row[3] for row in fs.policy_expression_rules(active_only=False)] == [
            "deny",
            None,
        ]
        assert {row[0] for row in fs.query(prefix="artifact:")} == {
            "artifact:public@v1",
            "artifact:restricted@v1",
            "artifact:combined@v1",
        }

        try:
            fs.set_policy_expression_rule(
                json.dumps({"all": []}),
                agent_id="policy_agent",
            )
            assert False, "empty all expression should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.set_policy_expression_rule(
                json.dumps(
                    {
                        "at_least": {
                            "count": 3,
                            "of": [
                                {"label": "sensitivity"},
                                {"label": "exportability"},
                            ],
                        }
                    }
                ),
                agent_id="policy_agent",
            )
            assert False, "at_least count above child count should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        threshold_allowed = ws.write("threshold-allowed.md", b"threshold allowed", [])
        ws.publish("threshold-allowed.md", "artifact:threshold-allowed@v1")
        threshold_blocked = ws.write("threshold-blocked.md", b"threshold blocked", [])
        ws.publish("threshold-blocked.md", "artifact:threshold-blocked@v1")
        threshold_stronger = ws.write("threshold-stronger.md", b"threshold stronger", [])
        ws.publish("threshold-stronger.md", "artifact:threshold-stronger@v1")

        fs.set_policy_label(
            "node", threshold_allowed, "sensitivity", "restricted", "policy_agent"
        )
        fs.set_policy_label(
            "node", threshold_blocked, "sensitivity", "restricted", "policy_agent"
        )
        fs.set_policy_label(
            "node", threshold_blocked, "exportability", "external", "policy_agent"
        )
        fs.set_policy_label(
            "node", threshold_stronger, "sensitivity", "restricted", "policy_agent"
        )
        fs.set_policy_label(
            "node", threshold_stronger, "exportability", "external", "policy_agent"
        )
        fs.set_policy_label(
            "node", threshold_stronger, "retention", "legal_hold", "policy_agent"
        )
        threshold_expression = json.dumps(
            {
                "at_least": {
                    "count": 2,
                    "of": [
                        {"label": "sensitivity", "value": "restricted"},
                        {"label": "exportability", "value": "external"},
                        {"label": "retention", "value": "legal_hold"},
                    ],
                }
            },
            separators=(",", ":"),
        )
        fs.set_policy_expression_rule(
            threshold_expression,
            effect="deny",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert {
            row[0]
            for row in fs.query(prefix="artifact:threshold", text="threshold")
        } == {"artifact:threshold-allowed@v1"}

        fragment_node = ws.write("fragment.md", b"public SECRET tail", [])
        secret_offset = b"public SECRET tail".index(b"SECRET")
        secret_len = len(b"SECRET")
        fs.set_fragment_policy_label(
            fragment_node,
            secret_offset,
            secret_len,
            "sensitivity",
            "restricted",
            "policy_agent",
        )
        fragment_expression = json.dumps(
            {
                "all": [
                    {"label": "sensitivity", "value": "restricted"},
                    {"label": "exportability", "value": "external"},
                ]
            },
            separators=(",", ":"),
        )
        fs.set_policy_expression_rule(
            fragment_expression,
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(fragment_node, secret_offset, secret_len)) == b"SECRET"
        fs.set_fragment_policy_label(
            fragment_node,
            secret_offset,
            secret_len,
            "exportability",
            "external",
            "policy_agent",
        )
        try:
            fs.read_node_range(fragment_node, secret_offset, secret_len)
            assert False, "fragment expression rule should block matching range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_integrity_verifier_detects_policy_expression_rule_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        assert fs.verify_integrity() == []

        wrong_kind_event = "event:bad-policy-expression-kind"
        invalid_expression_event = "event:bad-policy-expression-json"
        orphan_event = "event:bad-policy-expression-orphan"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (wrong_kind_event, "write", next_seq),
                (invalid_expression_event, "policy_expression_rule", next_seq + 1),
                (orphan_event, "policy_expression_rule", next_seq + 2),
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
                INSERT INTO policy_expression_rule_events
                    (scope, subject_type, expression_json, effect, event_id, created_at)
                VALUES ('visibility', 'node', ?, 'deny', ?, 0)
                """,
                (json.dumps({"label": "sensitivity", "value": "restricted"}), wrong_kind_event),
            )
            conn.execute(
                """
                INSERT INTO policy_expression_rule_events
                    (scope, subject_type, expression_json, effect, event_id, created_at)
                VALUES ('visibility', 'node', ?, 'deny', ?, 0)
                """,
                (json.dumps({"all": []}), invalid_expression_event),
            )

        issues = fs.verify_integrity()
        assert any(
            f"policy_expression_rule_event {wrong_kind_event} must reference policy_expression_rule event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_expression_rule_event {invalid_expression_event} has invalid expression: all must not be empty"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_expression_rule event {orphan_event} must have exactly one policy_expression_rule_events audit row; found 0"
            in issue
            for issue in issues
        )


def test_purpose_policy_rules_gate_read_and_consume_without_breaking_plain_reads():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        reader = fs.open_workspace("ws:reader", "reader_agent")

        node_id = writer.write(
            "memo.md",
            b"public summary\nsecret export note\n",
            [],
        )
        writer.publish("memo.md", "artifact:memo@v1")
        fs.set_policy_label(
            "ref",
            "artifact:memo@v1",
            "allowed_use",
            "internal_only",
            "policy_agent",
        )
        fs.set_policy_label(
            "node",
            node_id,
            "retention",
            "no_export",
            "policy_agent",
        )
        fs.set_fragment_policy_label(
            node_id,
            15,
            18,
            "contains",
            "secret",
            "policy_agent",
        )

        fs.set_purpose_policy_rule(
            "export",
            "allowed_use",
            value="internal_only",
            subject_type="ref",
            agent_id="policy_agent",
            tool_call_id="tc_purpose_ref",
        )
        active_rules = fs.purpose_policy_rules()
        assert len(active_rules) == 1
        assert active_rules[0][0:5] == (
            "export",
            "ref",
            "allowed_use",
            "internal_only",
            "deny",
        )
        assert fs.events(kind="purpose_policy_rule")[0][1] == active_rules[0][5]
        assert fs.events(tool_call_id="tc_purpose_ref")[0][1] == active_rules[0][5]

        assert bytes(reader.read_ref("artifact:memo@v1", purpose="analysis")) == (
            b"public summary\nsecret export note\n"
        )
        assert bytes(reader.read_ref("artifact:memo@v1")) == (
            b"public summary\nsecret export note\n"
        )
        analysis_search = reader.search("secret", "published", purpose="analysis")
        assert len(analysis_search) == 1
        assert analysis_search[0][0] == node_id
        assert "[secret]" in analysis_search[0][1]
        assert reader.search("secret", "published", purpose="export") == []
        assert [
            row[1]
            for row in reader.query(text="secret", state="published", purpose="analysis")
        ] == [node_id]
        assert reader.query(text="secret", state="published", purpose="export") == []
        assert fs.query(text="secret", state="published", purpose="export") == []
        export_search_payloads = [
            json.loads(fs.event(event_id)[6])
            for _seq, event_id, *_rest in fs.events(kind="search")
            if json.loads(fs.event(event_id)[6]).get("purpose") == "export"
        ]
        assert len(export_search_payloads) == 1
        export_search_payload = export_search_payloads[0]
        assert export_search_payload["purpose"] == "export"
        assert export_search_payload["result_count"] == 0
        try:
            reader.read_ref("artifact:memo@v1", purpose="export")
            assert False, "export purpose should be blocked by ref policy"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            reader.consume("artifact:memo@v1", purpose="export")
            assert False, "export consume should be blocked by ref policy"
        except anfs_core.PolicyDeniedError:
            pass

        fs.clear_purpose_policy_rule(
            "export",
            "allowed_use",
            value="internal_only",
            subject_type="ref",
            agent_id="policy_agent",
        )
        assert fs.purpose_policy_rules() == []
        assert [row[4] for row in fs.purpose_policy_rules(active_only=False)] == [
            "deny",
            None,
        ]

        fs.set_purpose_policy_rule(
            "export",
            "retention",
            value="no_export",
            subject_type="node",
            agent_id="policy_agent",
        )
        try:
            writer.read("memo.md", purpose="export")
            assert False, "export path read should be blocked by node policy"
        except anfs_core.PolicyDeniedError:
            pass
        fs.clear_purpose_policy_rule(
            "export",
            "retention",
            value="no_export",
            subject_type="node",
            agent_id="policy_agent",
        )

        fs.set_purpose_policy_rule(
            "export",
            "contains",
            value="secret",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(reader.read_range("artifact:memo@v1", 0, 14, purpose="export")) == (
            b"public summary"
        )
        try:
            reader.read_range("artifact:memo@v1", 15, 6, purpose="export")
            assert False, "export range read should block only overlapping fragment policy"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            reader.read_ref("artifact:memo@v1", purpose="export")
            assert False, "export read should be blocked by fragment policy"
        except anfs_core.PolicyDeniedError:
            pass
        fs.clear_purpose_policy_rule(
            "export",
            "contains",
            value="secret",
            subject_type="fragment",
            agent_id="policy_agent",
        )

        assert bytes(reader.read_ref("artifact:memo@v1", purpose="export")) == (
            b"public summary\nsecret export note\n"
        )
        assert fs.verify_integrity() == []


def test_purpose_capability_rules_authorize_explicit_purpose_without_breaking_plain_reads():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        writer.write("report.md", b"exportable report", [])
        writer.publish("report.md", "artifact:report@v1")

        reader = fs.open_workspace("ws:reader", "reader_agent", run_id="run:reader")
        assert bytes(reader.read_ref("artifact:report@v1", purpose="export")) == (
            b"exportable report"
        )

        fs.set_purpose_capability_rule(
            "export",
            "can_export",
            agent_id="policy_agent",
            tool_call_id="tc_purpose_capability",
        )
        active_rules = fs.purpose_capability_rules()
        assert len(active_rules) == 1
        assert active_rules[0][0:3] == ("export", "can_export", "require")
        assert fs.events(kind="purpose_capability_rule")[0][1] == active_rules[0][3]
        assert fs.events(tool_call_id="tc_purpose_capability")[0][1] == active_rules[0][3]

        assert bytes(reader.read_ref("artifact:report@v1")) == b"exportable report"
        try:
            reader.read_ref("artifact:report@v1", purpose="export")
            assert False, "export purpose should require can_export capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "requires capability can_export for agent reader_agent" in str(exc)

        assert reader.search("exportable", "published") != []
        try:
            reader.search("exportable", "published", purpose="export")
            assert False, "search should require can_export capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "search for purpose export requires capability can_export" in str(exc)
        try:
            reader.consume("artifact:report@v1", purpose="export")
            assert False, "consume should require can_export capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "consume for purpose export requires capability can_export" in str(exc)
        try:
            reader.query(text="exportable", state="published", purpose="export")
            assert False, "query should require can_export capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "query for purpose export requires capability can_export" in str(exc)

        fs.grant_agent_capability(
            "reader_agent",
            "can_export",
            agent_id="policy_agent",
            tool_call_id="tc_grant_capability",
        )
        active_capabilities = fs.agent_capabilities()
        assert len(active_capabilities) == 1
        assert active_capabilities[0][0:3] == ("reader_agent", "can_export", "grant")
        assert fs.events(kind="agent_capability")[0][1] == active_capabilities[0][3]
        assert fs.events(tool_call_id="tc_grant_capability")[0][1] == active_capabilities[0][3]

        assert bytes(reader.read_ref("artifact:report@v1", purpose="export")) == (
            b"exportable report"
        )
        assert reader.search("exportable", "published", purpose="export") != []
        assert reader.consume("artifact:report@v1", purpose="export") != ""
        assert reader.query(text="exportable", state="published", purpose="export") != []

        fs.revoke_agent_capability("reader_agent", "can_export", agent_id="policy_agent")
        assert fs.agent_capabilities() == []
        assert [row[2] for row in fs.agent_capabilities(active_only=False)] == [
            "grant",
            None,
        ]
        try:
            reader.read_ref("artifact:report@v1", purpose="export")
            assert False, "revoked capability should block export purpose"
        except anfs_core.PolicyDeniedError:
            pass

        fs.clear_purpose_capability_rule("export", "can_export", agent_id="policy_agent")
        assert fs.purpose_capability_rules() == []
        assert [row[2] for row in fs.purpose_capability_rules(active_only=False)] == [
            "require",
            None,
        ]
        assert bytes(reader.read_ref("artifact:report@v1", purpose="export")) == (
            b"exportable report"
        )
        assert fs.verify_integrity() == []


def test_operation_capability_rules_gate_lifecycle_operations():
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

        fs.set_operation_capability_rule(
            "approve",
            "reviewer",
            agent_id="policy_agent",
            tool_call_id="tc_operation_approve_rule",
        )
        active_rules = fs.operation_capability_rules()
        assert len(active_rules) == 1
        assert active_rules[0][0:3] == ("approve", "reviewer", "require")
        assert fs.events(kind="operation_capability_rule")[0][1] == active_rules[0][3]
        assert fs.events(tool_call_id="tc_operation_approve_rule")[0][1] == active_rules[0][3]

        try:
            fs.approve("artifact:patch@v1", ["artifact:test_result@v1"], "reviewer_agent")
            assert False, "approve should require reviewer capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "operation approve requires capability reviewer" in str(exc)
        assert fs.events(kind="approve") == []
        assert fs.get_ref("artifact:patch@v1")[3] == "published"

        fs.grant_agent_capability("reviewer_agent", "reviewer", agent_id="policy_agent")
        fs.approve("artifact:patch@v1", ["artifact:test_result@v1"], "reviewer_agent")
        assert fs.get_ref("artifact:patch@v1")[3] == "approved"

        fs.revoke_agent_capability("reviewer_agent", "reviewer", agent_id="policy_agent")
        bad_node = coder_ws.write("src/bad.py", b"print('bad')", [])
        coder_ws.publish("src/bad.py", "artifact:patch@bad")
        assert bad_node is not None
        fs.set_operation_capability_rule("reject_ref", "reviewer", agent_id="policy_agent")
        try:
            fs.reject_ref("artifact:patch@bad", "reviewer_agent")
            assert False, "reject_ref should require reviewer capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "operation reject_ref requires capability reviewer" in str(exc)
        assert fs.get_ref("artifact:patch@bad")[3] == "published"

        fs.grant_agent_capability("reviewer_agent", "reviewer", agent_id="policy_agent")
        fs.reject_ref("artifact:patch@bad", "reviewer_agent")
        assert fs.get_ref("artifact:patch@bad")[3] == "rejected"

        fs.set_operation_capability_rule("archive_ref", "archiver", agent_id="policy_agent")
        try:
            fs.archive_ref("artifact:patch@bad", "reviewer_agent")
            assert False, "archive_ref should require archiver capability"
        except anfs_core.PolicyDeniedError as exc:
            assert "operation archive_ref requires capability archiver" in str(exc)
        assert fs.get_ref("artifact:patch@bad")[3] == "rejected"

        fs.grant_agent_capability("retention_agent", "archiver", agent_id="policy_agent")
        fs.archive_ref("artifact:patch@bad", "retention_agent")
        assert fs.get_ref("artifact:patch@bad")[3] == "archived"

        fs.clear_operation_capability_rule("approve", "reviewer", agent_id="policy_agent")
        assert [row[2] for row in fs.operation_capability_rules(active_only=False)] == [
            "require",
            "require",
            "require",
            None,
        ]
        assert fs.verify_integrity() == []


def test_integrity_verifier_detects_purpose_policy_rule_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        ws.write("memo.md", b"memo", [])
        assert fs.verify_integrity() == []

        bad_event_id = "event:bad-purpose-policy-rule"
        with sqlite3.connect(db_path) as conn:
            write_event_id = conn.execute(
                "SELECT event_id FROM events WHERE kind = 'write'"
            ).fetchone()[0]
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'purpose_policy_rule', 'policy_agent', NULL, NULL, NULL, '{}', 0)
                """,
                (bad_event_id,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event_id, next_seq),
            )
            conn.execute(
                """
                INSERT INTO purpose_policy_rule_events
                    (purpose, subject_type, label, value, effect, event_id, created_at)
                VALUES ('', 'bad_subject', '', '', 'allow', ?, 0)
                """,
                (write_event_id,),
            )

        issues = fs.verify_integrity()
        assert any(
            f"purpose_policy_rule event {bad_event_id} must have exactly one purpose_policy_rule_events audit row; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"purpose_policy_rule_event {write_event_id} must reference purpose_policy_rule event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"purpose_policy_rule_event {write_event_id} has invalid subject_type bad_subject"
            in issue
            for issue in issues
        )
        assert any(
            f"purpose_policy_rule_event {write_event_id} has invalid effect allow" in issue
            for issue in issues
        )


def test_integrity_verifier_detects_capability_audit_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        ws.write("memo.md", b"memo", [])
        assert fs.verify_integrity() == []

        orphan_capability_event = "event:orphan-agent-capability"
        orphan_rule_event = "event:orphan-purpose-capability"
        orphan_operation_rule_event = "event:orphan-operation-capability"
        with sqlite3.connect(db_path) as conn:
            write_event_id = conn.execute(
                "SELECT event_id FROM events WHERE kind = 'write'"
            ).fetchone()[0]
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'agent_capability', 'policy_agent', NULL, NULL, NULL, '{}', 0)
                """,
                (orphan_capability_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (orphan_capability_event, next_seq),
            )
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'purpose_capability_rule', 'policy_agent', NULL, NULL, NULL, '{}', 0)
                """,
                (orphan_rule_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (orphan_rule_event, next_seq + 1),
            )
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'operation_capability_rule', 'policy_agent', NULL, NULL, NULL, '{}', 0)
                """,
                (orphan_operation_rule_event,),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (orphan_operation_rule_event, next_seq + 2),
            )
            conn.execute(
                """
                INSERT INTO agent_capability_events
                    (agent_id, capability, effect, event_id, created_at)
                VALUES ('', '', 'bad', ?, 0)
                """,
                (write_event_id,),
            )
            conn.execute(
                """
                INSERT INTO purpose_capability_rule_events
                    (purpose, capability, effect, event_id, created_at)
                VALUES ('', '', 'bad', ?, 0)
                """,
                (write_event_id,),
            )
            conn.execute(
                """
                INSERT INTO operation_capability_rule_events
                    (operation, capability, effect, event_id, created_at)
                VALUES ('', '', 'bad', ?, 0)
                """,
                (write_event_id,),
            )

        issues = fs.verify_integrity()
        assert any(
            f"agent_capability event {orphan_capability_event} must have exactly one agent_capability_events audit row; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"purpose_capability_rule event {orphan_rule_event} must have exactly one purpose_capability_rule_events audit row; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"operation_capability_rule event {orphan_operation_rule_event} must have exactly one operation_capability_rule_events audit row; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"agent_capability_event {write_event_id} must reference agent_capability event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"purpose_capability_rule_event {write_event_id} must reference purpose_capability_rule event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"operation_capability_rule_event {write_event_id} must reference operation_capability_rule event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"agent_capability_event {write_event_id} has invalid effect bad" in issue
            for issue in issues
        )
        assert any(
            f"purpose_capability_rule_event {write_event_id} has invalid effect bad" in issue
            for issue in issues
        )
        assert any(
            f"operation_capability_rule_event {write_event_id} has invalid effect bad" in issue
            for issue in issues
        )


def test_policy_labels_support_multiple_active_values_per_label():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:analyst", "analyst_agent")
        node_id = ws.write("record.md", b"multi-value policy record", [])
        ws.publish("record.md", "artifact:record@v1")

        fs.set_policy_label("node", node_id, "category", "pii", "policy_agent")
        fs.set_policy_label("node", node_id, "category", "financial", "policy_agent")
        active_values = {
            row[3]
            for row in fs.policy_labels(
                subject_type="node",
                subject_id=node_id,
                label="category",
            )
        }
        assert active_values == {"pii", "financial"}

        fs.set_policy_rule(
            "category",
            value="financial",
            effect="deny",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert fs.query(prefix="artifact:record") == []

        fs.clear_policy_rule(
            "category",
            value="financial",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert {row[0] for row in fs.query(prefix="artifact:record")} == {
            "artifact:record@v1"
        }

        fs.set_policy_label("node", node_id, "category", None, "policy_agent")
        assert fs.policy_labels(
            subject_type="node",
            subject_id=node_id,
            label="category",
        ) == []
        history = fs.policy_labels(
            subject_type="node",
            subject_id=node_id,
            label="category",
            active_only=False,
        )
        assert [row[3] for row in history] == ["pii", "financial", None]
        assert fs.verify_integrity() == []


def test_fragment_policy_labels_gate_ranges_and_whole_node_projection():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = b"public\nSECRET\npublic tail\n"
        node_id = ws.write("doc.txt", content, [])
        ws.publish("doc.txt", "artifact:doc@v1")
        secret_offset = content.index(b"SECRET")
        secret_len = len(b"SECRET")

        fs.set_fragment_policy_label(
            node_id,
            secret_offset,
            secret_len,
            "sensitivity",
            "restricted",
            "policy_agent",
            tool_call_id="tc_fragment_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            secret_offset,
            secret_len,
            "sensitivity",
            "restricted",
        )
        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )

        assert bytes(fs.read_node_range(node_id, 0, len(b"public"))) == b"public"
        try:
            fs.read_node_range(node_id, secret_offset, secret_len)
            assert False, "range overlapping denied fragment should be blocked"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.node_chunks(node_id, 8)
            assert False, "chunk map crossing denied fragment should be blocked"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.read_node(node_id)
            assert False, "full node read should be blocked by denied fragment"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            ws.read_ref("artifact:doc@v1")
            assert False, "read_ref should be blocked by denied fragment"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:doc") == []
        reader = fs.open_workspace("ws:reader", "reader_agent")
        try:
            reader.answer("answer.md", b"answer", ["artifact:doc@v1"])
            assert False, "answer citation should be blocked by denied fragment"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_fragment_policy_label(
            node_id,
            secret_offset,
            secret_len,
            "sensitivity",
            None,
            "policy_agent",
        )
        assert fs.fragment_policy_labels(node_id=node_id) == []
        history = fs.fragment_policy_labels(node_id=node_id, active_only=False)
        assert [row[4] for row in history] == ["restricted", None]
        assert bytes(fs.read_node(node_id)) == content
        assert {row[0] for row in fs.query(prefix="artifact:doc")} == {"artifact:doc@v1"}

        try:
            fs.set_fragment_policy_label(
                node_id,
                len(content),
                1,
                "sensitivity",
                "restricted",
                "policy_agent",
            )
            assert False, "fragment policy range beyond node size should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_json_field_policy_labels_map_semantic_paths_to_fragments():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = (
            b'{"customer":{"name":"Ada","ssn":"123-45-6789"},'
            b'"items":[{"sku":"book","price":12.5}]}'
        )
        node_id = ws.write("profile.json", content, [])
        ws.publish("profile.json", "artifact:profile@v1")

        spans = fs.json_field_spans(node_id)
        span_by_path = {row[0]: row for row in spans}
        ssn_path, ssn_offset, ssn_length, ssn_kind = span_by_path["$.customer.ssn"]
        assert (ssn_path, ssn_kind) == ("$.customer.ssn", "string")
        assert content[ssn_offset : ssn_offset + ssn_length] == b'"123-45-6789"'
        assert span_by_path["$.items[0].price"][3] == "number"

        fs.set_json_field_policy_label(
            node_id,
            "$.customer.ssn",
            "sensitivity",
            "restricted",
            "policy_agent",
            tool_call_id="tc_json_field_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            ssn_offset,
            ssn_length,
            "sensitivity",
            "restricted",
        )
        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, 0, content.index(b"ssn"))) == content[
            0 : content.index(b"ssn")
        ]
        try:
            fs.read_node_range(node_id, ssn_offset, ssn_length)
            assert False, "json field policy should block the field value range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:profile") == []

        fs.set_json_field_policy_label(
            node_id,
            "$.customer.ssn",
            "sensitivity",
            None,
            "policy_agent",
        )
        assert bytes(fs.read_node(node_id)) == content
        assert {row[0] for row in fs.query(prefix="artifact:profile")} == {
            "artifact:profile@v1"
        }

        try:
            fs.set_json_field_policy_label(
                node_id,
                "$.customer.missing",
                "sensitivity",
                "restricted",
                "policy_agent",
            )
            assert False, "missing JSON field path should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_fragment_policy_labels_propagate_to_exact_copy_nodes():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = b'{"customer":{"name":"Ada","ssn":"123-45-6789"}}'
        original_node = ws.write("profile.json", content, [])
        spans = {row[0]: row for row in fs.json_field_spans(original_node)}
        _path, ssn_offset, ssn_length, _kind = spans["$.customer.ssn"]
        fs.set_json_field_policy_label(
            original_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
            tool_call_id="tc_original_json_label",
        )

        copy_node = ws.write("profile-copy.json", content, [], tool_call_id="tc_copy")
        copy_labels = fs.fragment_policy_labels(node_id=copy_node)
        assert copy_labels[0][0:5] == (
            copy_node,
            ssn_offset,
            ssn_length,
            "pii",
            "true",
        )
        assert copy_labels[0][6] == "writer_agent"

        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(copy_node, ssn_offset, ssn_length)
            assert False, "exact copy should inherit denied fragment policy"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.read_node(copy_node)
            assert False, "exact copy should block whole-node projection"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_derived_outputs_inherit_source_policy_labels_conservatively():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = b'{"customer":{"name":"Ada","ssn":"123-45-6789"}}'
        source_node = ws.write("profile.json", content, [])
        ws.publish("profile.json", "artifact:profile@v1")
        fs.set_json_field_policy_label(
            source_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
        )

        summary = b"Summary for Ada."
        summary_node = ws.write("summary.md", summary, [source_node])
        ws.publish("summary.md", "artifact:summary@v1")
        assert fs.fragment_policy_labels(node_id=summary_node)[0][0:5] == (
            summary_node,
            0,
            len(summary),
            "pii",
            "true",
        )

        fs.set_policy_label(
            "node",
            source_node,
            "retention",
            "restricted",
            "policy_agent",
        )
        derived_node = ws.write("derived.md", b"Derived profile note.", [source_node])
        ws.publish("derived.md", "artifact:derived@v1")
        assert fs.policy_labels(
            subject_type="node",
            subject_id=derived_node,
            label="retention",
        )[0][0:4] == (
            "node",
            derived_node,
            "retention",
            "restricted",
        )

        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node(summary_node)
            assert False, "derived summary should inherit fragment denial"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:summary") == []

        fs.set_policy_rule(
            "retention",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="node",
            agent_id="policy_agent",
        )
        assert fs.query(prefix="artifact:derived") == []
        try:
            ws.read_ref("artifact:derived@v1")
            assert False, "derived node should inherit source node label denial"
        except anfs_core.PolicyDeniedError:
            pass

        assert fs.verify_integrity() == []


def test_partial_output_attribution_propagates_fragment_policy_to_output_span():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = b'{"customer":{"name":"Ada","ssn":"123-45-6789"}}'
        source_node = ws.write("profile.json", source, [])
        spans = {row[0]: row for row in fs.json_field_spans(source_node)}
        _path, source_offset, source_length, _kind = spans["$.customer.ssn"]
        fs.set_json_field_policy_label(
            source_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
        )

        output = b"Customer identifier: 123-45-6789; public note."
        output_node = ws.write("note.md", output, [])
        output_offset = output.index(b"123-45-6789")
        propagated = fs.propagate_fragment_policy_labels(
            source_node,
            source_offset,
            source_length,
            output_node,
            output_offset,
            len(b"123-45-6789"),
            "policy_agent",
            tool_call_id="tc_partial_attribution",
        )
        assert propagated == 1
        assert fs.fragment_policy_labels(node_id=output_node)[0][0:5] == (
            output_node,
            output_offset,
            len(b"123-45-6789"),
            "pii",
            "true",
        )
        history = fs.fragment_policy_labels(node_id=output_node, active_only=False)
        event = fs.event(history[0][5])
        payload = json.loads(event[6])
        assert payload["propagated_from_node_id"] == source_node
        assert payload["propagated_from_offset"] == source_offset
        assert payload["propagated_from_length"] == source_length

        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, output_offset)) == output[
            0:output_offset
        ]
        try:
            fs.read_node_range(output_node, output_offset, len(b"123-45-6789"))
            assert False, "partial output attribution should block the mapped span"
        except anfs_core.PolicyDeniedError:
            pass

        assert fs.verify_integrity() == []


def test_auto_attribution_propagates_unique_exact_fragment_matches_only():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = b"public SECRET public"
        source_node = ws.write("source.txt", source, [])
        source_offset = source.index(b"SECRET")
        fs.set_fragment_policy_label(
            source_node,
            source_offset,
            len(b"SECRET"),
            "sensitivity",
            "restricted",
            "policy_agent",
        )

        output = b"summary includes SECRET once"
        output_node = ws.write("summary.txt", output, [])
        propagated = fs.auto_propagate_fragment_policy_labels(
            source_node,
            output_node,
            "policy_agent",
            tool_call_id="tc_auto_attribution",
        )
        output_offset = output.index(b"SECRET")
        assert propagated == 1
        assert fs.fragment_policy_labels(node_id=output_node)[0][0:5] == (
            output_node,
            output_offset,
            len(b"SECRET"),
            "sensitivity",
            "restricted",
        )
        history = fs.fragment_policy_labels(node_id=output_node, active_only=False)
        event = fs.event(history[0][5])
        payload = json.loads(event[6])
        assert payload["propagated_from_node_id"] == source_node
        assert payload["propagated_from_offset"] == source_offset
        assert payload["propagated_from_length"] == len(b"SECRET")

        duplicate_node = ws.write("duplicate.txt", b"SECRET and SECRET", [])
        assert (
            fs.auto_propagate_fragment_policy_labels(
                source_node,
                duplicate_node,
                "policy_agent",
            )
            == 0
        )
        assert fs.fragment_policy_labels(node_id=duplicate_node) == []

        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, output_offset)) == output[
            0:output_offset
        ]
        try:
            fs.read_node_range(output_node, output_offset, len(b"SECRET"))
            assert False, "auto attribution should block the uniquely matched span"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_auto_attribution_propagates_unique_normalized_json_string_scalar():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = b'{"customer":{"name":"Ada","ssn":"123-45-6789"}}'
        source_node = ws.write("profile.json", source, [])
        spans = {row[0]: row for row in fs.json_field_spans(source_node)}
        _path, source_offset, source_length, _kind = spans["$.customer.ssn"]
        assert source[source_offset : source_offset + source_length] == b'"123-45-6789"'
        fs.set_json_field_policy_label(
            source_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
        )

        output = b"Customer SSN: 123-45-6789\n"
        output_node = ws.write("summary.md", output, [])
        assert (
            fs.auto_propagate_fragment_policy_labels(
                source_node,
                output_node,
                "policy_agent",
            )
            == 0
        )
        propagated = fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
            source_node,
            output_node,
            "policy_agent",
            tool_call_id="tc_normalized_attribution",
        )
        output_offset = output.index(b"123-45-6789")
        assert propagated == 1
        assert fs.fragment_policy_labels(node_id=output_node)[0][0:5] == (
            output_node,
            output_offset,
            len(b"123-45-6789"),
            "pii",
            "true",
        )
        history = fs.fragment_policy_labels(node_id=output_node, active_only=False)
        event = fs.event(history[0][5])
        payload = json.loads(event[6])
        assert payload["propagated_from_node_id"] == source_node
        assert payload["propagated_from_offset"] == source_offset
        assert payload["propagated_from_length"] == source_length

        duplicate_node = ws.write(
            "duplicate.md",
            b"Customer SSN: 123-45-6789 and audit copy 123-45-6789\n",
        )
        assert (
            fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
                source_node,
                duplicate_node,
                "policy_agent",
            )
            == 0
        )
        assert fs.fragment_policy_labels(node_id=duplicate_node) == []

        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, output_offset)) == output[
            0:output_offset
        ]
        try:
            fs.read_node_range(output_node, output_offset, len(b"123-45-6789"))
            assert False, "normalized scalar attribution should block the mapped span"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_auto_attribution_propagates_integer_normalized_json_number_scalar():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = b'{"customer":{"name":"Ada","risk_score":7.0}}'
        source_node = ws.write("profile.json", source, [])
        spans = {row[0]: row for row in fs.json_field_spans(source_node)}
        _path, source_offset, source_length, _kind = spans["$.customer.risk_score"]
        assert source[source_offset : source_offset + source_length] == b"7.0"
        fs.set_json_field_policy_label(
            source_node,
            "$.customer.risk_score",
            "risk",
            "restricted",
            "policy_agent",
        )

        output = b"Customer risk score: 7\n"
        output_node = ws.write("summary.md", output, [])
        assert (
            fs.auto_propagate_fragment_policy_labels(
                source_node,
                output_node,
                "policy_agent",
            )
            == 0
        )
        propagated = fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
            source_node,
            output_node,
            "policy_agent",
            tool_call_id="tc_normalized_number_attribution",
        )
        output_offset = output.index(b"7")
        assert propagated == 1
        assert fs.fragment_policy_labels(node_id=output_node)[0][0:5] == (
            output_node,
            output_offset,
            len(b"7"),
            "risk",
            "restricted",
        )
        history = fs.fragment_policy_labels(node_id=output_node, active_only=False)
        event = fs.event(history[0][5])
        payload = json.loads(event[6])
        assert payload["propagated_from_node_id"] == source_node
        assert payload["propagated_from_offset"] == source_offset
        assert payload["propagated_from_length"] == source_length

        duplicate_node = ws.write(
            "duplicate.md",
            b"Customer risk score: 7; audit score: 7\n",
            [],
        )
        assert (
            fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
                source_node,
                duplicate_node,
                "policy_agent",
            )
            == 0
        )
        assert fs.fragment_policy_labels(node_id=duplicate_node) == []

        fs.set_policy_rule(
            "risk",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, output_offset)) == output[
            0:output_offset
        ]
        try:
            fs.read_node_range(output_node, output_offset, len(b"7"))
            assert False, "normalized number attribution should block the mapped span"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_auto_attribution_propagates_json_bool_null_and_float_scalars():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = b'{"profile":{"active":true,"deleted":null,"score":0.75}}'
        source_node = ws.write("profile.json", source, [])
        spans = {row[0]: row for row in fs.json_field_spans(source_node)}
        active_span = spans["$.profile.active"]
        deleted_span = spans["$.profile.deleted"]
        score_span = spans["$.profile.score"]
        assert source[active_span[1] : active_span[1] + active_span[2]] == b"true"
        assert source[deleted_span[1] : deleted_span[1] + deleted_span[2]] == b"null"
        assert source[score_span[1] : score_span[1] + score_span[2]] == b"0.75"
        fs.set_json_field_policy_label(
            source_node, "$.profile.active", "profile-active", "sensitive", "policy_agent"
        )
        fs.set_json_field_policy_label(
            source_node, "$.profile.deleted", "profile-null", "sensitive", "policy_agent"
        )
        fs.set_json_field_policy_label(
            source_node, "$.profile.score", "profile-score", "sensitive", "policy_agent"
        )

        output = b"Profile active=true; deletion marker null; score 0.75.\n"
        output_node = ws.write("summary.md", output, [])
        propagated = fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
            source_node,
            output_node,
            "policy_agent",
            tool_call_id="tc_normalized_bool_null_float_attribution",
        )
        assert propagated == 3
        labels = {
            (row[3], row[4]): row
            for row in fs.fragment_policy_labels(node_id=output_node)
        }
        active_offset = output.index(b"true")
        null_offset = output.index(b"null")
        score_offset = output.index(b"0.75")
        assert labels[("profile-active", "sensitive")][1:3] == (
            active_offset,
            len(b"true"),
        )
        assert labels[("profile-null", "sensitive")][1:3] == (
            null_offset,
            len(b"null"),
        )
        assert labels[("profile-score", "sensitive")][1:3] == (
            score_offset,
            len(b"0.75"),
        )

        duplicate_output = (
            b"Profile active=true and true again; deletion marker null; score 0.75.\n"
        )
        duplicate_node = ws.write("duplicate.md", duplicate_output, [])
        assert (
            fs.auto_propagate_fragment_policy_labels_by_normalized_scalar(
                source_node,
                duplicate_node,
                "policy_agent",
            )
            == 2
        )
        duplicate_labels = {
            row[3]: row for row in fs.fragment_policy_labels(node_id=duplicate_node)
        }
        assert "profile-active" not in duplicate_labels
        assert duplicate_labels["profile-null"][1:3] == (
            duplicate_output.index(b"null"),
            len(b"null"),
        )
        fs.set_policy_rule(
            "profile-score",
            value="sensitive",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, score_offset)) == output[
            0:score_offset
        ]
        try:
            fs.read_node_range(output_node, score_offset, len(b"0.75"))
            assert False, "normalized float attribution should block the mapped span"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_auto_attribution_propagates_normalized_json_array_and_object_values():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        source = (
            b'{ "profile": { "flags": [ "urgent", "restricted" ], '
            b'"meta": { "risk": "high", "score": 7 } } }'
        )
        source_node = ws.write("profile.json", source, [])
        spans = {row[0]: row for row in fs.json_field_spans(source_node)}
        flags_span = spans["$.profile.flags"]
        meta_span = spans["$.profile.meta"]
        assert source[flags_span[1] : flags_span[1] + flags_span[2]] == (
            b'[ "urgent", "restricted" ]'
        )
        assert source[meta_span[1] : meta_span[1] + meta_span[2]] == (
            b'{ "risk": "high", "score": 7 }'
        )
        fs.set_json_field_policy_label(
            source_node,
            "$.profile.flags",
            "profile-flags",
            "restricted",
            "policy_agent",
        )
        fs.set_json_field_policy_label(
            source_node,
            "$.profile.meta",
            "profile-meta",
            "restricted",
            "policy_agent",
        )

        flags_json = b'["urgent","restricted"]'
        meta_json = b'{"risk":"high","score":7}'
        output = b"Flags " + flags_json + b"; meta " + meta_json + b".\n"
        output_node = ws.write("summary.md", output, [])
        assert (
            fs.auto_propagate_fragment_policy_labels(
                source_node,
                output_node,
                "policy_agent",
            )
            == 0
        )
        propagated = fs.auto_propagate_fragment_policy_labels_by_normalized_json(
            source_node,
            output_node,
            "policy_agent",
            tool_call_id="tc_normalized_json_value_attribution",
        )
        assert propagated == 2
        labels = {
            (row[3], row[4]): row
            for row in fs.fragment_policy_labels(node_id=output_node)
        }
        flags_offset = output.index(flags_json)
        meta_offset = output.index(meta_json)
        assert labels[("profile-flags", "restricted")][1:3] == (
            flags_offset,
            len(flags_json),
        )
        assert labels[("profile-meta", "restricted")][1:3] == (
            meta_offset,
            len(meta_json),
        )

        duplicate_output = (
            b"Flags "
            + flags_json
            + b"; duplicate "
            + flags_json
            + b"; meta "
            + meta_json
            + b".\n"
        )
        duplicate_node = ws.write("duplicate.md", duplicate_output, [])
        assert (
            fs.auto_propagate_fragment_policy_labels_by_normalized_json(
                source_node,
                duplicate_node,
                "policy_agent",
            )
            == 1
        )
        duplicate_labels = {
            row[3]: row for row in fs.fragment_policy_labels(node_id=duplicate_node)
        }
        assert "profile-flags" not in duplicate_labels
        assert duplicate_labels["profile-meta"][1:3] == (
            duplicate_output.index(meta_json),
            len(meta_json),
        )

        fs.set_policy_rule(
            "profile-meta",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(fs.read_node_range(output_node, 0, meta_offset)) == output[
            0:meta_offset
        ]
        try:
            fs.read_node_range(output_node, meta_offset, len(meta_json))
            assert False, "normalized JSON value attribution should block the mapped span"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_answer_outputs_inherit_citation_fragment_policy_labels():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        content = b'{"customer":{"name":"Ada","ssn":"123-45-6789"}}'
        source_node = writer.write("profile.json", content, [])
        writer.publish("profile.json", "artifact:profile@v1")
        fs.set_json_field_policy_label(
            source_node,
            "$.customer.ssn",
            "pii",
            "true",
            "policy_agent",
        )

        reader = fs.open_workspace("ws:reader", "reader_agent")
        answer = b"Ada is the customer."
        answer_node = reader.answer(
            "answers/profile.md",
            answer,
            ["artifact:profile@v1"],
        )
        assert fs.fragment_policy_labels(node_id=answer_node)[0][0:5] == (
            answer_node,
            0,
            len(answer),
            "pii",
            "true",
        )

        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node(answer_node)
            assert False, "answer should inherit citation fragment denial"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="ws:reader/answers") == []
        assert fs.verify_integrity() == []


def test_markdown_frontmatter_policy_labels_map_semantic_fields_to_fragments():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = (
            b"---\n"
            b"title: Contract note\n"
            b"owner_email: ada@example.com\n"
            b"\"owner.email\": quoted@example.com\n"
            b"owner:\n"
            b"  email: nested@example.com\n"
            b"  team: privacy\n"
            b"quoted_parent:\n"
            b"  \"team:name\": quoted-team\n"
            b"recipients:\n"
            b"  - legal@example.com\n"
            b"  - name: Ada Lovelace\n"
            b"summary: |\n"
            b"  Confidential line one\n"
            b"  Confidential line two\n"
            b"optional_owner: null\n"
            b"risk_score: 7\n"
            b"effective_date: 2026-06-09\n"
            b"reviewed_at: 2026-06-09T12:30:45Z\n"
            b"quoted_date: \"2026-06-09\"\n"
            b"quoted_scalars: [\"7\", 'true', !!int \"8\"]\n"
            b"escaped_scalar: \"Line\\nTwo \\u263A\"\n"
            b"single_quoted_escape: 'Ada''s team'\n"
            b"escaped_inline: {message: \"said \\\"hi\\\", then left\", team: ops}\n"
            b"escaped_sequence: [\"a, \\\"b\\\"\", \"line\\nnext\", 'Ada''s']\n"
            b"tags: [public, secret, 7, false]\n"
            b"date_values: [2026-06-09, \"2026-06-10\", 2026-06-09 12:30:45+00:00]\n"
            b"missing_values: [null, ~, present]\n"
            b"typed_score: !!str 7\n"
            b"typed_missing: !!null null\n"
            b"typed_when: !!timestamp 2026-06-09T12:00:00Z\n"
            b"typed_blob: !!binary SGVsbG8=\n"
            b"typed_set: !!set {read: null, write: null}\n"
            b"typed_omap: !!omap [{first: one}, {second: two}]\n"
            b"typed_values: [!!str 7, !!int 7, !!float 7.5, !!bool true, !!null null, !<tag:yaml.org,2002:str> 42, !<tag:yaml.org,2002:timestamp> 2026-06-09, !!binary SGVsbG8=]\n"
            b"typed_containers: [!!set {alpha: null}, !<tag:yaml.org,2002:omap> [{beta: two}]]\n"
            b"contact: {email: inline@example.com, team: legal, groups: [legal, privacy], manager: {email: manager@example.com, team: privacy}, priority: 3, active: true}\n"
            b"quoted_contact: {\"email.addr\": quoted-inline@example.com, 'team name': ops}\n"
            b"contacts: [{email: first@example.com, team: legal, meta: {risk: high, flags: [urgent, restricted], owner: Ada}}, {\"email.addr\": second@example.com, team: ops}]\n"
            b"decorated_owner: !pii &primary tagged@example.com\n"
            b"decorated_contact: !contact &contact_anchor {email: decorated@example.com, flags: !flagseq [review, !secret decorated-secret]}\n"
            b"alias_owner: *primary\n"
            b"alias_contact: {email: *primary, backups: [*primary, reviewer]}\n"
            b"anchored_template: &contact_template {email: template@example.com, flags: [sensitive-template, public-template]}\n"
            b"phone_template: &phone_template {phone: 555-0100}\n"
            b"alias_template: *contact_template\n"
            b"alias_template_chain: &template_alias *contact_template\n"
            b"alias_template_deep: *template_alias\n"
            b"merged_template: {<<: *contact_template, team: merged-team}\n"
            b"merged_template_chain: {<<: *template_alias, team: chain-team}\n"
            b"merged_array_template: {<<: [*contact_template, *phone_template], team: array-team}\n"
            b"scalar_alias_chain: &secondary *primary\n"
            b"scalar_alias_deep: *secondary\n"
            b"block_aliases:\n"
            b"  - *primary\n"
            b"  - reviewer\n"
            b"block_alias_field:\n"
            b"  - email: *primary\n"
            b"block_merge_items:\n"
            b"  - &block_template {email: block-template@example.com, label: inherited}\n"
            b"  - {<<: *block_template, label: explicit}\n"
            b"---\n"
            b"Body mentions renewal.\n"
        )
        node_id = ws.write("note.md", content, [])
        ws.publish("note.md", "artifact:note@v1")

        spans = fs.markdown_field_spans(node_id)
        span_by_path = {row[0]: row for row in spans}
        values_by_path = {row[0]: row for row in fs.markdown_field_values(node_id)}
        email_path, email_offset, email_length, email_kind = span_by_path[
            "frontmatter.owner_email"
        ]
        assert (email_path, email_kind) == ("frontmatter.owner_email", "string")
        assert content[email_offset : email_offset + email_length] == b"ada@example.com"
        nested_path, nested_offset, nested_length, nested_kind = span_by_path[
            "frontmatter.owner.email"
        ]
        assert (nested_path, nested_kind) == ("frontmatter.owner.email", "string")
        assert content[nested_offset : nested_offset + nested_length] == b"nested@example.com"
        quoted_top = span_by_path['frontmatter["owner.email"]']
        assert quoted_top[3] == "string"
        assert content[quoted_top[1] : quoted_top[1] + quoted_top[2]] == b"quoted@example.com"
        assert span_by_path["frontmatter.owner.team"][3] == "string"
        quoted_nested = span_by_path['frontmatter.quoted_parent["team:name"]']
        assert quoted_nested[3] == "string"
        assert content[quoted_nested[1] : quoted_nested[1] + quoted_nested[2]] == b"quoted-team"
        owner_span = span_by_path["frontmatter.owner"]
        assert owner_span[3] == "object"
        assert content[owner_span[1] : owner_span[1] + owner_span[2]].startswith(b"owner:\n")
        recipients_span = span_by_path["frontmatter.recipients"]
        assert recipients_span[3] == "array"
        assert b"legal@example.com" in content[
            recipients_span[1] : recipients_span[1] + recipients_span[2]
        ]
        first_recipient = span_by_path["frontmatter.recipients[0]"]
        assert first_recipient[3] == "string"
        assert content[first_recipient[1] : first_recipient[1] + first_recipient[2]] == (
            b"legal@example.com"
        )
        recipient_name = span_by_path["frontmatter.recipients[1].name"]
        assert recipient_name[3] == "string"
        assert content[recipient_name[1] : recipient_name[1] + recipient_name[2]] == (
            b"Ada Lovelace"
        )
        summary_span = span_by_path["frontmatter.summary"]
        assert summary_span[3] == "string"
        assert b"Confidential line two" in content[
            summary_span[1] : summary_span[1] + summary_span[2]
        ]
        optional_owner = span_by_path["frontmatter.optional_owner"]
        assert optional_owner[3] == "null"
        assert content[
            optional_owner[1] : optional_owner[1] + optional_owner[2]
        ] == b"null"
        assert span_by_path["frontmatter.risk_score"][3] == "number"
        effective_date = span_by_path["frontmatter.effective_date"]
        reviewed_at = span_by_path["frontmatter.reviewed_at"]
        quoted_date = span_by_path["frontmatter.quoted_date"]
        assert effective_date[3] == "timestamp"
        assert reviewed_at[3] == "timestamp"
        assert quoted_date[3] == "string"
        assert content[effective_date[1] : effective_date[1] + effective_date[2]] == (
            b"2026-06-09"
        )
        assert content[reviewed_at[1] : reviewed_at[1] + reviewed_at[2]] == (
            b"2026-06-09T12:30:45Z"
        )
        assert content[quoted_date[1] : quoted_date[1] + quoted_date[2]] == (
            b"2026-06-09"
        )
        quoted_scalars = span_by_path["frontmatter.quoted_scalars"]
        quoted_number = span_by_path["frontmatter.quoted_scalars[0]"]
        quoted_bool = span_by_path["frontmatter.quoted_scalars[1]"]
        typed_quoted_int = span_by_path["frontmatter.quoted_scalars[2]"]
        assert quoted_scalars[3] == "array"
        assert quoted_number[3] == quoted_bool[3] == "string"
        assert typed_quoted_int[3] == "number"
        assert content[quoted_number[1] : quoted_number[1] + quoted_number[2]] == b"7"
        assert content[quoted_bool[1] : quoted_bool[1] + quoted_bool[2]] == b"true"
        assert content[
            typed_quoted_int[1] : typed_quoted_int[1] + typed_quoted_int[2]
        ] == b"8"
        escaped_scalar = span_by_path["frontmatter.escaped_scalar"]
        single_quoted_escape = span_by_path["frontmatter.single_quoted_escape"]
        escaped_inline = span_by_path["frontmatter.escaped_inline"]
        escaped_inline_message = span_by_path["frontmatter.escaped_inline.message"]
        escaped_sequence = span_by_path["frontmatter.escaped_sequence"]
        escaped_sequence_first = span_by_path["frontmatter.escaped_sequence[0]"]
        escaped_sequence_second = span_by_path["frontmatter.escaped_sequence[1]"]
        escaped_sequence_third = span_by_path["frontmatter.escaped_sequence[2]"]
        assert escaped_scalar[3] == single_quoted_escape[3] == "string"
        assert escaped_inline[3] == "object"
        assert escaped_sequence[3] == "array"
        assert escaped_inline_message[3] == "string"
        assert escaped_sequence_first[3] == escaped_sequence_second[3] == "string"
        assert escaped_sequence_third[3] == "string"
        assert content[
            escaped_scalar[1] : escaped_scalar[1] + escaped_scalar[2]
        ] == b"Line\\nTwo \\u263A"
        assert content[
            escaped_inline_message[1] : escaped_inline_message[1]
            + escaped_inline_message[2]
        ] == b"said \\\"hi\\\", then left"
        assert values_by_path["frontmatter.escaped_scalar"] == (
            "frontmatter.escaped_scalar",
            "Line\nTwo \u263A",
            "string",
        )
        assert values_by_path["frontmatter.single_quoted_escape"] == (
            "frontmatter.single_quoted_escape",
            "Ada's team",
            "string",
        )
        assert values_by_path["frontmatter.escaped_inline.message"] == (
            "frontmatter.escaped_inline.message",
            'said "hi", then left',
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[0]"] == (
            "frontmatter.escaped_sequence[0]",
            'a, "b"',
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[1]"] == (
            "frontmatter.escaped_sequence[1]",
            "line\nnext",
            "string",
        )
        assert values_by_path["frontmatter.escaped_sequence[2]"] == (
            "frontmatter.escaped_sequence[2]",
            "Ada's",
            "string",
        )
        tags_span = span_by_path["frontmatter.tags"]
        assert tags_span[3] == "array"
        assert content[tags_span[1] : tags_span[1] + tags_span[2]] == (
            b"[public, secret, 7, false]"
        )
        first_tag = span_by_path["frontmatter.tags[0]"]
        secret_tag = span_by_path["frontmatter.tags[1]"]
        numeric_tag = span_by_path["frontmatter.tags[2]"]
        bool_tag = span_by_path["frontmatter.tags[3]"]
        assert first_tag[3] == secret_tag[3] == "string"
        assert numeric_tag[3] == "number"
        assert bool_tag[3] == "bool"
        assert content[first_tag[1] : first_tag[1] + first_tag[2]] == b"public"
        assert content[secret_tag[1] : secret_tag[1] + secret_tag[2]] == b"secret"
        date_values = span_by_path["frontmatter.date_values"]
        date_value = span_by_path["frontmatter.date_values[0]"]
        quoted_date_value = span_by_path["frontmatter.date_values[1]"]
        datetime_value = span_by_path["frontmatter.date_values[2]"]
        assert date_values[3] == "array"
        assert date_value[3] == "timestamp"
        assert quoted_date_value[3] == "string"
        assert datetime_value[3] == "timestamp"
        assert content[date_value[1] : date_value[1] + date_value[2]] == b"2026-06-09"
        assert content[
            quoted_date_value[1] : quoted_date_value[1] + quoted_date_value[2]
        ] == b"2026-06-10"
        assert content[datetime_value[1] : datetime_value[1] + datetime_value[2]] == (
            b"2026-06-09 12:30:45+00:00"
        )
        missing_values = span_by_path["frontmatter.missing_values"]
        missing_null = span_by_path["frontmatter.missing_values[0]"]
        missing_tilde = span_by_path["frontmatter.missing_values[1]"]
        missing_present = span_by_path["frontmatter.missing_values[2]"]
        assert missing_values[3] == "array"
        assert missing_null[3] == missing_tilde[3] == "null"
        assert missing_present[3] == "string"
        assert content[missing_null[1] : missing_null[1] + missing_null[2]] == b"null"
        assert content[missing_tilde[1] : missing_tilde[1] + missing_tilde[2]] == b"~"
        typed_score = span_by_path["frontmatter.typed_score"]
        typed_missing = span_by_path["frontmatter.typed_missing"]
        typed_when = span_by_path["frontmatter.typed_when"]
        typed_blob = span_by_path["frontmatter.typed_blob"]
        typed_set = span_by_path["frontmatter.typed_set"]
        typed_set_read = span_by_path["frontmatter.typed_set.read"]
        typed_omap = span_by_path["frontmatter.typed_omap"]
        typed_omap_first = span_by_path["frontmatter.typed_omap[0].first"]
        typed_values = span_by_path["frontmatter.typed_values"]
        typed_str = span_by_path["frontmatter.typed_values[0]"]
        typed_int = span_by_path["frontmatter.typed_values[1]"]
        typed_float = span_by_path["frontmatter.typed_values[2]"]
        typed_bool = span_by_path["frontmatter.typed_values[3]"]
        typed_null = span_by_path["frontmatter.typed_values[4]"]
        typed_uri_str = span_by_path["frontmatter.typed_values[5]"]
        typed_uri_timestamp = span_by_path["frontmatter.typed_values[6]"]
        typed_binary_item = span_by_path["frontmatter.typed_values[7]"]
        typed_containers = span_by_path["frontmatter.typed_containers"]
        typed_container_set = span_by_path["frontmatter.typed_containers[0]"]
        typed_container_omap = span_by_path["frontmatter.typed_containers[1]"]
        assert typed_score[3] == "string"
        assert typed_missing[3] == "null"
        assert typed_when[3] == "timestamp"
        assert typed_blob[3] == "binary"
        assert typed_set[3] == "set"
        assert typed_set_read[3] == "null"
        assert typed_omap[3] == "omap"
        assert typed_omap_first[3] == "string"
        assert typed_values[3] == "array"
        assert typed_str[3] == "string"
        assert typed_int[3] == typed_float[3] == "number"
        assert typed_bool[3] == "bool"
        assert typed_null[3] == "null"
        assert typed_uri_str[3] == "string"
        assert typed_uri_timestamp[3] == "timestamp"
        assert typed_binary_item[3] == "binary"
        assert typed_containers[3] == "array"
        assert typed_container_set[3] == "set"
        assert typed_container_omap[3] == "omap"
        assert content[typed_score[1] : typed_score[1] + typed_score[2]] == b"7"
        assert content[
            typed_missing[1] : typed_missing[1] + typed_missing[2]
        ] == b"null"
        assert content[
            typed_when[1] : typed_when[1] + typed_when[2]
        ] == b"2026-06-09T12:00:00Z"
        assert content[typed_blob[1] : typed_blob[1] + typed_blob[2]] == b"SGVsbG8="
        assert content[typed_set[1] : typed_set[1] + typed_set[2]] == (
            b"{read: null, write: null}"
        )
        assert content[typed_set_read[1] : typed_set_read[1] + typed_set_read[2]] == b"null"
        assert content[typed_omap[1] : typed_omap[1] + typed_omap[2]] == (
            b"[{first: one}, {second: two}]"
        )
        assert content[typed_omap_first[1] : typed_omap_first[1] + typed_omap_first[2]] == b"one"
        assert content[typed_str[1] : typed_str[1] + typed_str[2]] == b"7"
        assert content[typed_int[1] : typed_int[1] + typed_int[2]] == b"7"
        assert content[typed_float[1] : typed_float[1] + typed_float[2]] == b"7.5"
        assert content[typed_bool[1] : typed_bool[1] + typed_bool[2]] == b"true"
        assert content[typed_null[1] : typed_null[1] + typed_null[2]] == b"null"
        assert content[typed_uri_str[1] : typed_uri_str[1] + typed_uri_str[2]] == b"42"
        assert content[
            typed_uri_timestamp[1] : typed_uri_timestamp[1] + typed_uri_timestamp[2]
        ] == b"2026-06-09"
        assert content[
            typed_binary_item[1] : typed_binary_item[1] + typed_binary_item[2]
        ] == b"SGVsbG8="
        assert content[
            typed_container_set[1] : typed_container_set[1] + typed_container_set[2]
        ] == b"{alpha: null}"
        assert content[
            typed_container_omap[1] : typed_container_omap[1] + typed_container_omap[2]
        ] == b"[{beta: two}]"
        contact_span = span_by_path["frontmatter.contact"]
        contact_email = span_by_path["frontmatter.contact.email"]
        contact_team = span_by_path["frontmatter.contact.team"]
        contact_groups = span_by_path["frontmatter.contact.groups"]
        contact_group_legal = span_by_path["frontmatter.contact.groups[0]"]
        contact_group_privacy = span_by_path["frontmatter.contact.groups[1]"]
        contact_manager = span_by_path["frontmatter.contact.manager"]
        contact_manager_email = span_by_path["frontmatter.contact.manager.email"]
        contact_manager_team = span_by_path["frontmatter.contact.manager.team"]
        contact_priority = span_by_path["frontmatter.contact.priority"]
        contact_active = span_by_path["frontmatter.contact.active"]
        assert contact_span[3] == "object"
        assert content[contact_span[1] : contact_span[1] + contact_span[2]].startswith(
            b"{email: inline@example.com"
        )
        assert contact_email[3] == contact_team[3] == "string"
        assert contact_groups[3] == "array"
        assert contact_group_legal[3] == contact_group_privacy[3] == "string"
        assert content[
            contact_group_privacy[1] : contact_group_privacy[1] + contact_group_privacy[2]
        ] == b"privacy"
        assert contact_manager[3] == "object"
        assert contact_manager_email[3] == contact_manager_team[3] == "string"
        assert contact_priority[3] == "number"
        assert contact_active[3] == "bool"
        assert content[contact_email[1] : contact_email[1] + contact_email[2]] == (
            b"inline@example.com"
        )
        assert content[
            contact_manager_email[1] : contact_manager_email[1] + contact_manager_email[2]
        ] == b"manager@example.com"
        quoted_contact = span_by_path["frontmatter.quoted_contact"]
        quoted_contact_email = span_by_path['frontmatter.quoted_contact["email.addr"]']
        quoted_contact_team = span_by_path['frontmatter.quoted_contact["team name"]']
        assert quoted_contact[3] == "object"
        assert quoted_contact_email[3] == quoted_contact_team[3] == "string"
        assert content[
            quoted_contact_email[1] : quoted_contact_email[1] + quoted_contact_email[2]
        ] == b"quoted-inline@example.com"
        contacts_span = span_by_path["frontmatter.contacts"]
        first_contact = span_by_path["frontmatter.contacts[0]"]
        first_contact_email = span_by_path["frontmatter.contacts[0].email"]
        first_contact_team = span_by_path["frontmatter.contacts[0].team"]
        first_contact_meta = span_by_path["frontmatter.contacts[0].meta"]
        first_contact_meta_risk = span_by_path["frontmatter.contacts[0].meta.risk"]
        first_contact_meta_flags = span_by_path["frontmatter.contacts[0].meta.flags"]
        first_contact_meta_flag_urgent = span_by_path["frontmatter.contacts[0].meta.flags[0]"]
        first_contact_meta_flag_restricted = span_by_path["frontmatter.contacts[0].meta.flags[1]"]
        first_contact_meta_owner = span_by_path["frontmatter.contacts[0].meta.owner"]
        second_contact = span_by_path["frontmatter.contacts[1]"]
        second_contact_email = span_by_path['frontmatter.contacts[1]["email.addr"]']
        second_contact_team = span_by_path["frontmatter.contacts[1].team"]
        assert contacts_span[3] == "array"
        assert first_contact[3] == second_contact[3] == "object"
        assert first_contact_email[3] == first_contact_team[3] == "string"
        assert first_contact_meta[3] == "object"
        assert first_contact_meta_flags[3] == "array"
        assert first_contact_meta_flag_urgent[3] == first_contact_meta_flag_restricted[3] == "string"
        assert first_contact_meta_risk[3] == first_contact_meta_owner[3] == "string"
        assert second_contact_email[3] == second_contact_team[3] == "string"
        assert content[
            first_contact_email[1] : first_contact_email[1] + first_contact_email[2]
        ] == b"first@example.com"
        assert content[
            first_contact_meta_risk[1] : first_contact_meta_risk[1] + first_contact_meta_risk[2]
        ] == b"high"
        assert content[
            first_contact_meta_flag_restricted[1] : first_contact_meta_flag_restricted[1] + first_contact_meta_flag_restricted[2]
        ] == b"restricted"
        assert content[
            second_contact_email[1] : second_contact_email[1] + second_contact_email[2]
        ] == b"second@example.com"
        decorated_owner = span_by_path["frontmatter.decorated_owner"]
        decorated_contact = span_by_path["frontmatter.decorated_contact"]
        decorated_contact_email = span_by_path["frontmatter.decorated_contact.email"]
        decorated_contact_flags = span_by_path["frontmatter.decorated_contact.flags"]
        decorated_contact_flag_review = span_by_path["frontmatter.decorated_contact.flags[0]"]
        decorated_contact_flag_secret = span_by_path["frontmatter.decorated_contact.flags[1]"]
        assert decorated_owner[3] == "string"
        assert decorated_contact[3] == "object"
        assert decorated_contact_email[3] == "string"
        assert decorated_contact_flags[3] == "array"
        assert decorated_contact_flag_review[3] == decorated_contact_flag_secret[3] == "string"
        assert content[
            decorated_owner[1] : decorated_owner[1] + decorated_owner[2]
        ] == b"tagged@example.com"
        assert content[
            decorated_contact_email[1] : decorated_contact_email[1] + decorated_contact_email[2]
        ] == b"decorated@example.com"
        assert content[
            decorated_contact_flag_secret[1] : decorated_contact_flag_secret[1] + decorated_contact_flag_secret[2]
        ] == b"decorated-secret"
        alias_owner = span_by_path["frontmatter.alias_owner"]
        alias_owner_target = span_by_path["frontmatter.alias_owner.__target"]
        alias_contact = span_by_path["frontmatter.alias_contact"]
        alias_contact_email = span_by_path["frontmatter.alias_contact.email"]
        alias_contact_email_target = span_by_path["frontmatter.alias_contact.email.__target"]
        alias_contact_backups = span_by_path["frontmatter.alias_contact.backups"]
        alias_contact_backup_primary = span_by_path["frontmatter.alias_contact.backups[0]"]
        alias_contact_backup_primary_target = span_by_path[
            "frontmatter.alias_contact.backups[0].__target"
        ]
        alias_contact_backup_reviewer = span_by_path["frontmatter.alias_contact.backups[1]"]
        anchored_template = span_by_path["frontmatter.anchored_template"]
        anchored_template_email = span_by_path["frontmatter.anchored_template.email"]
        anchored_template_flags = span_by_path["frontmatter.anchored_template.flags"]
        anchored_template_flag_secret = span_by_path[
            "frontmatter.anchored_template.flags[0]"
        ]
        phone_template = span_by_path["frontmatter.phone_template"]
        phone_template_phone = span_by_path["frontmatter.phone_template.phone"]
        alias_template = span_by_path["frontmatter.alias_template"]
        alias_template_email = span_by_path["frontmatter.alias_template.email"]
        alias_template_flags = span_by_path["frontmatter.alias_template.flags"]
        alias_template_flag_secret = span_by_path["frontmatter.alias_template.flags[0]"]
        alias_template_chain = span_by_path["frontmatter.alias_template_chain"]
        alias_template_chain_email = span_by_path["frontmatter.alias_template_chain.email"]
        alias_template_chain_flags = span_by_path["frontmatter.alias_template_chain.flags"]
        alias_template_chain_flag_secret = span_by_path[
            "frontmatter.alias_template_chain.flags[0]"
        ]
        alias_template_deep = span_by_path["frontmatter.alias_template_deep"]
        alias_template_deep_email = span_by_path["frontmatter.alias_template_deep.email"]
        alias_template_deep_flags = span_by_path["frontmatter.alias_template_deep.flags"]
        alias_template_deep_flag_secret = span_by_path[
            "frontmatter.alias_template_deep.flags[0]"
        ]
        merged_template = span_by_path["frontmatter.merged_template"]
        merged_template_email = span_by_path["frontmatter.merged_template.email"]
        merged_template_flags = span_by_path["frontmatter.merged_template.flags"]
        merged_template_flag_secret = span_by_path["frontmatter.merged_template.flags[0]"]
        merged_template_team = span_by_path["frontmatter.merged_template.team"]
        merged_template_chain = span_by_path["frontmatter.merged_template_chain"]
        merged_template_chain_email = span_by_path[
            "frontmatter.merged_template_chain.email"
        ]
        merged_template_chain_flags = span_by_path[
            "frontmatter.merged_template_chain.flags"
        ]
        merged_template_chain_flag_secret = span_by_path[
            "frontmatter.merged_template_chain.flags[0]"
        ]
        merged_template_chain_team = span_by_path[
            "frontmatter.merged_template_chain.team"
        ]
        merged_array_template = span_by_path["frontmatter.merged_array_template"]
        merged_array_template_email = span_by_path[
            "frontmatter.merged_array_template.email"
        ]
        merged_array_template_flags = span_by_path[
            "frontmatter.merged_array_template.flags"
        ]
        merged_array_template_phone = span_by_path[
            "frontmatter.merged_array_template.phone"
        ]
        merged_array_template_team = span_by_path["frontmatter.merged_array_template.team"]
        scalar_alias_chain = span_by_path["frontmatter.scalar_alias_chain"]
        scalar_alias_chain_target = span_by_path[
            "frontmatter.scalar_alias_chain.__target"
        ]
        scalar_alias_deep = span_by_path["frontmatter.scalar_alias_deep"]
        scalar_alias_deep_target = span_by_path["frontmatter.scalar_alias_deep.__target"]
        block_aliases = span_by_path["frontmatter.block_aliases"]
        block_alias_primary = span_by_path["frontmatter.block_aliases[0]"]
        block_alias_primary_target = span_by_path["frontmatter.block_aliases[0].__target"]
        block_alias_reviewer = span_by_path["frontmatter.block_aliases[1]"]
        block_alias_field = span_by_path["frontmatter.block_alias_field"]
        block_alias_field_email = span_by_path["frontmatter.block_alias_field[0].email"]
        block_alias_field_email_target = span_by_path[
            "frontmatter.block_alias_field[0].email.__target"
        ]
        block_merge_items = span_by_path["frontmatter.block_merge_items"]
        block_merge_template = span_by_path["frontmatter.block_merge_items[0]"]
        block_merge_template_email = span_by_path[
            "frontmatter.block_merge_items[0].email"
        ]
        block_merge_template_label = span_by_path[
            "frontmatter.block_merge_items[0].label"
        ]
        block_merge_item = span_by_path["frontmatter.block_merge_items[1]"]
        block_merge_item_email = span_by_path["frontmatter.block_merge_items[1].email"]
        block_merge_item_label = span_by_path["frontmatter.block_merge_items[1].label"]
        assert alias_owner[3] == "alias"
        assert alias_owner_target[3] == "string"
        assert alias_contact[3] == "object"
        assert alias_contact_email[3] == "alias"
        assert alias_contact_email_target[3] == "string"
        assert alias_contact_backups[3] == "array"
        assert alias_contact_backup_primary[3] == "alias"
        assert alias_contact_backup_primary_target[3] == "string"
        assert alias_contact_backup_reviewer[3] == "string"
        assert anchored_template[3] == "object"
        assert anchored_template_email[3] == "string"
        assert anchored_template_flags[3] == "array"
        assert anchored_template_flag_secret[3] == "string"
        assert phone_template[3] == "object"
        assert phone_template_phone[3] == "string"
        assert alias_template[3] == "alias"
        assert alias_template_email[3] == "alias"
        assert alias_template_flags[3] == "alias"
        assert alias_template_flag_secret[3] == "alias"
        assert alias_template_chain[3] == "alias"
        assert alias_template_chain_email[3] == "alias"
        assert alias_template_chain_flags[3] == "alias"
        assert alias_template_chain_flag_secret[3] == "alias"
        assert alias_template_deep[3] == "alias"
        assert alias_template_deep_email[3] == "alias"
        assert alias_template_deep_flags[3] == "alias"
        assert alias_template_deep_flag_secret[3] == "alias"
        assert merged_template[3] == "object"
        assert merged_template_email[3] == "alias"
        assert merged_template_flags[3] == "alias"
        assert merged_template_flag_secret[3] == "alias"
        assert merged_template_team[3] == "string"
        assert merged_template_chain[3] == "object"
        assert merged_template_chain_email[3] == "alias"
        assert merged_template_chain_flags[3] == "alias"
        assert merged_template_chain_flag_secret[3] == "alias"
        assert merged_template_chain_team[3] == "string"
        assert merged_array_template[3] == "object"
        assert merged_array_template_email[3] == "alias"
        assert merged_array_template_flags[3] == "alias"
        assert merged_array_template_phone[3] == "alias"
        assert merged_array_template_team[3] == "string"
        assert scalar_alias_chain[3] == "alias"
        assert scalar_alias_chain_target[3] == "string"
        assert scalar_alias_deep[3] == "alias"
        assert scalar_alias_deep_target[3] == "string"
        assert block_aliases[3] == "array"
        assert block_alias_primary[3] == "alias"
        assert block_alias_primary_target[3] == "string"
        assert block_alias_reviewer[3] == "string"
        assert block_alias_field[3] == "array"
        assert block_alias_field_email[3] == "alias"
        assert block_alias_field_email_target[3] == "string"
        assert block_merge_items[3] == "array"
        assert block_merge_template[3] == block_merge_item[3] == "object"
        assert block_merge_template_email[3] == block_merge_template_label[3] == "string"
        assert block_merge_item_email[3] == "alias"
        assert block_merge_item_label[3] == "string"
        assert content[alias_owner[1] : alias_owner[1] + alias_owner[2]] == b"*primary"
        assert content[
            alias_owner_target[1] : alias_owner_target[1] + alias_owner_target[2]
        ] == b"tagged@example.com"
        assert content[
            alias_contact_email[1] : alias_contact_email[1] + alias_contact_email[2]
        ] == b"*primary"
        assert content[
            alias_contact_email_target[1] : alias_contact_email_target[1] + alias_contact_email_target[2]
        ] == b"tagged@example.com"
        assert content[
            alias_contact_backup_primary[1] : alias_contact_backup_primary[1] + alias_contact_backup_primary[2]
        ] == b"*primary"
        assert content[
            alias_contact_backup_primary_target[1] : alias_contact_backup_primary_target[1] + alias_contact_backup_primary_target[2]
        ] == b"tagged@example.com"
        assert content[
            anchored_template_email[1] : anchored_template_email[1] + anchored_template_email[2]
        ] == b"template@example.com"
        assert content[
            anchored_template_flag_secret[1] : anchored_template_flag_secret[1] + anchored_template_flag_secret[2]
        ] == b"sensitive-template"
        assert content[
            phone_template_phone[1] : phone_template_phone[1] + phone_template_phone[2]
        ] == b"555-0100"
        assert content[
            alias_template[1] : alias_template[1] + alias_template[2]
        ] == b"*contact_template"
        assert (
            alias_template_email[1],
            alias_template_email[2],
            alias_template_flags[1],
            alias_template_flags[2],
            alias_template_flag_secret[1],
            alias_template_flag_secret[2],
        ) == (
            alias_template[1],
            alias_template[2],
            alias_template[1],
            alias_template[2],
            alias_template[1],
            alias_template[2],
        )
        assert content[
            alias_template_chain[1] : alias_template_chain[1] + alias_template_chain[2]
        ] == b"*contact_template"
        assert (
            alias_template_chain_email[1],
            alias_template_chain_email[2],
            alias_template_chain_flags[1],
            alias_template_chain_flags[2],
            alias_template_chain_flag_secret[1],
            alias_template_chain_flag_secret[2],
        ) == (
            alias_template_chain[1],
            alias_template_chain[2],
            alias_template_chain[1],
            alias_template_chain[2],
            alias_template_chain[1],
            alias_template_chain[2],
        )
        assert content[
            alias_template_deep[1] : alias_template_deep[1] + alias_template_deep[2]
        ] == b"*template_alias"
        assert (
            alias_template_deep_email[1],
            alias_template_deep_email[2],
            alias_template_deep_flags[1],
            alias_template_deep_flags[2],
            alias_template_deep_flag_secret[1],
            alias_template_deep_flag_secret[2],
        ) == (
            alias_template_deep[1],
            alias_template_deep[2],
            alias_template_deep[1],
            alias_template_deep[2],
            alias_template_deep[1],
            alias_template_deep[2],
        )
        assert content[
            merged_template[1] : merged_template[1] + merged_template[2]
        ] == b"{<<: *contact_template, team: merged-team}"
        assert content[
            merged_template_email[1] : merged_template_email[1] + merged_template_email[2]
        ] == b"*contact_template"
        assert content[
            merged_template_team[1] : merged_template_team[1] + merged_template_team[2]
        ] == b"merged-team"
        assert (
            merged_template_email[1],
            merged_template_email[2],
            merged_template_flags[1],
            merged_template_flags[2],
            merged_template_flag_secret[1],
            merged_template_flag_secret[2],
        ) == (
            merged_template_email[1],
            merged_template_email[2],
            merged_template_email[1],
            merged_template_email[2],
            merged_template_email[1],
            merged_template_email[2],
        )
        assert content[
            merged_template_chain[1] : merged_template_chain[1]
            + merged_template_chain[2]
        ] == b"{<<: *template_alias, team: chain-team}"
        assert content[
            merged_template_chain_email[1] : merged_template_chain_email[1]
            + merged_template_chain_email[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_flags[1] : merged_template_chain_flags[1]
            + merged_template_chain_flags[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_flag_secret[1] : merged_template_chain_flag_secret[1]
            + merged_template_chain_flag_secret[2]
        ] == b"*template_alias"
        assert content[
            merged_template_chain_team[1] : merged_template_chain_team[1]
            + merged_template_chain_team[2]
        ] == b"chain-team"
        assert content[
            merged_array_template[1] : merged_array_template[1] + merged_array_template[2]
        ] == b"{<<: [*contact_template, *phone_template], team: array-team}"
        assert content[
            merged_array_template_email[1] : merged_array_template_email[1] + merged_array_template_email[2]
        ] == b"*contact_template"
        assert content[
            merged_array_template_flags[1] : merged_array_template_flags[1] + merged_array_template_flags[2]
        ] == b"*contact_template"
        assert content[
            merged_array_template_phone[1] : merged_array_template_phone[1] + merged_array_template_phone[2]
        ] == b"*phone_template"
        assert content[
            merged_array_template_team[1] : merged_array_template_team[1] + merged_array_template_team[2]
        ] == b"array-team"
        assert content[
            scalar_alias_chain[1] : scalar_alias_chain[1] + scalar_alias_chain[2]
        ] == b"*primary"
        assert content[
            scalar_alias_chain_target[1] : scalar_alias_chain_target[1]
            + scalar_alias_chain_target[2]
        ] == b"tagged@example.com"
        assert content[
            scalar_alias_deep[1] : scalar_alias_deep[1] + scalar_alias_deep[2]
        ] == b"*secondary"
        assert content[
            scalar_alias_deep_target[1] : scalar_alias_deep_target[1]
            + scalar_alias_deep_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_alias_primary[1] : block_alias_primary[1] + block_alias_primary[2]
        ] == b"*primary"
        assert content[
            block_alias_primary_target[1] : block_alias_primary_target[1] + block_alias_primary_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_alias_field_email[1] : block_alias_field_email[1] + block_alias_field_email[2]
        ] == b"*primary"
        assert content[
            block_alias_field_email_target[1] : block_alias_field_email_target[1] + block_alias_field_email_target[2]
        ] == b"tagged@example.com"
        assert content[
            block_merge_template_email[1] : block_merge_template_email[1] + block_merge_template_email[2]
        ] == b"block-template@example.com"
        assert content[
            block_merge_template_label[1] : block_merge_template_label[1] + block_merge_template_label[2]
        ] == b"inherited"
        assert content[
            block_merge_item_email[1] : block_merge_item_email[1] + block_merge_item_email[2]
        ] == b"*block_template"
        assert content[
            block_merge_item_label[1] : block_merge_item_label[1] + block_merge_item_label[2]
        ] == b"explicit"

        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.owner.email",
            "pii",
            "true",
            "policy_agent",
            tool_call_id="tc_markdown_field_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            nested_offset,
            nested_length,
            "pii",
            "true",
        )
        fs.set_policy_rule(
            "pii",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(node_id, nested_offset, nested_length)
            assert False, "markdown field policy should block the field value range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:note") == []
        fs.set_policy_rule(
            "confidential",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.summary",
            "confidential",
            "true",
            "policy_agent",
        )
        try:
            fs.read_node_range(node_id, summary_span[1], summary_span[2])
            assert False, "markdown block scalar policy should block the block range"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_policy_rule(
            "tag-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.tags[1]",
            "tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, first_tag[1], first_tag[2])) == b"public"
        try:
            fs.read_node_range(node_id, secret_tag[1], secret_tag[2])
            assert False, "markdown inline sequence item policy should block only the item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-null-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.missing_values[1]",
            "yaml-null-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, missing_present[1], missing_present[2])
        ) == b"present"
        try:
            fs.read_node_range(node_id, missing_tilde[1], missing_tilde[2])
            assert False, "YAML null item policy should block only that null item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-core-tag-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_values[5]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_int[1], typed_int[2])) == b"7"
        try:
            fs.read_node_range(node_id, typed_uri_str[1], typed_uri_str[2])
            assert False, "YAML core tag policy should block only that typed payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_values[6]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_binary_item[1], typed_binary_item[2])) == (
            b"SGVsbG8="
        )
        try:
            fs.read_node_range(node_id, typed_uri_timestamp[1], typed_uri_timestamp[2])
            assert False, "YAML timestamp tag policy should block only that typed payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.typed_set",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(fs.read_node_range(node_id, typed_omap[1], typed_omap[2])) == (
            b"[{first: one}, {second: two}]"
        )
        try:
            fs.read_node_range(node_id, typed_set[1], typed_set[2])
            assert False, "YAML set tag policy should block only that container payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.date_values[0]",
            "yaml-core-tag-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, quoted_date_value[1], quoted_date_value[2])
        ) == b"2026-06-10"
        try:
            fs.read_node_range(node_id, date_value[1], date_value[2])
            assert False, "implicit YAML timestamp policy should block only that payload"
        except anfs_core.PolicyDeniedError:
            pass
        fs.set_policy_rule(
            "contact-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contact.email",
            "contact-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, contact_team[1], contact_team[2])
        ) == b"legal"
        try:
            fs.read_node_range(node_id, contact_email[1], contact_email[2])
            assert False, "markdown inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "quoted-contact-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            'frontmatter.quoted_contact["email.addr"]',
            "quoted-contact-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, quoted_contact_team[1], quoted_contact_team[2])
        ) == b"ops"
        try:
            fs.read_node_range(
                node_id,
                quoted_contact_email[1],
                quoted_contact_email[2],
            )
            assert False, "quoted markdown inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "inline-sequence-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            'frontmatter.contacts[1]["email.addr"]',
            "inline-sequence-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, second_contact_team[1], second_contact_team[2])
        ) == b"ops"
        try:
            fs.read_node_range(
                node_id,
                second_contact_email[1],
                second_contact_email[2],
            )
            assert False, "inline sequence object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-inline-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contact.manager.email",
            "nested-inline-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, contact_manager_team[1], contact_manager_team[2])
        ) == b"privacy"
        try:
            fs.read_node_range(
                node_id,
                contact_manager_email[1],
                contact_manager_email[2],
            )
            assert False, "nested inline object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-sequence-object-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contacts[0].meta.risk",
            "nested-sequence-object-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                first_contact_meta_owner[1],
                first_contact_meta_owner[2],
            )
        ) == b"Ada"
        try:
            fs.read_node_range(
                node_id,
                first_contact_meta_risk[1],
                first_contact_meta_risk[2],
            )
            assert False, "nested inline sequence object field policy should block only the field"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "nested-inline-array-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.contacts[0].meta.flags[1]",
            "nested-inline-array-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                first_contact_meta_flag_urgent[1],
                first_contact_meta_flag_urgent[2],
            )
        ) == b"urgent"
        try:
            fs.read_node_range(
                node_id,
                first_contact_meta_flag_restricted[1],
                first_contact_meta_flag_restricted[2],
            )
            assert False, "nested inline array item policy should block only the item"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "decorated-yaml-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.decorated_contact.flags[1]",
            "decorated-yaml-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                decorated_contact_flag_review[1],
                decorated_contact_flag_review[2],
            )
        ) == b"review"
        try:
            fs.read_node_range(
                node_id,
                decorated_contact_flag_secret[1],
                decorated_contact_flag_secret[2],
            )
            assert False, "decorated YAML sequence item policy should block only the payload"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-alias-token-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_contact.backups[0]",
            "yaml-alias-token-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                alias_contact_backup_reviewer[1],
                alias_contact_backup_reviewer[2],
            )
        ) == b"reviewer"
        try:
            fs.read_node_range(
                node_id,
                alias_contact_backup_primary[1],
                alias_contact_backup_primary[2],
            )
            assert False, "YAML alias token policy should block only the alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-scalar-alias-target-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_contact.email.__target",
            "yaml-scalar-alias-target-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(node_id, alias_contact_email[1], alias_contact_email[2])
        ) == b"*primary"
        try:
            fs.read_node_range(
                node_id,
                alias_contact_email_target[1],
                alias_contact_email_target[2],
            )
            assert False, "scalar alias target policy should block the anchor payload"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-alias-expanded-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.alias_template.email",
            "yaml-alias-expanded-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                anchored_template_email[1],
                anchored_template_email[2],
            )
        ) == b"template@example.com"
        try:
            fs.read_node_range(node_id, alias_template[1], alias_template[2])
            assert False, "expanded YAML alias field policy should block the alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-merge-key-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.merged_template.email",
            "yaml-merge-key-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                anchored_template_email[1],
                anchored_template_email[2],
            )
        ) == b"template@example.com"
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_template_team[1],
                merged_template_team[2],
            )
        ) == b"merged-team"
        try:
            fs.read_node_range(
                node_id,
                merged_template_email[1],
                merged_template_email[2],
            )
            assert False, "YAML merge key policy should block only the merge alias token"
        except anfs_core.PolicyDeniedError:
            pass

        fs.set_policy_rule(
            "yaml-merge-array-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        fs.set_markdown_field_policy_label(
            node_id,
            "frontmatter.merged_array_template.phone",
            "yaml-merge-array-secret",
            "true",
            "policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_array_template_email[1],
                merged_array_template_email[2],
            )
        ) == b"*contact_template"
        assert bytes(
            fs.read_node_range(
                node_id,
                phone_template_phone[1],
                phone_template_phone[2],
            )
        ) == b"555-0100"
        assert bytes(
            fs.read_node_range(
                node_id,
                merged_array_template_team[1],
                merged_array_template_team[2],
            )
        ) == b"array-team"
        try:
            fs.read_node_range(
                node_id,
                merged_array_template_phone[1],
                merged_array_template_phone[2],
            )
            assert False, "YAML merge array policy should block only that merge alias token"
        except anfs_core.PolicyDeniedError:
            pass

        try:
            fs.set_markdown_field_policy_label(
                node_id,
                "frontmatter.missing",
                "pii",
                "true",
                "policy_agent",
            )
            assert False, "missing Markdown field path should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        plain_node = ws.write("plain.md", b"# No frontmatter\n", [])
        try:
            fs.markdown_field_spans(plain_node)
            assert False, "Markdown without frontmatter should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.verify_integrity() == []


def test_markdown_body_section_policy_labels_map_headings_to_fragments():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:writer", "writer_agent")
        content = (
            b"---\n"
            b"title: Contract note\n"
            b"---\n"
            b"# Overview\n"
            b"Public summary.\n"
            b"## Private Notes\n"
            b"Sensitive renewal strategy.\n"
            b"Internal paragraph line two.\n"
            b"\n"
            b"- Escalate internally\n"
            b"- Review privilege\n"
            b"\n"
            b"| Key | Value |\n"
            b"| --- | --- |\n"
            b"| Tier | Secret |\n"
            b"<div class=\"secret\">\n"
            b"Secret HTML note\n"
            b"</div>\n"
            b"\n"
            b"```text\n"
            b"# Not A Heading\n"
            b"```\n"
            b"---\n"
            b"    # Indented Not A Heading\n"
            b"    secret_indented()\n"
            b"### Nested Detail\n"
            b"Still private.\n"
            b"## Appendix\n"
            b"Public appendix.\n"
        )
        node_id = ws.write("sections.md", content, [])
        ws.publish("sections.md", "artifact:sections@v1")

        spans = fs.markdown_section_spans(node_id)
        span_by_path = {row[0]: row for row in spans}
        assert "body.not-a-heading" not in span_by_path
        assert span_by_path["body.overview"][3] == "h1"
        assert span_by_path["body.overview.paragraph.1"][3] == "paragraph"
        private_path, private_offset, private_length, private_kind = span_by_path[
            "body.private-notes"
        ]
        assert (private_path, private_kind) == ("body.private-notes", "h2")
        assert content[private_offset : private_offset + private_length].startswith(
            b"## Private Notes\n"
        )
        assert b"### Nested Detail\nStill private." in content[
            private_offset : private_offset + private_length
        ]
        assert b"## Appendix" not in content[private_offset : private_offset + private_length]
        paragraph_span = span_by_path["body.private-notes.paragraph.1"]
        assert paragraph_span[3] == "paragraph"
        assert b"Sensitive renewal strategy." in content[
            paragraph_span[1] : paragraph_span[1] + paragraph_span[2]
        ]
        first_paragraph_line = span_by_path["body.private-notes.paragraph.1.line.1"]
        second_paragraph_line = span_by_path["body.private-notes.paragraph.1.line.2"]
        assert first_paragraph_line[3] == second_paragraph_line[3] == "paragraph-line"
        assert content[
            first_paragraph_line[1] : first_paragraph_line[1] + first_paragraph_line[2]
        ] == b"Sensitive renewal strategy.\n"
        assert content[
            second_paragraph_line[1] : second_paragraph_line[1] + second_paragraph_line[2]
        ] == b"Internal paragraph line two.\n"
        list_span = span_by_path["body.private-notes.list.1"]
        assert list_span[3] == "list"
        assert b"Review privilege" in content[list_span[1] : list_span[1] + list_span[2]]
        list_item_span = span_by_path["body.private-notes.list.1.item.2"]
        assert list_item_span[3] == "list-item"
        assert b"Review privilege" in content[
            list_item_span[1] : list_item_span[1] + list_item_span[2]
        ]
        table_span = span_by_path["body.private-notes.table.1"]
        assert table_span[3] == "table"
        assert b"Tier | Secret" in content[table_span[1] : table_span[1] + table_span[2]]
        table_header_row = span_by_path["body.private-notes.table.1.row.1"]
        table_secret_row = span_by_path["body.private-notes.table.1.row.2"]
        assert table_header_row[3] == table_secret_row[3] == "table-row"
        assert b"Key | Value" in content[
            table_header_row[1] : table_header_row[1] + table_header_row[2]
        ]
        assert b"Tier | Secret" in content[
            table_secret_row[1] : table_secret_row[1] + table_secret_row[2]
        ]
        assert "body.private-notes.table.1.row.3" not in span_by_path
        html_span = span_by_path["body.private-notes.html.1"]
        assert html_span[3] == "html"
        assert b"Secret HTML note" in content[html_span[1] : html_span[1] + html_span[2]]
        code_span = span_by_path["body.private-notes.code.1"]
        assert code_span[3] == "code"
        assert b"# Not A Heading" in content[code_span[1] : code_span[1] + code_span[2]]
        thematic_span = span_by_path["body.private-notes.thematic-break.1"]
        assert thematic_span[3] == "thematic-break"
        assert content[thematic_span[1] : thematic_span[1] + thematic_span[2]] == b"---\n"
        indented_code_span = span_by_path["body.private-notes.code.2"]
        assert indented_code_span[3] == "code"
        assert b"# Indented Not A Heading" in content[
            indented_code_span[1] : indented_code_span[1] + indented_code_span[2]
        ]
        assert "body.indented-not-a-heading" not in span_by_path
        nested_paragraph = span_by_path["body.nested-detail.paragraph.1"]
        assert nested_paragraph[3] == "paragraph"

        fs.set_markdown_section_policy_label(
            node_id,
            "body.private-notes",
            "sensitivity",
            "restricted",
            "policy_agent",
            tool_call_id="tc_markdown_section_label",
        )
        assert fs.fragment_policy_labels(node_id=node_id)[0][0:5] == (
            node_id,
            private_offset,
            private_length,
            "sensitivity",
            "restricted",
        )

        fs.set_policy_rule(
            "sensitivity",
            value="restricted",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        overview_offset = content.index(b"# Overview")
        assert bytes(fs.read_node_range(node_id, overview_offset, len(b"# Overview"))) == b"# Overview"
        try:
            fs.read_node_range(node_id, private_offset, len(b"## Private Notes"))
            assert False, "markdown section policy should block the section range"
        except anfs_core.PolicyDeniedError:
            pass
        assert fs.query(prefix="artifact:sections") == []

        paragraph_line_content = (
            b"# Paragraph Lines\n"
            b"Public paragraph line one.\n"
            b"Secret paragraph line two.\n"
            b"\n"
            b"- Public list after paragraph\n"
        )
        paragraph_line_node = ws.write("paragraph-line.md", paragraph_line_content, [])
        paragraph_line_spans = {
            row[0]: row for row in fs.markdown_section_spans(paragraph_line_node)
        }
        paragraph_line_one = paragraph_line_spans[
            "body.paragraph-lines.paragraph.1.line.1"
        ]
        paragraph_line_two = paragraph_line_spans[
            "body.paragraph-lines.paragraph.1.line.2"
        ]
        fs.set_markdown_section_policy_label(
            paragraph_line_node,
            "body.paragraph-lines.paragraph.1.line.2",
            "paragraph-line-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "paragraph-line-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_one[1],
                len(b"Public paragraph line one."),
            )
        ) == b"Public paragraph line one."
        try:
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_two[1],
                len(b"Secret paragraph line two."),
            )
            assert False, "markdown paragraph line policy should block only the line"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                paragraph_line_node,
                paragraph_line_content.index(b"- Public list after paragraph"),
                len(b"- Public list after paragraph"),
            )
        ) == b"- Public list after paragraph"

        inline_emphasis_content = (
            b"# Inline Emphasis\n"
            b"Public *safe emphasis* and **secret strong** remain.\n"
            b"Private _secret emphasis_ and __public strong__ remain.\n"
            b"Code `*hidden emphasis*` then *outside emphasis*.\n"
        )
        inline_emphasis_node = ws.write(
            "inline-emphasis.md", inline_emphasis_content, []
        )
        inline_emphasis_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_emphasis_node)
        }
        safe_emphasis = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.emphasis.1"
        ]
        safe_emphasis_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.emphasis.1.text"
        ]
        secret_strong = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.strong.1"
        ]
        secret_strong_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.1.strong.1.text"
        ]
        secret_emphasis_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.2.emphasis.1.text"
        ]
        public_strong_text = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.2.strong.1.text"
        ]
        outside_emphasis = inline_emphasis_spans[
            "body.inline-emphasis.paragraph.1.line.3.emphasis.1"
        ]
        assert safe_emphasis[3] == "inline-emphasis"
        assert safe_emphasis_text[3] == secret_emphasis_text[3] == "inline-emphasis-text"
        assert secret_strong[3] == "inline-strong"
        assert secret_strong_text[3] == public_strong_text[3] == "inline-strong-text"
        assert inline_emphasis_content[
            safe_emphasis[1] : safe_emphasis[1] + safe_emphasis[2]
        ] == b"*safe emphasis*"
        assert inline_emphasis_content[
            safe_emphasis_text[1] : safe_emphasis_text[1] + safe_emphasis_text[2]
        ] == b"safe emphasis"
        assert inline_emphasis_content[
            secret_strong[1] : secret_strong[1] + secret_strong[2]
        ] == b"**secret strong**"
        assert inline_emphasis_content[
            secret_strong_text[1] : secret_strong_text[1] + secret_strong_text[2]
        ] == b"secret strong"
        assert inline_emphasis_content[
            secret_emphasis_text[1] : secret_emphasis_text[1] + secret_emphasis_text[2]
        ] == b"secret emphasis"
        assert inline_emphasis_content[
            public_strong_text[1] : public_strong_text[1] + public_strong_text[2]
        ] == b"public strong"
        assert inline_emphasis_content[
            outside_emphasis[1] : outside_emphasis[1] + outside_emphasis[2]
        ] == b"*outside emphasis*"
        assert "body.inline-emphasis.paragraph.1.line.3.emphasis.2" not in inline_emphasis_spans
        fs.set_markdown_section_policy_label(
            inline_emphasis_node,
            "body.inline-emphasis.paragraph.1.line.1.strong.1.text",
            "inline-strong-text-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-strong-text-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_emphasis_node,
                safe_emphasis_text[1],
                len(b"safe emphasis"),
            )
        ) == b"safe emphasis"
        assert bytes(
            fs.read_node_range(inline_emphasis_node, secret_strong[1], len(b"**"))
        ) == b"**"
        try:
            fs.read_node_range(
                inline_emphasis_node,
                secret_strong_text[1],
                len(b"secret strong"),
            )
            assert False, "markdown inline strong text policy should block only the text"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_emphasis_node,
                inline_emphasis_content.index(b" remain."),
                len(b" remain."),
            )
        ) == b" remain."

        escaped_inline_content = (
            b"# Escaped Inline\n"
            b"Literal \\*not emphasis\\* then *real emphasis*.\n"
            b"Literal \\**not strong\\** then **real strong**.\n"
            b"Literal \\[not link](https://example.test/no) then [real link](https://example.test/yes).\n"
            b"Label [literal \\] bracket](https://example.test/label) remains.\n"
            b"Image literal \\![not image] then ![real image](https://example.test/yes.png).\n"
            b"Code escaped \\`not code\\` then `real code`.\n"
            b"Literal \\<https://example.test/no> then <https://example.test/yes>.\n"
            b"Escaped reference \\[not ref][doc] then [real ref][doc].\n"
            b"[doc]: https://example.test/doc\n"
        )
        escaped_inline_node = ws.write("escaped-inline.md", escaped_inline_content, [])
        escaped_inline_spans = {
            row[0]: row for row in fs.markdown_section_spans(escaped_inline_node)
        }
        real_emphasis = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.1.emphasis.1"
        ]
        real_strong = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.2.strong.1"
        ]
        real_link = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.3.link.1"
        ]
        escaped_bracket_label = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.4.link.1.label"
        ]
        real_image = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.5.image.1"
        ]
        real_code = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.6.code.1"
        ]
        real_autolink = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.7.autolink.1"
        ]
        real_reference = escaped_inline_spans[
            "body.escaped-inline.paragraph.1.line.8.reference-link.1"
        ]
        assert escaped_inline_content[
            real_emphasis[1] : real_emphasis[1] + real_emphasis[2]
        ] == b"*real emphasis*"
        assert escaped_inline_content[
            real_strong[1] : real_strong[1] + real_strong[2]
        ] == b"**real strong**"
        assert escaped_inline_content[
            real_link[1] : real_link[1] + real_link[2]
        ] == b"[real link](https://example.test/yes)"
        assert escaped_inline_content[
            escaped_bracket_label[1] : escaped_bracket_label[1] + escaped_bracket_label[2]
        ] == b"literal \\] bracket"
        assert escaped_inline_content[
            real_image[1] : real_image[1] + real_image[2]
        ] == b"![real image](https://example.test/yes.png)"
        assert escaped_inline_content[
            real_code[1] : real_code[1] + real_code[2]
        ] == b"`real code`"
        assert escaped_inline_content[
            real_autolink[1] : real_autolink[1] + real_autolink[2]
        ] == b"<https://example.test/yes>"
        assert escaped_inline_content[
            real_reference[1] : real_reference[1] + real_reference[2]
        ] == b"[real ref][doc]"
        assert "body.escaped-inline.paragraph.1.line.1.emphasis.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.2.strong.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.3.link.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.5.image.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.6.code.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.7.autolink.2" not in escaped_inline_spans
        assert "body.escaped-inline.paragraph.1.line.8.reference-link.2" not in escaped_inline_spans

        inline_link_content = (
            b"# Inline Links\n"
            b"Public context [safe link](https://example.test/public).\n"
            b"Secret pointer [private link](https://example.test/private) remains.\n"
            b"Image marker ![not a link](https://example.test/image.png) stays plain.\n"
        )
        inline_link_node = ws.write("inline-link.md", inline_link_content, [])
        inline_link_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_node)
        }
        safe_link = inline_link_spans["body.inline-links.paragraph.1.line.1.link.1"]
        private_link = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1"
        ]
        safe_link_label = inline_link_spans[
            "body.inline-links.paragraph.1.line.1.link.1.label"
        ]
        private_link_label = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1.label"
        ]
        safe_link_destination = inline_link_spans[
            "body.inline-links.paragraph.1.line.1.link.1.destination"
        ]
        private_link_destination = inline_link_spans[
            "body.inline-links.paragraph.1.line.2.link.1.destination"
        ]
        assert safe_link[3] == private_link[3] == "inline-link"
        assert safe_link_label[3] == private_link_label[3] == "inline-link-label"
        assert (
            safe_link_destination[3]
            == private_link_destination[3]
            == "inline-link-destination"
        )
        assert "body.inline-links.paragraph.1.line.3.link.1" not in inline_link_spans
        assert inline_link_content[safe_link[1] : safe_link[1] + safe_link[2]] == (
            b"[safe link](https://example.test/public)"
        )
        assert inline_link_content[
            private_link[1] : private_link[1] + private_link[2]
        ] == b"[private link](https://example.test/private)"
        assert inline_link_content[
            safe_link_label[1] : safe_link_label[1] + safe_link_label[2]
        ] == b"safe link"
        assert inline_link_content[
            private_link_label[1] : private_link_label[1] + private_link_label[2]
        ] == b"private link"
        assert inline_link_content[
            safe_link_destination[1] : safe_link_destination[1] + safe_link_destination[2]
        ] == b"https://example.test/public"
        assert inline_link_content[
            private_link_destination[1] : private_link_destination[1] + private_link_destination[2]
        ] == b"https://example.test/private"
        fs.set_markdown_section_policy_label(
            inline_link_node,
            "body.inline-links.paragraph.1.line.2.link.1",
            "inline-link-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(inline_link_node, safe_link[1], len(b"[safe link]"))
        ) == b"[safe link]"
        assert bytes(
            fs.read_node_range(
                inline_link_node,
                inline_link_content.index(b"Secret pointer"),
                len(b"Secret pointer"),
            )
        ) == b"Secret pointer"
        try:
            fs.read_node_range(inline_link_node, private_link[1], len(b"[private link]"))
            assert False, "markdown inline link policy should block only the link"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_node,
                inline_link_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_label_content = (
            b"# Inline Link Labels\n"
            b"Public wrapper [secret label](https://example.test/public-target) remains.\n"
        )
        inline_link_label_node = ws.write(
            "inline-link-label.md", inline_link_label_content, []
        )
        inline_link_label_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_label_node)
        }
        secret_label = inline_link_label_spans[
            "body.inline-link-labels.paragraph.1.line.1.link.1.label"
        ]
        public_destination = inline_link_label_spans[
            "body.inline-link-labels.paragraph.1.line.1.link.1.destination"
        ]
        assert secret_label[3] == "inline-link-label"
        assert public_destination[3] == "inline-link-destination"
        assert inline_link_label_content[
            secret_label[1] : secret_label[1] + secret_label[2]
        ] == b"secret label"
        fs.set_markdown_section_policy_label(
            inline_link_label_node,
            "body.inline-link-labels.paragraph.1.line.1.link.1.label",
            "inline-link-label-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-label-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_label_node,
                public_destination[1],
                len(b"https://example.test/public-target"),
            )
        ) == b"https://example.test/public-target"
        try:
            fs.read_node_range(
                inline_link_label_node,
                secret_label[1],
                len(b"secret label"),
            )
            assert False, "markdown inline link label policy should block only the label"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_label_node,
                inline_link_label_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_destination_content = (
            b"# Inline Link Destinations\n"
            b"Public label [secret destination](https://example.test/secret-destination) remains.\n"
        )
        inline_link_destination_node = ws.write(
            "inline-link-destination.md", inline_link_destination_content, []
        )
        inline_link_destination_spans = {
            row[0]: row
            for row in fs.markdown_section_spans(inline_link_destination_node)
        }
        secret_destination = inline_link_destination_spans[
            "body.inline-link-destinations.paragraph.1.line.1.link.1.destination"
        ]
        assert secret_destination[3] == "inline-link-destination"
        assert inline_link_destination_content[
            secret_destination[1] : secret_destination[1] + secret_destination[2]
        ] == b"https://example.test/secret-destination"
        fs.set_markdown_section_policy_label(
            inline_link_destination_node,
            "body.inline-link-destinations.paragraph.1.line.1.link.1.destination",
            "inline-link-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_destination_node,
                inline_link_destination_content.index(b"[secret destination]"),
                len(b"[secret destination]"),
            )
        ) == b"[secret destination]"
        try:
            fs.read_node_range(
                inline_link_destination_node,
                secret_destination[1],
                len(b"https://example.test/secret-destination"),
            )
            assert False, "markdown inline link destination policy should block only the destination"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_destination_node,
                inline_link_destination_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_link_title_content = (
            b"# Inline Link Titles\n"
            b"Public wrapper [secret title](https://example.test/title-target \"Secret Title\") remains.\n"
            b"Paren wrapper [paren title](https://example.test/paren-target (Paren Link Title)) remains.\n"
        )
        inline_link_title_node = ws.write(
            "inline-link-title.md", inline_link_title_content, []
        )
        inline_link_title_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_link_title_node)
        }
        title_destination = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.1.link.1.destination"
        ]
        secret_title = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.1.link.1.title"
        ]
        paren_title_destination = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.2.link.1.destination"
        ]
        paren_title = inline_link_title_spans[
            "body.inline-link-titles.paragraph.1.line.2.link.1.title"
        ]
        assert title_destination[3] == "inline-link-destination"
        assert paren_title_destination[3] == "inline-link-destination"
        assert secret_title[3] == paren_title[3] == "inline-link-title"
        assert inline_link_title_content[
            title_destination[1] : title_destination[1] + title_destination[2]
        ] == b"https://example.test/title-target"
        assert inline_link_title_content[
            secret_title[1] : secret_title[1] + secret_title[2]
        ] == b"Secret Title"
        assert inline_link_title_content[
            paren_title_destination[1] : paren_title_destination[1]
            + paren_title_destination[2]
        ] == b"https://example.test/paren-target"
        assert inline_link_title_content[
            paren_title[1] : paren_title[1] + paren_title[2]
        ] == b"Paren Link Title"
        fs.set_markdown_section_policy_label(
            inline_link_title_node,
            "body.inline-link-titles.paragraph.1.line.1.link.1.title",
            "inline-link-title-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-link-title-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_link_title_node,
                title_destination[1],
                len(b"https://example.test/title-target"),
            )
        ) == b"https://example.test/title-target"
        try:
            fs.read_node_range(
                inline_link_title_node,
                secret_title[1],
                len(b"Secret Title"),
            )
            assert False, "markdown inline link title policy should block only the title"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_link_title_node,
                inline_link_title_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        nested_destination_content = (
            b"# Nested Destinations\n"
            b"Link [nested](https://example.test/report(v2)/final) remains.\n"
            b"Titled [nested title](https://example.test/report(v2) \"Nested Title\") remains.\n"
            b"Image ![nested image](https://example.test/chart(v2).png) remains.\n"
            b"Escaped [literal parens](https://example.test/a\\(literal\\)) remains.\n"
            b"Angle [angle destination](<https://example.test/angle path>) remains.\n"
            b"Angle title [angle title](<https://example.test/angle-title> \"Angle Title\") remains.\n"
            b"Angle image ![angle image](<https://example.test/angle image.png>) remains.\n"
        )
        nested_destination_node = ws.write(
            "nested-destination.md", nested_destination_content, []
        )
        nested_destination_spans = {
            row[0]: row for row in fs.markdown_section_spans(nested_destination_node)
        }
        nested_link = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.1.link.1"
        ]
        nested_link_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.1.link.1.destination"
        ]
        nested_titled_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.2.link.1.destination"
        ]
        nested_title = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.2.link.1.title"
        ]
        nested_image_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.3.image.1.destination"
        ]
        escaped_paren_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.4.link.1.destination"
        ]
        angle_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.5.link.1.destination"
        ]
        angle_title_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.6.link.1.destination"
        ]
        angle_title = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.6.link.1.title"
        ]
        angle_image_destination = nested_destination_spans[
            "body.nested-destinations.paragraph.1.line.7.image.1.destination"
        ]
        assert nested_link[3] == "inline-link"
        assert (
            nested_link_destination[3]
            == nested_titled_destination[3]
            == angle_destination[3]
            == angle_title_destination[3]
            == "inline-link-destination"
        )
        assert nested_title[3] == "inline-link-title"
        assert angle_title[3] == "inline-link-title"
        assert nested_image_destination[3] == angle_image_destination[3] == "inline-image-destination"
        assert escaped_paren_destination[3] == "inline-link-destination"
        assert nested_destination_content[
            nested_link[1] : nested_link[1] + nested_link[2]
        ] == b"[nested](https://example.test/report(v2)/final)"
        assert nested_destination_content[
            nested_link_destination[1] : nested_link_destination[1] + nested_link_destination[2]
        ] == b"https://example.test/report(v2)/final"
        assert nested_destination_content[
            nested_titled_destination[1] : nested_titled_destination[1]
            + nested_titled_destination[2]
        ] == b"https://example.test/report(v2)"
        assert nested_destination_content[
            nested_title[1] : nested_title[1] + nested_title[2]
        ] == b"Nested Title"
        assert nested_destination_content[
            nested_image_destination[1] : nested_image_destination[1]
            + nested_image_destination[2]
        ] == b"https://example.test/chart(v2).png"
        assert nested_destination_content[
            escaped_paren_destination[1] : escaped_paren_destination[1]
            + escaped_paren_destination[2]
        ] == b"https://example.test/a\\(literal\\)"
        assert nested_destination_content[
            angle_destination[1] : angle_destination[1] + angle_destination[2]
        ] == b"https://example.test/angle path"
        assert nested_destination_content[
            angle_title_destination[1] : angle_title_destination[1]
            + angle_title_destination[2]
        ] == b"https://example.test/angle-title"
        assert nested_destination_content[
            angle_title[1] : angle_title[1] + angle_title[2]
        ] == b"Angle Title"
        assert nested_destination_content[
            angle_image_destination[1] : angle_image_destination[1]
            + angle_image_destination[2]
        ] == b"https://example.test/angle image.png"

        reference_link_content = (
            b"# Reference Links\n"
            b"Public pointer [public label][Public Ref] remains.\n"
            b"Secret pointer [safe label][private ref] remains.\n"
            b"Code `[hidden][private ref]` then [outside][public ref].\n"
            b"Collapsed pointer [collapsed ref][] remains.\n"
            b"Shortcut pointer [shortcut ref] remains.\n"
            b"Parenthesized pointer [paren label][paren ref] remains.\n"
            b"Angle pointer [angle label][angle ref] remains.\n"
            b"Escaped pointer [escaped label][escaped\\] ref] remains.\n"
            b"\n"
            b"[public ref]: https://example.test/public-reference\n"
            b"[private ref]: https://example.test/private-reference \"Secret Reference Title\"\n"
            b"[collapsed ref]: https://example.test/collapsed-reference\n"
            b"[shortcut ref]: https://example.test/shortcut-reference\n"
            b"[paren ref]: https://example.test/paren-reference (Paren Reference Title)\n"
            b"[angle ref]: <https://example.test/angle reference> \"Angle Reference Title\"\n"
            b"[escaped\\] ref]: https://example.test/escaped-reference\n"
        )
        reference_link_node = ws.write("reference-link.md", reference_link_content, [])
        reference_link_spans = {
            row[0]: row for row in fs.markdown_section_spans(reference_link_node)
        }
        public_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.1.reference-link.1"
        ]
        private_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1"
        ]
        private_reference_label = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.label"
        ]
        private_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.reference"
        ]
        private_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-destination"
        ]
        private_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-title"
        ]
        private_reference_definition_label = reference_link_spans[
            "body.reference-links.link-reference.2.label"
        ]
        private_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.2.destination"
        ]
        private_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.2.title"
        ]
        hidden_reference_code = reference_link_spans[
            "body.reference-links.paragraph.1.line.3.code.1"
        ]
        outside_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.3.reference-link.1"
        ]
        collapsed_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1"
        ]
        collapsed_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1.reference"
        ]
        collapsed_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.4.reference-link.1.resolved-destination"
        ]
        shortcut_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1"
        ]
        shortcut_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1.reference"
        ]
        shortcut_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.5.reference-link.1.resolved-destination"
        ]
        paren_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.6.reference-link.1"
        ]
        paren_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.6.reference-link.1.resolved-title"
        ]
        paren_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.5.title"
        ]
        angle_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1"
        ]
        angle_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1.resolved-destination"
        ]
        angle_resolved_title = reference_link_spans[
            "body.reference-links.paragraph.1.line.7.reference-link.1.resolved-title"
        ]
        angle_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.6.destination"
        ]
        angle_reference_definition_title = reference_link_spans[
            "body.reference-links.link-reference.6.title"
        ]
        escaped_reference_link = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1"
        ]
        escaped_reference_marker = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1.reference"
        ]
        escaped_resolved_destination = reference_link_spans[
            "body.reference-links.paragraph.1.line.8.reference-link.1.resolved-destination"
        ]
        escaped_reference_definition_label = reference_link_spans[
            "body.reference-links.link-reference.7.label"
        ]
        escaped_reference_definition_destination = reference_link_spans[
            "body.reference-links.link-reference.7.destination"
        ]
        assert (
            public_reference_link[3]
            == private_reference_link[3]
            == outside_reference_link[3]
            == collapsed_reference_link[3]
            == shortcut_reference_link[3]
            == paren_reference_link[3]
            == angle_reference_link[3]
            == escaped_reference_link[3]
            == "reference-link"
        )
        assert private_reference_label[3] == "reference-link-label"
        assert private_reference_marker[3] == "reference-link-reference"
        assert (
            private_resolved_destination[3]
            == "reference-link-resolved-destination"
        )
        assert private_resolved_title[3] == "reference-link-resolved-title"
        assert private_reference_definition_label[3] == "link-reference-label"
        assert (
            private_reference_definition_destination[3]
            == "link-reference-destination"
        )
        assert private_reference_definition_title[3] == "link-reference-title"
        assert hidden_reference_code[3] == "inline-code"
        assert (
            "body.reference-links.paragraph.1.line.3.reference-link.2"
            not in reference_link_spans
        )
        assert collapsed_reference_marker[3] == "reference-link-reference"
        assert collapsed_reference_marker[2] == 0
        assert collapsed_resolved_destination[3] == "reference-link-resolved-destination"
        assert shortcut_reference_marker[3] == "reference-link-reference"
        assert shortcut_reference_marker[2] == 0
        assert shortcut_resolved_destination[3] == "reference-link-resolved-destination"
        assert paren_resolved_title[3] == "reference-link-resolved-title"
        assert angle_resolved_destination[3] == "reference-link-resolved-destination"
        assert angle_resolved_title[3] == "reference-link-resolved-title"
        assert paren_reference_definition_title[3] == "link-reference-title"
        assert angle_reference_definition_destination[3] == "link-reference-destination"
        assert angle_reference_definition_title[3] == "link-reference-title"
        assert escaped_reference_marker[3] == "reference-link-reference"
        assert escaped_resolved_destination[3] == "reference-link-resolved-destination"
        assert escaped_reference_definition_label[3] == "link-reference-label"
        assert escaped_reference_definition_destination[3] == "link-reference-destination"
        assert reference_link_content[
            public_reference_link[1] : public_reference_link[1]
            + public_reference_link[2]
        ] == b"[public label][Public Ref]"
        assert reference_link_content[
            private_reference_label[1] : private_reference_label[1]
            + private_reference_label[2]
        ] == b"safe label"
        assert reference_link_content[
            private_reference_marker[1] : private_reference_marker[1]
            + private_reference_marker[2]
        ] == b"private ref"
        assert reference_link_content[
            private_resolved_destination[1] : private_resolved_destination[1]
            + private_resolved_destination[2]
        ] == b"https://example.test/private-reference"
        assert reference_link_content[
            private_resolved_title[1] : private_resolved_title[1]
            + private_resolved_title[2]
        ] == b"Secret Reference Title"
        assert reference_link_content[
            private_reference_definition_label[1] : private_reference_definition_label[1]
            + private_reference_definition_label[2]
        ] == b"private ref"
        assert reference_link_content[
            private_reference_definition_destination[1] : private_reference_definition_destination[1]
            + private_reference_definition_destination[2]
        ] == b"https://example.test/private-reference"
        assert reference_link_content[
            private_reference_definition_title[1] : private_reference_definition_title[1]
            + private_reference_definition_title[2]
        ] == b"Secret Reference Title"
        assert reference_link_content[
            paren_resolved_title[1] : paren_resolved_title[1] + paren_resolved_title[2]
        ] == b"Paren Reference Title"
        assert reference_link_content[
            paren_reference_definition_title[1] : paren_reference_definition_title[1]
            + paren_reference_definition_title[2]
        ] == b"Paren Reference Title"
        assert reference_link_content[
            angle_resolved_destination[1] : angle_resolved_destination[1]
            + angle_resolved_destination[2]
        ] == b"https://example.test/angle reference"
        assert reference_link_content[
            angle_reference_definition_destination[1] : angle_reference_definition_destination[1]
            + angle_reference_definition_destination[2]
        ] == b"https://example.test/angle reference"
        assert reference_link_content[
            angle_resolved_title[1] : angle_resolved_title[1] + angle_resolved_title[2]
        ] == b"Angle Reference Title"
        assert reference_link_content[
            angle_reference_definition_title[1] : angle_reference_definition_title[1]
            + angle_reference_definition_title[2]
        ] == b"Angle Reference Title"
        assert reference_link_content[
            escaped_reference_marker[1] : escaped_reference_marker[1]
            + escaped_reference_marker[2]
        ] == b"escaped\\] ref"
        assert reference_link_content[
            escaped_resolved_destination[1] : escaped_resolved_destination[1]
            + escaped_resolved_destination[2]
        ] == b"https://example.test/escaped-reference"
        assert reference_link_content[
            escaped_reference_definition_label[1] : escaped_reference_definition_label[1]
            + escaped_reference_definition_label[2]
        ] == b"escaped\\] ref"
        assert reference_link_content[
            escaped_reference_definition_destination[1] : escaped_reference_definition_destination[1]
            + escaped_reference_definition_destination[2]
        ] == b"https://example.test/escaped-reference"
        assert reference_link_content[
            hidden_reference_code[1] : hidden_reference_code[1]
            + hidden_reference_code[2]
        ] == b"`[hidden][private ref]`"
        assert reference_link_content[
            collapsed_reference_link[1] : collapsed_reference_link[1]
            + collapsed_reference_link[2]
        ] == b"[collapsed ref][]"
        assert reference_link_content[
            collapsed_resolved_destination[1] : collapsed_resolved_destination[1]
            + collapsed_resolved_destination[2]
        ] == b"https://example.test/collapsed-reference"
        assert reference_link_content[
            shortcut_reference_link[1] : shortcut_reference_link[1]
            + shortcut_reference_link[2]
        ] == b"[shortcut ref]"
        assert reference_link_content[
            shortcut_resolved_destination[1] : shortcut_resolved_destination[1]
            + shortcut_resolved_destination[2]
        ] == b"https://example.test/shortcut-reference"
        fs.set_markdown_section_policy_label(
            reference_link_node,
            "body.reference-links.paragraph.1.line.2.reference-link.1.resolved-destination",
            "reference-link-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "reference-link-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                private_reference_link[1],
                len(b"[safe label][private ref]"),
            )
        ) == b"[safe label][private ref]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                private_resolved_title[1],
                len(b"Secret Reference Title"),
            )
        ) == b"Secret Reference Title"
        try:
            fs.read_node_range(
                reference_link_node,
                private_resolved_destination[1],
                len(b"https://example.test/private-reference"),
            )
            assert False, "reference-link resolved destination policy should block only the definition target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                outside_reference_link[1],
                len(b"[outside][public ref]"),
            )
        ) == b"[outside][public ref]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                collapsed_reference_link[1],
                len(b"[collapsed ref][]"),
            )
        ) == b"[collapsed ref][]"
        assert bytes(
            fs.read_node_range(
                reference_link_node,
                shortcut_reference_link[1],
                len(b"[shortcut ref]"),
            )
        ) == b"[shortcut ref]"

        reference_image_content = (
            b"# Reference Images\n"
            b"Public figure ![public chart][Public Image] remains.\n"
            b"Secret figure ![safe alt][private image] remains.\n"
            b"Code `![hidden][private image]` then ![outside][public image].\n"
            b"Collapsed figure ![collapsed image][] remains.\n"
            b"Shortcut figure ![shortcut image] remains.\n"
            b"Angle figure ![angle alt][angle image] remains.\n"
            b"Escaped figure ![escaped alt][escaped\\] image] remains.\n"
            b"\n"
            b"[public image]: https://example.test/public-image.png\n"
            b"[private image]: https://example.test/private-image.png 'Secret Image Reference Title'\n"
            b"[collapsed image]: https://example.test/collapsed-image.png\n"
            b"[shortcut image]: https://example.test/shortcut-image.png\n"
            b"[angle image]: <https://example.test/angle image.png> 'Angle Image Reference Title'\n"
            b"[escaped\\] image]: https://example.test/escaped-image.png\n"
        )
        reference_image_node = ws.write("reference-image.md", reference_image_content, [])
        reference_image_spans = {
            row[0]: row for row in fs.markdown_section_spans(reference_image_node)
        }
        public_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.1.reference-image.1"
        ]
        private_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1"
        ]
        private_reference_alt = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.alt"
        ]
        private_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.reference"
        ]
        private_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-destination"
        ]
        private_image_resolved_title = reference_image_spans[
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-title"
        ]
        private_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.2.destination"
        ]
        private_image_definition_title = reference_image_spans[
            "body.reference-images.link-reference.2.title"
        ]
        hidden_reference_image_code = reference_image_spans[
            "body.reference-images.paragraph.1.line.3.code.1"
        ]
        outside_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.3.reference-image.1"
        ]
        collapsed_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1"
        ]
        collapsed_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1.reference"
        ]
        collapsed_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.4.reference-image.1.resolved-destination"
        ]
        shortcut_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1"
        ]
        shortcut_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1.reference"
        ]
        shortcut_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.5.reference-image.1.resolved-destination"
        ]
        angle_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1"
        ]
        angle_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1.resolved-destination"
        ]
        angle_image_resolved_title = reference_image_spans[
            "body.reference-images.paragraph.1.line.6.reference-image.1.resolved-title"
        ]
        angle_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.5.destination"
        ]
        angle_image_definition_title = reference_image_spans[
            "body.reference-images.link-reference.5.title"
        ]
        escaped_reference_image = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1"
        ]
        escaped_image_reference_marker = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1.reference"
        ]
        escaped_image_resolved_destination = reference_image_spans[
            "body.reference-images.paragraph.1.line.7.reference-image.1.resolved-destination"
        ]
        escaped_image_definition_label = reference_image_spans[
            "body.reference-images.link-reference.6.label"
        ]
        escaped_image_definition_destination = reference_image_spans[
            "body.reference-images.link-reference.6.destination"
        ]
        assert (
            public_reference_image[3]
            == private_reference_image[3]
            == outside_reference_image[3]
            == collapsed_reference_image[3]
            == shortcut_reference_image[3]
            == angle_reference_image[3]
            == escaped_reference_image[3]
            == "reference-image"
        )
        assert private_reference_alt[3] == "reference-image-alt"
        assert private_image_reference_marker[3] == "reference-image-reference"
        assert (
            private_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert private_image_resolved_title[3] == "reference-image-resolved-title"
        assert private_image_definition_destination[3] == "link-reference-destination"
        assert private_image_definition_title[3] == "link-reference-title"
        assert hidden_reference_image_code[3] == "inline-code"
        assert (
            "body.reference-images.paragraph.1.line.3.reference-image.2"
            not in reference_image_spans
        )
        assert collapsed_image_reference_marker[3] == "reference-image-reference"
        assert collapsed_image_reference_marker[2] == 0
        assert (
            collapsed_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert shortcut_image_reference_marker[3] == "reference-image-reference"
        assert shortcut_image_reference_marker[2] == 0
        assert (
            shortcut_image_resolved_destination[3]
            == "reference-image-resolved-destination"
        )
        assert angle_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert angle_image_resolved_title[3] == "reference-image-resolved-title"
        assert angle_image_definition_destination[3] == "link-reference-destination"
        assert angle_image_definition_title[3] == "link-reference-title"
        assert escaped_image_reference_marker[3] == "reference-image-reference"
        assert escaped_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert escaped_image_definition_label[3] == "link-reference-label"
        assert escaped_image_definition_destination[3] == "link-reference-destination"
        assert reference_image_content[
            public_reference_image[1] : public_reference_image[1]
            + public_reference_image[2]
        ] == b"![public chart][Public Image]"
        assert reference_image_content[
            private_reference_alt[1] : private_reference_alt[1]
            + private_reference_alt[2]
        ] == b"safe alt"
        assert reference_image_content[
            private_image_reference_marker[1] : private_image_reference_marker[1]
            + private_image_reference_marker[2]
        ] == b"private image"
        assert reference_image_content[
            private_image_resolved_destination[1] : private_image_resolved_destination[1]
            + private_image_resolved_destination[2]
        ] == b"https://example.test/private-image.png"
        assert reference_image_content[
            private_image_resolved_title[1] : private_image_resolved_title[1]
            + private_image_resolved_title[2]
        ] == b"Secret Image Reference Title"
        assert reference_image_content[
            private_image_definition_destination[1] : private_image_definition_destination[1]
            + private_image_definition_destination[2]
        ] == b"https://example.test/private-image.png"
        assert reference_image_content[
            private_image_definition_title[1] : private_image_definition_title[1]
            + private_image_definition_title[2]
        ] == b"Secret Image Reference Title"
        assert reference_image_content[
            hidden_reference_image_code[1] : hidden_reference_image_code[1]
            + hidden_reference_image_code[2]
        ] == b"`![hidden][private image]`"
        assert reference_image_content[
            collapsed_reference_image[1] : collapsed_reference_image[1]
            + collapsed_reference_image[2]
        ] == b"![collapsed image][]"
        assert reference_image_content[
            collapsed_image_resolved_destination[1] : collapsed_image_resolved_destination[1]
            + collapsed_image_resolved_destination[2]
        ] == b"https://example.test/collapsed-image.png"
        assert reference_image_content[
            shortcut_reference_image[1] : shortcut_reference_image[1]
            + shortcut_reference_image[2]
        ] == b"![shortcut image]"
        assert reference_image_content[
            shortcut_image_resolved_destination[1] : shortcut_image_resolved_destination[1]
            + shortcut_image_resolved_destination[2]
        ] == b"https://example.test/shortcut-image.png"
        assert reference_image_content[
            angle_image_resolved_destination[1] : angle_image_resolved_destination[1]
            + angle_image_resolved_destination[2]
        ] == b"https://example.test/angle image.png"
        assert reference_image_content[
            angle_image_definition_destination[1] : angle_image_definition_destination[1]
            + angle_image_definition_destination[2]
        ] == b"https://example.test/angle image.png"
        assert reference_image_content[
            angle_image_resolved_title[1] : angle_image_resolved_title[1]
            + angle_image_resolved_title[2]
        ] == b"Angle Image Reference Title"
        assert reference_image_content[
            angle_image_definition_title[1] : angle_image_definition_title[1]
            + angle_image_definition_title[2]
        ] == b"Angle Image Reference Title"
        assert reference_image_content[
            escaped_image_reference_marker[1] : escaped_image_reference_marker[1]
            + escaped_image_reference_marker[2]
        ] == b"escaped\\] image"
        assert reference_image_content[
            escaped_image_resolved_destination[1] : escaped_image_resolved_destination[1]
            + escaped_image_resolved_destination[2]
        ] == b"https://example.test/escaped-image.png"
        assert reference_image_content[
            escaped_image_definition_label[1] : escaped_image_definition_label[1]
            + escaped_image_definition_label[2]
        ] == b"escaped\\] image"
        assert reference_image_content[
            escaped_image_definition_destination[1] : escaped_image_definition_destination[1]
            + escaped_image_definition_destination[2]
        ] == b"https://example.test/escaped-image.png"
        fs.set_markdown_section_policy_label(
            reference_image_node,
            "body.reference-images.paragraph.1.line.2.reference-image.1.resolved-destination",
            "reference-image-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "reference-image-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                private_reference_image[1],
                len(b"![safe alt][private image]"),
            )
        ) == b"![safe alt][private image]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                private_image_resolved_title[1],
                len(b"Secret Image Reference Title"),
            )
        ) == b"Secret Image Reference Title"
        try:
            fs.read_node_range(
                reference_image_node,
                private_image_resolved_destination[1],
                len(b"https://example.test/private-image.png"),
            )
            assert False, "reference-image resolved destination policy should block only the definition target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                outside_reference_image[1],
                len(b"![outside][public image]"),
            )
        ) == b"![outside][public image]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                collapsed_reference_image[1],
                len(b"![collapsed image][]"),
            )
        ) == b"![collapsed image][]"
        assert bytes(
            fs.read_node_range(
                reference_image_node,
                shortcut_reference_image[1],
                len(b"![shortcut image]"),
            )
        ) == b"![shortcut image]"

        duplicate_reference_content = (
            b"# Duplicate References\n"
            b"Link [winner][dup ref] remains.\n"
            b"Image ![winner image][dup image] remains.\n"
            b"\n"
            b"[dup ref]: https://example.test/first-link\n"
            b"[DUP REF]: https://example.test/second-link\n"
            b"[dup image]: https://example.test/first-image.png\n"
            b"[DUP IMAGE]: https://example.test/second-image.png\n"
        )
        duplicate_reference_node = ws.write(
            "duplicate-reference.md", duplicate_reference_content, []
        )
        duplicate_reference_spans = {
            row[0]: row for row in fs.markdown_section_spans(duplicate_reference_node)
        }
        duplicate_link_resolved_destination = duplicate_reference_spans[
            "body.duplicate-references.paragraph.1.line.1.reference-link.1.resolved-destination"
        ]
        duplicate_image_resolved_destination = duplicate_reference_spans[
            "body.duplicate-references.paragraph.1.line.2.reference-image.1.resolved-destination"
        ]
        duplicate_first_link_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.1.destination"
        ]
        duplicate_second_link_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.2.destination"
        ]
        duplicate_first_image_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.3.destination"
        ]
        duplicate_second_image_definition_destination = duplicate_reference_spans[
            "body.duplicate-references.link-reference.4.destination"
        ]
        assert duplicate_link_resolved_destination[3] == "reference-link-resolved-destination"
        assert duplicate_image_resolved_destination[3] == "reference-image-resolved-destination"
        assert duplicate_first_link_definition_destination[3] == "link-reference-destination"
        assert duplicate_second_link_definition_destination[3] == "link-reference-destination"
        assert duplicate_first_image_definition_destination[3] == "link-reference-destination"
        assert duplicate_second_image_definition_destination[3] == "link-reference-destination"
        assert duplicate_reference_content[
            duplicate_link_resolved_destination[1] : duplicate_link_resolved_destination[1]
            + duplicate_link_resolved_destination[2]
        ] == b"https://example.test/first-link"
        assert duplicate_reference_content[
            duplicate_image_resolved_destination[1] : duplicate_image_resolved_destination[1]
            + duplicate_image_resolved_destination[2]
        ] == b"https://example.test/first-image.png"
        assert duplicate_reference_content[
            duplicate_first_link_definition_destination[1] : duplicate_first_link_definition_destination[1]
            + duplicate_first_link_definition_destination[2]
        ] == b"https://example.test/first-link"
        assert duplicate_reference_content[
            duplicate_second_link_definition_destination[1] : duplicate_second_link_definition_destination[1]
            + duplicate_second_link_definition_destination[2]
        ] == b"https://example.test/second-link"
        assert duplicate_reference_content[
            duplicate_first_image_definition_destination[1] : duplicate_first_image_definition_destination[1]
            + duplicate_first_image_definition_destination[2]
        ] == b"https://example.test/first-image.png"
        assert duplicate_reference_content[
            duplicate_second_image_definition_destination[1] : duplicate_second_image_definition_destination[1]
            + duplicate_second_image_definition_destination[2]
        ] == b"https://example.test/second-image.png"

        multiline_reference_content = (
            b"# Multiline References\n"
            b"Link [two line][multi ref] remains.\n"
            b"Image ![two line image][multi image] remains.\n"
            b"\n"
            b"[multi ref]: https://example.test/multi-reference\n"
            b"  \"Multiline Reference Title\"\n"
            b"[multi image]: https://example.test/multi-image.png\n"
            b"  'Multiline Image Title'\n"
        )
        multiline_reference_node = ws.write(
            "multiline-reference.md", multiline_reference_content, []
        )
        multiline_reference_spans = {
            row[0]: row for row in fs.markdown_section_spans(multiline_reference_node)
        }
        multiline_reference_link = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.1.reference-link.1"
        ]
        multiline_link_resolved_title = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.1.reference-link.1.resolved-title"
        ]
        multiline_link_definition = multiline_reference_spans[
            "body.multiline-references.link-reference.1"
        ]
        multiline_link_definition_destination = multiline_reference_spans[
            "body.multiline-references.link-reference.1.destination"
        ]
        multiline_link_definition_title = multiline_reference_spans[
            "body.multiline-references.link-reference.1.title"
        ]
        multiline_reference_image = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.2.reference-image.1"
        ]
        multiline_image_resolved_title = multiline_reference_spans[
            "body.multiline-references.paragraph.1.line.2.reference-image.1.resolved-title"
        ]
        multiline_image_definition = multiline_reference_spans[
            "body.multiline-references.link-reference.2"
        ]
        multiline_image_definition_destination = multiline_reference_spans[
            "body.multiline-references.link-reference.2.destination"
        ]
        multiline_image_definition_title = multiline_reference_spans[
            "body.multiline-references.link-reference.2.title"
        ]
        assert multiline_reference_link[3] == "reference-link"
        assert multiline_reference_image[3] == "reference-image"
        assert multiline_link_definition[3] == multiline_image_definition[3] == "link-reference"
        assert multiline_link_definition_destination[3] == "link-reference-destination"
        assert multiline_image_definition_destination[3] == "link-reference-destination"
        assert multiline_link_definition_title[3] == multiline_image_definition_title[3] == "link-reference-title"
        assert multiline_link_resolved_title[3] == "reference-link-resolved-title"
        assert multiline_image_resolved_title[3] == "reference-image-resolved-title"
        assert multiline_reference_content[
            multiline_link_definition[1] : multiline_link_definition[1]
            + multiline_link_definition[2]
        ] == (
            b"[multi ref]: https://example.test/multi-reference\n"
            b"  \"Multiline Reference Title\"\n"
        )
        assert multiline_reference_content[
            multiline_image_definition[1] : multiline_image_definition[1]
            + multiline_image_definition[2]
        ] == (
            b"[multi image]: https://example.test/multi-image.png\n"
            b"  'Multiline Image Title'\n"
        )
        assert multiline_reference_content[
            multiline_link_definition_destination[1] : multiline_link_definition_destination[1]
            + multiline_link_definition_destination[2]
        ] == b"https://example.test/multi-reference"
        assert multiline_reference_content[
            multiline_image_definition_destination[1] : multiline_image_definition_destination[1]
            + multiline_image_definition_destination[2]
        ] == b"https://example.test/multi-image.png"
        assert multiline_reference_content[
            multiline_link_definition_title[1] : multiline_link_definition_title[1]
            + multiline_link_definition_title[2]
        ] == b"Multiline Reference Title"
        assert multiline_reference_content[
            multiline_link_resolved_title[1] : multiline_link_resolved_title[1]
            + multiline_link_resolved_title[2]
        ] == b"Multiline Reference Title"
        assert multiline_reference_content[
            multiline_image_definition_title[1] : multiline_image_definition_title[1]
            + multiline_image_definition_title[2]
        ] == b"Multiline Image Title"
        assert multiline_reference_content[
            multiline_image_resolved_title[1] : multiline_image_resolved_title[1]
            + multiline_image_resolved_title[2]
        ] == b"Multiline Image Title"

        inline_code_content = (
            b"# Inline Code\n"
            b"Public command `ls -la` is visible.\n"
            b"Secret token `TOKEN=abc123` remains text.\n"
            b"Double tick ``code with ` inner tick`` is captured.\n"
            b"Triple tick ```code with `` inner ticks``` is captured.\n"
        )
        inline_code_node = ws.write("inline-code.md", inline_code_content, [])
        inline_code_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_code_node)
        }
        public_code = inline_code_spans["body.inline-code.paragraph.1.line.1.code.1"]
        secret_code = inline_code_spans["body.inline-code.paragraph.1.line.2.code.1"]
        double_code = inline_code_spans["body.inline-code.paragraph.1.line.3.code.1"]
        triple_code = inline_code_spans["body.inline-code.paragraph.1.line.4.code.1"]
        assert public_code[3] == secret_code[3] == double_code[3] == triple_code[3] == "inline-code"
        assert inline_code_content[public_code[1] : public_code[1] + public_code[2]] == (
            b"`ls -la`"
        )
        assert inline_code_content[secret_code[1] : secret_code[1] + secret_code[2]] == (
            b"`TOKEN=abc123`"
        )
        assert inline_code_content[double_code[1] : double_code[1] + double_code[2]] == (
            b"``code with ` inner tick``"
        )
        assert inline_code_content[triple_code[1] : triple_code[1] + triple_code[2]] == (
            b"```code with `` inner ticks```"
        )
        fs.set_markdown_section_policy_label(
            inline_code_node,
            "body.inline-code.paragraph.1.line.2.code.1",
            "inline-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(inline_code_node, public_code[1], len(b"`ls -la`"))
        ) == b"`ls -la`"
        assert bytes(
            fs.read_node_range(
                inline_code_node,
                inline_code_content.index(b"Secret token"),
                len(b"Secret token"),
            )
        ) == b"Secret token"
        try:
            fs.read_node_range(inline_code_node, secret_code[1], len(b"`TOKEN"))
            assert False, "markdown inline code policy should block only the code span"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_code_node,
                inline_code_content.index(b" remains text"),
                len(b" remains text"),
            )
        ) == b" remains text"

        autolink_content = (
            b"# Autolinks\n"
            b"Public URL <https://example.test/public> stays visible.\n"
            b"Secret contact <mailto:private@example.test> remains text.\n"
            b"Angle text <not-a-url> stays plain.\n"
        )
        autolink_node = ws.write("autolink.md", autolink_content, [])
        autolink_spans = {
            row[0]: row for row in fs.markdown_section_spans(autolink_node)
        }
        public_autolink = autolink_spans["body.autolinks.paragraph.1.line.1.autolink.1"]
        secret_autolink = autolink_spans["body.autolinks.paragraph.1.line.2.autolink.1"]
        public_autolink_target = autolink_spans[
            "body.autolinks.paragraph.1.line.1.autolink.1.target"
        ]
        secret_autolink_target = autolink_spans[
            "body.autolinks.paragraph.1.line.2.autolink.1.target"
        ]
        assert public_autolink[3] == secret_autolink[3] == "autolink"
        assert public_autolink_target[3] == secret_autolink_target[3] == "autolink-target"
        assert "body.autolinks.paragraph.1.line.3.autolink.1" not in autolink_spans
        assert autolink_content[
            public_autolink[1] : public_autolink[1] + public_autolink[2]
        ] == b"<https://example.test/public>"
        assert autolink_content[
            secret_autolink[1] : secret_autolink[1] + secret_autolink[2]
        ] == b"<mailto:private@example.test>"
        assert autolink_content[
            public_autolink_target[1] : public_autolink_target[1] + public_autolink_target[2]
        ] == b"https://example.test/public"
        assert autolink_content[
            secret_autolink_target[1] : secret_autolink_target[1] + secret_autolink_target[2]
        ] == b"mailto:private@example.test"
        fs.set_markdown_section_policy_label(
            autolink_node,
            "body.autolinks.paragraph.1.line.2.autolink.1",
            "autolink-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "autolink-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                autolink_node,
                public_autolink[1],
                len(b"<https://example.test/public>"),
            )
        ) == b"<https://example.test/public>"
        assert bytes(
            fs.read_node_range(
                autolink_node,
                autolink_content.index(b"Secret contact"),
                len(b"Secret contact"),
            )
        ) == b"Secret contact"
        try:
            fs.read_node_range(autolink_node, secret_autolink[1], len(b"<mailto:private"))
            assert False, "markdown autolink policy should block only the autolink"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                autolink_node,
                autolink_content.index(b" remains text"),
                len(b" remains text"),
            )
        ) == b" remains text"

        autolink_target_content = (
            b"# Autolink Targets\n"
            b"Public wrapper <https://example.test/secret-target> remains.\n"
        )
        autolink_target_node = ws.write("autolink-target.md", autolink_target_content, [])
        autolink_target_spans = {
            row[0]: row for row in fs.markdown_section_spans(autolink_target_node)
        }
        secret_autolink_target = autolink_target_spans[
            "body.autolink-targets.paragraph.1.line.1.autolink.1.target"
        ]
        assert secret_autolink_target[3] == "autolink-target"
        assert autolink_target_content[
            secret_autolink_target[1] : secret_autolink_target[1] + secret_autolink_target[2]
        ] == b"https://example.test/secret-target"
        fs.set_markdown_section_policy_label(
            autolink_target_node,
            "body.autolink-targets.paragraph.1.line.1.autolink.1.target",
            "autolink-target-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "autolink-target-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                autolink_target_node,
                autolink_target_content.index(b"Public wrapper <"),
                len(b"Public wrapper <"),
            )
        ) == b"Public wrapper <"
        try:
            fs.read_node_range(
                autolink_target_node,
                secret_autolink_target[1],
                len(b"https://example.test/secret-target"),
            )
            assert False, "markdown autolink target policy should block only the target"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                autolink_target_node,
                autolink_target_content.index(b"> remains"),
                len(b"> remains"),
            )
        ) == b"> remains"

        inline_image_content = (
            b"# Inline Images\n"
            b"Public figure ![public chart](https://example.test/public.png) remains.\n"
            b"Secret source ![safe alt](https://example.test/secret.png) remains.\n"
        )
        inline_image_node = ws.write("inline-image.md", inline_image_content, [])
        inline_image_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_image_node)
        }
        public_image = inline_image_spans[
            "body.inline-images.paragraph.1.line.1.image.1"
        ]
        secret_image = inline_image_spans[
            "body.inline-images.paragraph.1.line.2.image.1"
        ]
        public_alt = inline_image_spans[
            "body.inline-images.paragraph.1.line.1.image.1.alt"
        ]
        secret_destination = inline_image_spans[
            "body.inline-images.paragraph.1.line.2.image.1.destination"
        ]
        assert public_image[3] == secret_image[3] == "inline-image"
        assert public_alt[3] == "inline-image-alt"
        assert secret_destination[3] == "inline-image-destination"
        assert inline_image_content[
            public_image[1] : public_image[1] + public_image[2]
        ] == b"![public chart](https://example.test/public.png)"
        assert inline_image_content[
            public_alt[1] : public_alt[1] + public_alt[2]
        ] == b"public chart"
        assert inline_image_content[
            secret_destination[1] : secret_destination[1] + secret_destination[2]
        ] == b"https://example.test/secret.png"
        fs.set_markdown_section_policy_label(
            inline_image_node,
            "body.inline-images.paragraph.1.line.2.image.1.destination",
            "inline-image-destination-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-image-destination-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_image_node,
                inline_image_content.index(b"![safe alt]"),
                len(b"![safe alt]"),
            )
        ) == b"![safe alt]"
        try:
            fs.read_node_range(
                inline_image_node,
                secret_destination[1],
                len(b"https://example.test/secret.png"),
            )
            assert False, "markdown inline image destination policy should block only the destination"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_image_node,
                inline_image_content.index(b" remains", secret_destination[1]),
                len(b" remains"),
            )
        ) == b" remains"

        inline_image_title_content = (
            b"# Inline Image Titles\n"
            b"Public wrapper ![safe alt](https://example.test/image-title.png 'Secret Image Title') remains.\n"
            b"Paren wrapper ![safe alt](https://example.test/paren-image.png (Paren Image Title)) remains.\n"
        )
        inline_image_title_node = ws.write(
            "inline-image-title.md", inline_image_title_content, []
        )
        inline_image_title_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_image_title_node)
        }
        image_title_destination = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.1.image.1.destination"
        ]
        secret_image_title = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.1.image.1.title"
        ]
        paren_image_title_destination = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.2.image.1.destination"
        ]
        paren_image_title = inline_image_title_spans[
            "body.inline-image-titles.paragraph.1.line.2.image.1.title"
        ]
        assert image_title_destination[3] == paren_image_title_destination[3] == "inline-image-destination"
        assert secret_image_title[3] == paren_image_title[3] == "inline-image-title"
        assert inline_image_title_content[
            image_title_destination[1] : image_title_destination[1]
            + image_title_destination[2]
        ] == b"https://example.test/image-title.png"
        assert inline_image_title_content[
            secret_image_title[1] : secret_image_title[1] + secret_image_title[2]
        ] == b"Secret Image Title"
        assert inline_image_title_content[
            paren_image_title_destination[1] : paren_image_title_destination[1]
            + paren_image_title_destination[2]
        ] == b"https://example.test/paren-image.png"
        assert inline_image_title_content[
            paren_image_title[1] : paren_image_title[1] + paren_image_title[2]
        ] == b"Paren Image Title"
        fs.set_markdown_section_policy_label(
            inline_image_title_node,
            "body.inline-image-titles.paragraph.1.line.1.image.1.title",
            "inline-image-title-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-image-title-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                inline_image_title_node,
                image_title_destination[1],
                len(b"https://example.test/image-title.png"),
            )
        ) == b"https://example.test/image-title.png"
        try:
            fs.read_node_range(
                inline_image_title_node,
                secret_image_title[1],
                len(b"Secret Image Title"),
            )
            assert False, "markdown inline image title policy should block only the title"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_image_title_node,
                inline_image_title_content.index(b" remains"),
                len(b" remains"),
            )
        ) == b" remains"

        inline_overlap_content = (
            b"# Inline Overlap\n"
            b"Code `[hidden](https://example.test/private)` then [public](https://example.test/public).\n"
            b"Code `<https://example.test/private>` then <https://example.test/public>.\n"
            b"Code `![hidden](https://example.test/private.png)` then ![public](https://example.test/public.png).\n"
            b"Code ``[hidden multi](https://example.test/private)`` then [public multi](https://example.test/public).\n"
        )
        inline_overlap_node = ws.write("inline-overlap.md", inline_overlap_content, [])
        inline_overlap_spans = {
            row[0]: row for row in fs.markdown_section_spans(inline_overlap_node)
        }
        hidden_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.1.code.1"
        ]
        public_link = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.1.link.1"
        ]
        hidden_url_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.2.code.1"
        ]
        public_autolink = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.2.autolink.1"
        ]
        hidden_image_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.3.code.1"
        ]
        public_image = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.3.image.1"
        ]
        hidden_multi_code = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.4.code.1"
        ]
        public_multi_link = inline_overlap_spans[
            "body.inline-overlap.paragraph.1.line.4.link.1"
        ]
        assert hidden_code[3] == hidden_url_code[3] == "inline-code"
        assert public_link[3] == "inline-link"
        assert public_autolink[3] == "autolink"
        assert hidden_image_code[3] == "inline-code"
        assert public_image[3] == "inline-image"
        assert hidden_multi_code[3] == "inline-code"
        assert public_multi_link[3] == "inline-link"
        assert "body.inline-overlap.paragraph.1.line.1.link.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.2.autolink.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.3.image.2" not in inline_overlap_spans
        assert "body.inline-overlap.paragraph.1.line.4.link.2" not in inline_overlap_spans
        assert inline_overlap_content[
            hidden_code[1] : hidden_code[1] + hidden_code[2]
        ] == b"`[hidden](https://example.test/private)`"
        assert inline_overlap_content[
            public_link[1] : public_link[1] + public_link[2]
        ] == b"[public](https://example.test/public)"
        assert inline_overlap_content[
            hidden_multi_code[1] : hidden_multi_code[1] + hidden_multi_code[2]
        ] == b"``[hidden multi](https://example.test/private)``"
        assert inline_overlap_content[
            public_multi_link[1] : public_multi_link[1] + public_multi_link[2]
        ] == b"[public multi](https://example.test/public)"
        assert inline_overlap_content[
            hidden_url_code[1] : hidden_url_code[1] + hidden_url_code[2]
        ] == b"`<https://example.test/private>`"
        assert inline_overlap_content[
            public_autolink[1] : public_autolink[1] + public_autolink[2]
        ] == b"<https://example.test/public>"
        assert inline_overlap_content[
            hidden_image_code[1] : hidden_image_code[1] + hidden_image_code[2]
        ] == b"`![hidden](https://example.test/private.png)`"
        assert inline_overlap_content[
            public_image[1] : public_image[1] + public_image[2]
        ] == b"![public](https://example.test/public.png)"
        fs.set_markdown_section_policy_label(
            inline_overlap_node,
            "body.inline-overlap.paragraph.1.line.1.code.1",
            "inline-overlap-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "inline-overlap-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(inline_overlap_node, hidden_code[1], len(b"`[hidden]"))
            assert False, "markdown inline code policy should block code-contained link text"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                inline_overlap_node,
                inline_overlap_content.index(b" then [public]"),
                len(b" then "),
            )
        ) == b" then "
        assert bytes(
            fs.read_node_range(inline_overlap_node, public_link[1], len(b"[public]"))
        ) == b"[public]"

        try:
            fs.set_markdown_section_policy_label(
                node_id,
                "body.missing",
                "sensitivity",
                "restricted",
                "policy_agent",
            )
            assert False, "missing Markdown section path should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        plain_content = (
            b"Plain body without headings.\nSecond line.\n\n"
            b"* * *\n\n"
            b"- Private item\n"
        )
        plain_node = ws.write("plain.md", plain_content, [])
        plain_spans = {row[0]: row for row in fs.markdown_section_spans(plain_node)}
        plain_paragraph = plain_spans["body.paragraph.1"]
        assert plain_paragraph[3] == "paragraph"
        assert plain_content[
            plain_paragraph[1] : plain_paragraph[1] + plain_paragraph[2]
        ].startswith(b"Plain body")
        plain_thematic = plain_spans["body.thematic-break.1"]
        assert plain_thematic[3] == "thematic-break"
        assert plain_content[
            plain_thematic[1] : plain_thematic[1] + plain_thematic[2]
        ] == b"* * *\n"
        assert plain_spans["body.list.1"][3] == "list"
        fs.set_markdown_section_policy_label(
            plain_node,
            "body.paragraph.1",
            "plain-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "plain-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(plain_node, plain_paragraph[1], len(b"Plain body"))
            assert False, "markdown body paragraph policy should block the paragraph range"
        except anfs_core.PolicyDeniedError:
            pass

        setext_content = (
            b"Public Title\n"
            b"============\n"
            b"Public setext introduction.\n"
            b"\n"
            b"Private Plan\n"
            b"------------\n"
            b"Secret setext details.\n"
            b"\n"
            b"# Appendix\n"
            b"Public appendix.\n"
        )
        setext_node = ws.write("setext.md", setext_content, [])
        setext_spans = {row[0]: row for row in fs.markdown_section_spans(setext_node)}
        assert setext_spans["body.public-title"][3] == "h1"
        assert setext_spans["body.private-plan"][3] == "h2"
        assert setext_spans["body.appendix"][3] == "h1"
        assert "body.public-title.thematic-break.1" not in setext_spans
        assert "body.private-plan.thematic-break.1" not in setext_spans
        assert "body.public-title.paragraph.1" in setext_spans
        assert "body.private-plan.paragraph.1" in setext_spans
        private_plan = setext_spans["body.private-plan"]
        assert setext_content[
            private_plan[1] : private_plan[1] + private_plan[2]
        ].startswith(b"Private Plan\n------------\n")
        assert b"# Appendix" not in setext_content[
            private_plan[1] : private_plan[1] + private_plan[2]
        ]
        fs.set_markdown_section_policy_label(
            setext_node,
            "body.private-plan",
            "setext-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "setext-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        try:
            fs.read_node_range(setext_node, private_plan[1], len(b"Private Plan"))
            assert False, "setext section policy should block the section range"
        except anfs_core.PolicyDeniedError:
            pass

        list_item_content = (
            b"# Tasks\n"
            b"- Public task\n"
            b"- Secret task\n"
            b"- Public follow-up\n"
        )
        list_item_node = ws.write("list-items.md", list_item_content, [])
        list_item_spans = {
            row[0]: row for row in fs.markdown_section_spans(list_item_node)
        }
        first_item = list_item_spans["body.tasks.list.1.item.1"]
        second_item = list_item_spans["body.tasks.list.1.item.2"]
        third_item = list_item_spans["body.tasks.list.1.item.3"]
        assert first_item[3] == second_item[3] == third_item[3] == "list-item"
        assert list_item_content[
            second_item[1] : second_item[1] + second_item[2]
        ] == b"- Secret task\n"
        fs.set_markdown_section_policy_label(
            list_item_node,
            "body.tasks.list.1.item.2",
            "list-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "list-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(list_item_node, first_item[1], len(b"- Public task"))
        ) == b"- Public task"
        try:
            fs.read_node_range(list_item_node, second_item[1], len(b"- Secret task"))
            assert False, "markdown list item policy should block only the labeled item"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(list_item_node, third_item[1], len(b"- Public follow-up"))
        ) == b"- Public follow-up"

        task_list_content = (
            b"# Checklist\n"
            b"- [ ] Public todo\n"
            b"- [x] Secret decision\n"
            b"1. [X] Public done\n"
        )
        task_list_node = ws.write("task-list.md", task_list_content, [])
        task_list_spans = {
            row[0]: row for row in fs.markdown_section_spans(task_list_node)
        }
        first_task_item = task_list_spans["body.checklist.list.1.item.1"]
        secret_task_item = task_list_spans["body.checklist.list.1.item.2"]
        third_task_item = task_list_spans["body.checklist.list.1.item.3"]
        first_checkbox = task_list_spans["body.checklist.list.1.item.1.checkbox"]
        secret_checkbox = task_list_spans["body.checklist.list.1.item.2.checkbox"]
        third_checkbox = task_list_spans["body.checklist.list.1.item.3.checkbox"]
        assert (
            first_task_item[3]
            == secret_task_item[3]
            == third_task_item[3]
            == "list-item"
        )
        assert (
            first_checkbox[3]
            == secret_checkbox[3]
            == third_checkbox[3]
            == "task-checkbox"
        )
        assert task_list_content[
            first_checkbox[1] : first_checkbox[1] + first_checkbox[2]
        ] == b"[ ]"
        assert task_list_content[
            secret_checkbox[1] : secret_checkbox[1] + secret_checkbox[2]
        ] == b"[x]"
        assert task_list_content[
            third_checkbox[1] : third_checkbox[1] + third_checkbox[2]
        ] == b"[X]"
        fs.set_markdown_section_policy_label(
            task_list_node,
            "body.checklist.list.1.item.2.checkbox",
            "task-state-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "task-state-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(task_list_node, first_checkbox[1], len(b"[ ]"))
        ) == b"[ ]"
        try:
            fs.read_node_range(task_list_node, secret_checkbox[1], len(b"[x]"))
            assert False, "markdown task checkbox policy should block only the checkbox"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                task_list_node,
                task_list_content.index(b"Secret decision"),
                len(b"Secret decision"),
            )
        ) == b"Secret decision"
        assert bytes(
            fs.read_node_range(task_list_node, third_checkbox[1], len(b"[X]"))
        ) == b"[X]"

        table_row_content = (
            b"# Accounts\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Ada | Public |\n"
            b"| Byron | Secret |\n"
        )
        table_row_node = ws.write("table-rows.md", table_row_content, [])
        table_row_spans = {
            row[0]: row for row in fs.markdown_section_spans(table_row_node)
        }
        header_row = table_row_spans["body.accounts.table.1.row.1"]
        public_row = table_row_spans["body.accounts.table.1.row.2"]
        secret_row = table_row_spans["body.accounts.table.1.row.3"]
        assert header_row[3] == public_row[3] == secret_row[3] == "table-row"
        name_align = table_row_spans["body.accounts.table.1.align.1"]
        status_align = table_row_spans["body.accounts.table.1.align.2"]
        assert name_align[3] == status_align[3] == "table-align"
        assert table_row_content[
            header_row[1] : header_row[1] + header_row[2]
        ] == b"| Name | Status |\n"
        assert table_row_content[
            public_row[1] : public_row[1] + public_row[2]
        ] == b"| Ada | Public |\n"
        assert table_row_content[
            secret_row[1] : secret_row[1] + secret_row[2]
        ] == b"| Byron | Secret |\n"
        assert table_row_content[
            name_align[1] : name_align[1] + name_align[2]
        ] == b"---"
        assert table_row_content[
            status_align[1] : status_align[1] + status_align[2]
        ] == b"---"
        header_name_cell = table_row_spans["body.accounts.table.1.row.1.cell.1"]
        header_status_cell = table_row_spans["body.accounts.table.1.row.1.cell.2"]
        public_name_cell = table_row_spans["body.accounts.table.1.row.2.cell.1"]
        secret_status_cell = table_row_spans["body.accounts.table.1.row.3.cell.2"]
        assert (
            header_name_cell[3]
            == header_status_cell[3]
            == public_name_cell[3]
            == secret_status_cell[3]
            == "table-cell"
        )
        assert table_row_content[
            header_name_cell[1] : header_name_cell[1] + header_name_cell[2]
        ] == b"Name"
        assert table_row_content[
            header_status_cell[1] : header_status_cell[1] + header_status_cell[2]
        ] == b"Status"
        assert table_row_content[
            public_name_cell[1] : public_name_cell[1] + public_name_cell[2]
        ] == b"Ada"
        assert table_row_content[
            secret_status_cell[1] : secret_status_cell[1] + secret_status_cell[2]
        ] == b"Secret"
        assert "body.accounts.table.1.row.4" not in table_row_spans
        fs.set_markdown_section_policy_label(
            table_row_node,
            "body.accounts.table.1.row.3",
            "table-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(table_row_node, header_row[1], len(b"| Name | Status |"))
        ) == b"| Name | Status |"
        assert bytes(
            fs.read_node_range(table_row_node, public_row[1], len(b"| Ada | Public |"))
        ) == b"| Ada | Public |"
        try:
            fs.read_node_range(table_row_node, secret_row[1], len(b"| Byron | Secret |"))
            assert False, "markdown table row policy should block only the labeled row"
        except anfs_core.PolicyDeniedError:
            pass

        table_cell_content = (
            b"# Table Cells\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Ada | Public |\n"
            b"| Byron | Secret |\n"
        )
        table_cell_node = ws.write("table-cells.md", table_cell_content, [])
        table_cell_spans = {
            row[0]: row for row in fs.markdown_section_spans(table_cell_node)
        }
        public_cell = table_cell_spans["body.table-cells.table.1.row.3.cell.1"]
        secret_cell = table_cell_spans["body.table-cells.table.1.row.3.cell.2"]
        assert public_cell[3] == secret_cell[3] == "table-cell"
        fs.set_markdown_section_policy_label(
            table_cell_node,
            "body.table-cells.table.1.row.3.cell.2",
            "table-cell-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-cell-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(table_cell_node, public_cell[1], len(b"Byron"))
        ) == b"Byron"
        try:
            fs.read_node_range(table_cell_node, secret_cell[1], len(b"Secret"))
            assert False, "markdown table cell policy should block only the labeled cell"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                table_cell_node,
                table_cell_content.index(b"| Ada | Public |"),
                len(b"| Ada | Public |"),
            )
        ) == b"| Ada | Public |"

        escaped_table_content = (
            b"# Escaped Table\n"
            b"| Name | Status |\n"
            b"| --- | --- |\n"
            b"| Plan A\\|B | Secret |\n"
        )
        escaped_table_node = ws.write("escaped-table.md", escaped_table_content, [])
        escaped_table_spans = {
            row[0]: row for row in fs.markdown_section_spans(escaped_table_node)
        }
        escaped_name_cell = escaped_table_spans[
            "body.escaped-table.table.1.row.2.cell.1"
        ]
        escaped_secret_cell = escaped_table_spans[
            "body.escaped-table.table.1.row.2.cell.2"
        ]
        assert (
            escaped_table_content[
                escaped_name_cell[1] : escaped_name_cell[1] + escaped_name_cell[2]
            ]
            == b"Plan A\\|B"
        )
        assert (
            escaped_table_content[
                escaped_secret_cell[1] : escaped_secret_cell[1]
                + escaped_secret_cell[2]
            ]
            == b"Secret"
        )
        assert "body.escaped-table.table.1.row.2.cell.3" not in escaped_table_spans
        fs.set_markdown_section_policy_label(
            escaped_table_node,
            "body.escaped-table.table.1.row.2.cell.2",
            "escaped-table-cell-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "escaped-table-cell-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                escaped_table_node, escaped_name_cell[1], len(b"Plan A\\|B")
            )
        ) == b"Plan A\\|B"
        try:
            fs.read_node_range(
                escaped_table_node, escaped_secret_cell[1], len(b"Secret")
            )
            assert False, "escaped pipe should not split an extra table cell"
        except anfs_core.PolicyDeniedError:
            pass

        align_table_content = (
            b"# Align Table\n"
            b"| Name | Amount | Notes |\n"
            b"| :--- | ---: | :---: |\n"
            b"| Ada | 10 | Public |\n"
        )
        align_table_node = ws.write("align-table.md", align_table_content, [])
        align_table_spans = {
            row[0]: row for row in fs.markdown_section_spans(align_table_node)
        }
        left_align = align_table_spans["body.align-table.table.1.align.1"]
        right_align = align_table_spans["body.align-table.table.1.align.2"]
        center_align = align_table_spans["body.align-table.table.1.align.3"]
        assert left_align[3] == right_align[3] == center_align[3] == "table-align"
        assert align_table_content[
            left_align[1] : left_align[1] + left_align[2]
        ] == b":---"
        assert align_table_content[
            right_align[1] : right_align[1] + right_align[2]
        ] == b"---:"
        assert align_table_content[
            center_align[1] : center_align[1] + center_align[2]
        ] == b":---:"
        fs.set_markdown_section_policy_label(
            align_table_node,
            "body.align-table.table.1.align.2",
            "table-align-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "table-align-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                align_table_node,
                align_table_content.index(b"| Ada | 10 | Public |"),
                len(b"| Ada | 10 | Public |"),
            )
        ) == b"| Ada | 10 | Public |"
        try:
            fs.read_node_range(align_table_node, right_align[1], len(b"---:"))
            assert False, "markdown table alignment policy should block only the labeled alignment"
        except anfs_core.PolicyDeniedError:
            pass

        html_content = (
            b"# Html\n"
            b"Public html intro.\n"
            b"<DIV class=\"secret\">\n"
            b"# Not A Heading\n"
            b"Secret HTML panel\n"
            b"</DIV>\n"
            b"\n"
            b"After html.\n"
        )
        html_node = ws.write("html-block.md", html_content, [])
        html_spans = {row[0]: row for row in fs.markdown_section_spans(html_node)}
        html_block = html_spans["body.html.html.1"]
        assert html_block[3] == "html"
        assert "body.not-a-heading" not in html_spans
        assert html_content[html_block[1] : html_block[1] + html_block[2]] == (
            b"<DIV class=\"secret\">\n# Not A Heading\nSecret HTML panel\n</DIV>\n"
        )
        assert html_spans["body.html.paragraph.1"][3] == "paragraph"
        assert html_spans["body.html.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            html_node,
            "body.html.html.1",
            "html-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "html-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                html_node,
                html_content.index(b"Public html intro"),
                len(b"Public html intro"),
            )
        ) == b"Public html intro"
        try:
            fs.read_node_range(html_node, html_block[1], len(b"<DIV"))
            assert False, "markdown HTML block policy should block only the HTML block"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                html_node,
                html_content.index(b"After html"),
                len(b"After html"),
            )
        ) == b"After html"

        indented_code_content = (
            b"# Code\n"
            b"Public code intro.\n"
            b"\n"
            b"    # Not A Heading\n"
            b"    secret_call()\n"
            b"\n"
            b"After code.\n"
        )
        indented_code_node = ws.write("indented-code.md", indented_code_content, [])
        indented_code_spans = {
            row[0]: row for row in fs.markdown_section_spans(indented_code_node)
        }
        indented_code_block = indented_code_spans["body.code.code.1"]
        assert indented_code_block[3] == "code"
        assert "body.not-a-heading" not in indented_code_spans
        assert indented_code_content[
            indented_code_block[1] : indented_code_block[1] + indented_code_block[2]
        ] == b"    # Not A Heading\n    secret_call()\n"
        assert indented_code_spans["body.code.paragraph.1"][3] == "paragraph"
        assert indented_code_spans["body.code.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            indented_code_node,
            "body.code.code.1",
            "indented-code-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "indented-code-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                indented_code_node,
                indented_code_content.index(b"Public code intro"),
                len(b"Public code intro"),
            )
        ) == b"Public code intro"
        try:
            fs.read_node_range(indented_code_node, indented_code_block[1], len(b"    #"))
            assert False, "markdown indented code policy should block only the code block"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                indented_code_node,
                indented_code_content.index(b"After code"),
                len(b"After code"),
            )
        ) == b"After code"

        link_ref_content = (
            b"# References\n"
            b"Public reference intro.\n"
            b"\n"
            b"[secret-ref]: https://example.test/private \"Secret\"\n"
            b"\n"
            b"After reference.\n"
            b"\n"
            b"[not-a-heading]: https://example.test/public\n"
            b"---\n"
        )
        link_ref_node = ws.write("link-reference.md", link_ref_content, [])
        link_ref_spans = {
            row[0]: row for row in fs.markdown_section_spans(link_ref_node)
        }
        link_ref_block = link_ref_spans["body.references.link-reference.1"]
        second_link_ref_block = link_ref_spans["body.references.link-reference.2"]
        assert link_ref_block[3] == second_link_ref_block[3] == "link-reference"
        assert "body.not-a-heading" not in link_ref_spans
        assert link_ref_content[
            link_ref_block[1] : link_ref_block[1] + link_ref_block[2]
        ] == b"[secret-ref]: https://example.test/private \"Secret\"\n"
        assert link_ref_content[
            second_link_ref_block[1] : second_link_ref_block[1] + second_link_ref_block[2]
        ] == b"[not-a-heading]: https://example.test/public\n"
        assert link_ref_spans["body.references.paragraph.1"][3] == "paragraph"
        assert link_ref_spans["body.references.paragraph.2"][3] == "paragraph"
        fs.set_markdown_section_policy_label(
            link_ref_node,
            "body.references.link-reference.1",
            "link-reference-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "link-reference-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                link_ref_content.index(b"Public reference intro"),
                len(b"Public reference intro"),
            )
        ) == b"Public reference intro"
        try:
            fs.read_node_range(link_ref_node, link_ref_block[1], len(b"[secret-ref]"))
            assert False, "markdown link reference policy should block only the reference"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                link_ref_content.index(b"After reference"),
                len(b"After reference"),
            )
        ) == b"After reference"
        assert bytes(
            fs.read_node_range(
                link_ref_node,
                second_link_ref_block[1],
                len(b"[not-a-heading]"),
            )
        ) == b"[not-a-heading]"

        blockquote_content = (
            b"# Quotes\n"
            b"Public quote intro.\n"
            b"\n"
            b"> Public quoted line\n"
            b"> Secret quoted line\n"
            b"> Public quoted follow-up\n"
            b"\n"
            b"After quote.\n"
        )
        blockquote_node = ws.write("blockquote.md", blockquote_content, [])
        blockquote_spans = {
            row[0]: row for row in fs.markdown_section_spans(blockquote_node)
        }
        blockquote_block = blockquote_spans["body.quotes.blockquote.1"]
        first_quote_line = blockquote_spans["body.quotes.blockquote.1.line.1"]
        secret_quote_line = blockquote_spans["body.quotes.blockquote.1.line.2"]
        third_quote_line = blockquote_spans["body.quotes.blockquote.1.line.3"]
        assert blockquote_block[3] == "blockquote"
        assert (
            first_quote_line[3]
            == secret_quote_line[3]
            == third_quote_line[3]
            == "blockquote-line"
        )
        assert blockquote_content[
            secret_quote_line[1] : secret_quote_line[1] + secret_quote_line[2]
        ] == b"> Secret quoted line\n"
        fs.set_markdown_section_policy_label(
            blockquote_node,
            "body.quotes.blockquote.1.line.2",
            "quote-secret",
            "true",
            "policy_agent",
        )
        fs.set_policy_rule(
            "quote-secret",
            value="true",
            effect="deny",
            scope="visibility",
            subject_type="fragment",
            agent_id="policy_agent",
        )
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                blockquote_content.index(b"Public quote intro"),
                len(b"Public quote intro"),
            )
        ) == b"Public quote intro"
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                first_quote_line[1],
                len(b"> Public quoted line"),
            )
        ) == b"> Public quoted line"
        try:
            fs.read_node_range(blockquote_node, secret_quote_line[1], len(b"> Secret"))
            assert False, "markdown blockquote line policy should block only the line"
        except anfs_core.PolicyDeniedError:
            pass
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                third_quote_line[1],
                len(b"> Public quoted follow-up"),
            )
        ) == b"> Public quoted follow-up"
        assert bytes(
            fs.read_node_range(
                blockquote_node,
                blockquote_content.index(b"After quote"),
                len(b"After quote"),
            )
        ) == b"After quote"
        assert fs.verify_integrity() == []


def test_search_policy_label_excludes_filter_context_results():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        blocked_node = writer.write("blocked.md", b"search label gate marker blocked", [])
        writer.publish("blocked.md", "artifact:search-blocked@v1")
        allowed_node = writer.write("allowed.md", b"search label gate marker allowed", [])
        writer.publish("allowed.md", "artifact:search-allowed@v1")

        fs.set_policy_label(
            "node",
            blocked_node,
            "sensitivity",
            "pii",
            "policy_agent",
        )
        reader = fs.open_workspace("ws:reader", "reader_agent", run_id="run:reader")
        rows = reader.search(
            "marker",
            "published",
            tool_call_id="tc_search_policy_filtered",
            policy_label_excludes=["sensitivity"],
        )
        assert rows == [(allowed_node, "search label gate [marker] allowed")]

        search_event = fs.event(fs.events(kind="search")[0][1])
        payload = json.loads(search_event[6])
        assert payload["query"] == "marker"
        assert payload["scope"] == "published"
        assert payload["policy_label_excludes"] == ["sensitivity"]
        assert payload["result_count"] == 1
        assert payload["results"][0]["ref_name"] == "artifact:search-allowed@v1"
        assert search_event[3] == "run:reader"
        assert search_event[4] == "tc_search_policy_filtered"
        assert search_event[8] == [
            (
                "input",
                allowed_node,
                "search_result:0",
                "artifact:search-allowed@v1",
            )
        ]

        fs.set_policy_label("node", blocked_node, "sensitivity", None, "policy_agent")
        restored = {
            node_id
            for node_id, _snippet in reader.search(
                "marker",
                "published",
                policy_label_excludes=["sensitivity"],
            )
        }
        assert restored == {allowed_node, blocked_node}

        try:
            reader.search("marker", "published", policy_label_excludes=[""])
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("empty search policy label excludes should be rejected")


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


def test_workspace_answer_records_citations_and_enforces_policy_labels():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        source_node = writer.write("source.md", b"answer evidence marker", [])
        writer.publish("source.md", "artifact:evidence@v1")
        other_node = writer.write("other.md", b"unretrieved evidence", [])
        writer.publish("other.md", "artifact:other@v1")

        reader = fs.open_workspace("ws:reader", "reader_agent", run_id="run:answer")
        retrieved = reader.query(
            prefix="artifact:",
            text="answer",
            state="published",
            tool_call_id="tc_answer_query",
        )
        assert [row[0] for row in retrieved] == ["artifact:evidence@v1"]
        retrieval_event_id = fs.events(kind="query")[0][1]
        answer_content = b"Final answer quotes answer evidence marker."
        answer_node = reader.answer(
            "answers/final.md",
            answer_content,
            ["artifact:evidence@v1"],
            tool_call_id="tc_answer",
            policy_label_excludes=["sensitivity"],
            retrieval_event_ids=[retrieval_event_id],
        )
        assert bytes(reader.read("answers/final.md")) == answer_content

        answer_event = fs.event(fs.events(kind="answer")[0][1])
        payload = json.loads(answer_event[6])
        estimate_tokens = lambda value: (len(value) + 3) // 4
        expected_answer_tokens = estimate_tokens(answer_content)
        expected_citation_tokens = estimate_tokens(b"answer evidence marker")
        expected_token_accounting = {
            "schema": "anfs.token_accounting.estimate.v1",
            "estimator": "ceil_bytes_div_4",
            "answer_tokens": expected_answer_tokens,
            "citation_tokens": expected_citation_tokens,
            "total_tokens": expected_answer_tokens + expected_citation_tokens,
            "citation_count": 1,
            "retrieval_event_count": 1,
        }
        assert payload["logical_path"] == "answers/final.md"
        assert payload["policy_label_excludes"] == ["sensitivity"]
        assert payload["retrieval_event_ids"] == [retrieval_event_id]
        assert payload["citation_count"] == 1
        assert payload["token_accounting"] == expected_token_accounting
        assert payload["citations"] == [
            {
                "ref_name": "artifact:evidence@v1",
                "node_id": source_node,
                "state": "published",
                "ref_version": 1,
            }
        ]
        assert answer_event[2] == "reader_agent"
        assert answer_event[3] == "run:answer"
        assert answer_event[4] == "tc_answer"
        assert answer_event[8] == [
            ("input", source_node, "answer_citation:0", "artifact:evidence@v1"),
            ("output", answer_node, "result", "answers/final.md"),
        ]
        assert fs.answer_evidence_coverage(answer_event[0]) == [
            ("artifact:evidence@v1", source_node, True, 1)
        ]
        assert fs.answer_token_accounting(answer_event[0]) == (
            "anfs.token_accounting.estimate.v1",
            "ceil_bytes_div_4",
            expected_answer_tokens,
            expected_citation_tokens,
            expected_answer_tokens + expected_citation_tokens,
            1,
            1,
        )
        assert fs.answer_quote_support(answer_event[0], min_quote_bytes=16) == [
            ("artifact:evidence@v1", source_node, True, len(b"answer evidence marker"))
        ]

        with sqlite3.connect(db_path) as conn:
            node_kind, metadata_json = conn.execute(
                "SELECT kind, metadata_json FROM nodes WHERE node_id = ?",
                (answer_node,),
            ).fetchone()
        assert node_kind == "answer"
        metadata = json.loads(metadata_json)
        assert metadata["schema"] == "anfs.answer_node.v1"
        assert metadata["retrieval_event_ids"] == [retrieval_event_id]
        assert metadata["token_accounting"] == expected_token_accounting
        assert fs.verify_integrity() == []

        no_retrieval_content = b"Answer cites evidence without linking retrieval context."
        reader.answer(
            "answers/no-retrieval.md",
            no_retrieval_content,
            ["artifact:evidence@v1"],
            tool_call_id="tc_answer_no_retrieval",
        )
        no_retrieval_event_id = fs.events(tool_call_id="tc_answer_no_retrieval")[0][1]
        assert fs.answer_evidence_coverage(no_retrieval_event_id) == [
            ("artifact:evidence@v1", source_node, False, 0)
        ]
        no_retrieval_answer_tokens = estimate_tokens(no_retrieval_content)
        assert fs.answer_token_accounting(no_retrieval_event_id) == (
            "anfs.token_accounting.estimate.v1",
            "ceil_bytes_div_4",
            no_retrieval_answer_tokens,
            expected_citation_tokens,
            no_retrieval_answer_tokens + expected_citation_tokens,
            1,
            0,
        )
        no_quote_support = fs.answer_quote_support(no_retrieval_event_id, min_quote_bytes=16)
        assert no_quote_support[0][0:3] == ("artifact:evidence@v1", source_node, False)
        assert no_quote_support[0][3] < 16
        try:
            fs.answer_quote_support(no_retrieval_event_id, min_quote_bytes=0)
            assert False, "non-positive quote support threshold should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        try:
            reader.answer(
                "answers/uncovered.md",
                b"uncovered answer",
                ["artifact:other@v1"],
                retrieval_event_ids=[retrieval_event_id],
            )
            assert False, "answer should reject citations not covered by retrieval events"
        except anfs_core.PolicyDeniedError:
            pass
        assert other_node is not None

        fs.set_policy_label(
            "node",
            source_node,
            "sensitivity",
            "pii",
            "policy_agent",
        )
        try:
            reader.answer(
                "answers/blocked.md",
                b"blocked answer",
                ["artifact:evidence@v1"],
                policy_label_excludes=["sensitivity"],
            )
            assert False, "answer should reject excluded citation labels"
        except anfs_core.PolicyDeniedError:
            pass

        draft_node = writer.write("draft.md", b"private draft", [])
        try:
            reader.answer(
                "answers/draft.md",
                b"blocked draft answer",
                ["ws:writer/draft.md"],
            )
            assert False, "answer should reject unreadable cross-workspace drafts"
        except anfs_core.PolicyDeniedError:
            pass
        assert draft_node is not None

        fs.set_policy_label("node", source_node, "sensitivity", None, "policy_agent")
        try:
            reader.answer("answers/empty.md", b"empty", [])
            assert False, "answer citations should be required"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            reader.answer(
                "answers/duplicate.md",
                b"duplicate",
                ["artifact:evidence@v1", "artifact:evidence@v1"],
            )
            assert False, "duplicate answer citations should be rejected"
        except anfs_core.PolicyDeniedError:
            pass


def test_token_cost_profile_estimates_answer_cost_without_vendor_price_defaults():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        source_node = writer.write("evidence.md", b"cost profile evidence", [])
        writer.publish("evidence.md", "artifact:evidence@v1")

        reader = fs.open_workspace("ws:reader", "reader_agent")
        answer_content = b"Costed answer."
        answer_node = reader.answer(
            "answers/costed.md",
            answer_content,
            ["artifact:evidence@v1"],
        )
        answer_event_id = fs.events(kind="answer")[0][1]
        estimate_tokens = lambda value: (len(value) + 3) // 4
        answer_tokens = estimate_tokens(answer_content)
        citation_tokens = estimate_tokens(b"cost profile evidence")

        first_profile_event = fs.set_token_cost_profile(
            "model-a",
            input_price_micros_per_million_tokens=1_000_000,
            output_price_micros_per_million_tokens=2_000_000,
            prompt_overhead_tokens=3,
            agent_id="pricing_agent",
            tool_call_id="tc_profile_1",
        )
        active_profiles = fs.token_cost_profiles()
        assert len(active_profiles) == 1
        assert active_profiles[0][0:7] == (
            "model-a",
            "ceil_bytes_div_4",
            1_000_000,
            2_000_000,
            3,
            first_profile_event,
            "pricing_agent",
        )
        assert active_profiles[0][7] > 0
        assert fs.answer_cost_estimate(answer_event_id, "model-a") == (
            "model-a",
            "ceil_bytes_div_4",
            citation_tokens + 3,
            answer_tokens,
            citation_tokens + 3 + answer_tokens,
            citation_tokens + 3,
            answer_tokens * 2,
            citation_tokens + 3 + (answer_tokens * 2),
            first_profile_event,
        )

        second_profile_event = fs.set_token_cost_profile(
            "model-a",
            input_price_micros_per_million_tokens=3_000_000,
            output_price_micros_per_million_tokens=4_000_000,
            prompt_overhead_tokens=0,
            agent_id="pricing_agent",
            tool_call_id="tc_profile_2",
        )
        assert [row[5] for row in fs.token_cost_profiles(active_only=False)] == [
            first_profile_event,
            second_profile_event,
        ]
        active_profiles = fs.token_cost_profiles()
        assert len(active_profiles) == 1
        assert active_profiles[0][0:6] == (
            "model-a",
            "ceil_bytes_div_4",
            3_000_000,
            4_000_000,
            0,
            second_profile_event,
        )
        assert fs.answer_cost_estimate(answer_event_id, "model-a") == (
            "model-a",
            "ceil_bytes_div_4",
            citation_tokens,
            answer_tokens,
            citation_tokens + answer_tokens,
            citation_tokens * 3,
            answer_tokens * 4,
            (citation_tokens * 3) + (answer_tokens * 4),
            second_profile_event,
        )

        try:
            fs.set_token_cost_profile("model-b", -1, 0)
            assert False, "negative token price should be rejected"
        except anfs_core.PolicyDeniedError:
            pass
        try:
            fs.answer_cost_estimate(answer_event_id, "missing-model")
            assert False, "missing model profile should be rejected"
        except anfs_core.PolicyDeniedError:
            pass

        with sqlite3.connect(db_path) as conn:
            try:
                conn.execute(
                    "UPDATE token_cost_profile_events SET prompt_overhead_tokens = 99"
                )
            except sqlite3.DatabaseError as exc:
                assert "token_cost_profile_events are immutable" in str(exc)
            else:
                raise AssertionError("token cost profile events should be immutable")

        assert bytes(fs.read_node(answer_node)) == answer_content
        assert bytes(fs.read_node(source_node)) == b"cost profile evidence"
        assert fs.verify_integrity() == []

        with sqlite3.connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO token_cost_profile_events
                    (model, estimator, input_price_micros_per_million_tokens,
                     output_price_micros_per_million_tokens, prompt_overhead_tokens,
                     event_id, agent_id, created_at)
                VALUES (?, 'ceil_bytes_div_4', 0, 0, 0, ?, 'tamper_agent', 0)
                """,
                ("tampered-model", answer_event_id),
            )
        issues = fs.verify_integrity()
        assert any(
            f"token_cost_profile_event {answer_event_id} must reference set_token_cost_profile event; found answer"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_answer_token_accounting_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        writer = fs.open_workspace("ws:writer", "writer_agent")
        source_node = writer.write("source.md", b"integrity evidence", [])
        writer.publish("source.md", "artifact:evidence@v1")

        reader = fs.open_workspace("ws:reader", "reader_agent")
        answer_node = reader.answer(
            "answers/final.md",
            b"Answer with auditable accounting.",
            ["artifact:evidence@v1"],
        )
        assert fs.verify_integrity() == []

        bad_event_id = "event:bad-answer-token-accounting"
        bad_payload = {
            "logical_path": "answers/bad.md",
            "content_hash": "bad",
            "content_size": 0,
            "policy_label_excludes": [],
            "citation_count": 1,
            "citations": [
                {
                    "ref_name": "artifact:evidence@v1",
                    "node_id": source_node,
                    "state": "published",
                    "ref_version": 1,
                }
            ],
            "retrieval_event_ids": [],
            "token_accounting": {
                "schema": "wrong.schema",
                "estimator": "wrong_estimator",
                "answer_tokens": 999,
                "citation_tokens": 888,
                "total_tokens": 777,
                "citation_count": 6,
                "retrieval_event_count": 5,
            },
        }
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            conn.execute(
                """
                INSERT INTO events
                    (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
                VALUES (?, 'answer', 'reader_agent', NULL, NULL, 'ws:reader', ?, 0)
                """,
                (bad_event_id, json.dumps(bad_payload)),
            )
            conn.execute(
                "INSERT INTO event_sequence (event_id, seq) VALUES (?, ?)",
                (bad_event_id, next_seq),
            )
            conn.execute(
                """
                INSERT INTO event_edges (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'input', ?, 'answer_citation:0', 'artifact:evidence@v1')
                """,
                (bad_event_id, source_node),
            )
            conn.execute(
                """
                INSERT INTO event_edges (event_id, direction, node_id, role, logical_path)
                VALUES (?, 'output', ?, 'result', 'answers/bad.md')
                """,
                (bad_event_id, answer_node),
            )

        issues = fs.verify_integrity()
        assert any(
            f"answer event {bad_event_id} token_accounting.schema wrong.schema is invalid"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.estimator wrong_estimator is invalid"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.answer_tokens 999 does not match expected"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.citation_tokens 888 does not match expected"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.total_tokens 777 does not match expected"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.citation_count 6 does not match expected 1"
            in issue
            for issue in issues
        )
        assert any(
            f"answer event {bad_event_id} token_accounting.retrieval_event_count 5 does not match expected 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_policy_label_event_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:analyst", "analyst_agent")
        node_id = ws.write("contract.md", b"Confidential contract", [])
        ws.publish("contract.md", "artifact:contract@v1")
        fs.set_policy_label(
            "ref",
            "artifact:contract@v1",
            "sensitivity",
            "confidential",
            "policy_agent",
        )
        assert fs.verify_integrity() == []

        wrong_kind_event = "event:bad-policy-label-kind"
        missing_edge_event = "event:bad-policy-label-edge"
        orphan_event = "event:bad-policy-label-orphan"
        with sqlite3.connect(db_path) as conn:
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (wrong_kind_event, "write", next_seq),
                (missing_edge_event, "policy_label", next_seq + 1),
                (orphan_event, "policy_label", next_seq + 2),
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
                INSERT INTO policy_label_events
                    (subject_type, subject_id, label, value, event_id, created_at)
                VALUES ('ref', 'artifact:contract@v1', 'sensitivity', 'secret', ?, 0)
                """,
                (wrong_kind_event,),
            )
            conn.execute(
                """
                INSERT INTO policy_label_events
                    (subject_type, subject_id, label, value, event_id, created_at)
                VALUES ('node', ?, 'exportability', 'internal', ?, 0)
                """,
                (node_id, missing_edge_event),
            )

        issues = fs.verify_integrity()
        assert any(
            f"policy_label_event {wrong_kind_event} must reference policy_label event; found write"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_label_event {wrong_kind_event} must have exactly one input policy_label_subject edge for ref artifact:contract@v1; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_label_event {missing_edge_event} must have exactly one input policy_label_subject edge for node {node_id}; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_label event {orphan_event} must have exactly one policy_label_events audit row; found 0"
            in issue
            for issue in issues
        )


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


def test_policy_decision_log_records_approve_allow_and_deny():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        coder_ws = fs.open_workspace("ws:coder", "coder_agent")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent")

        v1_node = coder_ws.write("src/db.py", b"print('v1')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v1")
        tester_ws.write("test.log", b"PASS", [v1_node])
        tester_ws.publish("test.log", "artifact:test_result@v1")

        coder_ws.write("src/db.py", b"print('v2')", [])
        coder_ws.publish("src/db.py", "artifact:patch@v2")

        try:
            fs.approve(
                "artifact:patch@v2",
                ["artifact:test_result@v1"],
                "reviewer_agent",
            )
        except anfs_core.LineageMismatchError:
            pass
        else:
            raise AssertionError("expected lineage mismatch for v2 approval")

        deny_decisions = fs.policy_decisions("artifact:patch@v2")
        assert len(deny_decisions) == 1
        deny_payload = json.loads(deny_decisions[0][1])
        assert deny_payload["policy"] == "lineage_approval"
        assert deny_payload["decision"] == "deny"
        assert deny_payload["reason_code"] == "lineage_evidence_missing"
        assert deny_payload["target_ref"] == "artifact:patch@v2"

        fs.approve(
            "artifact:patch@v1",
            ["artifact:test_result@v1"],
            "reviewer_agent",
        )
        allow_decisions = fs.policy_decisions("artifact:patch@v1")
        assert len(allow_decisions) == 1
        allow_payload = json.loads(allow_decisions[0][1])
        assert allow_payload["decision"] == "allow"
        assert allow_payload["reason_code"] == "lineage_evidence_covers_target"
        assert allow_payload["evidence_refs"] == ["artifact:test_result@v1"]

        all_decisions = fs.policy_decisions()
        assert len(all_decisions) == 2
        assert len(fs.policy_decisions(policy="lineage_approval")) == 2
        assert [
            json.loads(payload)["target_ref"]
            for _event_id, payload in fs.policy_decisions(decision="deny")
        ] == ["artifact:patch@v2"]
        assert [
            json.loads(payload)["target_ref"]
            for _event_id, payload in fs.policy_decisions(
                reason_code="lineage_evidence_covers_target"
            )
        ] == ["artifact:patch@v1"]
        assert (
            fs.policy_decisions(
                target_ref="artifact:patch@v2",
                policy="lineage_approval",
                decision="allow",
            )
            == []
        )

        with sqlite3.connect(db_path) as conn:
            edge_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM event_edges ee
                JOIN events e ON e.event_id = ee.event_id
                WHERE e.kind = 'policy_decision'
                """
            ).fetchone()[0]
            assert edge_count >= 4


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
        bad_fork = "event:bad-fork"
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
                (
                    bad_fork,
                    "fork_workspace",
                    '{"source_workspace":"ws:source","target_workspace":"ws:forked"}',
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
        assert any(
            f"fork_workspace event {bad_fork} must have at least one input fork_source edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"fork_workspace event {bad_fork} must have at least one output fork_workspace_view edge; found 0"
            in issue
            for issue in issues
        )


def test_integrity_verifier_detects_policy_merge_and_gc_event_mismatch():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent")

        node_id = ws.write("src/db.py", b"print('v1')", [])
        assert fs.verify_integrity() == []

        bad_policy = "event:bad-policy"
        bad_merge_policy = "event:bad-merge-policy"
        bad_merge = "event:bad-merge"
        bad_gc = "event:bad-gc"
        with sqlite3.connect(db_path) as conn:
            blob_hash, storage_uri, size = conn.execute(
                """
                SELECT b.hash, b.storage_uri, b.size
                FROM nodes n
                JOIN blobs b ON b.hash = n.blob_hash
                WHERE n.node_id = ?
                """,
                (node_id,),
            ).fetchone()
            next_seq = conn.execute("SELECT MAX(seq) + 1 FROM event_sequence").fetchone()[0]
            malformed_events = [
                (
                    bad_policy,
                    "policy_decision",
                    '{"policy":"lineage_approval","decision":"allow","reason_code":"x","evidence_refs":["artifact:test@v1"]}',
                    next_seq,
                ),
                (
                    bad_merge_policy,
                    "policy_decision",
                    '{"policy":"workspace_merge","decision":"deny","reason_code":"workspace_conflicts","conflict_count":1,"change_count":0}',
                    next_seq + 1,
                ),
                (bad_merge, "merge_workspace", "resource:repo/main@v1", next_seq + 2),
                (bad_gc, "gc_collect", '{"candidate_count":0}', next_seq + 3),
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
                INSERT INTO ref_events
                    (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
                VALUES ('resource:repo/main@v1/src/db.py', ?, NULL, ?, NULL, 'published')
                """,
                (bad_merge, node_id),
            )
            conn.execute(
                """
                INSERT INTO gc_blob_events
                    (hash, event_id, storage_uri, size, created_at)
                VALUES (?, ?, ?, ?, 0)
                """,
                (blob_hash, bad_gc, storage_uri, size),
            )
            conn.execute(
                """
                INSERT INTO gc_blob_events
                    (hash, event_id, storage_uri, size, created_at)
                VALUES (?, ?, ?, ?, 0)
                """,
                (blob_hash, bad_policy, storage_uri, size),
            )

        issues = fs.verify_integrity()
        assert any(
            f"policy_decision event {bad_policy} must have exactly one input policy_target edge; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_decision event {bad_policy} evidence_refs count 1 does not match policy_evidence edge count 0"
            in issue
            for issue in issues
        )
        assert any(
            f"policy_decision event {bad_merge_policy} workspace_merge conflict_count 1 must have merge_conflict input edges; found 0"
            in issue
            for issue in issues
        )
        assert any(
            f"merge_workspace event {bad_merge} ref audit count 1 must match merge output/deleted edge count 0"
            in issue
            for issue in issues
        )
        assert any(
            f"gc_collect event {bad_gc} has 1 gc_blob_events but candidate_count is 0"
            in issue
            for issue in issues
        )
        assert any(
            f"gc_blob_events for event {bad_policy} must reference gc_collect event; found 1 row(s)"
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


def test_replay_views_exclude_active_policy_labels():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        replay_dir = os.path.join(tmpdir, "replay")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:importer", "importer_agent")

        public_node = ws.write("public.txt", b"public replay content", [])
        ws.publish("public.txt", "resource:repo/main@v1/public.txt", "resource")
        secret_node = ws.write("secret.txt", b"secret replay content", [])
        ws.publish("secret.txt", "resource:repo/main@v1/secret.txt", "resource")
        secret_publish_event = fs.ref_history("resource:repo/main@v1/secret.txt")[0][1]

        fs.set_policy_label(
            "ref",
            "resource:repo/main@v1/secret.txt",
            "exportability",
            "blocked",
            "policy_agent",
        )
        filtered_view = {
            row[0]: row[1]
            for row in fs.ref_view_at_event(
                secret_publish_event,
                prefix="resource:repo/main@v1",
                policy_label_excludes=["exportability"],
            )
        }
        assert filtered_view == {"resource:repo/main@v1/public.txt": public_node}

        materialized = {
            row[1]: row[0]
            for row in fs.materialize_ref_view_at_event(
                secret_publish_event,
                replay_dir,
                prefix="resource:repo/main@v1",
                policy_label_excludes=["exportability"],
            )
        }
        assert materialized == {"public.txt": "resource:repo/main@v1/public.txt"}
        assert os.path.exists(os.path.join(replay_dir, "public.txt"))
        assert not os.path.exists(os.path.join(replay_dir, "secret.txt"))
        with open(
            os.path.join(replay_dir, ".anfs-replay-manifest.json"),
            "r",
            encoding="utf-8",
        ) as file:
            replay_manifest = json.load(file)
        assert [file["relative_path"] for file in replay_manifest["files"]] == [
            "public.txt"
        ]

        fs.set_policy_label(
            "ref",
            "resource:repo/main@v1/secret.txt",
            "exportability",
            None,
            "policy_agent",
        )
        restored_view = {
            row[0]
            for row in fs.ref_view_at_event(
                secret_publish_event,
                prefix="resource:repo/main@v1",
                policy_label_excludes=["exportability"],
            )
        }
        assert restored_view == {
            "resource:repo/main@v1/public.txt",
            "resource:repo/main@v1/secret.txt",
        }

        fs.set_policy_label("node", public_node, "retention", "blocked", "policy_agent")
        node_filtered_view = {
            row[0]
            for row in fs.ref_view_at_event(
                secret_publish_event,
                prefix="resource:repo/main@v1",
                policy_label_excludes=["retention"],
            )
        }
        assert node_filtered_view == {"resource:repo/main@v1/secret.txt"}

        try:
            fs.ref_view_at_event(secret_publish_event, policy_label_excludes=[""])
        except anfs_core.PolicyDeniedError:
            pass
        else:
            raise AssertionError("empty replay policy label excludes should be rejected")


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


def test_bundle_export_policy_label_excludes_block_labeled_refs_and_nodes():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "anfs.db")
        objs_dir = os.path.join(tmpdir, "objs")
        event_bundle_dir = os.path.join(tmpdir, "event-bundle")
        run_bundle_dir = os.path.join(tmpdir, "run-bundle")
        allowed_bundle_dir = os.path.join(tmpdir, "allowed-bundle")

        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace("ws:coder", "coder_agent", run_id="run:bundle-policy")

        public_node = ws.write("public.txt", b"public", [])
        ws.publish("public.txt", "artifact:public@v1")
        public_event = fs.ref_history("artifact:public@v1")[-1][1]
        secret_node = ws.write("secret.txt", b"secret", [public_node])
        ws.publish("secret.txt", "artifact:secret@v1")
        secret_event = fs.ref_history("artifact:secret@v1")[-1][1]

        fs.set_policy_label(
            "ref",
            "artifact:secret@v1",
            "exportability",
            "blocked",
            "policy_agent",
        )
        try:
            fs.export_event_bundle(
                secret_event,
                event_bundle_dir,
                policy_label_excludes=["exportability"],
            )
            assert False, "event bundle export should reject excluded active ref labels"
        except anfs_core.PolicyDeniedError:
            pass
        assert not os.path.exists(os.path.join(event_bundle_dir, "bundle.json"))

        fs.set_policy_label(
            "ref",
            "artifact:secret@v1",
            "exportability",
            None,
            "policy_agent",
        )
        fs.set_policy_label(
            "node",
            secret_node,
            "exportability",
            "blocked",
            "policy_agent",
        )
        try:
            fs.export_run_bundle(
                "run:bundle-policy",
                run_bundle_dir,
                policy_label_excludes=["exportability"],
            )
            assert False, "run bundle export should reject excluded active node labels"
        except anfs_core.PolicyDeniedError:
            pass
        assert not os.path.exists(os.path.join(run_bundle_dir, "bundle.json"))

        fs.set_policy_label(
            "node",
            secret_node,
            "exportability",
            None,
            "policy_agent",
        )
        bundle_path, event_count, node_count, blob_count = fs.export_event_bundle(
            public_event,
            allowed_bundle_dir,
            policy_label_excludes=["exportability"],
        )
        assert os.path.exists(bundle_path)
        assert event_count == 2
        assert node_count == 1
        assert blob_count == 1

        try:
            fs.export_event_bundle(secret_event, event_bundle_dir, policy_label_excludes=[""])
            assert False, "empty policy label excludes should be rejected"
        except anfs_core.PolicyDeniedError:
            pass


def test_bundle_import_policy_label_excludes_block_destination_labels_without_partial_writes():
    def table_counts(db_path):
        with sqlite3.connect(db_path) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("events", "nodes", "blobs", "ref_events")
            }

    with tempfile.TemporaryDirectory() as tmpdir:
        source_db_path = os.path.join(tmpdir, "source.db")
        source_objs_dir = os.path.join(tmpdir, "source-objs")
        bundle_dir = os.path.join(tmpdir, "bundle")
        ref_blocked_db_path = os.path.join(tmpdir, "ref-blocked.db")
        ref_blocked_objs_dir = os.path.join(tmpdir, "ref-blocked-objs")
        node_blocked_db_path = os.path.join(tmpdir, "node-blocked.db")
        node_blocked_objs_dir = os.path.join(tmpdir, "node-blocked-objs")

        source_fs = anfs_core.AnfsEngine(source_db_path, source_objs_dir)
        source_ws = source_fs.open_workspace("ws:coder", "coder_agent")
        patch_node = source_ws.write("src/db.py", b"print('v1')", [])
        source_ws.publish("src/db.py", "artifact:patch@v1")
        publish_event = source_fs.ref_history("artifact:patch@v1")[-1][1]
        bundle_path, event_count, node_count, blob_count = source_fs.export_event_bundle(
            publish_event,
            bundle_dir,
        )

        ref_blocked_fs = anfs_core.AnfsEngine(
            ref_blocked_db_path,
            ref_blocked_objs_dir,
        )
        ref_ws = ref_blocked_fs.open_workspace("ws:local", "local_agent")
        ref_ws.write("placeholder.txt", b"placeholder", [])
        ref_ws.publish("placeholder.txt", "artifact:patch@v1")
        ref_blocked_fs.set_policy_label(
            "ref",
            "artifact:patch@v1",
            "retention",
            "blocked",
            "policy_agent",
        )
        before_ref_block = table_counts(ref_blocked_db_path)
        try:
            ref_blocked_fs.import_event_bundle(
                bundle_path,
                policy_label_excludes=["retention"],
            )
            assert False, "bundle import should reject excluded destination ref labels"
        except anfs_core.PolicyDeniedError:
            pass
        assert table_counts(ref_blocked_db_path) == before_ref_block

        try:
            ref_blocked_fs.import_event_bundle(bundle_path, policy_label_excludes=[""])
            assert False, "empty policy label excludes should be rejected on import"
        except anfs_core.PolicyDeniedError:
            pass
        assert table_counts(ref_blocked_db_path) == before_ref_block

        node_blocked_fs = anfs_core.AnfsEngine(
            node_blocked_db_path,
            node_blocked_objs_dir,
        )
        imported = node_blocked_fs.import_event_bundle(bundle_path)
        assert imported == (publish_event, event_count, node_count, blob_count)
        node_blocked_fs.set_policy_label(
            "node",
            patch_node,
            "retention",
            "blocked",
            "policy_agent",
        )
        before_node_block = table_counts(node_blocked_db_path)
        try:
            node_blocked_fs.import_event_bundle(
                bundle_path,
                policy_label_excludes=["retention"],
            )
            assert False, "bundle import should reject excluded destination node labels"
        except anfs_core.PolicyDeniedError:
            pass
        assert table_counts(node_blocked_db_path) == before_node_block


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
    test_answer_outputs_inherit_citation_fragment_policy_labels()
    test_markdown_frontmatter_policy_labels_map_semantic_fields_to_fragments()
    test_markdown_body_section_policy_labels_map_headings_to_fragments()
    test_search_policy_label_excludes_filter_context_results()
    test_vector_embedding_projection_searches_visible_refs()
    test_chunk_embedding_projection_searches_visible_chunk_ranges()
    test_integrity_verifier_detects_corrupt_embedding_projection()
    test_integrity_verifier_detects_corrupt_chunk_embedding_projection()
    test_workspace_answer_records_citations_and_enforces_policy_labels()
    test_token_cost_profile_estimates_answer_cost_without_vendor_price_defaults()
    test_integrity_verifier_detects_answer_token_accounting_mismatch()
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
