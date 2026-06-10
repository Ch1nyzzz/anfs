use rusqlite::{params, Transaction};

use crate::{
    fetch_ref, insert_edge, insert_ref_event, now_millis, workspace_logical_path,
    workspace_ref_name, AnfsError, AnfsResult,
};

pub(crate) fn fork_workspace_refs(
    tx: &Transaction<'_>,
    source_workspace: &str,
    target_workspace: &str,
    event_id: &str,
) -> AnfsResult<Vec<(String, String, String)>> {
    if source_workspace == target_workspace {
        return Err(AnfsError::PolicyDenied(
            "fork source and target workspaces must differ".to_string(),
        ));
    }
    let source_prefix = format!("{}/%", source_workspace.trim_end_matches('/'));
    let mut stmt = tx.prepare(
        "
        SELECT ref_name, node_id
        FROM refs
        WHERE ref_name LIKE ?1
          AND state = 'draft'
        ORDER BY ref_name
        ",
    )?;
    let rows = stmt.query_map(params![source_prefix], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut entries = Vec::new();
    for row in rows {
        let (source_ref, node_id) = row?;
        let logical_path =
            workspace_logical_path(source_workspace, &source_ref).ok_or_else(|| {
                AnfsError::StorageCorruption(format!(
                    "source workspace ref {source_ref} is not under {source_workspace}"
                ))
            })?;
        entries.push((source_ref, logical_path, node_id));
    }
    if entries.is_empty() {
        return Err(AnfsError::RefNotFound(format!(
            "fork source workspace {source_workspace} has no draft refs"
        )));
    }

    let mut forked = Vec::new();
    for (idx, (source_ref, logical_path, node_id)) in entries.into_iter().enumerate() {
        let target_ref = workspace_ref_name(target_workspace, &logical_path);
        if fetch_ref(tx, &target_ref)?.is_some() {
            return Err(AnfsError::RefConflict(format!(
                "fork target ref {target_ref} already exists"
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
            Some(&node_id),
            None,
            Some("draft"),
        )?;

        let input_role = format!("fork_source:{idx}");
        let output_role = format!("fork_workspace_view:{idx}");
        insert_edge(
            tx,
            event_id,
            "input",
            &node_id,
            &input_role,
            Some(&source_ref),
        )?;
        insert_edge(
            tx,
            event_id,
            "output",
            &node_id,
            &output_role,
            Some(&target_ref),
        )?;
        forked.push((logical_path, target_ref, node_id));
    }
    Ok(forked)
}
