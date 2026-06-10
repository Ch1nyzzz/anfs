use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::json;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use uuid::Uuid;

use crate::{
    infer_ref_kind, insert_event, new_event_id, now_millis, read_node_bytes, sha256_hex, AnfsError,
    AnfsResult, MaterializedViewRow, RefViewCheckpointRow, RefViewCheckpointVerificationRow,
    RefViewRow, ReplayManifest, ReplayManifestFile, VisibilityPolicy,
};

pub(crate) fn ref_view_at_event(
    conn: &Connection,
    event_id: &str,
    prefix: Option<&str>,
    include_inactive: bool,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<RefViewRow>> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let event_seq: Option<i64> = conn
        .query_row(
            "SELECT seq FROM event_sequence WHERE event_id = ?1",
            params![event_id],
            |row| row.get(0),
        )
        .optional()?;
    let event_seq = event_seq.ok_or_else(|| AnfsError::EventNotFound(event_id.to_string()))?;

    let normalized_prefix = prefix.map(|value| value.trim_end_matches('/').to_string());
    let like_prefix = normalized_prefix
        .as_ref()
        .map(|value| ref_prefix_like_pattern(value));
    let include_inactive = if include_inactive { 1 } else { 0 };

    let mut stmt = conn.prepare(
        "
        WITH ranked AS (
            SELECT
                re.ref_name,
                re.new_node_id,
                re.new_state,
                re.event_id,
                e.created_at,
                ROW_NUMBER() OVER (
                    PARTITION BY re.ref_name
                    ORDER BY es.seq DESC, re.rowid DESC
                ) AS rank
            FROM ref_events re
            JOIN events e ON e.event_id = re.event_id
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE es.seq <= ?1
              AND (?2 IS NULL OR re.ref_name = ?2 OR re.ref_name LIKE ?3)
        )
        SELECT ref_name, new_node_id, new_state, event_id, created_at
        FROM ranked
        WHERE rank = 1
          AND new_node_id IS NOT NULL
          AND (?4 = 1 OR new_state NOT IN ('deleted', 'archived'))
        ORDER BY ref_name
        ",
    )?;
    let rows = stmt.query_map(
        params![
            event_seq,
            normalized_prefix.as_deref(),
            like_prefix.as_deref(),
            include_inactive
        ],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, i64>(4)?,
            ))
        },
    )?;

    let mut view = Vec::new();
    for row in rows {
        let (ref_name, node_id, state, source_event_id, created_at) = row?;
        if visibility.ref_node_blocked(conn, &ref_name, &node_id)? {
            continue;
        }
        let ref_kind = infer_ref_kind(&ref_name).to_string();
        view.push((
            ref_name,
            node_id,
            ref_kind,
            state,
            source_event_id,
            created_at,
        ));
    }
    Ok(view)
}

pub(crate) fn materialize_ref_view_at_event(
    conn: &Connection,
    objects_dir: &Path,
    event_id: &str,
    output_dir: &Path,
    prefix: Option<&str>,
    include_inactive: bool,
    overwrite: bool,
    atomic: bool,
    policy_label_excludes: &[String],
) -> AnfsResult<Vec<MaterializedViewRow>> {
    let view = ref_view_at_event(
        conn,
        event_id,
        prefix,
        include_inactive,
        policy_label_excludes,
    )?;
    let mut materialized = Vec::new();
    let mut writes = Vec::new();
    let mut relative_paths: BTreeMap<PathBuf, String> = BTreeMap::new();

    for (ref_name, node_id, _ref_kind, state, _source_event_id, _created_at) in view {
        if !include_inactive && (state == "deleted" || state == "archived") {
            continue;
        }

        let relative_path = materialized_relative_path(&ref_name, prefix)?;
        if let Some(existing_ref) = relative_paths.insert(relative_path.clone(), ref_name.clone()) {
            return Err(AnfsError::PolicyDenied(format!(
                "materialized relative path {} is produced by both {} and {}",
                relative_path.display(),
                existing_ref,
                ref_name
            )));
        }
        let bytes = read_node_bytes(conn, objects_dir, &node_id)?;
        let size = bytes.len() as i64;
        materialized.push((
            ref_name,
            relative_path.to_string_lossy().to_string(),
            node_id,
            size,
        ));
        writes.push((relative_path, bytes));
    }

    ensure_materialization_targets_writable(output_dir, &writes, overwrite)?;
    if atomic && (!output_dir.exists() || overwrite) {
        materialize_via_staging(
            output_dir,
            &writes,
            event_id,
            prefix,
            include_inactive,
            &materialized,
            overwrite,
        )?;
    } else {
        if overwrite && output_dir.exists() {
            fs::remove_dir_all(output_dir)?;
        }
        fs::create_dir_all(output_dir)?;
        write_materialized_files(output_dir, &writes)?;
        write_replay_manifest(
            output_dir,
            event_id,
            prefix,
            include_inactive,
            &materialized,
        )?;
    }

    Ok(materialized)
}

