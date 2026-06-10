use rusqlite::{params, Connection};

use crate::{purpose_hides_ref_node, AnfsError, AnfsResult, QueryRefRow, VisibilityPolicy};

pub(crate) fn collect_rows<T>(
    rows: rusqlite::MappedRows<'_, impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<T>>,
) -> AnfsResult<Vec<T>> {
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

pub(crate) fn query_refs(
    conn: &Connection,
    prefix: Option<&str>,
    text: Option<&str>,
    state: Option<&str>,
    agent_id: Option<&str>,
    run_id: Option<&str>,
    event_kind: Option<&str>,
    media_type: Option<&str>,
    created_after_ms: Option<i64>,
    created_before_ms: Option<i64>,
    policy: Option<&str>,
    decision: Option<&str>,
    reason_code: Option<&str>,
    policy_label_excludes: &[String],
    purpose: Option<&str>,
    limit: i64,
) -> AnfsResult<Vec<QueryRefRow>> {
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
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;

    let prefix_like = prefix.map(|value| format!("{}%", escape_like(value)));
    let text_query = text.map(fts_literal_query).transpose()?;
    let unrestricted = visibility.is_unrestricted_for(conn)?;
    let sql_limit = if unrestricted && purpose.is_none() {
        limit
    } else {
        1000
    };

    let mut stmt = conn.prepare(
        "
        SELECT
            r.ref_name,
            r.node_id,
            r.ref_kind,
            r.state,
            r.ref_version,
            n.kind,
            n.media_type,
            n.created_at,
            (
                SELECT es.seq
                FROM ref_events re
                JOIN event_sequence es ON es.event_id = re.event_id
                WHERE re.ref_name = r.ref_name
                ORDER BY es.seq DESC
                LIMIT 1
            ) AS last_event_seq,
            (
                SELECT e.kind
                FROM ref_events re
                JOIN events e ON e.event_id = re.event_id
                JOIN event_sequence es ON es.event_id = re.event_id
                WHERE re.ref_name = r.ref_name
                ORDER BY es.seq DESC
                LIMIT 1
            ) AS last_event_kind,
            CASE
                WHEN ?2 IS NULL THEN NULL
                ELSE (
                    SELECT snippet(node_fts, 1, '[', ']', '...', 12)
                    FROM node_fts
                    WHERE node_fts.node_id = r.node_id
                      AND node_fts MATCH ?2
                    LIMIT 1
                )
            END AS snippet
        FROM refs r
        JOIN nodes n ON n.node_id = r.node_id
        WHERE (?1 IS NULL OR r.ref_name LIKE ?1 ESCAPE '\\')
          AND (?2 IS NULL OR EXISTS (
              SELECT 1
              FROM node_fts
              WHERE node_fts.node_id = r.node_id
                AND node_fts MATCH ?2
          ))
          AND (?3 IS NULL OR r.state = ?3)
          AND (?4 IS NULL OR EXISTS (
              SELECT 1
              FROM event_edges ee
              JOIN events e ON e.event_id = ee.event_id
              WHERE ee.node_id = r.node_id
                AND e.agent_id = ?4
          ) OR EXISTS (
              SELECT 1
              FROM ref_events re
              JOIN events e ON e.event_id = re.event_id
              WHERE re.ref_name = r.ref_name
                AND e.agent_id = ?4
          ))
          AND (?5 IS NULL OR EXISTS (
              SELECT 1
              FROM event_edges ee
              JOIN events e ON e.event_id = ee.event_id
              WHERE ee.node_id = r.node_id
                AND e.run_id = ?5
          ) OR EXISTS (
              SELECT 1
              FROM ref_events re
              JOIN events e ON e.event_id = re.event_id
              WHERE re.ref_name = r.ref_name
                AND e.run_id = ?5
          ))
          AND (?6 IS NULL OR EXISTS (
              SELECT 1
              FROM event_edges ee
              JOIN events e ON e.event_id = ee.event_id
              WHERE ee.node_id = r.node_id
                AND e.kind = ?6
          ) OR EXISTS (
              SELECT 1
              FROM ref_events re
              JOIN events e ON e.event_id = re.event_id
              WHERE re.ref_name = r.ref_name
                AND e.kind = ?6
          ))
          AND (?7 IS NULL OR n.media_type = ?7)
          AND (?8 IS NULL OR n.created_at >= ?8)
          AND (?9 IS NULL OR n.created_at <= ?9)
          AND ((?10 IS NULL AND ?11 IS NULL AND ?12 IS NULL) OR EXISTS (
              SELECT 1
              FROM events pe
              WHERE pe.kind = 'policy_decision'
                AND json_extract(pe.payload_json, '$.target_ref') = r.ref_name
                AND (?10 IS NULL OR json_extract(pe.payload_json, '$.policy') = ?10)
                AND (?11 IS NULL OR json_extract(pe.payload_json, '$.decision') = ?11)
                AND (?12 IS NULL OR json_extract(pe.payload_json, '$.reason_code') = ?12)
          ))
        ORDER BY r.ref_name
        LIMIT ?13
        ",
    )?;
    let rows = stmt.query_map(
        params![
            prefix_like,
            text_query.as_deref(),
            state,
            agent_id,
            run_id,
            event_kind,
            media_type,
            created_after_ms,
            created_before_ms,
            policy,
            decision,
            reason_code,
            sql_limit,
        ],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, String>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, i64>(7)?,
                row.get::<_, Option<i64>>(8)?,
                row.get::<_, Option<String>>(9)?,
                row.get::<_, Option<String>>(10)?,
            ))
        },
    )?;
    let rows = collect_rows(rows)?;
    if unrestricted && purpose.is_none() {
        return Ok(rows);
    }

    let mut filtered = Vec::new();
    for row in rows {
        let ref_name = row.0.as_str();
        let node_id = row.1.as_str();
        if !unrestricted && visibility.ref_node_blocked(conn, ref_name, node_id)? {
            continue;
        }
        if let Some(purpose_value) = purpose {
            if purpose_hides_ref_node(conn, purpose_value, ref_name, node_id)? {
                continue;
            }
        }
        filtered.push(row);
        if filtered.len() >= limit as usize {
            break;
        }
    }
    Ok(filtered)
}

fn escape_like(value: &str) -> String {
    value
        .replace('\\', "\\\\")
        .replace('%', "\\%")
        .replace('_', "\\_")
}

pub(crate) fn fts_literal_query(value: &str) -> AnfsResult<String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "full-text query must not be empty".to_string(),
        ));
    }
    Ok(format!("\"{}\"", trimmed.replace('"', "\"\"")))
}
