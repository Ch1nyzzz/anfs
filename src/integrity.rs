use rusqlite::{params, Connection, OptionalExtension};
use std::collections::{BTreeMap, HashSet};
use std::fs;
use std::path::Path;

use crate::{
    file_blob_path, gc_snapshot::is_blob_gc_collected, is_known_run_state, is_known_state,
    is_terminal_run_state, node_manifest_metadata, read_node_bytes, schema_migrations, sha256_hex,
    validate_state_transition, verify_ref_view_checkpoint, workspace_logical_path,
    workspace_ref_name, AnfsError, AnfsResult, ManifestDoc, CURRENT_SCHEMA_NAME,
    CURRENT_SCHEMA_VERSION, MANIFEST_MEDIA_TYPE,
};


pub(crate) fn verify_integrity(conn: &Connection, objects_dir: &Path) -> AnfsResult<Vec<String>> {
    let mut issues = Vec::new();

    check_schema_and_event_log(conn, &mut issues)?;
    check_ref_view_checkpoints(conn, &mut issues)?;
    check_event_edge_shapes(conn, &mut issues)?;
    check_access_event_edges(conn, &mut issues)?;
    check_worktree_commit_events(conn, &mut issues)?;
    check_lifecycle_event_edges(conn, &mut issues)?;
    check_workspace_event_edges(conn, &mut issues)?;
    check_policy_decision_edges(conn, &mut issues)?;
    check_merge_events(conn, &mut issues)?;
    check_gc_collect_events(conn, &mut issues)?;
    check_blobs(conn, objects_dir, &mut issues)?;
    check_chunk_indexes(conn, objects_dir, &mut issues)?;
    check_working_sets(conn, objects_dir, &mut issues)?;
    check_fts(conn, objects_dir, &mut issues)?;
    check_embeddings(conn, &mut issues)?;
    check_fragments(conn, &mut issues)?;
    check_manifests(conn, objects_dir, &mut issues)?;
    check_ref_states_and_transitions(conn, &mut issues)?;
    check_ref_event_linkage(conn, &mut issues)?;
    check_ref_audit_chain(conn, &mut issues)?;
    check_runs(conn, &mut issues)?;
    check_gc_pins(conn, &mut issues)?;
    check_retention_and_token_cost_profiles(conn, &mut issues)?;
    check_policy_labels(conn, &mut issues)?;
    check_policy_rules(conn, &mut issues)?;
    check_capability_rules(conn, &mut issues)?;

    Ok(issues)
}

// Verifies schema migration rows, foreign key constraints, and event log sequencing invariants.
fn check_schema_and_event_log(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let migrations = schema_migrations(conn)?;
    if migrations.is_empty() {
        issues.push("schema_migrations has no applied schema rows".to_string());
    }
    if !migrations
        .iter()
        .any(|(version, name)| *version == CURRENT_SCHEMA_VERSION && name == CURRENT_SCHEMA_NAME)
    {
        issues.push(format!(
            "schema_migrations is missing current schema version {CURRENT_SCHEMA_VERSION} ({CURRENT_SCHEMA_NAME})"
        ));
    }
    for (version, name) in &migrations {
        if *version > CURRENT_SCHEMA_VERSION {
            issues.push(format!(
                "schema_migrations contains future schema version {version} ({name})"
            ));
        }
    }

    let mut fk_stmt = conn.prepare("PRAGMA foreign_key_check")?;
    let fk_rows = fk_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in fk_rows {
        let (table, rowid, parent, fkid) = row?;
        issues.push(format!(
            "foreign key violation: table={table} rowid={rowid} parent={parent} fkid={fkid}"
        ));
    }

    let event_count: i64 = conn.query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))?;
    let (sequence_count, min_sequence, max_sequence): (i64, Option<i64>, Option<i64>) = conn
        .query_row(
            "SELECT COUNT(*), MIN(seq), MAX(seq) FROM event_sequence",
            [],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )?;
    if event_count != sequence_count {
        issues.push(format!(
            "event_sequence count mismatch: events={event_count} sequences={sequence_count}"
        ));
    }
    if sequence_count > 0 && (min_sequence != Some(1) || max_sequence != Some(sequence_count)) {
        issues.push(format!(
            "event_sequence is not contiguous: count={sequence_count} min={:?} max={:?}",
            min_sequence, max_sequence
        ));
    }
    Ok(())
}

// Verifies ref_view_checkpoint events and their checkpoint rows and checksums.
fn check_ref_view_checkpoints(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut missing_checkpoint_stmt = conn.prepare(
        "
        SELECT e.event_id
        FROM events e
        LEFT JOIN ref_view_checkpoints rvc ON rvc.checkpoint_id = e.event_id
        WHERE e.kind = 'ref_view_checkpoint'
          AND rvc.checkpoint_id IS NULL
        ORDER BY e.event_id
        ",
    )?;
    let missing_checkpoint_rows =
        missing_checkpoint_stmt.query_map([], |row| row.get::<_, String>(0))?;
    for row in missing_checkpoint_rows {
        let event_id = row?;
        issues.push(format!(
            "ref_view_checkpoint event {event_id} is missing checkpoint row"
        ));
    }

    let mut checkpoint_stmt = conn.prepare(
        "
        SELECT rvc.checkpoint_id, e.kind
        FROM ref_view_checkpoints rvc
        JOIN events e ON e.event_id = rvc.checkpoint_id
        ORDER BY rvc.checkpoint_id
        ",
    )?;
    let checkpoint_rows = checkpoint_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in checkpoint_rows {
        let (checkpoint_id, event_kind) = row?;
        if event_kind != "ref_view_checkpoint" {
            issues.push(format!(
                "ref_view_checkpoint row {checkpoint_id} points to event kind {event_kind}"
            ));
            continue;
        }
        match verify_ref_view_checkpoint(conn, &checkpoint_id) {
            Ok((
                _checkpoint_id,
                true,
                _expected_checksum,
                _actual_checksum,
                _expected_count,
                _actual_count,
            )) => {}
            Ok((
                _checkpoint_id,
                false,
                expected_checksum,
                actual_checksum,
                expected_count,
                actual_count,
            )) => issues.push(format!(
                "ref_view_checkpoint {checkpoint_id} mismatch: expected_checksum={expected_checksum} actual_checksum={actual_checksum} expected_count={expected_count} actual_count={actual_count}"
            )),
            Err(err) => issues.push(format!(
                "ref_view_checkpoint {checkpoint_id} verification failed: {err:?}"
            )),
        }
    }
    Ok(())
}

// Verifies event_edge direction validity and write/publish/approve edge shapes.
fn check_event_edge_shapes(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut edge_direction_stmt = conn.prepare(
        "
        SELECT event_id, node_id, role, direction
        FROM event_edges
        WHERE direction NOT IN ('input', 'output')
        ORDER BY event_id, node_id, role
        ",
    )?;
    let edge_direction_rows = edge_direction_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
        ))
    })?;
    for row in edge_direction_rows {
        let (event_id, node_id, role, direction) = row?;
        issues.push(format!(
            "event_edge {event_id}/{node_id}/{role} has invalid direction {direction}"
        ));
    }

    let mut edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'source' THEN 1 ELSE 0 END) AS source_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND (ee.role = 'result' OR ee.role LIKE 'result:%') THEN 1 ELSE 0 END) AS result_outputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'workspace_node' THEN 1 ELSE 0 END) AS workspace_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'published_ref' THEN 1 ELSE 0 END) AS published_outputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'target' THEN 1 ELSE 0 END) AS target_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'evidence' THEN 1 ELSE 0 END) AS evidence_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'approved_target' THEN 1 ELSE 0 END) AS approved_outputs
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind IN ('write', 'publish', 'approve')
        GROUP BY e.event_id, e.kind
        ORDER BY e.event_id
        ",
    )?;
    let edge_shape_rows = edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
            row.get::<_, i64>(7)?,
            row.get::<_, i64>(8)?,
        ))
    })?;
    for row in edge_shape_rows {
        let (
            event_id,
            kind,
            _source_inputs,
            result_outputs,
            workspace_inputs,
            published_outputs,
            target_inputs,
            evidence_inputs,
            approved_outputs,
        ) = row?;
        match kind.as_str() {
            "write" => {
                if result_outputs < 1 {
                    issues.push(format!(
                        "write event {event_id} must have at least one output result edge; found {result_outputs}"
                    ));
                }
            }
            "publish" => {
                if workspace_inputs != 1 {
                    issues.push(format!(
                        "publish event {event_id} must have exactly one input workspace_node edge; found {workspace_inputs}"
                    ));
                }
                if published_outputs != 1 {
                    issues.push(format!(
                        "publish event {event_id} must have exactly one output published_ref edge; found {published_outputs}"
                    ));
                }
            }
            "approve" => {
                if target_inputs != 1 {
                    issues.push(format!(
                        "approve event {event_id} must have exactly one input target edge; found {target_inputs}"
                    ));
                }
                if evidence_inputs < 1 {
                    issues.push(format!(
                        "approve event {event_id} must have at least one input evidence edge; found {evidence_inputs}"
                    ));
                }
                if approved_outputs != 1 {
                    issues.push(format!(
                        "approve event {event_id} must have exactly one output approved_target edge; found {approved_outputs}"
                    ));
                }
            }
            _ => {}
        }
    }
    Ok(())
}

// Verifies consume/read_ref/search/answer event edge shapes and answer payload consistency.
fn check_access_event_edges(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut access_edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               e.workspace_id,
               e.payload_json,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'consumed' THEN 1 ELSE 0 END) AS consumed_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'read_ref' THEN 1 ELSE 0 END) AS read_ref_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'search_result:%' THEN 1 ELSE 0 END) AS search_result_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'answer_citation:%' THEN 1 ELSE 0 END) AS answer_citation_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'result' THEN 1 ELSE 0 END) AS answer_outputs
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind IN ('consume', 'read_ref', 'search', 'answer')
        GROUP BY e.event_id, e.kind, e.workspace_id, e.payload_json
        ORDER BY e.event_id
        ",
    )?;
    let access_edge_shape_rows = access_edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
            row.get::<_, i64>(7)?,
            row.get::<_, i64>(8)?,
        ))
    })?;
    for row in access_edge_shape_rows {
        let (
            event_id,
            kind,
            workspace_id,
            payload_json,
            consumed_inputs,
            read_ref_inputs,
            search_result_inputs,
            answer_citation_inputs,
            answer_outputs,
        ) = row?;
        match kind.as_str() {
            "consume" => {
                if consumed_inputs != 1 {
                    issues.push(format!(
                        "consume event {event_id} must have exactly one input consumed edge; found {consumed_inputs}"
                    ));
                }
            }
            "read_ref" => {
                if read_ref_inputs != 1 {
                    issues.push(format!(
                        "read_ref event {event_id} must have exactly one input read_ref edge; found {read_ref_inputs}"
                    ));
                }
            }
            "search" => {
                let expected_count = payload_json
                    .as_deref()
                    .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok())
                    .and_then(|payload| {
                        payload.get("result_count").and_then(|value| value.as_i64())
                    });
                match expected_count {
                    Some(expected_count) => {
                        if search_result_inputs != expected_count {
                            issues.push(format!(
                                "search event {event_id} result_count {expected_count} does not match search_result edge count {search_result_inputs}"
                            ));
                        }
                    }
                    None => issues.push(format!(
                        "search event {event_id} has missing or invalid result_count payload"
                    )),
                }
            }
            "answer" => {
                let payload_value = payload_json
                    .as_deref()
                    .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok());
                let expected_count = payload_value.as_ref().and_then(|payload| {
                    payload
                        .get("citation_count")
                        .and_then(|value| value.as_i64())
                });
                match expected_count {
                    Some(expected_count) => {
                        if answer_citation_inputs != expected_count {
                            issues.push(format!(
                                "answer event {event_id} citation_count {expected_count} does not match answer_citation edge count {answer_citation_inputs}"
                            ));
                        }
                    }
                    None => issues.push(format!(
                        "answer event {event_id} has missing or invalid citation_count payload"
                    )),
                }
                if answer_outputs != 1 {
                    issues.push(format!(
                        "answer event {event_id} must have exactly one output result edge; found {answer_outputs}"
                    ));
                }
                if let Some(payload) = payload_value.as_ref() {
                    verify_answer_token_accounting(
                        conn,
                        issues,
                        &event_id,
                        payload,
                        answer_citation_inputs,
                    )?;
                    if let Some(retrieval_ids) = payload
                        .get("retrieval_event_ids")
                        .and_then(|value| value.as_array())
                    {
                        for retrieval_id in retrieval_ids {
                            let Some(retrieval_id) = retrieval_id.as_str() else {
                                issues.push(format!(
                                    "answer event {event_id} has non-string retrieval_event_id"
                                ));
                                continue;
                            };
                            match conn
                                .query_row(
                                    "SELECT kind, workspace_id FROM events WHERE event_id = ?1",
                                    [retrieval_id],
                                    |row| {
                                        Ok((
                                            row.get::<_, String>(0)?,
                                            row.get::<_, Option<String>>(1)?,
                                        ))
                                    },
                                )
                                .optional()?
                            {
                                Some((retrieval_kind, retrieval_workspace)) => {
                                    if retrieval_kind != "query" && retrieval_kind != "search" {
                                        issues.push(format!(
                                            "answer event {event_id} retrieval event {retrieval_id} must be query or search; found {retrieval_kind}"
                                        ));
                                    }
                                    if retrieval_workspace != workspace_id {
                                        issues.push(format!(
                                            "answer event {event_id} retrieval event {retrieval_id} workspace mismatch"
                                        ));
                                    }
                                }
                                None => issues.push(format!(
                                    "answer event {event_id} references missing retrieval event {retrieval_id}"
                                )),
                            }
                        }

                        if !retrieval_ids.is_empty() {
                            if let Some(citations) =
                                payload.get("citations").and_then(|value| value.as_array())
                            {
                                for citation in citations {
                                    let citation_ref =
                                        citation.get("ref_name").and_then(|value| value.as_str());
                                    let citation_node =
                                        citation.get("node_id").and_then(|value| value.as_str());
                                    let (Some(citation_ref), Some(citation_node)) =
                                        (citation_ref, citation_node)
                                    else {
                                        issues.push(format!(
                                            "answer event {event_id} has invalid citation payload"
                                        ));
                                        continue;
                                    };
                                    let mut covered = 0;
                                    for retrieval_id in retrieval_ids {
                                        if let Some(retrieval_id) = retrieval_id.as_str() {
                                            covered += conn.query_row(
                                                "
                                                SELECT COUNT(*)
                                                FROM event_edges ee
                                                WHERE ee.event_id = ?1
                                                  AND ee.direction = 'input'
                                                  AND (ee.role LIKE 'query_result:%' OR ee.role LIKE 'search_result:%')
                                                  AND ee.node_id = ?2
                                                  AND ee.logical_path = ?3
                                                ",
                                                (retrieval_id, citation_node, citation_ref),
                                                |row| row.get::<_, i64>(0),
                                            )?;
                                        }
                                    }
                                    if covered == 0 {
                                        issues.push(format!(
                                            "answer event {event_id} citation {citation_ref}/{citation_node} is not covered by retrieval events"
                                        ));
                                    }
                                }
                            }
                        }
                    }
                }
            }
            _ => {}
        }
    }
    Ok(())
}

