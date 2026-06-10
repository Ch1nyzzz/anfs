use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};
use std::collections::{BTreeMap, BTreeSet};
use std::fs;
use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;
use uuid::Uuid;

use crate::{
    insert_edge, insert_event, lock_conn, new_event_id, normalize_workspace, now_millis,
    read_node_bytes, sha256_hex, validate_workspace_name, with_sqlite_busy_retry, AnfsError,
    AnfsResult, CachedWorkingSetResult, CachedWorkingSetRow, Inner, VisibilityPolicy, Workspace,
    WorktreeCommitRow, WorktreeMaterializeRow, WorktreeReadinessRow,
};

const WORKTREE_MANIFEST: &str = ".anfs-worktree-manifest.json";
const CACHE_LOCK_DIR: &str = ".anfs-cache-locks";
const CACHE_MATCH_RETRY_ATTEMPTS: usize = 20;
const CACHE_MATCH_RETRY_MS: u64 = 25;

#[cfg(unix)]
use std::os::unix::io::AsRawFd;

#[cfg(unix)]
const LOCK_EX: i32 = 2;
#[cfg(unix)]
const LOCK_UN: i32 = 8;

#[cfg(unix)]
extern "C" {
    fn flock(fd: i32, operation: i32) -> i32;
}

pub(crate) fn materialize_workspace(
    conn: &Connection,
    objects_dir: &Path,
    workspace: &str,
    output_dir: &Path,
    overwrite: bool,
    atomic: bool,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<WorktreeMaterializeRow>> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let workspace = validate_workspace_name(workspace)?;
    let rows = active_workspace_files(conn, objects_dir, &workspace, &visibility)?;
    let writes = worktree_writes(&rows, conn, objects_dir)?;
    ensure_worktree_output_writable(output_dir, overwrite)?;

    if atomic && (!output_dir.exists() || overwrite) {
        let parent = output_dir.parent().unwrap_or_else(|| Path::new("."));
        let staging = parent.join(format!(
            ".{}.tmp-{}",
            output_dir
                .file_name()
                .and_then(|value| value.to_str())
                .unwrap_or("anfs-worktree"),
            Uuid::new_v4()
        ));
        fs::create_dir_all(&staging)?;
        write_worktree_files(&staging, &writes)?;
        write_worktree_manifest(&staging, &workspace, &rows)?;
        if output_dir.exists() {
            fs::remove_dir_all(output_dir)?;
        }
        fs::rename(&staging, output_dir)?;
    } else {
        if overwrite && output_dir.exists() {
            fs::remove_dir_all(output_dir)?;
        }
        fs::create_dir_all(output_dir)?;
        write_worktree_files(output_dir, &writes)?;
        write_worktree_manifest(output_dir, &workspace, &rows)?;
    }

    Ok(rows)
}

