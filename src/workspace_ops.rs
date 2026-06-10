use rusqlite::{params, Connection, OptionalExtension};
use std::collections::BTreeMap;
use std::path::Path;

use crate::{
    checkout_logical_path, fetch_ref, manifest_children, validate_workspace_name,
    workspace_logical_path, AnfsError, AnfsResult,
};

pub(crate) fn workspace_diff(
    conn: &Connection,
    objects_dir: &Path,
    base: &str,
    workspace: &str,
) -> AnfsResult<Vec<(String, String, Option<String>, Option<String>)>> {
    let base_prefix = base.trim_end_matches('/');
    let workspace = validate_workspace_name(workspace)?;
    let workspace_like = format!("{}/%", workspace);

    let base_nodes = load_base_view_nodes(conn, objects_dir, base_prefix)?;

    let mut workspace_nodes: BTreeMap<String, (String, String)> = BTreeMap::new();
    let mut workspace_stmt = conn.prepare(
        "SELECT ref_name, node_id, state
         FROM refs
         WHERE ref_name LIKE ?1
         ORDER BY ref_name",
    )?;
    let workspace_rows = workspace_stmt.query_map(params![workspace_like], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    for row in workspace_rows {
        let (ref_name, node_id, state) = row?;
        if let Some(path) = workspace_logical_path(&workspace, &ref_name) {
            workspace_nodes.insert(path, (node_id, state));
        }
    }

    let mut all_paths = BTreeMap::new();
    for path in base_nodes.keys() {
        all_paths.insert(path.clone(), ());
    }
    for path in workspace_nodes.keys() {
        all_paths.insert(path.clone(), ());
    }

    let mut diff = Vec::new();
    for path in all_paths.keys() {
        let base_node = base_nodes.get(path).cloned();
        let workspace_entry = workspace_nodes.get(path).cloned();
        let (status, workspace_node) = match (base_node.as_ref(), workspace_entry.as_ref()) {
            (Some(_), Some((_, state))) if state == "deleted" => ("deleted", None),
            (None, Some((_, state))) if state == "deleted" => ("deleted", None),
            (None, Some((node_id, _))) => ("added", Some(node_id.clone())),
            (Some(base_node), Some((workspace_node, _))) if base_node == workspace_node => {
                ("unchanged", Some(workspace_node.clone()))
            }
            (Some(_), Some((workspace_node, _))) => ("modified", Some(workspace_node.clone())),
            (Some(_), None) => ("deleted", None),
            (None, None) => continue,
        };
        diff.push((path.clone(), status.to_string(), base_node, workspace_node));
    }

    Ok(diff)
}

pub(crate) fn workspace_conflicts(
    conn: &Connection,
    base: &str,
    workspace: &str,
) -> AnfsResult<Vec<(String, Option<String>, Option<String>, Option<String>)>> {
    let base_prefix = base.trim_end_matches('/');
    let workspace = validate_workspace_name(workspace)?;
    let merge_target_prefix = merge_target_prefix_for_base(conn, base_prefix)?;
    let current_base = load_base_namespace_nodes(conn, &merge_target_prefix)?;
    let workspace_nodes = load_workspace_nodes(conn, &workspace)?;

    let mut checkout_base: BTreeMap<String, String> = BTreeMap::new();
    let mut checkout_stmt = conn.prepare(
        "SELECT logical_path, node_id
         FROM workspace_base_refs
         WHERE workspace_id = ?1 AND base_ref = ?2
         ORDER BY logical_path",
    )?;
    let checkout_rows = checkout_stmt.query_map(params![workspace, base_prefix], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in checkout_rows {
        let (path, node_id) = row?;
        checkout_base.insert(path, node_id);
    }
    if checkout_base.is_empty() {
        return Err(AnfsError::RefNotFound(format!(
            "workspace {workspace} has no checkout base snapshot for {base_prefix}"
        )));
    }

    let mut paths = BTreeMap::new();
    for path in checkout_base.keys() {
        paths.insert(path.clone(), ());
    }
    for path in current_base.keys() {
        paths.insert(path.clone(), ());
    }
    for path in workspace_nodes.keys() {
        paths.insert(path.clone(), ());
    }

    let mut conflicts = Vec::new();
    for path in paths.keys() {
        let checkout_node = checkout_base.get(path).cloned();
        let current_node = current_base.get(path).cloned();
        let workspace_node = match workspace_nodes.get(path) {
            Some((_, state)) if state == "deleted" => None,
            Some((node_id, _)) => Some(node_id.clone()),
            None => checkout_node.clone(),
        };

        let workspace_changed = workspace_node != checkout_node;
        let base_changed = current_node != checkout_node;
        if workspace_changed && base_changed && workspace_node != current_node {
            conflicts.push((path.clone(), checkout_node, current_node, workspace_node));
        }
    }

    Ok(conflicts)
}

pub(crate) fn workspace_changes_since_checkout(
    conn: &Connection,
    base: &str,
    workspace: &str,
) -> AnfsResult<Vec<(String, String, Option<String>, Option<String>)>> {
    let base_prefix = base.trim_end_matches('/');
    let workspace = validate_workspace_name(workspace)?;
    let checkout_base = load_checkout_base_nodes(conn, base_prefix, &workspace)?;
    if checkout_base.is_empty() {
        return Err(AnfsError::RefNotFound(format!(
            "workspace {workspace} has no checkout base snapshot for {base_prefix}"
        )));
    }
    let workspace_nodes = load_workspace_nodes(conn, &workspace)?;

    let mut paths = BTreeMap::new();
    for path in checkout_base.keys() {
        paths.insert(path.clone(), ());
    }
    for path in workspace_nodes.keys() {
        paths.insert(path.clone(), ());
    }

    let mut changes = Vec::new();
    for path in paths.keys() {
        let checkout_node = checkout_base.get(path).cloned();
        let workspace_entry = workspace_nodes.get(path).cloned();
        let (status, workspace_node) = match (checkout_node.as_ref(), workspace_entry.as_ref()) {
            (Some(_), Some((_, state))) if state == "deleted" => ("deleted", None),
            (None, Some((_, state))) if state == "deleted" => ("deleted", None),
            (None, Some((node_id, _))) => ("added", Some(node_id.clone())),
            (Some(checkout_node), Some((workspace_node, _))) if checkout_node == workspace_node => {
                ("unchanged", Some(workspace_node.clone()))
            }
            (Some(_), Some((workspace_node, _))) => ("modified", Some(workspace_node.clone())),
            (Some(_), None) => ("deleted", None),
            (None, None) => continue,
        };
        changes.push((
            path.clone(),
            status.to_string(),
            checkout_node,
            workspace_node,
        ));
    }
    Ok(changes)
}

fn load_base_view_nodes(
    conn: &Connection,
    objects_dir: &Path,
    base_prefix: &str,
) -> AnfsResult<BTreeMap<String, String>> {
    if let Some(base_ref) = fetch_ref(conn, base_prefix)? {
        if base_ref.state == "published" || base_ref.state == "approved" {
            let children = manifest_children(conn, objects_dir, &base_ref.node_id)?;
            if !children.is_empty() {
                return Ok(children.into_iter().collect());
            }
        }
    }
    load_base_namespace_nodes(conn, base_prefix)
}

fn load_base_namespace_nodes(
    conn: &Connection,
    base_prefix: &str,
) -> AnfsResult<BTreeMap<String, String>> {
    let base_like = format!("{base_prefix}/%");
    let mut base_nodes = BTreeMap::new();
    let mut stmt = conn.prepare(
        "SELECT ref_name, node_id
         FROM refs
         WHERE (ref_name = ?1 OR ref_name LIKE ?2)
           AND state IN ('published', 'approved')
         ORDER BY ref_name",
    )?;
    let rows = stmt.query_map(params![base_prefix, base_like], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (ref_name, node_id) = row?;
        base_nodes.insert(checkout_logical_path(base_prefix, &ref_name), node_id);
    }
    Ok(base_nodes)
}

pub(crate) fn merge_target_prefix_for_base(conn: &Connection, base: &str) -> AnfsResult<String> {
    let base_prefix = base.trim_end_matches('/');
    if let Some(snapshot_prefix) = snapshot_manifest_source_prefix(conn, base_prefix)? {
        return Ok(snapshot_prefix);
    }
    Ok(base_prefix.to_string())
}

fn snapshot_manifest_source_prefix(
    conn: &Connection,
    base_ref_name: &str,
) -> AnfsResult<Option<String>> {
    let Some(base_ref) = fetch_ref(conn, base_ref_name)? else {
        return Ok(None);
    };
    if base_ref.state != "published" && base_ref.state != "approved" {
        return Ok(None);
    }
    let metadata_json: Option<String> = conn
        .query_row(
            "SELECT metadata_json FROM nodes WHERE node_id = ?1",
            params![base_ref.node_id],
            |row| row.get(0),
        )
        .optional()?;
    let Some(metadata_json) = metadata_json else {
        return Ok(None);
    };
    let metadata: serde_json::Value = serde_json::from_str(&metadata_json).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "invalid metadata_json for base ref {base_ref_name}: {err}"
        ))
    })?;
    let extra = metadata.get("extra").unwrap_or(&serde_json::Value::Null);
    if extra.get("snapshot").and_then(|value| value.as_bool()) != Some(true) {
        return Ok(None);
    }
    let Some(prefix) = extra.get("prefix").and_then(|value| value.as_str()) else {
        return Err(AnfsError::StorageCorruption(format!(
            "snapshot manifest {base_ref_name} is missing source prefix"
        )));
    };
    let prefix = prefix.trim_end_matches('/');
    if prefix.is_empty() {
        return Err(AnfsError::StorageCorruption(format!(
            "snapshot manifest {base_ref_name} has empty source prefix"
        )));
    }
    Ok(Some(prefix.to_string()))
}