// Verifies worktree_commit event payload counts against worktree edges.
fn check_worktree_commit_events(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut worktree_commit_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.payload_json,
               SUM(CASE WHEN ee.role LIKE 'worktree_%' THEN 1 ELSE 0 END) AS worktree_edges,
               SUM(CASE WHEN ee.role LIKE 'worktree_added:%'
                         OR ee.role LIKE 'worktree_modified:%'
                         OR ee.role LIKE 'worktree_deleted:%'
                        THEN 1 ELSE 0 END) AS changed_edges
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind = 'worktree_commit'
        GROUP BY e.event_id, e.payload_json
        ORDER BY e.event_id
        ",
    )?;
    let worktree_commit_shape_rows = worktree_commit_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in worktree_commit_shape_rows {
        let (event_id, payload_json, worktree_edges, changed_edges) = row?;
        let Some(payload) = payload_json
            .as_deref()
            .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok())
        else {
            issues.push(format!(
                "worktree_commit event {event_id} has missing or invalid payload"
            ));
            continue;
        };
        let result_count = payload.get("result_count").and_then(|value| value.as_i64());
        let changed_count = payload
            .get("changed_count")
            .and_then(|value| value.as_i64());
        let edge_count = payload.get("edge_count").and_then(|value| value.as_i64());
        let results_len = payload
            .get("results")
            .and_then(|value| value.as_array())
            .map(|values| values.len() as i64);
        match (result_count, results_len) {
            (Some(result_count), Some(results_len)) if result_count == results_len => {}
            (Some(result_count), Some(results_len)) => issues.push(format!(
                "worktree_commit event {event_id} result_count {result_count} does not match results length {results_len}"
            )),
            _ => issues.push(format!(
                "worktree_commit event {event_id} has missing or invalid result_count/results payload"
            )),
        }
        match edge_count {
            Some(edge_count) if edge_count == worktree_edges => {}
            Some(edge_count) => issues.push(format!(
                "worktree_commit event {event_id} edge_count {edge_count} does not match worktree edge count {worktree_edges}"
            )),
            None => issues.push(format!(
                "worktree_commit event {event_id} has missing or invalid edge_count payload"
            )),
        }
        match changed_count {
            Some(changed_count) if changed_count == changed_edges => {}
            Some(changed_count) => issues.push(format!(
                "worktree_commit event {event_id} changed_count {changed_count} does not match changed worktree edge count {changed_edges}"
            )),
            None => issues.push(format!(
                "worktree_commit event {event_id} has missing or invalid changed_count payload"
            )),
        }
    }
    Ok(())
}

// Verifies publish_manifest/delete_ref/reject event edge shapes.
fn check_lifecycle_event_edges(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut lifecycle_edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'manifest_child:%' THEN 1 ELSE 0 END) AS manifest_child_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'manifest' THEN 1 ELSE 0 END) AS manifest_outputs,
               SUM(CASE WHEN ee.direction = 'input' AND (ee.role = 'deleted_ref' OR ee.role LIKE 'deleted:%') THEN 1 ELSE 0 END) AS deleted_ref_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'target' THEN 1 ELSE 0 END) AS target_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'rejected_target' THEN 1 ELSE 0 END) AS rejected_outputs
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind IN ('publish_manifest', 'delete_ref', 'reject')
        GROUP BY e.event_id, e.kind
        ORDER BY e.event_id
        ",
    )?;
    let lifecycle_edge_shape_rows = lifecycle_edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
        ))
    })?;
    for row in lifecycle_edge_shape_rows {
        let (
            event_id,
            kind,
            manifest_child_inputs,
            manifest_outputs,
            deleted_ref_inputs,
            target_inputs,
            rejected_outputs,
        ) = row?;
        match kind.as_str() {
            "publish_manifest" => {
                if manifest_child_inputs < 1 {
                    issues.push(format!(
                        "publish_manifest event {event_id} must have at least one input manifest_child edge; found {manifest_child_inputs}"
                    ));
                }
                if manifest_outputs != 1 {
                    issues.push(format!(
                        "publish_manifest event {event_id} must have exactly one output manifest edge; found {manifest_outputs}"
                    ));
                }
            }
            "delete_ref" => {
                if deleted_ref_inputs < 1 {
                    issues.push(format!(
                        "delete_ref event {event_id} must have at least one input deleted_ref edge; found {deleted_ref_inputs}"
                    ));
                }
            }
            "reject" => {
                if target_inputs != 1 {
                    issues.push(format!(
                        "reject event {event_id} must have exactly one input target edge; found {target_inputs}"
                    ));
                }
                if rejected_outputs != 1 {
                    issues.push(format!(
                        "reject event {event_id} must have exactly one output rejected_target edge; found {rejected_outputs}"
                    ));
                }
            }
            _ => {}
        }
    }
    Ok(())
}

// Verifies checkout/fork_workspace/archive_ref/snapshot_namespace edge shapes and workspace base ref snapshots.
fn check_workspace_event_edges(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut workspace_edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               e.payload_json,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'base_source:%' THEN 1 ELSE 0 END) AS base_source_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role LIKE 'workspace_view:%' THEN 1 ELSE 0 END) AS workspace_view_outputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'fork_source:%' THEN 1 ELSE 0 END) AS fork_source_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role LIKE 'fork_workspace_view:%' THEN 1 ELSE 0 END) AS fork_workspace_view_outputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'archived_ref' THEN 1 ELSE 0 END) AS archived_ref_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'snapshot_child:%' THEN 1 ELSE 0 END) AS snapshot_child_inputs,
               SUM(CASE WHEN ee.direction = 'output' AND ee.role = 'snapshot_manifest' THEN 1 ELSE 0 END) AS snapshot_manifest_outputs
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind IN ('checkout', 'fork_workspace', 'archive_ref', 'snapshot_namespace')
        GROUP BY e.event_id, e.kind, e.payload_json
        ORDER BY e.event_id
        ",
    )?;
    let workspace_edge_shape_rows = workspace_edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
            row.get::<_, i64>(7)?,
            row.get::<_, i64>(8)?,
            row.get::<_, i64>(9)?,
        ))
    })?;
    for row in workspace_edge_shape_rows {
        let (
            event_id,
            kind,
            payload_json,
            base_source_inputs,
            workspace_view_outputs,
            fork_source_inputs,
            fork_workspace_view_outputs,
            archived_ref_inputs,
            snapshot_child_inputs,
            snapshot_manifest_outputs,
        ) = row?;
        match kind.as_str() {
            "checkout" => {
                if payload_json
                    .as_deref()
                    .is_some_and(|payload| !payload.is_empty())
                {
                    if base_source_inputs < 1 {
                        issues.push(format!(
                            "checkout event {event_id} with base must have at least one input base_source edge; found {base_source_inputs}"
                        ));
                    }
                    if workspace_view_outputs < 1 {
                        issues.push(format!(
                            "checkout event {event_id} with base must have at least one output workspace_view edge; found {workspace_view_outputs}"
                        ));
                    }
                }
            }
            "fork_workspace" => {
                if fork_source_inputs < 1 {
                    issues.push(format!(
                        "fork_workspace event {event_id} must have at least one input fork_source edge; found {fork_source_inputs}"
                    ));
                }
                if fork_workspace_view_outputs < 1 {
                    issues.push(format!(
                        "fork_workspace event {event_id} must have at least one output fork_workspace_view edge; found {fork_workspace_view_outputs}"
                    ));
                }
            }
            "archive_ref" => {
                if archived_ref_inputs != 1 {
                    issues.push(format!(
                        "archive_ref event {event_id} must have exactly one input archived_ref edge; found {archived_ref_inputs}"
                    ));
                }
            }
            "snapshot_namespace" => {
                if snapshot_child_inputs < 1 {
                    issues.push(format!(
                        "snapshot_namespace event {event_id} must have at least one input snapshot_child edge; found {snapshot_child_inputs}"
                    ));
                }
                if snapshot_manifest_outputs != 1 {
                    issues.push(format!(
                        "snapshot_namespace event {event_id} must have exactly one output snapshot_manifest edge; found {snapshot_manifest_outputs}"
                    ));
                }
            }
            _ => {}
        }
    }

    let mut checkout_base_snapshot_stmt = conn.prepare(
        "
        SELECT wbr.workspace_id,
               wbr.base_ref,
               wbr.logical_path,
               wbr.source_ref,
               wbr.node_id,
               wbr.checkout_event_id,
               e.kind,
               (
                   SELECT COUNT(*)
                   FROM event_edges ee
                   WHERE ee.event_id = wbr.checkout_event_id
                     AND ee.direction = 'input'
                     AND ee.role LIKE 'base_source:%'
                     AND ee.node_id = wbr.node_id
                     AND ee.logical_path = wbr.source_ref
               ) AS source_edges,
               (
                   SELECT COUNT(*)
                   FROM event_edges ee
                   WHERE ee.event_id = wbr.checkout_event_id
                     AND ee.direction = 'output'
                     AND ee.role LIKE 'workspace_view:%'
                     AND ee.node_id = wbr.node_id
                     AND ee.logical_path = wbr.workspace_id || '/' || wbr.logical_path
               ) AS workspace_edges
        FROM workspace_base_refs wbr
        JOIN events e ON e.event_id = wbr.checkout_event_id
        ORDER BY wbr.workspace_id, wbr.base_ref, wbr.logical_path
        ",
    )?;
    let checkout_base_snapshot_rows = checkout_base_snapshot_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, i64>(7)?,
            row.get::<_, i64>(8)?,
        ))
    })?;
    for row in checkout_base_snapshot_rows {
        let (
            workspace_id,
            base_ref,
            logical_path,
            source_ref,
            node_id,
            checkout_event_id,
            event_kind,
            source_edges,
            workspace_edges,
        ) = row?;
        if event_kind != "checkout" {
            issues.push(format!(
                "workspace_base_ref {workspace_id}/{base_ref}/{logical_path} must reference checkout event {checkout_event_id}; found {event_kind}"
            ));
        }
        if source_edges != 1 {
            issues.push(format!(
                "workspace_base_ref {workspace_id}/{base_ref}/{logical_path} must have exactly one matching input base_source edge for source {source_ref} node {node_id}; found {source_edges}"
            ));
        }
        if workspace_edges != 1 {
            let target_ref = workspace_ref_name(&workspace_id, &logical_path);
            issues.push(format!(
                "workspace_base_ref {workspace_id}/{base_ref}/{logical_path} must have exactly one matching output workspace_view edge for target {target_ref} node {node_id}; found {workspace_edges}"
            ));
        }
    }
    Ok(())
}

// Verifies policy_decision event payloads and edge shapes.
fn check_policy_decision_edges(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut policy_edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.payload_json,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role = 'policy_target' THEN 1 ELSE 0 END) AS policy_target_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'policy_evidence:%' THEN 1 ELSE 0 END) AS policy_evidence_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'merge_conflict:%' THEN 1 ELSE 0 END) AS merge_conflict_inputs,
               SUM(CASE WHEN ee.direction = 'input' AND ee.role LIKE 'merge_change:%' THEN 1 ELSE 0 END) AS merge_change_inputs
        FROM events e
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE e.kind = 'policy_decision'
        GROUP BY e.event_id, e.payload_json
        ORDER BY e.event_id
        ",
    )?;
    let policy_edge_shape_rows = policy_edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in policy_edge_shape_rows {
        let (
            event_id,
            payload_json,
            policy_target_inputs,
            policy_evidence_inputs,
            merge_conflict_inputs,
            merge_change_inputs,
        ) = row?;
        let Some(payload) = payload_json
            .as_deref()
            .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok())
        else {
            issues.push(format!(
                "policy_decision event {event_id} has missing or invalid JSON payload"
            ));
            continue;
        };
        let Some(policy) = payload.get("policy").and_then(|value| value.as_str()) else {
            issues.push(format!(
                "policy_decision event {event_id} has missing or invalid policy payload"
            ));
            continue;
        };
        if policy == "workspace_merge" {
            let conflict_count = payload
                .get("conflict_count")
                .and_then(|value| value.as_i64());
            let change_count = payload.get("change_count").and_then(|value| value.as_i64());
            match conflict_count {
                Some(conflict_count) => {
                    if conflict_count > 0 && merge_conflict_inputs < 1 {
                        issues.push(format!(
                            "policy_decision event {event_id} workspace_merge conflict_count {conflict_count} must have merge_conflict input edges; found {merge_conflict_inputs}"
                        ));
                    }
                }
                None => issues.push(format!(
                    "policy_decision event {event_id} workspace_merge has missing or invalid conflict_count payload"
                )),
            }
            match change_count {
                Some(change_count) => {
                    if change_count > 0 && merge_change_inputs < change_count {
                        issues.push(format!(
                            "policy_decision event {event_id} workspace_merge change_count {change_count} exceeds merge_change edge count {merge_change_inputs}"
                        ));
                    }
                }
                None => issues.push(format!(
                    "policy_decision event {event_id} workspace_merge has missing or invalid change_count payload"
                )),
            }
        } else {
            if policy_target_inputs != 1 {
                issues.push(format!(
                    "policy_decision event {event_id} must have exactly one input policy_target edge; found {policy_target_inputs}"
                ));
            }
            let evidence_ref_count = payload
                .get("evidence_refs")
                .and_then(|value| value.as_array())
                .map(|values| values.len() as i64);
            match evidence_ref_count {
                Some(evidence_ref_count) => {
                    if policy_evidence_inputs != evidence_ref_count {
                        issues.push(format!(
                            "policy_decision event {event_id} evidence_refs count {evidence_ref_count} does not match policy_evidence edge count {policy_evidence_inputs}"
                        ));
                    }
                }
                None => issues.push(format!(
                    "policy_decision event {event_id} has missing or invalid evidence_refs payload"
                )),
            }
        }
    }
    Ok(())
}

