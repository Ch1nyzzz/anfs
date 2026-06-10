use rusqlite::{params, Connection, OptionalExtension, Transaction};

use crate::{now_millis, AnfsError, AnfsResult, RefRecord};

pub(crate) enum RefWriteMode {
    WorkspaceDraft,
    PublishImmutable,
}

pub(crate) fn fetch_ref(conn: &Connection, ref_name: &str) -> AnfsResult<Option<RefRecord>> {
    conn.query_row(
        "SELECT node_id, ref_kind, state, ref_version FROM refs WHERE ref_name = ?1",
        params![ref_name],
        |row| {
            Ok(RefRecord {
                node_id: row.get(0)?,
                ref_kind: row.get(1)?,
                state: row.get(2)?,
                ref_version: row.get(3)?,
            })
        },
    )
    .optional()
    .map_err(AnfsError::from)
}

pub(crate) fn require_ref(conn: &Connection, ref_name: &str) -> AnfsResult<RefRecord> {
    fetch_ref(conn, ref_name)?.ok_or_else(|| AnfsError::RefNotFound(ref_name.to_string()))
}

pub(crate) fn insert_new_ref(
    tx: &Transaction<'_>,
    ref_name: &str,
    node_id: &str,
    ref_kind: &str,
    state: &str,
    event_id: &str,
) -> AnfsResult<()> {
    tx.execute(
        "INSERT INTO refs
         (ref_name, node_id, ref_kind, state, ref_version, updated_at)
         VALUES (?1, ?2, ?3, ?4, 1, ?5)",
        params![ref_name, node_id, ref_kind, state, now_millis()],
    )?;
    insert_ref_event(
        tx,
        ref_name,
        event_id,
        None,
        Some(node_id),
        None,
        Some(state),
    )?;
    Ok(())
}

pub(crate) fn upsert_ref(
    tx: &Transaction<'_>,
    ref_name: &str,
    node_id: &str,
    ref_kind: &str,
    state: &str,
    event_id: &str,
    mode: RefWriteMode,
) -> AnfsResult<()> {
    let old = fetch_ref(tx, ref_name)?;
    match (&old, mode) {
        (Some(old), RefWriteMode::WorkspaceDraft) => {
            validate_state_transition(&old.state, state)?;
            tx.execute(
                "UPDATE refs
                 SET node_id = ?1, ref_kind = ?2, state = ?3,
                     ref_version = ref_version + 1, updated_at = ?4
                 WHERE ref_name = ?5 AND ref_version = ?6",
                params![
                    node_id,
                    ref_kind,
                    state,
                    now_millis(),
                    ref_name,
                    old.ref_version
                ],
            )?;
            ensure_one_row_changed(tx, ref_name)?;
            insert_ref_event(
                tx,
                ref_name,
                event_id,
                Some(&old.node_id),
                Some(node_id),
                Some(&old.state),
                Some(state),
            )?;
        }
        (Some(old), RefWriteMode::PublishImmutable) => {
            if old.node_id != node_id {
                return Err(AnfsError::RefConflict(format!(
                    "published ref {ref_name} already points to {}",
                    old.node_id
                )));
            }
            if old.state != state {
                validate_state_transition(&old.state, state)?;
                update_ref_state(tx, ref_name, old, state, event_id)?;
            }
        }
        (None, _) => {
            tx.execute(
                "INSERT INTO refs
                 (ref_name, node_id, ref_kind, state, ref_version, updated_at)
                 VALUES (?1, ?2, ?3, ?4, 1, ?5)",
                params![ref_name, node_id, ref_kind, state, now_millis()],
            )?;
            insert_ref_event(
                tx,
                ref_name,
                event_id,
                None,
                Some(node_id),
                None,
                Some(state),
            )?;
        }
    }
    Ok(())
}

pub(crate) fn update_ref_state(
    tx: &Transaction<'_>,
    ref_name: &str,
    old: &RefRecord,
    new_state: &str,
    event_id: &str,
) -> AnfsResult<()> {
    validate_state_transition(&old.state, new_state)?;
    tx.execute(
        "UPDATE refs
         SET state = ?1, ref_version = ref_version + 1, updated_at = ?2
         WHERE ref_name = ?3 AND ref_version = ?4",
        params![new_state, now_millis(), ref_name, old.ref_version],
    )?;
    ensure_one_row_changed(tx, ref_name)?;
    insert_ref_event(
        tx,
        ref_name,
        event_id,
        Some(&old.node_id),
        Some(&old.node_id),
        Some(&old.state),
        Some(new_state),
    )?;
    Ok(())
}