pub(crate) fn create_ref_view_checkpoint(
    tx: &Transaction<'_>,
    event_id: Option<&str>,
    prefix: Option<&str>,
    include_inactive: bool,
    agent_id: &str,
) -> AnfsResult<RefViewCheckpointRow> {
    let (target_event_id, target_seq) = resolve_checkpoint_target_event(tx, event_id)?;
    let prefix = normalize_checkpoint_prefix(prefix);
    let view = ref_view_at_event(
        tx,
        &target_event_id,
        prefix.as_deref(),
        include_inactive,
        &[],
    )?;
    let checksum = ref_view_checkpoint_checksum(
        &target_event_id,
        target_seq,
        prefix.as_deref(),
        include_inactive,
        &view,
    )?;
    let checkpoint_id = new_event_id();
    let ref_count = view.len() as i64;
    let payload = json!({
        "schema": "anfs.ref_view_checkpoint.v1",
        "checkpoint_id": checkpoint_id,
        "target_event_id": target_event_id,
        "target_seq": target_seq,
        "prefix": prefix.clone(),
        "include_inactive": include_inactive,
        "ref_count": ref_count,
        "checksum": checksum,
    })
    .to_string();
    insert_event(
        tx,
        &checkpoint_id,
        "ref_view_checkpoint",
        Some(agent_id),
        None,
        None,
        None,
        Some(&payload),
    )?;
    let created_at = now_millis();
    tx.execute(
        "INSERT INTO ref_view_checkpoints
         (checkpoint_id, target_event_id, target_seq, prefix, include_inactive,
          ref_count, checksum, agent_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            &checkpoint_id,
            &target_event_id,
            target_seq,
            prefix.as_deref(),
            if include_inactive { 1 } else { 0 },
            ref_count,
            &checksum,
            agent_id,
            created_at
        ],
    )?;
    Ok((
        checkpoint_id,
        target_event_id,
        target_seq,
        prefix,
        include_inactive,
        ref_count,
        checksum,
        Some(agent_id.to_string()),
        created_at,
    ))
}

pub(crate) fn ref_view_checkpoints(conn: &Connection) -> AnfsResult<Vec<RefViewCheckpointRow>> {
    let mut stmt = conn.prepare(
        "
        SELECT checkpoint_id, target_event_id, target_seq, prefix,
               include_inactive, ref_count, checksum, agent_id, created_at
        FROM ref_view_checkpoints
        ORDER BY created_at, checkpoint_id
        ",
    )?;
    let rows = stmt.query_map([], |row| {
        let include_inactive: i64 = row.get(4)?;
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)?,
            row.get::<_, Option<String>>(3)?,
            include_inactive != 0,
            row.get::<_, i64>(5)?,
            row.get::<_, String>(6)?,
            row.get::<_, Option<String>>(7)?,
            row.get::<_, i64>(8)?,
        ))
    })?;
    let mut checkpoints = Vec::new();
    for row in rows {
        checkpoints.push(row?);
    }
    Ok(checkpoints)
}