// Verifies merge_workspace event edge shapes and merge ref audit linkage.
fn check_merge_events(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut merge_edge_shape_stmt = conn.prepare(
        "
        SELECT e.event_id,
               (SELECT COUNT(*) FROM ref_events re WHERE re.event_id = e.event_id) AS ref_event_count,
               (SELECT COUNT(*) FROM event_edges ee WHERE ee.event_id = e.event_id AND ee.direction = 'input' AND ee.role LIKE 'merge_input:%') AS merge_inputs,
               (SELECT COUNT(*) FROM event_edges ee WHERE ee.event_id = e.event_id AND ee.direction = 'output' AND ee.role LIKE 'merge_output:%') AS merge_outputs,
               (SELECT COUNT(*) FROM event_edges ee WHERE ee.event_id = e.event_id AND ee.direction = 'input' AND ee.role LIKE 'merge_deleted:%') AS merge_deleted_inputs
        FROM events e
        WHERE e.kind = 'merge_workspace'
        ORDER BY e.event_id
        ",
    )?;
    let merge_edge_shape_rows = merge_edge_shape_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
        ))
    })?;
    for row in merge_edge_shape_rows {
        let (event_id, ref_event_count, merge_inputs, merge_outputs, merge_deleted_inputs) = row?;
        if ref_event_count > 0 {
            let changed_edge_count = merge_outputs + merge_deleted_inputs;
            if changed_edge_count != ref_event_count {
                issues.push(format!(
                    "merge_workspace event {event_id} ref audit count {ref_event_count} must match merge output/deleted edge count {changed_edge_count}"
                ));
            }
            if merge_inputs != merge_outputs {
                issues.push(format!(
                    "merge_workspace event {event_id} merge_input edge count {merge_inputs} must match merge_output edge count {merge_outputs}"
                ));
            }
        }
    }

    let mut merge_ref_linkage_stmt = conn.prepare(
        "
        SELECT re.ref_name,
               re.event_id,
               re.new_node_id,
               re.new_state,
               (SELECT COUNT(*)
                FROM event_edges ee
                WHERE ee.event_id = re.event_id
                  AND ee.direction = 'output'
                  AND ee.node_id = re.new_node_id
                  AND ee.role LIKE 'merge_output:%'
                  AND ee.logical_path = re.ref_name) AS merge_output_edges,
               (SELECT COUNT(*)
                FROM event_edges ee
                WHERE ee.event_id = re.event_id
                  AND ee.direction = 'input'
                  AND ee.node_id = re.new_node_id
                  AND ee.role LIKE 'merge_deleted:%'
                  AND ee.logical_path = re.ref_name) AS merge_deleted_edges
        FROM ref_events re
        JOIN events e ON e.event_id = re.event_id
        WHERE e.kind = 'merge_workspace'
        ORDER BY re.ref_name, re.event_id
        ",
    )?;
    let merge_ref_linkage_rows = merge_ref_linkage_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in merge_ref_linkage_rows {
        let (ref_name, event_id, new_node_id, new_state, merge_output_edges, merge_deleted_edges) =
            row?;
        let Some(new_node_id) = new_node_id else {
            continue;
        };
        match new_state.as_deref() {
            Some("published") => {
                if merge_output_edges != 1 {
                    issues.push(format!(
                        "merge_workspace ref_event {event_id} for {ref_name} must have exactly one matching output merge_output edge for node {new_node_id}; found {merge_output_edges}"
                    ));
                }
            }
            Some("archived")
                if merge_deleted_edges != 1 => {
                    issues.push(format!(
                        "merge_workspace ref_event {event_id} for {ref_name} must have exactly one matching input merge_deleted edge for node {new_node_id}; found {merge_deleted_edges}"
                    ));
                }
            _ => {}
        }
    }
    Ok(())
}

// Verifies gc_collect event payloads and gc_blob_events linkage.
fn check_gc_collect_events(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut gc_collect_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.payload_json,
               (SELECT COUNT(*) FROM gc_blob_events gbe WHERE gbe.event_id = e.event_id) AS blob_event_count
        FROM events e
        WHERE e.kind = 'gc_collect'
        ORDER BY e.event_id
        ",
    )?;
    let gc_collect_rows = gc_collect_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, Option<String>>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;
    for row in gc_collect_rows {
        let (event_id, payload_json, blob_event_count) = row?;
        let candidate_count = payload_json
            .as_deref()
            .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok())
            .and_then(|payload| {
                payload
                    .get("candidate_count")
                    .and_then(|value| value.as_i64())
            });
        match candidate_count {
            Some(candidate_count) => {
                if blob_event_count > candidate_count {
                    issues.push(format!(
                        "gc_collect event {event_id} has {blob_event_count} gc_blob_events but candidate_count is {candidate_count}"
                    ));
                }
            }
            None => issues.push(format!(
                "gc_collect event {event_id} has missing or invalid candidate_count payload"
            )),
        }
    }

    let mut gc_blob_event_stmt = conn.prepare(
        "
        SELECT gbe.event_id, COUNT(*) AS blob_event_count
        FROM gc_blob_events gbe
        LEFT JOIN events e ON e.event_id = gbe.event_id
        WHERE e.event_id IS NULL OR e.kind != 'gc_collect'
        GROUP BY gbe.event_id
        ORDER BY gbe.event_id
        ",
    )?;
    let gc_blob_event_rows = gc_blob_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in gc_blob_event_rows {
        let (event_id, blob_event_count) = row?;
        issues.push(format!(
            "gc_blob_events for event {event_id} must reference gc_collect event; found {blob_event_count} row(s)"
        ));
    }
    Ok(())
}

// Verifies blob storage integrity (size and hash for inline and file blobs).
fn check_blobs(conn: &Connection, objects_dir: &Path, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut blob_stmt = conn.prepare(
        "SELECT hash, size, storage_kind, storage_uri, inline_content
         FROM blobs
         ORDER BY hash",
    )?;
    let blob_rows = blob_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<Vec<u8>>>(4)?,
        ))
    })?;
    for row in blob_rows {
        let (hash, size, storage_kind, storage_uri, inline_content) = row?;
        match storage_kind.as_str() {
            "inline" => match inline_content {
                Some(bytes) => {
                    if bytes.len() as i64 != size {
                        issues.push(format!(
                            "inline blob size mismatch: hash={hash} expected={size} actual={}",
                            bytes.len()
                        ));
                    }
                    let actual = sha256_hex(&bytes);
                    if actual != hash {
                        issues.push(format!(
                            "inline blob hash mismatch: expected={hash} actual={actual}"
                        ));
                    }
                }
                None => issues.push(format!("inline blob {hash} has no inline_content")),
            },
            "file" => match storage_uri {
                Some(uri) => {
                    let path = file_blob_path(objects_dir, &uri);
                    match fs::read(&path) {
                        Ok(bytes) => {
                            if bytes.len() as i64 != size {
                                issues.push(format!(
                                    "file blob size mismatch: hash={hash} expected={size} actual={} path={}",
                                    bytes.len(),
                                    path.display()
                                ));
                            }
                            let actual = sha256_hex(&bytes);
                            if actual != hash {
                                issues.push(format!(
                                    "file blob hash mismatch: expected={hash} actual={actual} path={}",
                                    path.display()
                                ));
                            }
                        }
                        Err(err)
                            if err.kind() == std::io::ErrorKind::NotFound
                                && is_blob_gc_collected(conn, &hash)? =>
                        {
                            continue;
                        }
                        Err(err) => issues.push(format!(
                            "file blob {hash} unreadable at {}: {err}",
                            path.display()
                        )),
                    }
                }
                None => issues.push(format!("file blob {hash} has no storage_uri")),
            },
            other => issues.push(format!("blob {hash} has invalid storage_kind {other}")),
        }
    }
    Ok(())
}

// Verifies node chunk index summaries and per-chunk offsets, sizes, and digests.
fn check_chunk_indexes(conn: &Connection, objects_dir: &Path, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut chunk_index_stmt = conn.prepare(
        "
        SELECT ci.node_id,
               ci.chunk_size,
               ci.blob_hash,
               ci.blob_size,
               ci.chunk_count,
               n.blob_hash,
               b.size
        FROM node_chunk_indexes ci
        LEFT JOIN nodes n ON n.node_id = ci.node_id
        LEFT JOIN blobs b ON b.hash = n.blob_hash
        ORDER BY ci.node_id, ci.chunk_size
        ",
    )?;
    let chunk_index_rows = chunk_index_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, Option<String>>(5)?,
            row.get::<_, Option<i64>>(6)?,
        ))
    })?;
    for row in chunk_index_rows {
        let (
            node_id,
            chunk_size,
            cached_blob_hash,
            cached_blob_size,
            chunk_count,
            node_blob_hash,
            node_blob_size,
        ) = row?;
        if chunk_size <= 0 {
            issues.push(format!(
                "node_chunk_indexes for {node_id} has invalid chunk_size {chunk_size}"
            ));
        }
        if chunk_count < 0 {
            issues.push(format!(
                "node_chunk_indexes for {node_id} chunk_size {chunk_size} has invalid chunk_count {chunk_count}"
            ));
        }
        match (node_blob_hash.as_deref(), node_blob_size) {
            (Some(actual_hash), Some(actual_size)) => {
                if actual_hash != cached_blob_hash {
                    issues.push(format!(
                        "node_chunk_indexes for {node_id} chunk_size {chunk_size} blob_hash {cached_blob_hash} does not match node blob {actual_hash}"
                    ));
                }
                if actual_size != cached_blob_size {
                    issues.push(format!(
                        "node_chunk_indexes for {node_id} chunk_size {chunk_size} blob_size {cached_blob_size} does not match node blob size {actual_size}"
                    ));
                }
            }
            _ => issues.push(format!(
                "node_chunk_indexes for {node_id} chunk_size {chunk_size} references missing node/blob"
            )),
        }

        let mut chunk_stmt = conn.prepare(
            "
            SELECT chunk_index, offset, size, sha256
            FROM node_chunk_index
            WHERE node_id = ?1 AND chunk_size = ?2
            ORDER BY chunk_index
            ",
        )?;
        let chunk_rows = chunk_stmt.query_map(params![node_id, chunk_size], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, i64>(2)?,
                row.get::<_, String>(3)?,
            ))
        })?;
        let mut chunks = Vec::new();
        for chunk_row in chunk_rows {
            chunks.push(chunk_row?);
        }
        if chunks.len() as i64 != chunk_count {
            issues.push(format!(
                "node_chunk_indexes for {node_id} chunk_size {chunk_size} chunk_count {chunk_count} does not match row count {}",
                chunks.len()
            ));
        }
        let mut expected_offset = 0;
        for (expected_index, (chunk_index, offset, size, digest)) in chunks.iter().enumerate() {
            let expected_index = expected_index as i64;
            if *chunk_index != expected_index {
                issues.push(format!(
                    "node_chunk_index for {node_id} chunk_size {chunk_size} has chunk_index {chunk_index}, expected {expected_index}"
                ));
            }
            if *offset != expected_offset {
                issues.push(format!(
                    "node_chunk_index for {node_id} chunk_size {chunk_size} chunk {chunk_index} has offset {offset}, expected {expected_offset}"
                ));
            }
            if *size <= 0 {
                issues.push(format!(
                    "node_chunk_index for {node_id} chunk_size {chunk_size} chunk {chunk_index} has invalid size {size}"
                ));
            }
            if *offset >= 0 && *size > 0 {
                match read_node_bytes(conn, objects_dir, &node_id) {
                    Ok(node_bytes)
                        if (*offset as usize).saturating_add(*size as usize) <= node_bytes.len() =>
                    {
                        let start = *offset as usize;
                        let end = start + *size as usize;
                        let actual = sha256_hex(&node_bytes[start..end]);
                        if actual != *digest {
                            issues.push(format!(
                                "node_chunk_index for {node_id} chunk_size {chunk_size} chunk {chunk_index} sha256 mismatch: expected {digest} actual {actual}"
                            ));
                        }
                    }
                    Ok(node_bytes) => issues.push(format!(
                        "node_chunk_index for {node_id} chunk_size {chunk_size} chunk {chunk_index} range {offset}..{} exceeds node size {}",
                        offset.saturating_add(*size),
                        node_bytes.len()
                    )),
                    Err(err) => issues.push(format!(
                        "node_chunk_index for {node_id} chunk_size {chunk_size} chunk {chunk_index} is unreadable: {err:?}"
                    )),
                }
            }
            expected_offset = offset.saturating_add(*size);
        }
        if node_blob_size.is_some() && expected_offset != cached_blob_size {
            issues.push(format!(
                "node_chunk_index for {node_id} chunk_size {chunk_size} covers {expected_offset} bytes, expected {cached_blob_size}"
            ));
        }
    }
    Ok(())
}