pub(crate) fn cache_materialized_workspace(
    conn: &mut Connection,
    objects_dir: &Path,
    workspace: &str,
    cache_root: &Path,
    cache_key: Option<&str>,
    overwrite: bool,
    atomic: bool,
    policy_label_excludes: &[String],
) -> AnfsResult<CachedWorkingSetResult> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let workspace = validate_workspace_name(workspace)?;
    let rows = active_workspace_files(conn, objects_dir, &workspace, &visibility)?;
    let manifest_hash = working_set_manifest_hash(&workspace, &rows, policy_label_excludes)?;
    let cache_key = match cache_key {
        Some(value) => validate_working_set_cache_key(value)?.to_string(),
        None => format!("wset-{}", &manifest_hash[..24]),
    };
    let _cache_lock = lock_cache_key(cache_root, &cache_key)?;
    let output_dir = cache_root.join(&cache_key);
    let output_dir_string = output_dir.to_string_lossy().to_string();

    if !overwrite
        && output_dir.is_dir()
        && cached_working_set_matches(conn, &cache_key, &output_dir_string, &manifest_hash)?
    {
        return Ok((cache_key, output_dir_string, rows.len() as i64, true));
    }
    if output_dir.exists() && !overwrite {
        if output_dir.is_dir()
            && wait_for_cached_working_set_match(
                conn,
                &cache_key,
                &output_dir_string,
                &manifest_hash,
            )?
        {
            return Ok((cache_key, output_dir_string, rows.len() as i64, true));
        }
        return Err(AnfsError::PolicyDenied(format!(
            "cached working set {} is stale or unmanaged; pass overwrite=True to replace it",
            output_dir.display()
        )));
    }

    let writes = worktree_writes(&rows, conn, objects_dir)?;
    if atomic {
        let parent = output_dir.parent().unwrap_or_else(|| Path::new("."));
        fs::create_dir_all(parent)?;
        let staging = parent.join(format!(".{}.tmp-{}", cache_key, Uuid::new_v4()));
        fs::create_dir_all(&staging)?;
        let result = write_worktree_files(&staging, &writes)
            .and_then(|_| write_worktree_manifest(&staging, &workspace, &rows));
        if result.is_err() {
            let _ = fs::remove_dir_all(&staging);
        }
        result?;
        if output_dir.exists() {
            if !overwrite
                && output_dir.is_dir()
                && wait_for_cached_working_set_match(
                    conn,
                    &cache_key,
                    &output_dir_string,
                    &manifest_hash,
                )?
            {
                let _ = fs::remove_dir_all(&staging);
                return Ok((cache_key, output_dir_string, rows.len() as i64, true));
            }
            let result = fs::remove_dir_all(&output_dir);
            if result.is_err() {
                let _ = fs::remove_dir_all(&staging);
            }
            result?;
        }
        let result = fs::rename(&staging, &output_dir);
        if result.is_err() {
            let _ = fs::remove_dir_all(&staging);
        }
        result?;
    } else {
        if output_dir.exists() {
            fs::remove_dir_all(&output_dir)?;
        }
        fs::create_dir_all(&output_dir)?;
        write_worktree_files(&output_dir, &writes)?;
        write_worktree_manifest(&output_dir, &workspace, &rows)?;
    }

    record_cached_working_set(
        conn,
        &cache_key,
        &workspace,
        &output_dir_string,
        &manifest_hash,
        policy_label_excludes,
        &rows,
    )?;
    Ok((cache_key, output_dir_string, rows.len() as i64, false))
}