pub(crate) fn verify_ref_view_checkpoint(
    conn: &Connection,
    checkpoint_id: &str,
) -> AnfsResult<RefViewCheckpointVerificationRow> {
    let checkpoint: Option<(String, i64, Option<String>, bool, i64, String)> = conn
        .query_row(
            "
            SELECT target_event_id, target_seq, prefix, include_inactive,
                   ref_count, checksum
            FROM ref_view_checkpoints
            WHERE checkpoint_id = ?1
            ",
            params![checkpoint_id],
            |row| {
                let include_inactive: i64 = row.get(3)?;
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    include_inactive != 0,
                    row.get::<_, i64>(4)?,
                    row.get::<_, String>(5)?,
                ))
            },
        )
        .optional()?;
    let (target_event_id, target_seq, prefix, include_inactive, expected_count, expected_checksum) =
        checkpoint.ok_or_else(|| AnfsError::EventNotFound(checkpoint_id.to_string()))?;
    let view = ref_view_at_event(
        conn,
        &target_event_id,
        prefix.as_deref(),
        include_inactive,
        &[],
    )?;
    let actual_count = view.len() as i64;
    let actual_checksum = ref_view_checkpoint_checksum(
        &target_event_id,
        target_seq,
        prefix.as_deref(),
        include_inactive,
        &view,
    )?;
    Ok((
        checkpoint_id.to_string(),
        expected_checksum == actual_checksum && expected_count == actual_count,
        expected_checksum,
        actual_checksum,
        expected_count,
        actual_count,
    ))
}

fn resolve_checkpoint_target_event(
    conn: &Connection,
    event_id: Option<&str>,
) -> AnfsResult<(String, i64)> {
    if let Some(event_id) = event_id {
        let target: Option<(String, i64)> = conn
            .query_row(
                "SELECT event_id, seq FROM event_sequence WHERE event_id = ?1",
                params![event_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()?;
        return target.ok_or_else(|| AnfsError::EventNotFound(event_id.to_string()));
    }

    let target: Option<(String, i64)> = conn
        .query_row(
            "
            SELECT e.event_id, es.seq
            FROM events e
            JOIN event_sequence es ON es.event_id = e.event_id
            ORDER BY es.seq DESC
            LIMIT 1
            ",
            [],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    target.ok_or_else(|| AnfsError::EventNotFound("ref view checkpoint target".to_string()))
}

fn normalize_checkpoint_prefix(prefix: Option<&str>) -> Option<String> {
    prefix
        .map(|value| value.trim().trim_end_matches('/').to_string())
        .filter(|value| !value.is_empty())
}

fn ref_view_checkpoint_checksum(
    target_event_id: &str,
    target_seq: i64,
    prefix: Option<&str>,
    include_inactive: bool,
    view: &[RefViewRow],
) -> AnfsResult<String> {
    let payload = json!({
        "schema": "anfs.ref_view_checkpoint.digest.v1",
        "target_event_id": target_event_id,
        "target_seq": target_seq,
        "prefix": prefix,
        "include_inactive": include_inactive,
        "refs": view,
    });
    let bytes = serde_json::to_vec(&payload).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "failed to serialize ref view checkpoint digest: {err}"
        ))
    })?;
    Ok(sha256_hex(&bytes))
}

fn ensure_materialization_targets_writable(
    output_dir: &Path,
    writes: &[(PathBuf, Vec<u8>)],
    overwrite: bool,
) -> AnfsResult<()> {
    if overwrite {
        return Ok(());
    }
    let manifest_path = output_dir.join(".anfs-replay-manifest.json");
    if manifest_path.exists() {
        return Err(AnfsError::PolicyDenied(format!(
            "replay manifest {} already exists; pass overwrite=True to replace it",
            manifest_path.display()
        )));
    }
    for (relative_path, _bytes) in writes {
        let target_path = output_dir.join(relative_path);
        if target_path.exists() {
            return Err(AnfsError::PolicyDenied(format!(
                "materialized target {} already exists; pass overwrite=True to replace it",
                target_path.display()
            )));
        }
    }
    Ok(())
}

