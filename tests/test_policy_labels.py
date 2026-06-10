"""Policy labels, rules, capabilities, purposes, and fragment label propagation."""

import hashlib
import json
import os
import sqlite3
import tempfile

import anfs_core


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