pub(crate) fn cached_working_sets(
    conn: &Connection,
    workspace: Option<&str>,
) -> AnfsResult<Vec<CachedWorkingSetRow>> {
    let normalized_workspace = workspace.map(normalize_workspace);
    let mut stmt = conn.prepare(
        "
        SELECT cache_key, workspace_id, output_dir, manifest_hash, file_count, materialized_at
        FROM materialized_working_sets
        WHERE (?1 IS NULL OR workspace_id = ?1)
        ORDER BY materialized_at DESC, cache_key
        ",
    )?;
    let rows = stmt.query_map(params![normalized_workspace.as_deref()], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, i64>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

pub(crate) fn commit_worktree(
    inner: &std::sync::Arc<Inner>,
    workspace: &str,
    input_dir: &Path,
    agent_id: &str,
    run_id: Option<String>,
    tool_call_id: Option<String>,
    delete_missing: bool,
    require_manifest_match: bool,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<WorktreeCommitRow>> {
    with_sqlite_busy_retry(|| {
        if !input_dir.is_dir() {
            return Err(AnfsError::PolicyDenied(format!(
                "worktree input {} is not a directory",
                input_dir.display()
            )));
        }
        let visibility = VisibilityPolicy::new(policy_label_excludes)?;
        let workspace = validate_workspace_name(workspace)?;
        let files = scan_worktree_files(input_dir)?;

        let ws = Workspace {
            inner: inner.clone(),
            workspace_name: workspace.clone(),
            agent_id: agent_id.to_string(),
            run_id: run_id.clone(),
        };
        let mut committed = Vec::new();
        let mut audit_rows = Vec::new();

        let mut conn = lock_conn(inner)?;
        let tx = conn.transaction()?;
        let current = active_workspace_entries(&tx, &inner.objects_dir, &workspace, &visibility)?;
        if require_manifest_match {
            ensure_worktree_manifest_matches(input_dir, &workspace, &current)?;
        }
        let current_paths: BTreeSet<String> = current.keys().cloned().collect();
        let file_paths: BTreeSet<String> = files.keys().cloned().collect();

        for path in file_paths.iter() {
            let bytes = files.get(path).expect("file path from key set");
            let existing = current.get(path);
            let status = match existing {
                Some((node_id, existing_bytes)) if existing_bytes == bytes => {
                    committed.push((path.clone(), "unchanged".to_string(), Some(node_id.clone())));
                    audit_rows.push(WorktreeCommitAuditRow {
                        path: path.clone(),
                        status: "unchanged".to_string(),
                        old_node_id: Some(node_id.clone()),
                        new_node_id: Some(node_id.clone()),
                    });
                    continue;
                }
                Some((_node_id, _existing_bytes)) => "modified",
                None => "added",
            };
            let derived_from_nodes = existing
                .map(|(node_id, _bytes)| vec![node_id.clone()])
                .unwrap_or_default();
            let node_id = ws.write_in_tx(
                &tx,
                path,
                bytes,
                derived_from_nodes,
                tool_call_id.as_deref(),
            )?;
            audit_rows.push(WorktreeCommitAuditRow {
                path: path.clone(),
                status: status.to_string(),
                old_node_id: existing.map(|(node_id, _bytes)| node_id.clone()),
                new_node_id: Some(node_id.clone()),
            });
            committed.push((path.clone(), status.to_string(), Some(node_id)));
        }

        if delete_missing {
            for path in current_paths.difference(&file_paths) {
                let old_node_id = current.get(path).map(|(node_id, _bytes)| node_id.clone());
                ws.delete_in_tx(&tx, path, tool_call_id.as_deref(), None, None, &[])?;
                audit_rows.push(WorktreeCommitAuditRow {
                    path: path.clone(),
                    status: "deleted".to_string(),
                    old_node_id,
                    new_node_id: None,
                });
                committed.push((path.clone(), "deleted".to_string(), None));
            }
        }

        insert_worktree_commit_event(
            &tx,
            &workspace,
            input_dir,
            agent_id,
            ws.run_id.as_deref(),
            tool_call_id.as_deref(),
            delete_missing,
            require_manifest_match,
            policy_label_excludes,
            &audit_rows,
        )?;
        tx.commit()?;

        Ok(committed)
    })
}

pub(crate) fn worktree_readiness(
    conn: &Connection,
    objects_dir: &Path,
    workspace: &str,
    input_dir: &Path,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<WorktreeReadinessRow>> {
    let workspace = validate_workspace_name(workspace)?;
    let mut rows = Vec::new();
    if !input_dir.is_dir() {
        rows.push((
            "input_dir".to_string(),
            false,
            format!("worktree input {} is not a directory", input_dir.display()),
            0,
        ));
        rows.push((
            "manifest_lease".to_string(),
            false,
            "worktree manifest cannot be checked without an input directory".to_string(),
            0,
        ));
        rows.push((
            "local_changes".to_string(),
            false,
            "local changes cannot be scanned without an input directory".to_string(),
            0,
        ));
        rows.push((
            "checked_commit_ready".to_string(),
            false,
            "checked commit preconditions are not satisfied".to_string(),
            0,
        ));
        return Ok(rows);
    }

    rows.push((
        "input_dir".to_string(),
        true,
        format!("worktree input {} is a directory", input_dir.display()),
        1,
    ));
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let current = active_workspace_entries(conn, objects_dir, &workspace, &visibility)?;
    let manifest_result = ensure_worktree_manifest_matches(input_dir, &workspace, &current);
    let manifest_ok = manifest_result.is_ok();
    rows.push((
        "manifest_lease".to_string(),
        manifest_ok,
        match manifest_result {
            Ok(()) => "worktree manifest matches current workspace refs".to_string(),
            Err(err) => format!("worktree manifest mismatch: {err:?}"),
        },
        current.len() as i64,
    ));

    let files = match scan_worktree_files(input_dir) {
        Ok(files) => {
            rows.push((
                "supported_entries".to_string(),
                true,
                "worktree contains only regular files, directories, and the ANFS manifest"
                    .to_string(),
                files.len() as i64,
            ));
            files
        }
        Err(err) => {
            rows.push((
                "supported_entries".to_string(),
                false,
                format!("worktree contains unsupported entries: {err:?}"),
                0,
            ));
            rows.push((
                "local_changes".to_string(),
                false,
                "local changes cannot be counted while unsupported entries are present".to_string(),
                0,
            ));
            rows.push((
                "checked_commit_ready".to_string(),
                false,
                "checked commit preconditions are not satisfied".to_string(),
                0,
            ));
            return Ok(rows);
        }
    };
    let (added, modified, deleted, unchanged) = worktree_change_counts(&files, &current);
    let changed = added + modified + deleted;
    rows.push((
        "local_changes".to_string(),
        true,
        format!("added={added} modified={modified} deleted={deleted} unchanged={unchanged}"),
        changed,
    ));
    rows.push((
        "checked_commit_ready".to_string(),
        manifest_ok,
        if manifest_ok {
            "checked commit preconditions are satisfied".to_string()
        } else {
            "checked commit would fail before mutation".to_string()
        },
        if manifest_ok { 1 } else { 0 },
    ));
    Ok(rows)
}

fn worktree_change_counts(
    files: &BTreeMap<String, Vec<u8>>,
    current: &BTreeMap<String, (String, Vec<u8>)>,
) -> (i64, i64, i64, i64) {
    let current_paths: BTreeSet<&String> = current.keys().collect();
    let file_paths: BTreeSet<&String> = files.keys().collect();
    let mut added = 0;
    let mut modified = 0;
    let mut unchanged = 0;
    for path in &file_paths {
        match current.get(*path) {
            Some((_node_id, existing_bytes)) if existing_bytes == files.get(*path).unwrap() => {
                unchanged += 1;
            }
            Some((_node_id, _existing_bytes)) => modified += 1,
            None => added += 1,
        }
    }
    let deleted = current_paths.difference(&file_paths).count() as i64;
    (added, modified, deleted, unchanged)
}

fn working_set_manifest_hash(
    workspace: &str,
    rows: &[WorktreeMaterializeRow],
    policy_label_excludes: &[String],
) -> AnfsResult<String> {
    let payload_rows: Vec<_> = rows
        .iter()
        .map(|(path, ref_name, node_id, size)| {
            json!({
                "path": path,
                "ref_name": ref_name,
                "node_id": node_id,
                "size": size,
            })
        })
        .collect();
    let payload = json!({
        "schema": "anfs.working_set_manifest_hash.v1",
        "workspace": workspace,
        "policy_label_excludes": policy_label_excludes,
        "files": payload_rows,
    });
    let bytes = serde_json::to_vec(&payload).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to hash working set manifest: {err}"))
    })?;
    Ok(sha256_hex(&bytes))
}

fn validate_working_set_cache_key(cache_key: &str) -> AnfsResult<&str> {
    if cache_key.is_empty()
        || cache_key.len() > 128
        || cache_key.starts_with('.')
        || !cache_key
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_')
    {
        return Err(AnfsError::PolicyDenied(format!(
            "unsafe working set cache key {cache_key}"
        )));
    }
    Ok(cache_key)
}

struct CacheKeyLock {
    #[cfg(unix)]
    file: Option<fs::File>,
}

#[cfg(unix)]
impl Drop for CacheKeyLock {
    fn drop(&mut self) {
        if let Some(file) = &self.file {
            let _ = unsafe { flock(file.as_raw_fd(), LOCK_UN) };
        }
    }
}

#[cfg(unix)]
fn lock_cache_key(cache_root: &Path, cache_key: &str) -> AnfsResult<CacheKeyLock> {
    let lock_dir = cache_root.join(CACHE_LOCK_DIR);
    fs::create_dir_all(&lock_dir)?;
    let lock_path = lock_dir.join(format!("{cache_key}.lock"));
    let file = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(lock_path)?;
    let rc = unsafe { flock(file.as_raw_fd(), LOCK_EX) };
    if rc != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    Ok(CacheKeyLock { file: Some(file) })
}

#[cfg(not(unix))]
fn lock_cache_key(_cache_root: &Path, _cache_key: &str) -> AnfsResult<CacheKeyLock> {
    Ok(CacheKeyLock {})
}

fn cached_working_set_matches(
    conn: &Connection,
    cache_key: &str,
    output_dir: &str,
    manifest_hash: &str,
) -> AnfsResult<bool> {
    let existing: Option<(String, String)> = conn
        .query_row(
            "SELECT output_dir, manifest_hash
             FROM materialized_working_sets
             WHERE cache_key = ?1",
            params![cache_key],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    Ok(matches!(
        existing,
        Some((existing_output_dir, existing_hash))
            if existing_output_dir == output_dir && existing_hash == manifest_hash
    ))
}

fn wait_for_cached_working_set_match(
    conn: &Connection,
    cache_key: &str,
    output_dir: &str,
    manifest_hash: &str,
) -> AnfsResult<bool> {
    for _attempt in 0..CACHE_MATCH_RETRY_ATTEMPTS {
        thread::sleep(Duration::from_millis(CACHE_MATCH_RETRY_MS));
        if cached_working_set_matches(conn, cache_key, output_dir, manifest_hash)? {
            return Ok(true);
        }
    }
    Ok(false)
}

fn record_cached_working_set(
    conn: &mut Connection,
    cache_key: &str,
    workspace: &str,
    output_dir: &str,
    manifest_hash: &str,
    policy_label_excludes: &[String],
    rows: &[WorktreeMaterializeRow],
) -> AnfsResult<()> {
    let policy_json = serde_json::to_string(policy_label_excludes).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize policy labels: {err}"))
    })?;
    let tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM materialized_working_set_files WHERE cache_key = ?1",
        params![cache_key],
    )?;
    tx.execute(
        "DELETE FROM materialized_working_sets WHERE cache_key = ?1",
        params![cache_key],
    )?;
    tx.execute(
        "INSERT INTO materialized_working_sets
         (cache_key, workspace_id, output_dir, manifest_hash, policy_label_excludes_json,
          file_count, materialized_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            cache_key,
            workspace,
            output_dir,
            manifest_hash,
            policy_json,
            rows.len() as i64,
            now_millis()
        ],
    )?;
    for (path, ref_name, node_id, size) in rows {
        tx.execute(
            "INSERT INTO materialized_working_set_files
             (cache_key, path, ref_name, node_id, size)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![cache_key, path, ref_name, node_id, size],
        )?;
    }
    tx.commit()?;
    Ok(())
}

