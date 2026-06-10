use rusqlite::{params, Connection, OptionalExtension};

use crate::{
    ensure_node_exists, query::collect_rows, AnfsError, AnfsResult, EventListRow, EventRecord,
    RefHistoryRow,
};

pub(crate) fn ref_history(conn: &Connection, ref_name: &str) -> AnfsResult<Vec<RefHistoryRow>> {
    let ref_exists: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM refs WHERE ref_name = ?1 LIMIT 1",
            params![ref_name],
            |row| row.get(0),
        )
        .optional()?;
    if ref_exists.is_none() {
        return Err(AnfsError::RefNotFound(ref_name.to_string()));
    }

    let mut stmt = conn.prepare(
        "
        SELECT
            re.ref_name,
            re.event_id,
            e.kind,
            e.agent_id,
            re.old_node_id,
            re.new_node_id,
            re.old_state,
            re.new_state,
            e.created_at
        FROM ref_events re
        JOIN events e ON e.event_id = re.event_id
        JOIN event_sequence es ON es.event_id = e.event_id
        WHERE re.ref_name = ?1
        ORDER BY es.seq, re.rowid
        ",
    )?;
    let rows = stmt.query_map(params![ref_name], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
            row.get::<_, Option<String>>(6)?,
            row.get::<_, Option<String>>(7)?,
            row.get::<_, i64>(8)?,
        ))
    })?;

    let mut history = Vec::new();
    for row in rows {
        history.push(row?);
    }
    Ok(history)
}

pub(crate) fn event_record(conn: &Connection, event_id: &str) -> AnfsResult<EventRecord> {
    let event: Option<(
        String,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        i64,
    )> = conn
        .query_row(
            "
            SELECT event_id, kind, agent_id, run_id, tool_call_id,
                   workspace_id, payload_json, created_at
            FROM events
            WHERE event_id = ?1
            ",
            params![event_id],
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
        .optional()?;

    let (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at) =
        event.ok_or_else(|| AnfsError::EventNotFound(event_id.to_string()))?;

    let mut stmt = conn.prepare(
        "
        SELECT direction, node_id, role, logical_path
        FROM event_edges
        WHERE event_id = ?1
        ORDER BY direction, role, node_id
        ",
    )?;
    let rows = stmt.query_map(params![event_id], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
        ))
    })?;
    let mut edges = Vec::new();
    for row in rows {
        edges.push(row?);
    }

    Ok((
        event_id,
        kind,
        agent_id,
        run_id,
        tool_call_id,
        workspace_id,
        payload_json,
        created_at,
        edges,
    ))
}

pub(crate) fn event_list(
    conn: &Connection,
    after_seq: i64,
    limit: i64,
    kind: Option<&str>,
    agent_id: Option<&str>,
    workspace_id: Option<&str>,
    run_id: Option<&str>,
    created_after_ms: Option<i64>,
    created_before_ms: Option<i64>,
    payload_contains: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<Vec<EventListRow>> {
    if after_seq < 0 {
        return Err(AnfsError::PolicyDenied(
            "after_seq must be non-negative".to_string(),
        ));
    }
    if limit <= 0 || limit > 1000 {
        return Err(AnfsError::PolicyDenied(
            "limit must be between 1 and 1000".to_string(),
        ));
    }
    if let (Some(after), Some(before)) = (created_after_ms, created_before_ms) {
        if after > before {
            return Err(AnfsError::PolicyDenied(
                "created_after_ms must be <= created_before_ms".to_string(),
            ));
        }
    }
    let payload_like = payload_contains.map(|needle| format!("%{}%", escape_like(needle)));

    let mut stmt = conn.prepare(
        "
        SELECT
            es.seq,
            e.event_id,
            e.kind,
            e.agent_id,
            e.workspace_id,
            e.created_at,
            COALESCE(SUM(CASE WHEN ee.direction = 'input' THEN 1 ELSE 0 END), 0) AS input_count,
            COALESCE(SUM(CASE WHEN ee.direction = 'output' THEN 1 ELSE 0 END), 0) AS output_count
        FROM event_sequence es
        JOIN events e ON e.event_id = es.event_id
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE es.seq > ?1
          AND (?2 IS NULL OR e.kind = ?2)
          AND (?3 IS NULL OR e.agent_id = ?3)
          AND (?4 IS NULL OR e.workspace_id = ?4)
          AND (?5 IS NULL OR e.run_id = ?5)
          AND (?6 IS NULL OR e.created_at >= ?6)
          AND (?7 IS NULL OR e.created_at <= ?7)
          AND (?8 IS NULL OR e.payload_json LIKE ?8 ESCAPE '\\')
          AND (?9 IS NULL OR e.tool_call_id = ?9)
        GROUP BY es.seq, e.event_id, e.kind, e.agent_id, e.workspace_id, e.created_at
        ORDER BY es.seq
        LIMIT ?10
        ",
    )?;
    let rows = stmt.query_map(
        params![
            after_seq,
            kind,
            agent_id,
            workspace_id,
            run_id,
            created_after_ms,
            created_before_ms,
            payload_like,
            tool_call_id,
            limit
        ],
        |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, i64>(5)?,
                row.get::<_, i64>(6)?,
                row.get::<_, i64>(7)?,
            ))
        },
    )?;
    collect_rows(rows)
}

pub(crate) fn node_event_list(
    conn: &Connection,
    node_id: &str,
    direction: Option<&str>,
    role: Option<&str>,
    after_seq: i64,
    limit: i64,
) -> AnfsResult<Vec<EventListRow>> {
    ensure_node_exists(conn, node_id)?;
    if after_seq < 0 {
        return Err(AnfsError::PolicyDenied(
            "after_seq must be non-negative".to_string(),
        ));
    }
    if limit <= 0 || limit > 1000 {
        return Err(AnfsError::PolicyDenied(
            "limit must be between 1 and 1000".to_string(),
        ));
    }
    if let Some(direction) = direction {
        if direction != "input" && direction != "output" {
            return Err(AnfsError::PolicyDenied(
                "direction must be 'input' or 'output'".to_string(),
            ));
        }
    }

    let mut stmt = conn.prepare(
        "
        SELECT
            es.seq,
            e.event_id,
            e.kind,
            e.agent_id,
            e.workspace_id,
            e.created_at,
            COALESCE(SUM(CASE WHEN ee.direction = 'input' THEN 1 ELSE 0 END), 0) AS input_count,
            COALESCE(SUM(CASE WHEN ee.direction = 'output' THEN 1 ELSE 0 END), 0) AS output_count
        FROM event_sequence es
        JOIN events e ON e.event_id = es.event_id
        LEFT JOIN event_edges ee ON ee.event_id = e.event_id
        WHERE es.seq > ?1
          AND EXISTS (
              SELECT 1
              FROM event_edges matched
              WHERE matched.event_id = e.event_id
                AND matched.node_id = ?2
                AND (?3 IS NULL OR matched.direction = ?3)
                AND (?4 IS NULL OR matched.role = ?4)
          )
        GROUP BY es.seq, e.event_id, e.kind, e.agent_id, e.workspace_id, e.created_at
        ORDER BY es.seq
        LIMIT ?5
        ",
    )?;
    let rows = stmt.query_map(params![after_seq, node_id, direction, role, limit], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
            row.get::<_, i64>(6)?,
            row.get::<_, i64>(7)?,
        ))
    })?;
    collect_rows(rows)
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}
