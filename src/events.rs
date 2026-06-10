use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::json;

use crate::{new_event_id, now_millis, AnfsResult};

pub(crate) fn insert_event(
    tx: &Transaction<'_>,
    event_id: &str,
    kind: &str,
    agent_id: Option<&str>,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    workspace_id: Option<&str>,
    payload_json: Option<&str>,
) -> AnfsResult<()> {
    tx.execute(
        "INSERT INTO events
         (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
        params![
            event_id,
            kind,
            agent_id,
            run_id,
            tool_call_id,
            workspace_id,
            payload_json,
            now_millis()
        ],
    )?;
    insert_event_sequence_if_missing(tx, event_id)?;
    Ok(())
}

pub(crate) fn insert_event_sequence_if_missing(
    conn: &Connection,
    event_id: &str,
) -> AnfsResult<()> {
    let existing: Option<i64> = conn
        .query_row(
            "SELECT seq FROM event_sequence WHERE event_id = ?1",
            params![event_id],
            |row| row.get(0),
        )
        .optional()?;
    if existing.is_some() {
        return Ok(());
    }
    let seq = allocate_event_seq(conn)?;
    conn.execute(
        "INSERT INTO event_sequence (event_id, seq) VALUES (?1, ?2)",
        params![event_id, seq],
    )?;
    Ok(())
}

pub(crate) fn seed_event_sequence_allocator(conn: &Connection) -> AnfsResult<()> {
    let allocation_count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM event_sequence_allocations",
        [],
        |row| row.get(0),
    )?;
    if allocation_count == 0 {
        let max_seq: Option<i64> =
            conn.query_row("SELECT MAX(seq) FROM event_sequence", [], |row| row.get(0))?;
        if let Some(max_seq) = max_seq {
            if max_seq > 0 {
                conn.execute(
                    "INSERT INTO event_sequence_allocations (seq) VALUES (?1)",
                    params![max_seq],
                )?;
            }
        }
    }
    Ok(())
}

pub(crate) fn allocate_event_seq(conn: &Connection) -> AnfsResult<i64> {
    conn.execute("INSERT INTO event_sequence_allocations DEFAULT VALUES", [])?;
    Ok(conn.last_insert_rowid())
}

pub(crate) fn backfill_event_sequence(conn: &Connection) -> AnfsResult<()> {
    let mut stmt = conn.prepare(
        "SELECT e.event_id
         FROM events e
         WHERE NOT EXISTS (
             SELECT 1 FROM event_sequence es WHERE es.event_id = e.event_id
         )
         ORDER BY e.rowid",
    )?;
    let rows = stmt.query_map([], |row| row.get::<_, String>(0))?;
    let mut missing = Vec::new();
    for row in rows {
        missing.push(row?);
    }
    drop(stmt);

    for event_id in missing {
        insert_event_sequence_if_missing(conn, &event_id)?;
    }
    Ok(())
}