// Verifies materialized working set rows against on-disk files and node bytes.
fn check_working_sets(conn: &Connection, objects_dir: &Path, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut working_set_stmt = conn.prepare(
        "
        SELECT cache_key, workspace_id, output_dir, file_count
        FROM materialized_working_sets
        ORDER BY cache_key
        ",
    )?;
    let working_set_rows = working_set_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in working_set_rows {
        let (cache_key, workspace_id, output_dir, file_count) = row?;
        if file_count < 0 {
            issues.push(format!(
                "materialized_working_set {cache_key} has invalid file_count {file_count}"
            ));
        }
        let row_count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM materialized_working_set_files WHERE cache_key = ?1",
            params![cache_key],
            |row| row.get(0),
        )?;
        if row_count != file_count {
            issues.push(format!(
                "materialized_working_set {cache_key} file_count {file_count} does not match row count {row_count}"
            ));
        }
        let output_path = Path::new(&output_dir);
        if !output_path.is_dir() {
            issues.push(format!(
                "materialized_working_set {cache_key} output_dir {output_dir} is missing"
            ));
        } else if !output_path.join(".anfs-worktree-manifest.json").is_file() {
            issues.push(format!(
                "materialized_working_set {cache_key} output_dir {output_dir} is missing .anfs-worktree-manifest.json"
            ));
        }

        let mut file_stmt = conn.prepare(
            "
            SELECT path, ref_name, node_id, size
            FROM materialized_working_set_files
            WHERE cache_key = ?1
            ORDER BY path
            ",
        )?;
        let file_rows = file_stmt.query_map(params![cache_key], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
            ))
        })?;
        for file_row in file_rows {
            let (relative_path, ref_name, node_id, size) = file_row?;
            if !ref_name.starts_with(&format!("{}/", workspace_id.trim_end_matches('/'))) {
                issues.push(format!(
                    "materialized_working_set {cache_key} file {relative_path} ref {ref_name} is outside workspace {workspace_id}"
                ));
            }
            if size < 0 {
                issues.push(format!(
                    "materialized_working_set {cache_key} file {relative_path} has invalid size {size}"
                ));
            }
            if !is_safe_materialized_cache_path(&relative_path) {
                issues.push(format!(
                    "materialized_working_set {cache_key} has unsafe file path {relative_path}"
                ));
                continue;
            }
            let expected_bytes = match read_node_bytes(conn, objects_dir, &node_id) {
                Ok(bytes) => bytes,
                Err(err) => {
                    issues.push(format!(
                        "materialized_working_set {cache_key} file {relative_path} node {node_id} is unreadable: {err:?}"
                    ));
                    continue;
                }
            };
            if expected_bytes.len() as i64 != size {
                issues.push(format!(
                    "materialized_working_set {cache_key} file {relative_path} size {size} does not match node byte size {}",
                    expected_bytes.len()
                ));
            }
            let materialized_path = output_path.join(&relative_path);
            match fs::read(&materialized_path) {
                Ok(actual_bytes) => {
                    if actual_bytes != expected_bytes {
                        issues.push(format!(
                            "materialized_working_set {cache_key} file {relative_path} bytes do not match node {node_id}"
                        ));
                    }
                }
                Err(err) => issues.push(format!(
                    "materialized_working_set {cache_key} file {relative_path} unreadable at {}: {err}",
                    materialized_path.display()
                )),
            }
        }
    }
    Ok(())
}

// Verifies node_fts coverage, orphans, duplicates, and body freshness.
fn check_fts(conn: &Connection, objects_dir: &Path, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut fts_stmt = conn.prepare(
        "
        SELECT n.node_id, COALESCE(n.media_type, '')
        FROM nodes n
        LEFT JOIN node_fts f ON f.node_id = n.node_id
        WHERE f.node_id IS NULL
          AND (
              n.media_type LIKE 'text/%'
              OR n.media_type = 'application/json'
              OR n.media_type = ?1
          )
        ORDER BY n.node_id
        ",
    )?;
    let fts_rows = fts_stmt.query_map(params![MANIFEST_MEDIA_TYPE], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in fts_rows {
        let (node_id, media_type) = row?;
        issues.push(format!(
            "node {node_id} with media_type {media_type} is missing node_fts row"
        ));
    }

    let mut fts_orphan_stmt = conn.prepare(
        "
        SELECT f.node_id
        FROM node_fts f
        LEFT JOIN nodes n ON n.node_id = f.node_id
        WHERE n.node_id IS NULL
        GROUP BY f.node_id
        ORDER BY f.node_id
        ",
    )?;
    let fts_orphan_rows = fts_orphan_stmt.query_map([], |row| row.get::<_, String>(0))?;
    for row in fts_orphan_rows {
        let node_id = row?;
        issues.push(format!("node_fts row references missing node {node_id}"));
    }

    let mut fts_duplicate_stmt = conn.prepare(
        "
        SELECT node_id, COUNT(*)
        FROM node_fts
        GROUP BY node_id
        HAVING COUNT(*) > 1
        ORDER BY node_id
        ",
    )?;
    let fts_duplicate_rows = fts_duplicate_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in fts_duplicate_rows {
        let (node_id, count) = row?;
        issues.push(format!("node_fts has {count} rows for node {node_id}"));
    }

    let mut fts_body_stmt = conn.prepare(
        "
        SELECT n.node_id,
               COALESCE(n.media_type, ''),
               b.hash,
               b.storage_kind,
               b.storage_uri,
               b.inline_content,
               f.body
        FROM nodes n
        JOIN blobs b ON b.hash = n.blob_hash
        JOIN node_fts f ON f.node_id = n.node_id
        WHERE n.media_type LIKE 'text/%'
           OR n.media_type = 'application/json'
           OR n.media_type = ?1
        ORDER BY n.node_id
        ",
    )?;
    let fts_body_rows = fts_body_stmt.query_map(params![MANIFEST_MEDIA_TYPE], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<Vec<u8>>>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;
    for row in fts_body_rows {
        let (node_id, media_type, hash, storage_kind, storage_uri, inline_content, indexed_body) =
            row?;
        let expected_bytes = match storage_kind.as_str() {
            "inline" => inline_content,
            "file" => match storage_uri {
                Some(uri) => {
                    let path = file_blob_path(objects_dir, &uri);
                    match fs::read(&path) {
                        Ok(bytes) => Some(bytes),
                        Err(err)
                            if err.kind() == std::io::ErrorKind::NotFound
                                && is_blob_gc_collected(conn, &hash)? =>
                        {
                            continue;
                        }
                        Err(_) => continue,
                    }
                }
                None => continue,
            },
            _ => continue,
        };
        let Some(expected_bytes) = expected_bytes else {
            continue;
        };
        let expected_body = String::from_utf8_lossy(&expected_bytes);
        if indexed_body != expected_body {
            issues.push(format!(
                "node {node_id} with media_type {media_type} has stale node_fts body"
            ));
        }
    }
    Ok(())
}

// Verifies node and chunk embedding rows (vectors, dimensions, norms).
fn check_fragments(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    // fragments: node exists, blob_hash matches the node's current blob (not
    // stale), and the byte span is well formed.
    let mut fragment_stmt = conn.prepare(
        "SELECT f.fragment_id, f.node_id, f.blob_hash, f.byte_start, f.byte_end, n.blob_hash
         FROM fragments f
         LEFT JOIN nodes n ON n.node_id = f.node_id
         ORDER BY f.fragment_id",
    )?;
    let fragment_rows = fragment_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    for row in fragment_rows {
        let (fragment_id, node_id, blob_hash, byte_start, byte_end, node_blob) = row?;
        match node_blob {
            None => issues.push(format!(
                "fragments row {fragment_id} references missing node {node_id}"
            )),
            Some(current) if current != blob_hash => issues.push(format!(
                "fragments row {fragment_id} is stale: blob_hash {blob_hash} != node {node_id} blob {current}"
            )),
            _ => {}
        }
        if byte_start < 0 || byte_end < byte_start {
            issues.push(format!(
                "fragments row {fragment_id} has invalid span {byte_start}..{byte_end}"
            ));
        }
    }

    // fragment_index_runs: node must exist.
    let mut run_stmt = conn.prepare(
        "SELECT r.node_id, r.parser, n.node_id
         FROM fragment_index_runs r
         LEFT JOIN nodes n ON n.node_id = r.node_id",
    )?;
    let run_rows = run_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
        ))
    })?;
    for row in run_rows {
        let (node_id, parser, linked) = row?;
        if linked.is_none() {
            issues.push(format!(
                "fragment_index_runs row references missing node {node_id} (parser {parser})"
            ));
        }
    }

    // fragment_edges: source fragment and evidence node must exist.
    let mut edge_stmt = conn.prepare(
        "SELECT e.src_fragment_id, e.dst_name, e.evidence_node_id, f.fragment_id, n.node_id
         FROM fragment_edges e
         LEFT JOIN fragments f ON f.fragment_id = e.src_fragment_id
         LEFT JOIN nodes n ON n.node_id = e.evidence_node_id",
    )?;
    let edge_rows = edge_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
        ))
    })?;
    for row in edge_rows {
        let (src_fragment_id, dst_name, evidence_node_id, src_exists, node_exists) = row?;
        if src_exists.is_none() {
            issues.push(format!(
                "fragment_edges row references missing src fragment {src_fragment_id} (dst {dst_name})"
            ));
        }
        if node_exists.is_none() {
            issues.push(format!(
                "fragment_edges row references missing evidence node {evidence_node_id}"
            ));
        }
    }

    Ok(())
}

fn check_embeddings(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut embedding_stmt = conn.prepare(
        "
        SELECT ne.node_id,
               ne.model,
               ne.dimensions,
               ne.vector_json,
               ne.norm,
               n.node_id
        FROM node_embeddings ne
        LEFT JOIN nodes n ON n.node_id = ne.node_id
        ORDER BY ne.node_id, ne.model
        ",
    )?;
    let embedding_rows = embedding_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, f64>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    for row in embedding_rows {
        let (node_id, model, dimensions, vector_json, norm, linked_node_id) = row?;
        if linked_node_id.is_none() {
            issues.push(format!(
                "node_embeddings row references missing node {node_id}"
            ));
        }
        if model.trim().is_empty() {
            issues.push(format!("node_embeddings row for {node_id} has empty model"));
        }
        let vector = serde_json::from_str::<Vec<f64>>(&vector_json);
        let Ok(vector) = vector else {
            issues.push(format!(
                "node_embeddings row for {node_id} model {model} has invalid vector_json"
            ));
            continue;
        };
        if dimensions <= 0 || dimensions as usize != vector.len() {
            issues.push(format!(
                "node_embeddings row for {node_id} model {model} dimensions {dimensions} does not match vector length {}",
                vector.len()
            ));
            continue;
        }
        if vector.iter().any(|value| !value.is_finite()) {
            issues.push(format!(
                "node_embeddings row for {node_id} model {model} contains non-finite values"
            ));
            continue;
        }
        let actual_norm = vector.iter().map(|value| value * value).sum::<f64>().sqrt();
        if norm <= 0.0 || !norm.is_finite() || (actual_norm - norm).abs() > 1e-9 {
            issues.push(format!(
                "node_embeddings row for {node_id} model {model} has stale norm"
            ));
        }
    }

    let mut chunk_embedding_stmt = conn.prepare(
        "
        SELECT nce.node_id,
               nce.chunk_size,
               nce.chunk_index,
               nce.model,
               nce.dimensions,
               nce.vector_json,
               nce.norm,
               nci.node_id
        FROM node_chunk_embeddings nce
        LEFT JOIN node_chunk_index nci
          ON nci.node_id = nce.node_id
         AND nci.chunk_size = nce.chunk_size
         AND nci.chunk_index = nce.chunk_index
        ORDER BY nce.node_id, nce.chunk_size, nce.chunk_index, nce.model
        ",
    )?;
    let chunk_embedding_rows = chunk_embedding_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, f64>(6)?,
            row.get::<_, Option<String>>(7)?,
        ))
    })?;
    for row in chunk_embedding_rows {
        let (
            node_id,
            chunk_size,
            chunk_index,
            model,
            dimensions,
            vector_json,
            norm,
            linked_chunk_id,
        ) = row?;
        if linked_chunk_id.is_none() {
            issues.push(format!(
                "node_chunk_embeddings row references missing chunk {chunk_index} for node {node_id} chunk_size {chunk_size}"
            ));
        }
        if chunk_size <= 0 {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} has invalid chunk_size {chunk_size}"
            ));
        }
        if chunk_index < 0 {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} has invalid chunk_index {chunk_index}"
            ));
        }
        if model.trim().is_empty() {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} chunk {chunk_index} has empty model"
            ));
        }
        let vector = serde_json::from_str::<Vec<f64>>(&vector_json);
        let Ok(vector) = vector else {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} chunk {chunk_index} model {model} has invalid vector_json"
            ));
            continue;
        };
        if dimensions <= 0 || dimensions as usize != vector.len() {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} chunk {chunk_index} model {model} dimensions {dimensions} does not match vector length {}",
                vector.len()
            ));
            continue;
        }
        if vector.iter().any(|value| !value.is_finite()) {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} chunk {chunk_index} model {model} contains non-finite values"
            ));
            continue;
        }
        let actual_norm = vector.iter().map(|value| value * value).sum::<f64>().sqrt();
        if norm <= 0.0 || !norm.is_finite() || (actual_norm - norm).abs() > 1e-9 {
            issues.push(format!(
                "node_chunk_embeddings row for {node_id} chunk_size {chunk_size} chunk {chunk_index} model {model} has stale norm"
            ));
        }
    }
    Ok(())
}