struct WorktreeCommitAuditRow {
    path: String,
    status: String,
    old_node_id: Option<String>,
    new_node_id: Option<String>,
}

fn insert_worktree_commit_event(
    tx: &rusqlite::Transaction<'_>,
    workspace: &str,
    input_dir: &Path,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    delete_missing: bool,
    require_manifest_match: bool,
    policy_label_excludes: &[String],
    rows: &[WorktreeCommitAuditRow],
) -> AnfsResult<()> {
    let event_id = new_event_id();
    let payload_rows: Vec<_> = rows
        .iter()
        .map(|row| {
            json!({
                "path": row.path,
                "status": row.status,
                "old_node_id": row.old_node_id,
                "new_node_id": row.new_node_id,
            })
        })
        .collect();
    let changed_count = rows.iter().filter(|row| row.status != "unchanged").count();
    let edge_count: usize = rows
        .iter()
        .map(|row| match row.status.as_str() {
            "modified" => {
                usize::from(row.old_node_id.is_some()) + usize::from(row.new_node_id.is_some())
            }
            "added" | "deleted" | "unchanged" => 1,
            _ => 0,
        })
        .sum();
    let payload = json!({
        "workspace": workspace,
        "input_dir": input_dir.to_string_lossy(),
        "delete_missing": delete_missing,
        "require_manifest_match": require_manifest_match,
        "policy_label_excludes": policy_label_excludes,
        "result_count": rows.len(),
        "changed_count": changed_count,
        "edge_count": edge_count,
        "results": payload_rows,
    })
    .to_string();
    insert_event(
        tx,
        &event_id,
        "worktree_commit",
        Some(agent_id),
        run_id,
        tool_call_id,
        Some(workspace),
        Some(&payload),
    )?;
    for (idx, row) in rows.iter().enumerate() {
        match row.status.as_str() {
            "added" => {
                if let Some(new_node_id) = &row.new_node_id {
                    let role = format!("worktree_added:{idx}");
                    insert_edge(tx, &event_id, "output", new_node_id, &role, Some(&row.path))?;
                }
            }
            "modified" => {
                if let Some(old_node_id) = &row.old_node_id {
                    let role = format!("worktree_input:{idx}");
                    insert_edge(tx, &event_id, "input", old_node_id, &role, Some(&row.path))?;
                }
                if let Some(new_node_id) = &row.new_node_id {
                    let role = format!("worktree_modified:{idx}");
                    insert_edge(tx, &event_id, "output", new_node_id, &role, Some(&row.path))?;
                }
            }
            "deleted" => {
                if let Some(old_node_id) = &row.old_node_id {
                    let role = format!("worktree_deleted:{idx}");
                    insert_edge(tx, &event_id, "input", old_node_id, &role, Some(&row.path))?;
                }
            }
            "unchanged" => {
                if let Some(old_node_id) = &row.old_node_id {
                    let role = format!("worktree_unchanged:{idx}");
                    insert_edge(tx, &event_id, "input", old_node_id, &role, Some(&row.path))?;
                }
            }
            _ => {}
        }
    }
    Ok(())
}