pub(crate) fn update_ref_node(
    tx: &Transaction<'_>,
    ref_name: &str,
    old: &RefRecord,
    new_node_id: &str,
    new_state: &str,
    event_id: &str,
) -> AnfsResult<()> {
    validate_state_transition(&old.state, new_state)?;
    tx.execute(
        "UPDATE refs
         SET node_id = ?1, state = ?2, ref_version = ref_version + 1, updated_at = ?3
         WHERE ref_name = ?4 AND ref_version = ?5",
        params![
            new_node_id,
            new_state,
            now_millis(),
            ref_name,
            old.ref_version
        ],
    )?;
    ensure_one_row_changed(tx, ref_name)?;
    insert_ref_event(
        tx,
        ref_name,
        event_id,
        Some(&old.node_id),
        Some(new_node_id),
        Some(&old.state),
        Some(new_state),
    )?;
    Ok(())
}

fn ensure_one_row_changed(tx: &Transaction<'_>, ref_name: &str) -> AnfsResult<()> {
    if tx.changes() != 1 {
        return Err(AnfsError::RefConflict(format!(
            "ref {ref_name} changed concurrently"
        )));
    }
    Ok(())
}

pub(crate) fn require_expected_ref_version(
    ref_name: &str,
    current: &RefRecord,
    expected_version: Option<i64>,
) -> AnfsResult<()> {
    if let Some(expected_version) = expected_version {
        if current.ref_version != expected_version {
            return Err(AnfsError::RefConflict(format!(
                "ref {ref_name} version mismatch: expected {expected_version}, actual {}",
                current.ref_version
            )));
        }
    }
    Ok(())
}

pub(crate) fn target_has_producer_agent(
    conn: &Connection,
    target_node_id: &str,
    agent_id: &str,
) -> AnfsResult<bool> {
    let producer: Option<i64> = conn
        .query_row(
            "
            SELECT 1
            FROM event_edges ee
            JOIN events e ON e.event_id = ee.event_id
            WHERE ee.node_id = ?1
              AND ee.direction = 'output'
              AND e.agent_id = ?2
              AND e.kind IN (
                  'write',
                  'publish',
                  'publish_manifest',
                  'snapshot_namespace',
                  'import_event_bundle'
              )
            LIMIT 1
            ",
            params![target_node_id, agent_id],
            |row| row.get(0),
        )
        .optional()?;
    Ok(producer.is_some())
}

pub(crate) fn insert_ref_event(
    tx: &Transaction<'_>,
    ref_name: &str,
    event_id: &str,
    old_node_id: Option<&str>,
    new_node_id: Option<&str>,
    old_state: Option<&str>,
    new_state: Option<&str>,
) -> AnfsResult<()> {
    tx.execute(
        "INSERT INTO ref_events
         (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            ref_name,
            event_id,
            old_node_id,
            new_node_id,
            old_state,
            new_state
        ],
    )?;
    Ok(())
}

pub(crate) fn require_state(actual: &str, expected: &str, op: &str) -> AnfsResult<()> {
    if actual != expected {
        return Err(AnfsError::InvalidStateTransition(format!(
            "cannot {op} ref in state {actual}; expected {expected}"
        )));
    }
    Ok(())
}

pub(crate) fn validate_state_transition(old_state: &str, new_state: &str) -> AnfsResult<()> {
    if old_state == new_state {
        return Ok(());
    }

    let allowed = matches!(
        (old_state, new_state),
        ("draft", "draft")
            | ("draft", "published")
            | ("draft", "deleted")
            | ("draft", "archived")
            | ("published", "approved")
            | ("published", "rejected")
            | ("published", "archived")
            | ("approved", "archived")
            | ("rejected", "archived")
    );
    if !allowed {
        return Err(AnfsError::InvalidStateTransition(format!(
            "invalid ref state transition {old_state} -> {new_state}"
        )));
    }
    Ok(())
}

pub(crate) fn is_known_state(state: &str) -> bool {
    matches!(
        state,
        "draft" | "published" | "approved" | "rejected" | "archived" | "deleted"
    )
}