// Verifies manifest documents and manifest-producing event edge linkage.
fn check_manifests(conn: &Connection, objects_dir: &Path, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut manifest_stmt = conn.prepare(
        "
        SELECT n.node_id,
               b.hash,
               b.storage_kind,
               b.storage_uri,
               b.inline_content
        FROM nodes n
        JOIN blobs b ON b.hash = n.blob_hash
        WHERE n.media_type = ?1
        ORDER BY n.node_id
        ",
    )?;
    let manifest_rows = manifest_stmt.query_map(params![MANIFEST_MEDIA_TYPE], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<Vec<u8>>>(4)?,
        ))
    })?;
    for row in manifest_rows {
        let (node_id, hash, storage_kind, storage_uri, inline_content) = row?;
        let manifest_bytes = match storage_kind.as_str() {
            "inline" => inline_content,
            "file" => match storage_uri {
                Some(uri) => {
                    let path = file_blob_path(objects_dir, &uri);
                    match fs::read(&path) {
                        Ok(bytes) => Some(bytes),
                        Err(err)
                            if err.kind() == std::io::ErrorKind::NotFound
                                && is_blob_gc_collected(conn, &hash)? =>
                        {
                            continue;
                        }
                        Err(_) => continue,
                    }
                }
                None => continue,
            },
            _ => continue,
        };
        let Some(manifest_bytes) = manifest_bytes else {
            issues.push(format!(
                "manifest node {node_id} has no readable manifest bytes"
            ));
            continue;
        };
        let manifest: ManifestDoc = match serde_json::from_slice(&manifest_bytes) {
            Ok(manifest) => manifest,
            Err(err) => {
                issues.push(format!("manifest node {node_id} has invalid JSON: {err}"));
                continue;
            }
        };
        if manifest.schema != "anfs.manifest.v1" {
            issues.push(format!(
                "manifest node {node_id} has unsupported schema {}",
                manifest.schema
            ));
            continue;
        }
        let mut child_paths = HashSet::new();
        for child in manifest.children {
            if child.path.is_empty() {
                issues.push(format!("manifest node {node_id} has empty child path"));
            }
            if !child_paths.insert(child.path.clone()) {
                issues.push(format!(
                    "manifest node {node_id} has duplicate child path {}",
                    child.path
                ));
            }
            match node_manifest_metadata(conn, &child.node_id) {
                Ok((digest, media_type, size)) => {
                    if let Some(child_digest) = child.digest.as_deref() {
                        if child_digest != digest {
                            issues.push(format!(
                                "manifest node {node_id} child {} digest mismatch: expected={digest} actual={child_digest}",
                                child.path
                            ));
                        }
                    }
                    if child.media_type != media_type {
                        issues.push(format!(
                            "manifest node {node_id} child {} media_type mismatch: expected={:?} actual={:?}",
                            child.path, media_type, child.media_type
                        ));
                    }
                    if child.size != Some(size) {
                        issues.push(format!(
                            "manifest node {node_id} child {} size mismatch: expected={size} actual={:?}",
                            child.path, child.size
                        ));
                    }
                }
                Err(AnfsError::NodeNotFound(_)) => issues.push(format!(
                    "manifest node {node_id} child {} references missing node {}",
                    child.path, child.node_id
                )),
                Err(err) => return Err(err),
            }
        }
    }

    let mut manifest_event_stmt = conn.prepare(
        "
        SELECT e.event_id, e.kind, ee.node_id
        FROM events e
        JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE ee.direction = 'output'
          AND (
              (e.kind = 'publish_manifest' AND ee.role = 'manifest')
              OR (e.kind = 'snapshot_namespace' AND ee.role = 'snapshot_manifest')
          )
        ORDER BY e.event_id, ee.node_id
        ",
    )?;
    let manifest_event_rows = manifest_event_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    for row in manifest_event_rows {
        let (event_id, kind, manifest_node_id) = row?;
        let manifest_bytes = match read_node_bytes(conn, objects_dir, &manifest_node_id) {
            Ok(bytes) => bytes,
            Err(AnfsError::StorageCorruption(err)) => {
                issues.push(format!(
                    "{kind} event {event_id} output manifest {manifest_node_id} is unreadable: {err}"
                ));
                continue;
            }
            Err(AnfsError::NodeNotFound(_)) => {
                issues.push(format!(
                    "{kind} event {event_id} output manifest {manifest_node_id} references missing node"
                ));
                continue;
            }
            Err(err) => return Err(err),
        };
        let manifest: ManifestDoc = match serde_json::from_slice(&manifest_bytes) {
            Ok(manifest) => manifest,
            Err(err) => {
                issues.push(format!(
                    "{kind} event {event_id} output manifest {manifest_node_id} has invalid JSON: {err}"
                ));
                continue;
            }
        };
        let child_role_prefix = if kind == "publish_manifest" {
            "manifest_child"
        } else {
            "snapshot_child"
        };
        let child_role_like = format!("{child_role_prefix}:%");
        let child_edge_count: i64 = conn.query_row(
            "SELECT COUNT(*)
             FROM event_edges
             WHERE event_id = ?1
               AND direction = 'input'
               AND role LIKE ?2",
            params![event_id, child_role_like],
            |row| row.get(0),
        )?;
        if child_edge_count != manifest.children.len() as i64 {
            issues.push(format!(
                "{kind} event {event_id} child edge count {child_edge_count} does not match manifest child count {}",
                manifest.children.len()
            ));
        }
        for child in manifest.children {
            let edge_count: i64 = conn.query_row(
                "SELECT COUNT(*)
                 FROM event_edges
                 WHERE event_id = ?1
                   AND direction = 'input'
                   AND role LIKE ?2
                   AND node_id = ?3
                   AND logical_path = ?4",
                params![event_id, child_role_like, child.node_id, child.path],
                |row| row.get(0),
            )?;
            if edge_count != 1 {
                issues.push(format!(
                    "{kind} event {event_id} manifest child {} ({}) must have exactly one matching input {child_role_prefix} edge; found {edge_count}",
                    child.path, child.node_id
                ));
            }
        }
    }
    Ok(())
}

// Verifies ref states, ref_event state transitions, and ref_event node references.
fn check_ref_states_and_transitions(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut ref_stmt = conn.prepare("SELECT ref_name, state FROM refs ORDER BY ref_name")?;
    let ref_rows = ref_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in ref_rows {
        let (ref_name, state) = row?;
        if !is_known_state(&state) {
            issues.push(format!("ref {ref_name} has invalid state {state}"));
        }
    }

    let mut ref_event_stmt = conn.prepare(
        "SELECT ref_name, event_id, old_state, new_state
         FROM ref_events
         ORDER BY ref_name, event_id",
    )?;
    let ref_event_rows = ref_event_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
        ))
    })?;
    for row in ref_event_rows {
        let (ref_name, event_id, old_state, new_state) = row?;
        match (old_state.as_deref(), new_state.as_deref()) {
            (None, Some(new_state)) => {
                if !is_known_state(new_state) {
                    issues.push(format!(
                        "ref_event {event_id} for {ref_name} creates invalid state {new_state}"
                    ));
                }
            }
            (Some(old_state), Some(new_state)) => {
                if let Err(err) = validate_state_transition(old_state, new_state) {
                    issues.push(format!(
                        "ref_event {event_id} for {ref_name} has invalid transition {old_state} -> {new_state}: {err:?}"
                    ));
                }
            }
            _ => issues.push(format!(
                "ref_event {event_id} for {ref_name} has missing new_state"
            )),
        }
    }

    let mut ref_event_node_stmt = conn.prepare(
        "
        SELECT re.ref_name,
               re.event_id,
               re.old_node_id,
               old_node.node_id IS NOT NULL AS old_node_exists,
               re.new_node_id,
               new_node.node_id IS NOT NULL AS new_node_exists
        FROM ref_events re
        LEFT JOIN nodes old_node ON old_node.node_id = re.old_node_id
        LEFT JOIN nodes new_node ON new_node.node_id = re.new_node_id
        WHERE re.new_node_id IS NULL
           OR new_node.node_id IS NULL
           OR (re.old_node_id IS NOT NULL AND old_node.node_id IS NULL)
        ORDER BY re.ref_name, re.event_id
        ",
    )?;
    let ref_event_node_rows = ref_event_node_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, bool>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, bool>(5)?,
        ))
    })?;
    for row in ref_event_node_rows {
        let (ref_name, event_id, old_node_id, old_node_exists, new_node_id, new_node_exists) = row?;
        if let Some(old_node_id) = old_node_id {
            if !old_node_exists {
                issues.push(format!(
                    "ref_event {event_id} for {ref_name} old_node_id {old_node_id} references missing node"
                ));
            }
        }
        match new_node_id {
            Some(new_node_id) if !new_node_exists => issues.push(format!(
                "ref_event {event_id} for {ref_name} new_node_id {new_node_id} references missing node"
            )),
            None => issues.push(format!(
                "ref_event {event_id} for {ref_name} has missing new_node_id"
            )),
            _ => {}
        }
    }
    Ok(())
}

// Verifies ref_event linkage to event edges per event kind.
fn check_ref_event_linkage(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut ref_event_linkage_stmt = conn.prepare(
        "
        SELECT re.ref_name,
               re.event_id,
               e.kind,
               e.workspace_id,
               e.payload_json,
               re.new_node_id
        FROM ref_events re
        JOIN events e ON e.event_id = re.event_id
        WHERE e.kind IN (
            'write',
            'publish',
            'publish_manifest',
            'approve',
            'reject',
            'delete_ref',
            'link',
            'archive_ref',
            'snapshot_namespace',
            'checkout',
            'fork_workspace'
        )
        ORDER BY re.ref_name, re.event_id
        ",
    )?;
    let ref_event_linkage_rows = ref_event_linkage_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    for row in ref_event_linkage_rows {
        let (ref_name, event_id, event_kind, workspace_id, payload_json, new_node_id) = row?;
        let Some(new_node_id) = new_node_id else {
            continue;
        };
        match event_kind.as_str() {
            "write" => match (workspace_id.as_deref(), payload_json.as_deref()) {
                (Some(workspace), Some(logical_path)) => {
                    let expected_ref = workspace_ref_name(workspace, logical_path);
                    let ref_logical_path = workspace_logical_path(workspace, &ref_name);
                    let is_batch_ref = ref_logical_path
                        .as_deref()
                        .is_some_and(|path| path.starts_with(&integrity_dir_prefix(logical_path)));
                    if expected_ref != ref_name && !is_batch_ref {
                        issues.push(format!(
                            "ref_event {event_id} for {ref_name} write target does not match workspace path {expected_ref}"
                        ));
                    }
                    match ref_logical_path {
                        Some(ref_logical_path) => {
                            let edge_count = count_event_edges(
                                conn,
                                &event_id,
                                "output",
                                &new_node_id,
                                "result%",
                                Some(&ref_logical_path),
                                true,
                            )?;
                            if edge_count != 1 {
                                issues.push(format!(
                                    "ref_event {event_id} for {ref_name} must have exactly one matching output result edge for node {new_node_id} path {ref_logical_path}; found {edge_count}"
                                ));
                            }
                        }
                        None => issues.push(format!(
                            "ref_event {event_id} for {ref_name} write event has non-workspace ref name"
                        )),
                    }
                }
                _ => issues.push(format!(
                    "ref_event {event_id} for {ref_name} write event is missing workspace path payload"
                )),
            },
            "publish" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "published_ref",
                    false,
                )?;
            }
            "publish_manifest" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "manifest",
                    false,
                )?;
            }
            "approve" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "approved_target",
                    false,
                )?;
            }
            "reject" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "rejected_target",
                    false,
                )?;
            }
            "delete_ref" => match (workspace_id.as_deref(), payload_json.as_deref()) {
                (Some(workspace), Some(logical_path)) => {
                    let expected_ref = workspace_ref_name(workspace, logical_path);
                    let ref_logical_path = workspace_logical_path(workspace, &ref_name);
                    let is_batch_ref = ref_logical_path
                        .as_deref()
                        .is_some_and(|path| path.starts_with(&integrity_dir_prefix(logical_path)));
                    if expected_ref != ref_name && !is_batch_ref {
                        issues.push(format!(
                            "ref_event {event_id} for {ref_name} delete target does not match workspace path {expected_ref}"
                        ));
                    }
                    match ref_logical_path {
                        Some(ref_logical_path) => {
                            let edge_count = count_event_edges(
                                conn,
                                &event_id,
                                "input",
                                &new_node_id,
                                "deleted%",
                                Some(&ref_logical_path),
                                true,
                            )?;
                            if edge_count != 1 {
                                issues.push(format!(
                                    "ref_event {event_id} for {ref_name} must have exactly one matching input deleted edge for node {new_node_id} path {ref_logical_path}; found {edge_count}"
                                ));
                            }
                        }
                        None => issues.push(format!(
                            "ref_event {event_id} for {ref_name} delete_ref event has non-workspace ref name"
                        )),
                    }
                }
                _ => issues.push(format!(
                    "ref_event {event_id} for {ref_name} delete_ref event is missing workspace path payload"
                )),
            },
            "link" => match (workspace_id.as_deref(), payload_json.as_deref()) {
                (Some(workspace), Some(payload_json)) => {
                    let payload =
                        serde_json::from_str::<serde_json::Value>(payload_json).ok();
                    let destination_path = payload
                        .as_ref()
                        .and_then(|payload| payload.get("destination_path"))
                        .and_then(serde_json::Value::as_str);
                    match destination_path {
                        Some(destination_path) => {
                            let expected_ref = workspace_ref_name(workspace, destination_path);
                            if expected_ref != ref_name {
                                issues.push(format!(
                                    "ref_event {event_id} for {ref_name} link target does not match workspace path {expected_ref}"
                                ));
                            }
                            let ref_logical_path = workspace_logical_path(workspace, &ref_name);
                            match ref_logical_path {
                                Some(ref_logical_path) => {
                                    let edge_count = count_event_edges(
                                        conn,
                                        &event_id,
                                        "output",
                                        &new_node_id,
                                        "result",
                                        Some(&ref_logical_path),
                                        false,
                                    )?;
                                    if edge_count != 1 {
                                        issues.push(format!(
                                            "ref_event {event_id} for {ref_name} link event must have exactly one matching output result edge for node {new_node_id} path {ref_logical_path}; found {edge_count}"
                                        ));
                                    }
                                }
                                None => issues.push(format!(
                                    "ref_event {event_id} for {ref_name} link event has non-workspace ref name"
                                )),
                            }
                        }
                        None => issues.push(format!(
                            "ref_event {event_id} for {ref_name} link event is missing destination_path payload"
                        )),
                    }
                }
                _ => issues.push(format!(
                    "ref_event {event_id} for {ref_name} link event is missing workspace or payload"
                )),
            },
            "archive_ref" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "input",
                    "archived_ref",
                    false,
                )?;
            }
            "snapshot_namespace" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "snapshot_manifest",
                    false,
                )?;
            }
            "checkout" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "workspace_view:%",
                    true,
                )?;
            }
            "fork_workspace" => {
                push_ref_event_edge_issue_if_missing(
                    conn,
                    issues,
                    &event_id,
                    &ref_name,
                    &new_node_id,
                    "output",
                    "fork_workspace_view:%",
                    true,
                )?;
            }
            _ => {}
        }
    }
    Ok(())
}