fn ensure_worktree_manifest_matches(
    input_dir: &Path,
    workspace: &str,
    current: &BTreeMap<String, (String, Vec<u8>)>,
) -> AnfsResult<()> {
    let manifest_path = input_dir.join(WORKTREE_MANIFEST);
    let manifest_bytes = fs::read(&manifest_path).map_err(|err| {
        AnfsError::RefConflict(format!(
            "worktree manifest {} is required for checked commit: {err}",
            manifest_path.display()
        ))
    })?;
    let manifest: Value = serde_json::from_slice(&manifest_bytes).map_err(|err| {
        AnfsError::RefConflict(format!(
            "worktree manifest {} is invalid JSON: {err}",
            manifest_path.display()
        ))
    })?;
    let schema = manifest.get("schema").and_then(Value::as_str);
    if schema != Some("anfs.worktree.v1") {
        return Err(AnfsError::RefConflict(format!(
            "worktree manifest {} has unsupported schema",
            manifest_path.display()
        )));
    }
    let manifest_workspace = manifest.get("workspace").and_then(Value::as_str);
    if manifest_workspace != Some(workspace) {
        return Err(AnfsError::RefConflict(format!(
            "worktree manifest workspace {:?} does not match commit workspace {}",
            manifest_workspace, workspace
        )));
    }
    let Some(files) = manifest.get("files").and_then(Value::as_array) else {
        return Err(AnfsError::RefConflict(format!(
            "worktree manifest {} is missing files",
            manifest_path.display()
        )));
    };

    let mut manifest_nodes = BTreeMap::new();
    for file in files {
        let Some(path) = file.get("path").and_then(Value::as_str) else {
            return Err(AnfsError::RefConflict(
                "worktree manifest file is missing path".to_string(),
            ));
        };
        validate_relative_worktree_path(path)?;
        let Some(ref_name) = file.get("ref_name").and_then(Value::as_str) else {
            return Err(AnfsError::RefConflict(format!(
                "worktree manifest file {path} is missing ref_name"
            )));
        };
        let expected_ref = format!("{}/{}", workspace.trim_end_matches('/'), path);
        if ref_name != expected_ref {
            return Err(AnfsError::RefConflict(format!(
                "worktree manifest file {path} ref {ref_name} does not match expected {expected_ref}"
            )));
        }
        let Some(node_id) = file.get("node_id").and_then(Value::as_str) else {
            return Err(AnfsError::RefConflict(format!(
                "worktree manifest file {path} is missing node_id"
            )));
        };
        if manifest_nodes
            .insert(path.to_string(), node_id.to_string())
            .is_some()
        {
            return Err(AnfsError::RefConflict(format!(
                "worktree manifest has duplicate path {path}"
            )));
        }
    }

    for (path, node_id) in &manifest_nodes {
        match current.get(path) {
            Some((current_node_id, _bytes)) if current_node_id == node_id => {}
            Some((current_node_id, _bytes)) => {
                return Err(AnfsError::RefConflict(format!(
                    "worktree base for {path} is stale: manifest node {node_id}, current node {current_node_id}"
                )));
            }
            None => {
                return Err(AnfsError::RefConflict(format!(
                    "worktree base for {path} is stale: current workspace ref is missing"
                )));
            }
        }
    }

    for path in current.keys() {
        if !manifest_nodes.contains_key(path) {
            return Err(AnfsError::RefConflict(format!(
                "worktree base is stale: current workspace has new path {path}"
            )));
        }
    }

    Ok(())
}

