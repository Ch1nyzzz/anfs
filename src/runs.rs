use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::json;

use crate::{
    insert_event, new_event_id, now_millis, query::collect_rows, AnfsError, AnfsResult, RunRow,
};

pub(crate) fn start_run(
    tx: &Transaction<'_>,
    run_id: &str,
    agent_id: Option<&str>,
    workspace_id: Option<&str>,
    metadata_json: Option<&str>,
) -> AnfsResult<()> {
    if fetch_run(tx, run_id)?.is_some() {
        return Err(AnfsError::RefConflict(format!(
            "run {run_id} already exists"
        )));
    }
    let event_id = new_event_id();
    let payload = json!({
        "run_id": run_id,
        "state": "active",
        "metadata_json": metadata_json,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "run_start",
        agent_id,
        Some(run_id),
        None,
        workspace_id,
        Some(&payload),
    )?;
    let now = now_millis();
    tx.execute(
        "INSERT INTO runs
         (run_id, agent_id, workspace_id, state, metadata_json, created_at, updated_at, ended_at)
         VALUES (?1, ?2, ?3, 'active', ?4, ?5, ?5, NULL)",
        params![run_id, agent_id, workspace_id, metadata_json, now],
    )?;
    insert_run_event(tx, run_id, &event_id, None, "active", None, metadata_json)?;
    Ok(())
}

pub(crate) fn finish_run(
    tx: &Transaction<'_>,
    run_id: &str,
    state: &str,
    metadata_json: Option<&str>,
) -> AnfsResult<()> {
    if !is_terminal_run_state(state) {
        return Err(AnfsError::InvalidStateTransition(format!(
            "invalid terminal run state {state}"
        )));
    }
    let current =
        fetch_run(tx, run_id)?.ok_or_else(|| AnfsError::RunNotFound(run_id.to_string()))?;
    if current.3 != "active" {
        return Err(AnfsError::InvalidStateTransition(format!(
            "cannot finish run {run_id} from state {}",
            current.3
        )));
    }
    let new_metadata = metadata_json.or(current.4.as_deref());
    let event_id = new_event_id();
    let payload = json!({
        "run_id": run_id,
        "state": state,
        "metadata_json": new_metadata,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "run_finish",
        current.1.as_deref(),
        Some(run_id),
        None,
        current.2.as_deref(),
        Some(&payload),
    )?;
    let now = now_millis();
    tx.execute(
        "UPDATE runs
         SET state = ?1, metadata_json = ?2, updated_at = ?3, ended_at = ?3
         WHERE run_id = ?4 AND state = 'active'",
        params![state, new_metadata, now, run_id],
    )?;
    ensure_one_run_changed(tx, run_id)?;
    insert_run_event(
        tx,
        run_id,
        &event_id,
        Some(&current.3),
        state,
        current.4.as_deref(),
        new_metadata,
    )?;
    Ok(())
}

pub(crate) fn fetch_run(conn: &Connection, run_id: &str) -> AnfsResult<Option<RunRow>> {
    conn.query_row(
        "SELECT run_id, agent_id, workspace_id, state, metadata_json,
                created_at, updated_at, ended_at
         FROM runs
         WHERE run_id = ?1",
        params![run_id],
        |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
                row.get(6)?,
                row.get(7)?,
            ))
        },
    )
    .optional()
    .map_err(AnfsError::from)
}

pub(crate) fn runs(
    conn: &Connection,
    state: Option<&str>,
    agent_id: Option<&str>,
    workspace_id: Option<&str>,
) -> AnfsResult<Vec<RunRow>> {
    let mut stmt = conn.prepare(
        "SELECT run_id, agent_id, workspace_id, state, metadata_json,
                created_at, updated_at, ended_at
         FROM runs
         WHERE (?1 IS NULL OR state = ?1)
           AND (?2 IS NULL OR agent_id = ?2)
           AND (?3 IS NULL OR workspace_id = ?3)
         ORDER BY created_at, run_id",
    )?;
    let rows = stmt.query_map(params![state, agent_id, workspace_id], |row| {
        Ok((
            row.get(0)?,
            row.get(1)?,
            row.get(2)?,
            row.get(3)?,
            row.get(4)?,
            row.get(5)?,
            row.get(6)?,
            row.get(7)?,
        ))
    })?;
    collect_rows(rows)
}

pub(crate) fn insert_run_event(
    tx: &Transaction<'_>,
    run_id: &str,
    event_id: &str,
    old_state: Option<&str>,
    new_state: &str,
    old_metadata_json: Option<&str>,
    new_metadata_json: Option<&str>,
) -> AnfsResult<()> {
    tx.execute(
        "INSERT INTO run_events
         (run_id, event_id, old_state, new_state, old_metadata_json, new_metadata_json)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            run_id,
            event_id,
            old_state,
            new_state,
            old_metadata_json,
            new_metadata_json
        ],
    )?;
    Ok(())
}

pub(crate) fn ensure_one_run_changed(tx: &Transaction<'_>, run_id: &str) -> AnfsResult<()> {
    if tx.changes() != 1 {
        return Err(AnfsError::RefConflict(format!(
            "run {run_id} changed concurrently"
        )));
    }
    Ok(())
}

pub(crate) fn is_terminal_run_state(state: &str) -> bool {
    matches!(state, "succeeded" | "failed" | "cancelled")
}

pub(crate) fn is_known_run_state(state: &str) -> bool {
    state == "active" || is_terminal_run_state(state)
}