fn materialize_via_staging(
    output_dir: &Path,
    writes: &[(PathBuf, Vec<u8>)],
    event_id: &str,
    prefix: Option<&str>,
    include_inactive: bool,
    materialized: &[MaterializedViewRow],
    overwrite: bool,
) -> AnfsResult<()> {
    let parent = output_dir.parent().unwrap_or_else(|| Path::new("."));
    fs::create_dir_all(parent)?;
    let output_name = output_dir
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("replay");
    let staging_dir = parent.join(format!(".{output_name}.anfs-staging-{}", Uuid::new_v4()));
    fs::create_dir_all(&staging_dir)?;

    let result = (|| -> AnfsResult<()> {
        write_materialized_files(&staging_dir, writes)?;
        write_replay_manifest(
            &staging_dir,
            event_id,
            prefix,
            include_inactive,
            materialized,
        )?;
        if output_dir.exists() {
            if !overwrite {
                return Err(AnfsError::PolicyDenied(format!(
                    "replay directory {} already exists; pass overwrite=True to replace it",
                    output_dir.display()
                )));
            }
            fs::remove_dir_all(output_dir)?;
        }
        fs::rename(&staging_dir, output_dir)?;
        Ok(())
    })();

    if result.is_err() {
        let _ = fs::remove_dir_all(&staging_dir);
    }
    result
}

fn write_materialized_files(output_dir: &Path, writes: &[(PathBuf, Vec<u8>)]) -> AnfsResult<()> {
    for (relative_path, bytes) in writes {
        let target_path = output_dir.join(relative_path);
        if let Some(parent) = target_path.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(target_path, bytes)?;
    }
    Ok(())
}

fn write_replay_manifest(
    output_dir: &Path,
    event_id: &str,
    prefix: Option<&str>,
    include_inactive: bool,
    materialized: &[MaterializedViewRow],
) -> AnfsResult<()> {
    let files = materialized
        .iter()
        .map(
            |(ref_name, relative_path, node_id, size)| ReplayManifestFile {
                ref_name: ref_name.clone(),
                relative_path: relative_path.clone(),
                node_id: node_id.clone(),
                size: *size,
            },
        )
        .collect();
    let manifest = ReplayManifest {
        schema: "anfs.replay_manifest.v1".to_string(),
        event_id: event_id.to_string(),
        prefix: prefix.map(ToString::to_string),
        include_inactive,
        materialized_at: now_millis(),
        files,
    };
    let bytes = serde_json::to_vec_pretty(&manifest).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize replay manifest: {err}"))
    })?;
    fs::write(output_dir.join(".anfs-replay-manifest.json"), bytes)?;
    Ok(())
}

fn materialized_relative_path(ref_name: &str, prefix: Option<&str>) -> AnfsResult<PathBuf> {
    let logical_path = match prefix {
        Some(prefix) => {
            let prefix = prefix.trim_end_matches('/');
            if ref_name == prefix {
                "__ref__".to_string()
            } else {
                let child = if prefix.ends_with(':') {
                    ref_name.strip_prefix(prefix)
                } else {
                    let child_prefix = format!("{prefix}/");
                    ref_name.strip_prefix(&child_prefix)
                };
                child
                    .ok_or_else(|| {
                        AnfsError::StorageCorruption(format!(
                            "ref {ref_name} is outside materialize prefix {prefix}"
                        ))
                    })?
                    .to_string()
            }
        }
        None => ref_name.to_string(),
    };

    let mut path = PathBuf::new();
    for segment in logical_path.split('/') {
        if segment.is_empty() || segment == "." || segment == ".." || segment.contains('\\') {
            return Err(AnfsError::PolicyDenied(format!(
                "unsafe materialized path segment in {logical_path}"
            )));
        }
        path.push(segment);
    }
    if path.is_absolute() {
        return Err(AnfsError::PolicyDenied(format!(
            "unsafe absolute materialized path {logical_path}"
        )));
    }
    Ok(path)
}

pub(crate) fn ref_prefix_like_pattern(prefix: &str) -> String {
    if prefix.ends_with(':') {
        format!("{prefix}%")
    } else {
        format!("{prefix}/%")
    }
}