fn active_workspace_files(
    conn: &Connection,
    objects_dir: &Path,
    workspace: &str,
    visibility: &VisibilityPolicy<'_>,
) -> AnfsResult<Vec<WorktreeMaterializeRow>> {
    let prefix = format!("{}/%", workspace.trim_end_matches('/'));
    let mut stmt = conn.prepare(
        "
        SELECT r.ref_name, r.node_id, b.size
        FROM refs r
        JOIN nodes n ON n.node_id = r.node_id
        JOIN blobs b ON b.hash = n.blob_hash
        WHERE r.ref_name LIKE ?1
          AND r.state NOT IN ('deleted', 'archived')
          AND n.kind != 'directory'
        ORDER BY r.ref_name
        ",
    )?;
    let rows = stmt.query_map(params![prefix], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        let (ref_name, node_id, size) = row?;
        visibility.ensure_ref_node_visible(
            conn,
            &ref_name,
            &node_id,
            "workspace materialization",
        )?;
        let logical_path = workspace_logical_path_checked(workspace, &ref_name)?;
        validate_relative_worktree_path(&logical_path)?;
        let _ = read_node_bytes(conn, objects_dir, &node_id)?;
        out.push((logical_path, ref_name, node_id, size));
    }
    Ok(out)
}

fn active_workspace_entries(
    conn: &Connection,
    objects_dir: &Path,
    workspace: &str,
    visibility: &VisibilityPolicy<'_>,
) -> AnfsResult<BTreeMap<String, (String, Vec<u8>)>> {
    let prefix = format!("{}/%", workspace.trim_end_matches('/'));
    let mut stmt = conn.prepare(
        "
        SELECT r.ref_name, r.node_id
        FROM refs r
        JOIN nodes n ON n.node_id = r.node_id
        WHERE r.ref_name LIKE ?1
          AND r.state NOT IN ('deleted', 'archived')
          AND n.kind != 'directory'
        ORDER BY r.ref_name
        ",
    )?;
    let rows = stmt.query_map(params![prefix], |row| {
        Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
    })?;
    let mut out = BTreeMap::new();
    for row in rows {
        let (ref_name, node_id) = row?;
        visibility.ensure_ref_node_visible(conn, &ref_name, &node_id, "worktree commit")?;
        let logical_path = workspace_logical_path_checked(workspace, &ref_name)?;
        validate_relative_worktree_path(&logical_path)?;
        let bytes = read_node_bytes(conn, objects_dir, &node_id)?;
        out.insert(logical_path, (node_id, bytes));
    }
    Ok(out)
}