pub(crate) fn insert_policy_decision_event(
    tx: &Transaction<'_>,
    policy: &str,
    decision: &str,
    reason_code: &str,
    reason: &str,
    agent_id: Option<&str>,
    target_ref: Option<&str>,
    target_node_id: &str,
    evidence_nodes: &[(String, String)],
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<String> {
    let event_id = new_event_id();
    let evidence_refs: Vec<&str> = evidence_nodes
        .iter()
        .map(|(evidence_ref, _)| evidence_ref.as_str())
        .collect();
    let payload = json!({
        "policy": policy,
        "decision": decision,
        "reason_code": reason_code,
        "reason": reason,
        "target_ref": target_ref,
        "target_node_id": target_node_id,
        "evidence_refs": evidence_refs,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "policy_decision",
        agent_id,
        run_id,
        tool_call_id,
        None,
        Some(&payload),
    )?;
    insert_edge(
        tx,
        &event_id,
        "input",
        target_node_id,
        "policy_target",
        target_ref,
    )?;
    for (idx, (evidence_ref, evidence_node_id)) in evidence_nodes.iter().enumerate() {
        let role = format!("policy_evidence:{idx}");
        insert_edge(
            tx,
            &event_id,
            "input",
            evidence_node_id,
            &role,
            Some(evidence_ref),
        )?;
    }
    Ok(event_id)
}

pub(crate) fn insert_merge_policy_decision_event(
    tx: &Transaction<'_>,
    decision: &str,
    reason_code: &str,
    agent_id: Option<&str>,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    base: &str,
    workspace: &str,
    conflicts: &[(String, Option<String>, Option<String>, Option<String>)],
    changes: &[(String, String, Option<String>, Option<String>)],
) -> AnfsResult<String> {
    let event_id = new_event_id();
    let conflict_payload: Vec<_> = conflicts
        .iter()
        .map(
            |(path, checkout_node_id, current_base_node_id, workspace_node_id)| {
                json!({
                    "path": path,
                    "checkout_node_id": checkout_node_id,
                    "current_base_node_id": current_base_node_id,
                    "workspace_node_id": workspace_node_id,
                })
            },
        )
        .collect();
    let change_payload: Vec<_> = changes
        .iter()
        .filter(|(_path, status, _base_node_id, _workspace_node_id)| status != "unchanged")
        .map(|(path, status, base_node_id, workspace_node_id)| {
            json!({
                "path": path,
                "status": status,
                "base_node_id": base_node_id,
                "workspace_node_id": workspace_node_id,
            })
        })
        .collect();
    let payload = json!({
        "policy": "workspace_merge",
        "decision": decision,
        "reason_code": reason_code,
        "reason": reason_code,
        "target_ref": base,
        "base": base,
        "workspace": workspace,
        "conflict_count": conflicts.len(),
        "change_count": change_payload.len(),
        "conflicts": conflict_payload,
        "changes": change_payload,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "policy_decision",
        agent_id,
        run_id,
        tool_call_id,
        Some(workspace),
        Some(&payload),
    )?;

    for (idx, (path, checkout_node_id, current_base_node_id, workspace_node_id)) in
        conflicts.iter().enumerate()
    {
        if let Some(node_id) = checkout_node_id {
            let role = format!("merge_conflict:{idx}:checkout");
            insert_edge(tx, &event_id, "input", node_id, &role, Some(path))?;
        }
        if let Some(node_id) = current_base_node_id {
            let role = format!("merge_conflict:{idx}:current_base");
            insert_edge(tx, &event_id, "input", node_id, &role, Some(path))?;
        }
        if let Some(node_id) = workspace_node_id {
            let role = format!("merge_conflict:{idx}:workspace");
            insert_edge(tx, &event_id, "input", node_id, &role, Some(path))?;
        }
    }

    for (idx, (path, status, base_node_id, workspace_node_id)) in changes.iter().enumerate() {
        if status == "unchanged" {
            continue;
        }
        if let Some(node_id) = base_node_id {
            let role = format!("merge_change:{idx}:base");
            insert_edge(tx, &event_id, "input", node_id, &role, Some(path))?;
        }
        if let Some(node_id) = workspace_node_id {
            let role = format!("merge_change:{idx}:workspace");
            insert_edge(tx, &event_id, "input", node_id, &role, Some(path))?;
        }
    }

    Ok(event_id)
}

pub(crate) fn insert_edge(
    tx: &Transaction<'_>,
    event_id: &str,
    direction: &str,
    node_id: &str,
    role: &str,
    logical_path: Option<&str>,
) -> AnfsResult<()> {
    tx.execute(
        "INSERT INTO event_edges (event_id, direction, node_id, role, logical_path)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        params![event_id, direction, node_id, role, logical_path],
    )?;
    Ok(())
}

pub(crate) fn policy_decisions(
    conn: &Connection,
    target_ref: Option<&str>,
    policy: Option<&str>,
    decision: Option<&str>,
    reason_code: Option<&str>,
) -> AnfsResult<Vec<(String, String)>> {
    let mut stmt = conn.prepare(
        "SELECT e.event_id, COALESCE(e.payload_json, '')
         FROM events e
         JOIN event_sequence es ON es.event_id = e.event_id
         WHERE e.kind = 'policy_decision'
           AND (?1 IS NULL OR json_extract(e.payload_json, '$.target_ref') = ?1)
           AND (?2 IS NULL OR json_extract(e.payload_json, '$.policy') = ?2)
           AND (?3 IS NULL OR json_extract(e.payload_json, '$.decision') = ?3)
           AND (?4 IS NULL OR json_extract(e.payload_json, '$.reason_code') = ?4)
         ORDER BY es.seq",
    )?;
    let rows = stmt.query_map(params![target_ref, policy, decision, reason_code], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut decisions = Vec::new();
    for row in rows {
        decisions.push(row?);
    }
    Ok(decisions)
}