// Verifies ref_event audit chain continuity and refs-vs-latest-audit divergence.
fn check_ref_audit_chain(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut ref_event_chain_stmt = conn.prepare(
        "
        SELECT ref_name,
               event_id,
               old_node_id,
               old_state,
               prev_new_node_id,
               prev_new_state
        FROM (
            SELECT re.ref_name,
                   re.event_id,
                   re.old_node_id,
                   re.old_state,
                   re.new_node_id,
                   re.new_state,
                   LAG(re.new_node_id) OVER (
                       PARTITION BY re.ref_name
                       ORDER BY es.seq
                   ) AS prev_new_node_id,
                   LAG(re.new_state) OVER (
                       PARTITION BY re.ref_name
                       ORDER BY es.seq
                   ) AS prev_new_state
            FROM ref_events re
            JOIN event_sequence es ON es.event_id = re.event_id
        ) ordered
        WHERE (
                prev_new_node_id IS NULL
                AND (old_node_id IS NOT NULL OR old_state IS NOT NULL)
              )
           OR (
                prev_new_node_id IS NOT NULL
                AND (
                    old_node_id IS NOT prev_new_node_id
                    OR old_state IS NOT prev_new_state
                )
              )
        ORDER BY ref_name, event_id
        ",
    )?;
    let ref_event_chain_rows = ref_event_chain_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    for row in ref_event_chain_rows {
        let (ref_name, event_id, old_node_id, old_state, prev_new_node_id, prev_new_state) = row?;
        match (prev_new_node_id, prev_new_state) {
            (None, None) => issues.push(format!(
                "ref_event {event_id} for {ref_name} is first audit row but has old=({:?}, {:?})",
                old_node_id, old_state
            )),
            (prev_new_node_id, prev_new_state) => issues.push(format!(
                "ref_event {event_id} for {ref_name} old=({:?}, {:?}) does not match previous new=({:?}, {:?})",
                old_node_id, old_state, prev_new_node_id, prev_new_state
            )),
        }
    }

    let mut ref_audit_stmt = conn.prepare(
        "
        SELECT r.ref_name, r.node_id, r.state, latest.new_node_id, latest.new_state
        FROM refs r
        LEFT JOIN (
            SELECT ranked.ref_name, ranked.new_node_id, ranked.new_state
            FROM (
                SELECT re.ref_name,
                       re.new_node_id,
                       re.new_state,
                       ROW_NUMBER() OVER (
                           PARTITION BY re.ref_name
                           ORDER BY es.seq DESC
                       ) AS rn
                FROM ref_events re
                JOIN event_sequence es ON es.event_id = re.event_id
            ) ranked
            WHERE ranked.rn = 1
        ) latest ON latest.ref_name = r.ref_name
        ORDER BY r.ref_name
        ",
    )?;
    let ref_audit_rows = ref_audit_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
        ))
    })?;
    for row in ref_audit_rows {
        let (ref_name, node_id, state, audit_node_id, audit_state) = row?;
        match (audit_node_id, audit_state) {
            (Some(audit_node_id), Some(audit_state)) => {
                if audit_node_id != node_id || audit_state != state {
                    issues.push(format!(
                        "ref {ref_name} current state diverges from latest audit event: current=({node_id}, {state}) audit=({audit_node_id}, {audit_state})"
                    ));
                }
            }
            _ => issues.push(format!("ref {ref_name} has no complete latest audit event")),
        }
    }
    Ok(())
}

// Verifies run states, run_event transitions, audit linkage, chain continuity, and divergence.
fn check_runs(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut run_stmt = conn.prepare("SELECT run_id, state FROM runs ORDER BY run_id")?;
    let run_rows = run_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in run_rows {
        let (run_id, state) = row?;
        if !is_known_run_state(&state) {
            issues.push(format!("run {run_id} has invalid state {state}"));
        }
    }

    let mut run_event_stmt = conn.prepare(
        "SELECT re.run_id,
                re.event_id,
                re.old_state,
                re.new_state,
                e.kind,
                e.run_id,
                e.payload_json
         FROM run_events re
         JOIN events e ON e.event_id = re.event_id
         ORDER BY re.run_id, re.event_id",
    )?;
    let run_event_rows = run_event_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, Option<String>>(5)?,
            row.get::<_, Option<String>>(6)?,
        ))
    })?;
    for row in run_event_rows {
        let (run_id, event_id, old_state, new_state, event_kind, event_run_id, payload_json) = row?;
        if !is_known_run_state(&new_state) {
            issues.push(format!(
                "run_event {event_id} for {run_id} creates invalid state {new_state}"
            ));
            continue;
        }
        let expected_kind = match old_state.as_deref() {
            None if new_state == "active" => Some("run_start"),
            Some("active") if is_terminal_run_state(&new_state) => Some("run_finish"),
            _ => {
                issues.push(format!(
                    "run_event {event_id} for {run_id} has invalid transition {:?} -> {new_state}",
                    old_state
                ));
                None
            }
        };
        if let Some(expected_kind) = expected_kind {
            if event_kind != expected_kind {
                issues.push(format!(
                    "run_event {event_id} for {run_id} transition {:?} -> {new_state} must reference event kind {expected_kind}; found {event_kind}",
                    old_state
                ));
            }
        }
        if event_run_id.as_deref() != Some(run_id.as_str()) {
            let found_run_id = event_run_id.as_deref().unwrap_or("<none>");
            issues.push(format!(
                "run_event {event_id} for {run_id} must reference event with matching run_id; found {found_run_id}"
            ));
        }
        let payload = payload_json
            .as_deref()
            .and_then(|payload| serde_json::from_str::<serde_json::Value>(payload).ok());
        match payload {
            Some(payload) => {
                if payload.get("run_id").and_then(|value| value.as_str()) != Some(run_id.as_str()) {
                    issues.push(format!(
                        "run_event {event_id} for {run_id} has payload run_id mismatch"
                    ));
                }
                if payload.get("state").and_then(|value| value.as_str()) != Some(new_state.as_str())
                {
                    issues.push(format!(
                        "run_event {event_id} for {run_id} has payload state mismatch"
                    ));
                }
            }
            None => issues.push(format!(
                "run_event {event_id} for {run_id} has missing or invalid JSON payload"
            )),
        }
    }

    let mut run_event_link_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               COUNT(re.run_id) AS audit_rows
        FROM events e
        LEFT JOIN run_events re ON re.event_id = e.event_id
        WHERE e.kind IN ('run_start', 'run_finish')
        GROUP BY e.event_id, e.kind
        ORDER BY e.event_id
        ",
    )?;
    let run_event_link_rows = run_event_link_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;
    for row in run_event_link_rows {
        let (event_id, kind, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "{kind} event {event_id} must have exactly one run_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut run_event_chain_stmt = conn.prepare(
        "
        SELECT run_id,
               event_id,
               old_state,
               old_metadata_json,
               prev_new_state,
               prev_new_metadata_json
        FROM (
            SELECT re.run_id,
                   re.event_id,
                   re.old_state,
                   re.new_state,
                   re.old_metadata_json,
                   re.new_metadata_json,
                   LAG(re.new_state) OVER (
                       PARTITION BY re.run_id
                       ORDER BY es.seq
                   ) AS prev_new_state,
                   LAG(re.new_metadata_json) OVER (
                       PARTITION BY re.run_id
                       ORDER BY es.seq
                   ) AS prev_new_metadata_json
            FROM run_events re
            JOIN event_sequence es ON es.event_id = re.event_id
        ) ordered
        WHERE (
                prev_new_state IS NULL
                AND (old_state IS NOT NULL OR old_metadata_json IS NOT NULL)
              )
           OR (
                prev_new_state IS NOT NULL
                AND (
                    old_state IS NOT prev_new_state
                    OR old_metadata_json IS NOT prev_new_metadata_json
                )
              )
        ORDER BY run_id, event_id
        ",
    )?;
    let run_event_chain_rows = run_event_chain_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    for row in run_event_chain_rows {
        let (
            run_id,
            event_id,
            old_state,
            old_metadata_json,
            prev_new_state,
            prev_new_metadata_json,
        ) = row?;
        match (prev_new_state, prev_new_metadata_json) {
            (None, None) => issues.push(format!(
                "run_event {event_id} for {run_id} is first audit row but has old=({:?}, {:?})",
                old_state, old_metadata_json
            )),
            (prev_new_state, prev_new_metadata_json) => issues.push(format!(
                "run_event {event_id} for {run_id} old=({:?}, {:?}) does not match previous new=({:?}, {:?})",
                old_state, old_metadata_json, prev_new_state, prev_new_metadata_json
            )),
        }
    }

    let mut run_audit_stmt = conn.prepare(
        "
        SELECT r.run_id,
               r.state,
               r.metadata_json,
               latest.new_state,
               latest.new_metadata_json
        FROM runs r
        LEFT JOIN (
            SELECT ranked.run_id, ranked.new_state, ranked.new_metadata_json
            FROM (
                SELECT re.run_id,
                       re.new_state,
                       re.new_metadata_json,
                       ROW_NUMBER() OVER (
                           PARTITION BY re.run_id
                           ORDER BY es.seq DESC
                       ) AS rn
                FROM run_events re
                JOIN event_sequence es ON es.event_id = re.event_id
            ) ranked
            WHERE ranked.rn = 1
        ) latest ON latest.run_id = r.run_id
        ORDER BY r.run_id
        ",
    )?;
    let run_audit_rows = run_audit_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
        ))
    })?;
    for row in run_audit_rows {
        let (run_id, state, metadata_json, audit_state, audit_metadata_json) = row?;
        match audit_state {
            Some(audit_state) => {
                if audit_state != state || audit_metadata_json != metadata_json {
                    issues.push(format!(
                        "run {run_id} current state diverges from latest audit event: current=({state}, {:?}) audit=({audit_state}, {:?})",
                        metadata_json, audit_metadata_json
                    ));
                }
            }
            None => issues.push(format!("run {run_id} has no latest audit event")),
        }
    }
    Ok(())
}

// Verifies gc pin/unpin event ordering, event linkage, and audit rows.
fn check_gc_pins(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut gc_pin_stmt = conn.prepare(
        "SELECT gpe.pin_id, gpe.event_id, gpe.action, es.seq
         FROM gc_pin_events gpe
         JOIN event_sequence es ON es.event_id = gpe.event_id
         ORDER BY gpe.pin_id, es.seq",
    )?;
    let gc_pin_rows = gc_pin_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    let mut pin_states: BTreeMap<String, String> = BTreeMap::new();
    for row in gc_pin_rows {
        let (pin_id, event_id, action, _seq) = row?;
        match action.as_str() {
            "pin" => match pin_states.get(&pin_id).map(|state| state.as_str()) {
                None => {
                    pin_states.insert(pin_id, "active".to_string());
                }
                Some("active") => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} pins an already active pin"
                )),
                Some("unpinned") => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} re-pins a terminal pin"
                )),
                Some(other) => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} has unknown verifier state {other}"
                )),
            },
            "unpin" => match pin_states.get(&pin_id).map(|state| state.as_str()) {
                Some("active") => {
                    pin_states.insert(pin_id, "unpinned".to_string());
                }
                None => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} unpins before pin"
                )),
                Some("unpinned") => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} unpins an inactive pin"
                )),
                Some(other) => issues.push(format!(
                    "gc_pin_event {event_id} for {pin_id} has unknown verifier state {other}"
                )),
            },
            _ => issues.push(format!(
                "gc_pin_event {event_id} for {pin_id} has invalid action {action}"
            )),
        }
    }

    let mut gc_pin_link_stmt = conn.prepare(
        "
        SELECT gpe.pin_id,
               gpe.event_id,
               gpe.action,
               e.kind,
               gpe.ref_name,
               gpe.node_id,
               (
                   SELECT COUNT(*)
                   FROM event_edges ee
                   WHERE ee.event_id = gpe.event_id
                     AND ee.direction = 'input'
                     AND (
                         (gpe.action = 'pin' AND ee.role = 'gc_pin_root')
                         OR (gpe.action = 'unpin' AND ee.role = 'gc_unpin_root')
                     )
                     AND ee.node_id = gpe.node_id
                     AND ee.logical_path = gpe.ref_name
               ) AS matching_root_edges
        FROM gc_pin_events gpe
        JOIN events e ON e.event_id = gpe.event_id
        WHERE gpe.action IN ('pin', 'unpin')
        ORDER BY gpe.pin_id, gpe.event_id
        ",
    )?;
    let gc_pin_link_rows = gc_pin_link_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, i64>(6)?,
        ))
    })?;
    for row in gc_pin_link_rows {
        let (pin_id, event_id, action, event_kind, ref_name, node_id, matching_root_edges) = row?;
        let (expected_kind, expected_role) = match action.as_str() {
            "pin" => ("gc_pin", "gc_pin_root"),
            "unpin" => ("gc_unpin", "gc_unpin_root"),
            _ => continue,
        };
        if event_kind != expected_kind {
            issues.push(format!(
                "gc_pin_event {event_id} for {pin_id} action {action} must reference event kind {expected_kind}; found {event_kind}"
            ));
        }
        if matching_root_edges != 1 {
            issues.push(format!(
                "gc_pin_event {event_id} for {pin_id} action {action} must have exactly one input {expected_role} edge matching ref {ref_name} and node {node_id}; found {matching_root_edges}"
            ));
        }
    }

    let mut gc_pin_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               e.kind,
               COUNT(gpe.pin_id) AS audit_rows
        FROM events e
        LEFT JOIN gc_pin_events gpe ON gpe.event_id = e.event_id
        WHERE e.kind IN ('gc_pin', 'gc_unpin')
        GROUP BY e.event_id, e.kind
        ORDER BY e.event_id
        ",
    )?;
    let gc_pin_event_rows = gc_pin_event_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;
    for row in gc_pin_event_rows {
        let (event_id, kind, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "{kind} event {event_id} must have exactly one gc_pin_events audit row; found {audit_rows}"
            ));
        }
    }
    Ok(())
}

