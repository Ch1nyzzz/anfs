use rusqlite::{params, Connection};
use std::collections::HashSet;
use std::path::Path;

use crate::{
    ensure_node_exists, manifest_children, query::collect_rows, AnfsError, AnfsResult,
    LineageGraphRow,
};

pub(crate) fn lineage_nodes(
    conn: &Connection,
    node_id: &str,
    direction: &str,
) -> AnfsResult<Vec<String>> {
    ensure_node_exists(conn, node_id)?;

    let sql = match direction {
        "ancestors" => {
            "
            WITH RECURSIVE lineage(node_id) AS (
                SELECT ?1
                UNION
                SELECT e_in.node_id
                FROM lineage l
                JOIN event_edges e_out
                    ON e_out.node_id = l.node_id
                   AND e_out.direction = 'output'
                JOIN event_edges e_in
                    ON e_in.event_id = e_out.event_id
                   AND e_in.direction = 'input'
            )
            SELECT node_id FROM lineage ORDER BY node_id
            "
        }
        "descendants" => {
            "
            WITH RECURSIVE lineage(node_id) AS (
                SELECT ?1
                UNION
                SELECT e_out.node_id
                FROM lineage l
                JOIN event_edges e_in
                    ON e_in.node_id = l.node_id
                   AND e_in.direction = 'input'
                JOIN event_edges e_out
                    ON e_out.event_id = e_in.event_id
                   AND e_out.direction = 'output'
            )
            SELECT node_id FROM lineage ORDER BY node_id
            "
        }
        _ => {
            return Err(AnfsError::PolicyDenied(
                "direction must be 'ancestors' or 'descendants'".to_string(),
            ))
        }
    };

    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(params![node_id], |row| row.get::<_, String>(0))?;
    let mut nodes = Vec::new();
    for row in rows {
        nodes.push(row?);
    }
    Ok(nodes)
}

pub(crate) fn lineage_graph(
    conn: &Connection,
    node_id: &str,
    direction: &str,
) -> AnfsResult<Vec<LineageGraphRow>> {
    ensure_node_exists(conn, node_id)?;

    let sql = match direction {
        "ancestors" => {
            "
            WITH RECURSIVE lineage(node_id) AS (
                SELECT ?1
                UNION
                SELECT e_in.node_id
                FROM lineage l
                JOIN event_edges e_out
                    ON e_out.node_id = l.node_id
                   AND e_out.direction = 'output'
                JOIN event_edges e_in
                    ON e_in.event_id = e_out.event_id
                   AND e_in.direction = 'input'
            )
            SELECT DISTINCT
                es.seq,
                e.event_id,
                e.kind,
                e_out.node_id AS from_node_id,
                e_out.role AS from_role,
                e_out.logical_path AS from_path,
                e_in.node_id AS to_node_id,
                e_in.role AS to_role,
                e_in.logical_path AS to_path
            FROM lineage l
            JOIN event_edges e_out
                ON e_out.node_id = l.node_id
               AND e_out.direction = 'output'
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
            JOIN events e ON e.event_id = e_out.event_id
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e_in.node_id IN (SELECT node_id FROM lineage)
            ORDER BY es.seq, e.event_id, from_node_id, to_node_id, from_role, to_role
            "
        }
        "descendants" => {
            "
            WITH RECURSIVE lineage(node_id) AS (
                SELECT ?1
                UNION
                SELECT e_out.node_id
                FROM lineage l
                JOIN event_edges e_in
                    ON e_in.node_id = l.node_id
                   AND e_in.direction = 'input'
                JOIN event_edges e_out
                    ON e_out.event_id = e_in.event_id
                   AND e_out.direction = 'output'
            )
            SELECT DISTINCT
                es.seq,
                e.event_id,
                e.kind,
                e_in.node_id AS from_node_id,
                e_in.role AS from_role,
                e_in.logical_path AS from_path,
                e_out.node_id AS to_node_id,
                e_out.role AS to_role,
                e_out.logical_path AS to_path
            FROM lineage l
            JOIN event_edges e_in
                ON e_in.node_id = l.node_id
               AND e_in.direction = 'input'
            JOIN event_edges e_out
                ON e_out.event_id = e_in.event_id
               AND e_out.direction = 'output'
            JOIN events e ON e.event_id = e_in.event_id
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e_out.node_id IN (SELECT node_id FROM lineage)
            ORDER BY es.seq, e.event_id, from_node_id, to_node_id, from_role, to_role
            "
        }
        _ => {
            return Err(AnfsError::PolicyDenied(
                "direction must be 'ancestors' or 'descendants'".to_string(),
            ))
        }
    };

    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map(params![node_id], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, String>(4)?,
            row.get::<_, Option<String>>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, String>(7)?,
            row.get::<_, Option<String>>(8)?,
        ))
    })?;
    collect_rows(rows)
}

pub(crate) fn lineage_ancestors(
    conn: &Connection,
    evidence_node_id: &str,
) -> AnfsResult<Vec<String>> {
    lineage_nodes(conn, evidence_node_id, "ancestors")
}

pub(crate) fn lineage_evidence_covers_target(
    conn: &Connection,
    objects_dir: &Path,
    target_node_id: &str,
    evidence_ancestors: &HashSet<String>,
) -> AnfsResult<bool> {
    if evidence_ancestors.contains(target_node_id) {
        return Ok(true);
    }

    let children = manifest_children(conn, objects_dir, target_node_id)?;
    if children.is_empty() {
        return Ok(false);
    }

    Ok(children
        .iter()
        .all(|(_, child_node_id)| evidence_ancestors.contains(child_node_id)))
}