fn load_checkout_base_nodes(
    conn: &Connection,
    base_prefix: &str,
    workspace: &str,
) -> AnfsResult<BTreeMap<String, String>> {
    let mut checkout_base = BTreeMap::new();
    let mut stmt = conn.prepare(
        "SELECT logical_path, node_id
         FROM workspace_base_refs
         WHERE workspace_id = ?1 AND base_ref = ?2
         ORDER BY logical_path",
    )?;
    let rows = stmt.query_map(params![workspace, base_prefix], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    for row in rows {
        let (path, node_id) = row?;
        checkout_base.insert(path, node_id);
    }
    Ok(checkout_base)
}

fn load_workspace_nodes(
    conn: &Connection,
    workspace: &str,
) -> AnfsResult<BTreeMap<String, (String, String)>> {
    let workspace_like = format!("{}/%", workspace.trim_end_matches('/'));
    let mut workspace_nodes = BTreeMap::new();
    let mut stmt = conn.prepare(
        "SELECT ref_name, node_id, state
         FROM refs
         WHERE ref_name LIKE ?1
         ORDER BY ref_name",
    )?;
    let rows = stmt.query_map(params![workspace_like], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;
    for row in rows {
        let (ref_name, node_id, state) = row?;
        if let Some(path) = workspace_logical_path(workspace, &ref_name) {
            workspace_nodes.insert(path, (node_id, state));
        }
    }
    Ok(workspace_nodes)
}

pub(crate) fn ensure_node_exists(conn: &Connection, node_id: &str) -> AnfsResult<()> {
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM nodes WHERE node_id = ?1 LIMIT 1",
            params![node_id],
            |row| row.get(0),
        )
        .optional()?;
    if found.is_none() {
        return Err(AnfsError::NodeNotFound(node_id.to_string()));
    }
    Ok(())
}