fn worktree_writes(
    rows: &[WorktreeMaterializeRow],
    conn: &Connection,
    objects_dir: &Path,
) -> AnfsResult<Vec<(PathBuf, Vec<u8>)>> {
    let mut writes = Vec::new();
    for (logical_path, _ref_name, node_id, _size) in rows {
        let bytes = read_node_bytes(conn, objects_dir, node_id)?;
        writes.push((PathBuf::from(logical_path), bytes));
    }
    Ok(writes)
}

fn ensure_worktree_output_writable(output_dir: &Path, overwrite: bool) -> AnfsResult<()> {
    if output_dir.exists() && !overwrite {
        return Err(AnfsError::PolicyDenied(format!(
            "worktree output {} already exists; pass overwrite=True to replace it",
            output_dir.display()
        )));
    }
    Ok(())
}

fn write_worktree_files(output_dir: &Path, writes: &[(PathBuf, Vec<u8>)]) -> AnfsResult<()> {
    for (relative_path, bytes) in writes {
        let output_path = output_dir.join(relative_path);
        if let Some(parent) = output_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(output_path, bytes)?;
    }
    Ok(())
}

fn write_worktree_manifest(
    output_dir: &Path,
    workspace: &str,
    rows: &[WorktreeMaterializeRow],
) -> AnfsResult<()> {
    let files: Vec<_> = rows
        .iter()
        .map(|(path, ref_name, node_id, size)| {
            json!({
                "path": path,
                "ref_name": ref_name,
                "node_id": node_id,
                "size": size,
            })
        })
        .collect();
    let manifest = json!({
        "schema": "anfs.worktree.v1",
        "workspace": workspace,
        "files": files,
    });
    fs::write(
        output_dir.join(WORKTREE_MANIFEST),
        serde_json::to_vec_pretty(&manifest).map_err(|err| {
            AnfsError::StorageCorruption(format!("failed to serialize worktree manifest: {err}"))
        })?,
    )?;
    Ok(())
}