// Verifies retention policy and token cost profile event rows and audit linkage.
fn check_retention_and_token_cost_profiles(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut retention_policy_stmt = conn.prepare(
        "
        SELECT rpe.policy_name,
               rpe.effect,
               rpe.include_workspaces,
               rpe.min_age_ms,
               rpe.limit_count,
               rpe.event_id,
               e.kind
        FROM retention_policy_events rpe
        JOIN events e ON e.event_id = rpe.event_id
        ORDER BY rpe.event_id, rpe.policy_name
        ",
    )?;
    let retention_policy_rows = retention_policy_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, Option<i64>>(3)?,
            row.get::<_, Option<i64>>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;
    for row in retention_policy_rows {
        let (policy_name, effect, include_workspaces, min_age_ms, limit, event_id, event_kind) =
            row?;
        if event_kind != "retention_policy" {
            issues.push(format!(
                "retention_policy_event {event_id} must reference retention_policy event; found {event_kind}"
            ));
        }
        if policy_name.trim().is_empty() {
            issues.push(format!(
                "retention_policy_event {event_id} has empty policy_name"
            ));
        }
        if effect != "enabled" && effect != "disabled" {
            issues.push(format!(
                "retention_policy_event {event_id} has invalid effect {effect}"
            ));
        }
        if include_workspaces != 0 && include_workspaces != 1 {
            issues.push(format!(
                "retention_policy_event {event_id} has invalid include_workspaces {include_workspaces}"
            ));
        }
        if matches!(min_age_ms, Some(value) if value < 0) {
            issues.push(format!(
                "retention_policy_event {event_id} has negative min_age_ms"
            ));
        }
        if matches!(limit, Some(value) if !(1..=10000).contains(&value)) {
            issues.push(format!(
                "retention_policy_event {event_id} has invalid limit_count"
            ));
        }
    }

    let mut retention_policy_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(rpe.event_id) AS audit_rows
        FROM events e
        LEFT JOIN retention_policy_events rpe ON rpe.event_id = e.event_id
        WHERE e.kind = 'retention_policy'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let retention_policy_event_rows = retention_policy_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in retention_policy_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "retention_policy event {event_id} must have exactly one retention_policy_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut token_cost_profile_stmt = conn.prepare(
        "
        SELECT tcpe.model,
               tcpe.estimator,
               tcpe.input_price_micros_per_million_tokens,
               tcpe.output_price_micros_per_million_tokens,
               tcpe.prompt_overhead_tokens,
               tcpe.event_id,
               e.kind
        FROM token_cost_profile_events tcpe
        JOIN events e ON e.event_id = tcpe.event_id
        ORDER BY tcpe.event_id, tcpe.model
        ",
    )?;
    let token_cost_profile_rows = token_cost_profile_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, i64>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;
    for row in token_cost_profile_rows {
        let (
            model,
            estimator,
            input_price,
            output_price,
            prompt_overhead_tokens,
            event_id,
            event_kind,
        ) = row?;
        if event_kind != "set_token_cost_profile" {
            issues.push(format!(
                "token_cost_profile_event {event_id} must reference set_token_cost_profile event; found {event_kind}"
            ));
        }
        if model.trim().is_empty() {
            issues.push(format!(
                "token_cost_profile_event {event_id} has empty model"
            ));
        }
        if estimator != "ceil_bytes_div_4" {
            issues.push(format!(
                "token_cost_profile_event {event_id} has invalid estimator {estimator}"
            ));
        }
        if input_price < 0 {
            issues.push(format!(
                "token_cost_profile_event {event_id} has negative input price"
            ));
        }
        if output_price < 0 {
            issues.push(format!(
                "token_cost_profile_event {event_id} has negative output price"
            ));
        }
        if prompt_overhead_tokens < 0 {
            issues.push(format!(
                "token_cost_profile_event {event_id} has negative prompt_overhead_tokens"
            ));
        }
    }

    let mut token_cost_profile_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(tcpe.event_id) AS audit_rows
        FROM events e
        LEFT JOIN token_cost_profile_events tcpe ON tcpe.event_id = e.event_id
        WHERE e.kind = 'set_token_cost_profile'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let token_cost_profile_event_rows = token_cost_profile_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in token_cost_profile_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "set_token_cost_profile event {event_id} must have exactly one token_cost_profile_events audit row; found {audit_rows}"
            ));
        }
    }
    Ok(())
}

// Verifies policy label and fragment policy label event rows and audit linkage.
fn check_policy_labels(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut policy_label_stmt = conn.prepare(
        "
        SELECT ple.subject_type,
               ple.subject_id,
               ple.label,
               ple.event_id,
               e.kind,
               (
                   SELECT COUNT(*)
                   FROM event_edges ee
                   WHERE ee.event_id = ple.event_id
                     AND ee.direction = 'input'
                     AND ee.role = 'policy_label_subject'
                     AND ee.logical_path = ple.subject_id
               ) AS matching_subject_edges
        FROM policy_label_events ple
        JOIN events e ON e.event_id = ple.event_id
        ORDER BY ple.event_id, ple.subject_type, ple.subject_id, ple.label
        ",
    )?;
    let policy_label_rows = policy_label_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    for row in policy_label_rows {
        let (subject_type, subject_id, label, event_id, event_kind, matching_subject_edges) = row?;
        if label.trim().is_empty() {
            issues.push(format!("policy_label_event {event_id} has empty label"));
        }
        if event_kind != "policy_label" {
            issues.push(format!(
                "policy_label_event {event_id} must reference policy_label event; found {event_kind}"
            ));
        }
        match subject_type.as_str() {
            "ref" => {
                let ref_exists: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM refs WHERE ref_name = ?1",
                    params![subject_id],
                    |row| row.get(0),
                )?;
                if ref_exists != 1 {
                    issues.push(format!(
                        "policy_label_event {event_id} references missing ref {subject_id}"
                    ));
                }
            }
            "node" => {
                let node_exists: i64 = conn.query_row(
                    "SELECT COUNT(*) FROM nodes WHERE node_id = ?1",
                    params![subject_id],
                    |row| row.get(0),
                )?;
                if node_exists != 1 {
                    issues.push(format!(
                        "policy_label_event {event_id} references missing node {subject_id}"
                    ));
                }
            }
            other => issues.push(format!(
                "policy_label_event {event_id} has invalid subject_type {other}"
            )),
        }
        if matching_subject_edges != 1 {
            issues.push(format!(
                "policy_label_event {event_id} must have exactly one input policy_label_subject edge for {subject_type} {subject_id}; found {matching_subject_edges}"
            ));
        }
    }

    let mut policy_label_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(ple.event_id) AS audit_rows
        FROM events e
        LEFT JOIN policy_label_events ple ON ple.event_id = e.event_id
        WHERE e.kind = 'policy_label'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let policy_label_event_rows = policy_label_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in policy_label_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "policy_label event {event_id} must have exactly one policy_label_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut fragment_policy_label_stmt = conn.prepare(
        "
        SELECT fple.node_id,
               fple.offset,
               fple.length,
               fple.label,
               fple.event_id,
               e.kind,
               b.size,
               (
                   SELECT COUNT(*)
                   FROM event_edges ee
                   WHERE ee.event_id = fple.event_id
                     AND ee.direction = 'input'
                     AND ee.node_id = fple.node_id
                     AND ee.role = 'fragment_policy_subject'
                     AND ee.logical_path = fple.node_id
               ) AS matching_subject_edges
        FROM fragment_policy_label_events fple
        JOIN events e ON e.event_id = fple.event_id
        LEFT JOIN nodes n ON n.node_id = fple.node_id
        LEFT JOIN blobs b ON b.hash = n.blob_hash
        ORDER BY fple.event_id, fple.node_id, fple.offset, fple.length, fple.label
        ",
    )?;
    let fragment_policy_label_rows = fragment_policy_label_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, Option<i64>>(6)?,
            row.get::<_, i64>(7)?,
        ))
    })?;
    for row in fragment_policy_label_rows {
        let (node_id, offset, length, label, event_id, event_kind, node_size, matching_edges) =
            row?;
        if event_kind != "fragment_policy_label" {
            issues.push(format!(
                "fragment_policy_label_event {event_id} must reference fragment_policy_label event; found {event_kind}"
            ));
        }
        if label.trim().is_empty() {
            issues.push(format!(
                "fragment_policy_label_event {event_id} has empty label"
            ));
        }
        if offset < 0 || length <= 0 {
            issues.push(format!(
                "fragment_policy_label_event {event_id} has invalid range offset={offset} length={length}"
            ));
        }
        match node_size {
            Some(size) => {
                if offset.saturating_add(length) > size {
                    issues.push(format!(
                        "fragment_policy_label_event {event_id} range {offset}..{} exceeds node {node_id} size {size}",
                        offset.saturating_add(length)
                    ));
                }
            }
            None => issues.push(format!(
                "fragment_policy_label_event {event_id} references missing node {node_id}"
            )),
        }
        if matching_edges != 1 {
            issues.push(format!(
                "fragment_policy_label_event {event_id} must have exactly one input fragment_policy_subject edge for node {node_id}; found {matching_edges}"
            ));
        }
    }

    let mut fragment_policy_label_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(fple.event_id) AS audit_rows
        FROM events e
        LEFT JOIN fragment_policy_label_events fple ON fple.event_id = e.event_id
        WHERE e.kind = 'fragment_policy_label'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let fragment_policy_label_event_rows = fragment_policy_label_event_stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
    for row in fragment_policy_label_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "fragment_policy_label event {event_id} must have exactly one fragment_policy_label_events audit row; found {audit_rows}"
            ));
        }
    }
    Ok(())
}

// Verifies policy rule, policy expression rule, and purpose policy rule events and audit linkage.
fn check_policy_rules(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut policy_rule_stmt = conn.prepare(
        "
        SELECT pre.scope,
               pre.subject_type,
               pre.label,
               pre.value,
               pre.effect,
               pre.event_id,
               e.kind
        FROM policy_rule_events pre
        JOIN events e ON e.event_id = pre.event_id
        ORDER BY pre.event_id, pre.scope, pre.subject_type, pre.label, pre.value
        ",
    )?;
    let policy_rule_rows = policy_rule_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;
    for row in policy_rule_rows {
        let (scope, subject_type, label, value, effect, event_id, event_kind) = row?;
        if event_kind != "policy_rule" {
            issues.push(format!(
                "policy_rule_event {event_id} must reference policy_rule event; found {event_kind}"
            ));
        }
        if scope != "visibility" {
            issues.push(format!(
                "policy_rule_event {event_id} has invalid scope {scope}"
            ));
        }
        if !matches!(subject_type.as_str(), "*" | "ref" | "node" | "fragment") {
            issues.push(format!(
                "policy_rule_event {event_id} has invalid subject_type {subject_type}"
            ));
        }
        if label.trim().is_empty() {
            issues.push(format!("policy_rule_event {event_id} has empty label"));
        }
        if value.trim().is_empty() {
            issues.push(format!("policy_rule_event {event_id} has empty value"));
        }
        if let Some(effect) = effect {
            if effect != "deny" {
                issues.push(format!(
                    "policy_rule_event {event_id} has invalid effect {effect}"
                ));
            }
        }
    }

    let mut policy_rule_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(pre.event_id) AS audit_rows
        FROM events e
        LEFT JOIN policy_rule_events pre ON pre.event_id = e.event_id
        WHERE e.kind = 'policy_rule'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let policy_rule_event_rows = policy_rule_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in policy_rule_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "policy_rule event {event_id} must have exactly one policy_rule_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut policy_expression_rule_stmt = conn.prepare(
        "
        SELECT pere.scope,
               pere.subject_type,
               pere.expression_json,
               pere.effect,
               pere.event_id,
               e.kind
        FROM policy_expression_rule_events pere
        JOIN events e ON e.event_id = pere.event_id
        ORDER BY pere.event_id, pere.scope, pere.subject_type, pere.expression_json
        ",
    )?;
    let policy_expression_rule_rows = policy_expression_rule_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, String>(5)?,
        ))
    })?;
    for row in policy_expression_rule_rows {
        let (scope, subject_type, expression_json, effect, event_id, event_kind) = row?;
        if event_kind != "policy_expression_rule" {
            issues.push(format!(
                "policy_expression_rule_event {event_id} must reference policy_expression_rule event; found {event_kind}"
            ));
        }
        if scope != "visibility" {
            issues.push(format!(
                "policy_expression_rule_event {event_id} has invalid scope {scope}"
            ));
        }
        if !matches!(subject_type.as_str(), "*" | "ref" | "node" | "fragment") {
            issues.push(format!(
                "policy_expression_rule_event {event_id} has invalid subject_type {subject_type}"
            ));
        }
        if let Some(effect) = effect {
            if effect != "deny" {
                issues.push(format!(
                    "policy_expression_rule_event {event_id} has invalid effect {effect}"
                ));
            }
        }
        match serde_json::from_str::<serde_json::Value>(&expression_json) {
            Ok(expression) => {
                if let Err(message) = validate_policy_expression_for_integrity(&expression) {
                    issues.push(format!(
                        "policy_expression_rule_event {event_id} has invalid expression: {message}"
                    ));
                }
            }
            Err(err) => issues.push(format!(
                "policy_expression_rule_event {event_id} has invalid expression JSON: {err}"
            )),
        }
    }

    let mut policy_expression_rule_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(pere.event_id) AS audit_rows
        FROM events e
        LEFT JOIN policy_expression_rule_events pere ON pere.event_id = e.event_id
        WHERE e.kind = 'policy_expression_rule'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let policy_expression_rule_event_rows = policy_expression_rule_event_stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
    for row in policy_expression_rule_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "policy_expression_rule event {event_id} must have exactly one policy_expression_rule_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut purpose_policy_rule_stmt = conn.prepare(
        "
        SELECT ppre.purpose,
               ppre.subject_type,
               ppre.label,
               ppre.value,
               ppre.effect,
               ppre.event_id,
               e.kind
        FROM purpose_policy_rule_events ppre
        JOIN events e ON e.event_id = ppre.event_id
        ORDER BY ppre.event_id, ppre.purpose, ppre.subject_type, ppre.label, ppre.value
        ",
    )?;
    let purpose_policy_rule_rows = purpose_policy_rule_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, String>(6)?,
        ))
    })?;
    for row in purpose_policy_rule_rows {
        let (purpose, subject_type, label, value, effect, event_id, event_kind) = row?;
        if event_kind != "purpose_policy_rule" {
            issues.push(format!(
                "purpose_policy_rule_event {event_id} must reference purpose_policy_rule event; found {event_kind}"
            ));
        }
        if purpose.trim().is_empty() {
            issues.push(format!(
                "purpose_policy_rule_event {event_id} has empty purpose"
            ));
        }
        if !matches!(subject_type.as_str(), "*" | "ref" | "node" | "fragment") {
            issues.push(format!(
                "purpose_policy_rule_event {event_id} has invalid subject_type {subject_type}"
            ));
        }
        if label.trim().is_empty() {
            issues.push(format!(
                "purpose_policy_rule_event {event_id} has empty label"
            ));
        }
        if value.trim().is_empty() {
            issues.push(format!(
                "purpose_policy_rule_event {event_id} has empty value"
            ));
        }
        if let Some(effect) = effect {
            if effect != "deny" {
                issues.push(format!(
                    "purpose_policy_rule_event {event_id} has invalid effect {effect}"
                ));
            }
        }
    }

    let mut purpose_policy_rule_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(ppre.event_id) AS audit_rows
        FROM events e
        LEFT JOIN purpose_policy_rule_events ppre ON ppre.event_id = e.event_id
        WHERE e.kind = 'purpose_policy_rule'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let purpose_policy_rule_event_rows = purpose_policy_rule_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in purpose_policy_rule_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "purpose_policy_rule event {event_id} must have exactly one purpose_policy_rule_events audit row; found {audit_rows}"
            ));
        }
    }
    Ok(())
}

