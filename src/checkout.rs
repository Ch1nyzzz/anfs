use rusqlite::{params, Connection, Transaction};
use std::path::Path;

use crate::{
    checkout_logical_path, fetch_ref, insert_edge, insert_ref_event, manifest_children, now_millis,
    workspace_ref_name, AnfsError, AnfsResult,
};

pub(crate) fn checkout_base_refs(
    tx: &Transaction<'_>,
    objects_dir: &Path,
    base: &str,
    workspace: &str,
    event_id: &str,
) -> AnfsResult<()> {
    let base_prefix = base.trim_end_matches('/');
    let entries = checkout_entries_for_base(tx, objects_dir, base_prefix)?;

    for (copied, (source_ref, logical_path, node_id)) in entries.iter().enumerate() {
        let target_ref = workspace_ref_name(workspace, logical_path);
        if fetch_ref(tx, &target_ref)?.is_some() {
            return Err(AnfsError::RefConflict(format!(
                "checkout target ref {target_ref} already exists"
            )));
        }

        tx.execute(
            "INSERT INTO refs
             (ref_name, node_id, ref_kind, state, ref_version, updated_at)
             VALUES (?1, ?2, 'workspace', 'draft', 1, ?3)",
            params![target_ref, node_id, now_millis()],
        )?;
        insert_ref_event(
            tx,
            &target_ref,
            event_id,
            None,
            Some(node_id),
            None,
            Some("draft"),
        )?;
        tx.execute(
            "INSERT INTO workspace_base_refs
             (workspace_id, base_ref, logical_path, source_ref, node_id, checkout_event_id, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
            params![
                workspace,
                base_prefix,
                logical_path,
                source_ref,
                node_id,
                event_id,
                now_millis()
            ],
        )?;

        let input_role = format!("base_source:{copied}");
        let output_role = format!("workspace_view:{copied}");
        insert_edge(
            tx,
            event_id,
            "input",
            node_id,
            &input_role,
            Some(source_ref),
        )?;
        insert_edge(
            tx,
            event_id,
            "output",
            node_id,
            &output_role,
            Some(&target_ref),
        )?;
    }

    Ok(())
}

pub(crate) fn checkout_entries_for_base(
    conn: &Connection,
    objects_dir: &Path,
    base_prefix: &str,
) -> AnfsResult<Vec<(String, String, String)>> {
    if let Some(base_ref) = fetch_ref(conn, base_prefix)? {
        if base_ref.state == "published" || base_ref.state == "approved" {
            let children = manifest_children(conn, objects_dir, &base_ref.node_id)?;
            if !children.is_empty() {
                return Ok(children
                    .into_iter()
                    .map(|(path, node_id)| (base_prefix.to_string(), path, node_id))
                    .collect());
            }
        }
    }

    let like_pattern = format!("{base_prefix}/%");
    let mut stmt = conn.prepare(
        "SELECT ref_name, node_id
         FROM refs
         WHERE (ref_name = ?1 OR ref_name LIKE ?2)
           AND state IN ('published', 'approved')
         ORDER BY ref_name",
    )?;
    let rows = stmt.query_map(params![base_prefix, like_pattern], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut entries = Vec::new();
    for row in rows {
        let (source_ref, node_id) = row?;
        let logical_path = checkout_logical_path(base_prefix, &source_ref);
        entries.push((source_ref, logical_path, node_id));
    }
    if entries.is_empty() {
        return Err(AnfsError::RefNotFound(format!(
            "checkout base {base_prefix} has no published or approved refs"
        )));
    }
    Ok(entries)
}