fn scan_worktree_files(input_dir: &Path) -> AnfsResult<BTreeMap<String, Vec<u8>>> {
    let mut out = BTreeMap::new();
    scan_worktree_dir(input_dir, input_dir, &mut out)?;
    Ok(out)
}

fn scan_worktree_dir(
    root: &Path,
    dir: &Path,
    out: &mut BTreeMap<String, Vec<u8>>,
) -> AnfsResult<()> {
    for entry in fs::read_dir(dir)? {
        let entry = entry?;
        let path = entry.path();
        let relative = path.strip_prefix(root).map_err(|err| {
            AnfsError::PolicyDenied(format!(
                "failed to normalize worktree path {}: {err}",
                path.display()
            ))
        })?;
        if relative == Path::new(WORKTREE_MANIFEST) {
            continue;
        }
        let file_type = entry.file_type()?;
        if file_type.is_dir() {
            scan_worktree_dir(root, &path, out)?;
        } else if file_type.is_file() {
            let logical_path = relative_path_to_logical(relative)?;
            validate_relative_worktree_path(&logical_path)?;
            out.insert(logical_path, fs::read(&path)?);
        } else if file_type.is_symlink() {
            return Err(AnfsError::PolicyDenied(format!(
                "unsupported worktree symlink entry {}",
                path.display()
            )));
        } else {
            return Err(AnfsError::PolicyDenied(format!(
                "unsupported worktree special entry {}",
                path.display()
            )));
        }
    }
    Ok(())
}

fn workspace_logical_path_checked(workspace: &str, ref_name: &str) -> AnfsResult<String> {
    let prefix = format!("{}/", workspace.trim_end_matches('/'));
    ref_name
        .strip_prefix(&prefix)
        .filter(|path| !path.is_empty())
        .map(|path| path.to_string())
        .ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "workspace ref {ref_name} is not under workspace {workspace}"
            ))
        })
}

fn relative_path_to_logical(path: &Path) -> AnfsResult<String> {
    let mut parts = Vec::new();
    for component in path.components() {
        let std::path::Component::Normal(part) = component else {
            return Err(AnfsError::PolicyDenied(format!(
                "unsafe worktree path {}",
                path.display()
            )));
        };
        let Some(part) = part.to_str() else {
            return Err(AnfsError::PolicyDenied(format!(
                "non-utf8 worktree path {}",
                path.display()
            )));
        };
        parts.push(part);
    }
    Ok(parts.join("/"))
}

fn validate_relative_worktree_path(path: &str) -> AnfsResult<()> {
    if path.is_empty()
        || path.starts_with('/')
        || path == WORKTREE_MANIFEST
        || path.contains('\\')
        || path
            .split('/')
            .any(|segment| segment.is_empty() || segment == "." || segment == "..")
    {
        return Err(AnfsError::PolicyDenied(format!(
            "unsafe worktree relative path {path}"
        )));
    }
    Ok(())
}