// Verifies agent/purpose/operation capability rule events and audit linkage.
fn check_capability_rules(conn: &Connection, issues: &mut Vec<String>) -> AnfsResult<()> {
    let mut agent_capability_stmt = conn.prepare(
        "
        SELECT ace.agent_id,
               ace.capability,
               ace.effect,
               ace.event_id,
               e.kind
        FROM agent_capability_events ace
        JOIN events e ON e.event_id = ace.event_id
        ORDER BY ace.event_id, ace.agent_id, ace.capability
        ",
    )?;
    let agent_capability_rows = agent_capability_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    for row in agent_capability_rows {
        let (agent_id, capability, effect, event_id, event_kind) = row?;
        if event_kind != "agent_capability" {
            issues.push(format!(
                "agent_capability_event {event_id} must reference agent_capability event; found {event_kind}"
            ));
        }
        if agent_id.trim().is_empty() {
            issues.push(format!(
                "agent_capability_event {event_id} has empty agent_id"
            ));
        }
        if capability.trim().is_empty() {
            issues.push(format!(
                "agent_capability_event {event_id} has empty capability"
            ));
        }
        if let Some(effect) = effect {
            if effect != "grant" {
                issues.push(format!(
                    "agent_capability_event {event_id} has invalid effect {effect}"
                ));
            }
        }
    }

    let mut agent_capability_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(ace.event_id) AS audit_rows
        FROM events e
        LEFT JOIN agent_capability_events ace ON ace.event_id = e.event_id
        WHERE e.kind = 'agent_capability'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let agent_capability_event_rows = agent_capability_event_stmt.query_map([], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
    })?;
    for row in agent_capability_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "agent_capability event {event_id} must have exactly one agent_capability_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut purpose_capability_rule_stmt = conn.prepare(
        "
        SELECT pcre.purpose,
               pcre.capability,
               pcre.effect,
               pcre.event_id,
               e.kind
        FROM purpose_capability_rule_events pcre
        JOIN events e ON e.event_id = pcre.event_id
        ORDER BY pcre.event_id, pcre.purpose, pcre.capability
        ",
    )?;
    let purpose_capability_rule_rows = purpose_capability_rule_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    for row in purpose_capability_rule_rows {
        let (purpose, capability, effect, event_id, event_kind) = row?;
        if event_kind != "purpose_capability_rule" {
            issues.push(format!(
                "purpose_capability_rule_event {event_id} must reference purpose_capability_rule event; found {event_kind}"
            ));
        }
        if purpose.trim().is_empty() {
            issues.push(format!(
                "purpose_capability_rule_event {event_id} has empty purpose"
            ));
        }
        if capability.trim().is_empty() {
            issues.push(format!(
                "purpose_capability_rule_event {event_id} has empty capability"
            ));
        }
        if let Some(effect) = effect {
            if effect != "require" {
                issues.push(format!(
                    "purpose_capability_rule_event {event_id} has invalid effect {effect}"
                ));
            }
        }
    }

    let mut purpose_capability_rule_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(pcre.event_id) AS audit_rows
        FROM events e
        LEFT JOIN purpose_capability_rule_events pcre ON pcre.event_id = e.event_id
        WHERE e.kind = 'purpose_capability_rule'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let purpose_capability_rule_event_rows = purpose_capability_rule_event_stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
    for row in purpose_capability_rule_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "purpose_capability_rule event {event_id} must have exactly one purpose_capability_rule_events audit row; found {audit_rows}"
            ));
        }
    }

    let mut operation_capability_rule_stmt = conn.prepare(
        "
        SELECT ocre.operation,
               ocre.capability,
               ocre.effect,
               ocre.event_id,
               e.kind
        FROM operation_capability_rule_events ocre
        JOIN events e ON e.event_id = ocre.event_id
        ORDER BY ocre.event_id, ocre.operation, ocre.capability
        ",
    )?;
    let operation_capability_rule_rows = operation_capability_rule_stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
        ))
    })?;
    for row in operation_capability_rule_rows {
        let (operation, capability, effect, event_id, event_kind) = row?;
        if event_kind != "operation_capability_rule" {
            issues.push(format!(
                "operation_capability_rule_event {event_id} must reference operation_capability_rule event; found {event_kind}"
            ));
        }
        if operation.trim().is_empty() {
            issues.push(format!(
                "operation_capability_rule_event {event_id} has empty operation"
            ));
        }
        if capability.trim().is_empty() {
            issues.push(format!(
                "operation_capability_rule_event {event_id} has empty capability"
            ));
        }
        if let Some(effect) = effect {
            if effect != "require" {
                issues.push(format!(
                    "operation_capability_rule_event {event_id} has invalid effect {effect}"
                ));
            }
        }
    }

    let mut operation_capability_rule_event_stmt = conn.prepare(
        "
        SELECT e.event_id,
               COUNT(ocre.event_id) AS audit_rows
        FROM events e
        LEFT JOIN operation_capability_rule_events ocre ON ocre.event_id = e.event_id
        WHERE e.kind = 'operation_capability_rule'
        GROUP BY e.event_id
        ORDER BY e.event_id
        ",
    )?;
    let operation_capability_rule_event_rows = operation_capability_rule_event_stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })?;
    for row in operation_capability_rule_event_rows {
        let (event_id, audit_rows) = row?;
        if audit_rows != 1 {
            issues.push(format!(
                "operation_capability_rule event {event_id} must have exactly one operation_capability_rule_events audit row; found {audit_rows}"
            ));
        }
    }
    Ok(())
}

fn verify_answer_token_accounting(
    conn: &Connection,
    issues: &mut Vec<String>,
    event_id: &str,
    payload: &serde_json::Value,
    answer_citation_inputs: i64,
) -> AnfsResult<()> {
    let Some(token_accounting) = payload.get("token_accounting") else {
        issues.push(format!(
            "answer event {event_id} has missing token_accounting payload"
        ));
        return Ok(());
    };
    let Some(token_accounting) = token_accounting.as_object() else {
        issues.push(format!(
            "answer event {event_id} has invalid token_accounting payload"
        ));
        return Ok(());
    };

    match token_accounting
        .get("schema")
        .and_then(|value| value.as_str())
    {
        Some("anfs.token_accounting.estimate.v1") => {}
        Some(schema) => issues.push(format!(
            "answer event {event_id} token_accounting.schema {schema} is invalid"
        )),
        None => issues.push(format!(
            "answer event {event_id} has missing or invalid token_accounting.schema"
        )),
    }
    match token_accounting
        .get("estimator")
        .and_then(|value| value.as_str())
    {
        Some("ceil_bytes_div_4") => {}
        Some(estimator) => issues.push(format!(
            "answer event {event_id} token_accounting.estimator {estimator} is invalid"
        )),
        None => issues.push(format!(
            "answer event {event_id} has missing or invalid token_accounting.estimator"
        )),
    }

    let answer_size = conn
        .query_row(
            "
            SELECT b.size
            FROM event_edges ee
            JOIN nodes n ON n.node_id = ee.node_id
            JOIN blobs b ON b.hash = n.blob_hash
            WHERE ee.event_id = ?1
              AND ee.direction = 'output'
              AND ee.role = 'result'
            ",
            [event_id],
            |row| row.get::<_, i64>(0),
        )
        .optional()?;
    let expected_answer_tokens = answer_size.map(estimated_token_count);

    let mut citation_stmt = conn.prepare(
        "
        SELECT b.size
        FROM event_edges ee
        JOIN nodes n ON n.node_id = ee.node_id
        JOIN blobs b ON b.hash = n.blob_hash
        WHERE ee.event_id = ?1
          AND ee.direction = 'input'
          AND ee.role LIKE 'answer_citation:%'
        ORDER BY CAST(substr(ee.role, 17) AS INTEGER)
        ",
    )?;
    let citation_rows = citation_stmt.query_map([event_id], |row| row.get::<_, i64>(0))?;
    let mut expected_citation_tokens = 0_i64;
    for row in citation_rows {
        expected_citation_tokens += estimated_token_count(row?);
    }

    if let Some(expected_answer_tokens) = expected_answer_tokens {
        verify_token_accounting_i64(
            issues,
            event_id,
            token_accounting,
            "answer_tokens",
            expected_answer_tokens,
        );
        verify_token_accounting_i64(
            issues,
            event_id,
            token_accounting,
            "total_tokens",
            expected_answer_tokens + expected_citation_tokens,
        );
    }
    verify_token_accounting_i64(
        issues,
        event_id,
        token_accounting,
        "citation_tokens",
        expected_citation_tokens,
    );
    verify_token_accounting_i64(
        issues,
        event_id,
        token_accounting,
        "citation_count",
        answer_citation_inputs,
    );

    let expected_retrieval_event_count = payload
        .get("retrieval_event_ids")
        .and_then(|value| value.as_array())
        .map(|values| values.len() as i64);
    if let Some(expected_retrieval_event_count) = expected_retrieval_event_count {
        verify_token_accounting_i64(
            issues,
            event_id,
            token_accounting,
            "retrieval_event_count",
            expected_retrieval_event_count,
        );
    }

    Ok(())
}

fn verify_token_accounting_i64(
    issues: &mut Vec<String>,
    event_id: &str,
    token_accounting: &serde_json::Map<String, serde_json::Value>,
    field: &str,
    expected: i64,
) {
    match token_accounting.get(field).and_then(|value| value.as_i64()) {
        Some(actual) if actual == expected => {}
        Some(actual) => issues.push(format!(
            "answer event {event_id} token_accounting.{field} {actual} does not match expected {expected}"
        )),
        None => issues.push(format!(
            "answer event {event_id} has missing or invalid token_accounting.{field}"
        )),
    }
}

fn estimated_token_count(byte_count: i64) -> i64 {
    if byte_count <= 0 {
        0
    } else {
        (byte_count + 3) / 4
    }
}

fn push_ref_event_edge_issue_if_missing(
    conn: &Connection,
    issues: &mut Vec<String>,
    event_id: &str,
    ref_name: &str,
    node_id: &str,
    direction: &str,
    role: &str,
    role_is_like: bool,
) -> AnfsResult<()> {
    let edge_count = count_event_edges(
        conn,
        event_id,
        direction,
        node_id,
        role,
        Some(ref_name),
        role_is_like,
    )?;
    if edge_count != 1 {
        issues.push(format!(
            "ref_event {event_id} for {ref_name} must have exactly one matching {direction} {role} edge for node {node_id}; found {edge_count}"
        ));
    }
    Ok(())
}

fn integrity_dir_prefix(path: &str) -> String {
    let normalized = path
        .split('/')
        .filter(|segment| !segment.is_empty() && *segment != ".")
        .collect::<Vec<_>>()
        .join("/");
    if normalized.is_empty() {
        normalized
    } else {
        format!("{normalized}/")
    }
}

fn count_event_edges(
    conn: &Connection,
    event_id: &str,
    direction: &str,
    node_id: &str,
    role: &str,
    logical_path: Option<&str>,
    role_is_like: bool,
) -> AnfsResult<i64> {
    let role_predicate = if role_is_like {
        "role LIKE ?4 ESCAPE '\\'"
    } else {
        "role = ?4"
    };
    let sql = format!(
        "
        SELECT COUNT(*)
        FROM event_edges
        WHERE event_id = ?1
          AND direction = ?2
          AND node_id = ?3
          AND {role_predicate}
          AND (
                (?5 IS NULL AND logical_path IS NULL)
                OR logical_path = ?5
              )
        "
    );
    let count = conn.query_row(
        &sql,
        params![event_id, direction, node_id, role, logical_path],
        |row| row.get(0),
    )?;
    Ok(count)
}

fn is_safe_materialized_cache_path(path: &str) -> bool {
    if path.is_empty() || path.contains('\\') {
        return false;
    }
    let path = Path::new(path);
    if path.is_absolute() {
        return false;
    }
    path.components()
        .all(|component| matches!(component, std::path::Component::Normal(_)))
}

fn validate_policy_expression_for_integrity(expression: &serde_json::Value) -> Result<(), String> {
    let object = expression
        .as_object()
        .ok_or_else(|| "expression must be an object".to_string())?;
    let operator_count = ["label", "all", "any", "not", "at_least"]
        .iter()
        .filter(|key| object.contains_key(**key))
        .count();
    if operator_count != 1 {
        return Err(
            "expression must contain exactly one of label, all, any, not, or at_least".to_string(),
        );
    }
    if let Some(label) = object.get("label") {
        let label = label
            .as_str()
            .ok_or_else(|| "label must be a string".to_string())?;
        if label.trim().is_empty() {
            return Err("label must not be empty".to_string());
        }
        if let Some(value) = object.get("value") {
            let value = value
                .as_str()
                .ok_or_else(|| "value must be a string".to_string())?;
            if value.trim().is_empty() || value == "*" {
                return Err("value must be non-empty and must not be '*'".to_string());
            }
        }
        return Ok(());
    }
    for key in ["all", "any"] {
        if let Some(children) = object.get(key) {
            let children = children
                .as_array()
                .ok_or_else(|| format!("{key} must be an array"))?;
            if children.is_empty() {
                return Err(format!("{key} must not be empty"));
            }
            for child in children {
                validate_policy_expression_for_integrity(child)?;
            }
            return Ok(());
        }
    }
    if let Some(child) = object.get("not") {
        validate_policy_expression_for_integrity(child)?;
        return Ok(());
    }
    if let Some(at_least) = object.get("at_least") {
        let at_least = at_least
            .as_object()
            .ok_or_else(|| "at_least must be an object".to_string())?;
        let count = at_least
            .get("count")
            .and_then(serde_json::Value::as_i64)
            .ok_or_else(|| "at_least.count must be an integer".to_string())?;
        let children = at_least
            .get("of")
            .and_then(serde_json::Value::as_array)
            .ok_or_else(|| "at_least.of must be an array".to_string())?;
        if children.is_empty() {
            return Err("at_least.of must not be empty".to_string());
        }
        if count < 1 || count as usize > children.len() {
            return Err(format!(
                "at_least.count must be between 1 and {}",
                children.len()
            ));
        }
        for child in children {
            validate_policy_expression_for_integrity(child)?;
        }
    }
    Ok(())
}
