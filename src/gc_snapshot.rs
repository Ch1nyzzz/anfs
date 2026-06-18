use rusqlite::{params, Connection, OptionalExtension};
use serde_json::json;
use std::fs;
use std::path::{Path, PathBuf};

use crate::{
    all_chunked_chunk_hashes, file_blob_path, insert_edge, insert_event, insert_manifest_node,
    new_event_id, new_pin_id, node_manifest_metadata, now_millis, ref_prefix_like_pattern,
    ref_view_at_event, ref_view_checkpoints, require_ref, sha256_hex, upsert_ref, AnfsError,
    AnfsResult, GcPinRow, GcResultRow, ManifestChild, RefWriteMode, RetentionPolicyRow,
};

pub(crate) fn is_blob_gc_collected(conn: &Connection, hash: &str) -> AnfsResult<bool> {
    let found: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM gc_blob_events WHERE hash = ?1 LIMIT 1",
            params![hash],
            |row| row.get(0),
        )
        .optional()?;
    Ok(found.is_some())
}

pub(crate) fn pin_ref(
    conn: &mut Connection,
    ref_name: &str,
    reason: Option<&str>,
    agent_id: &str,
    run_id: Option<&str>,
) -> AnfsResult<String> {
    let tx = conn.transaction()?;
    let target =
        require_ref(&tx, ref_name)?;
    let pin_id = new_pin_id();
    let event_id = new_event_id();
    let payload = json!({
        "pin_id": &pin_id,
        "ref_name": ref_name,
        "node_id": &target.node_id,
        "reason": reason,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "gc_pin",
        Some(agent_id),
        run_id,
        None,
        None,
        Some(&payload),
    )?;
    insert_edge(
        &tx,
        &event_id,
        "input",
        &target.node_id,
        "gc_pin_root",
        Some(ref_name),
    )?;
    tx.execute(
        "INSERT INTO gc_pin_events
         (pin_id, event_id, action, ref_name, node_id, reason, created_at)
         VALUES (?1, ?2, 'pin', ?3, ?4, ?5, ?6)",
        params![
            pin_id,
            event_id,
            ref_name,
            target.node_id,
            reason,
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok(pin_id)
}

pub(crate) fn unpin_gc_root(
    conn: &mut Connection,
    pin_id: &str,
    agent_id: &str,
    run_id: Option<&str>,
) -> AnfsResult<()> {
    let tx = conn.transaction()?;
    let active_pin = active_gc_pin(&tx, pin_id)?.ok_or_else(|| {
        AnfsError::PolicyDenied(format!("gc pin {pin_id} is not currently active"))
    })?;
    let (_pin_id, _action, ref_name, node_id, reason, _created_at) = active_pin;
    let event_id = new_event_id();
    let payload = json!({
        "pin_id": pin_id,
        "ref_name": &ref_name,
        "node_id": &node_id,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "gc_unpin",
        Some(agent_id),
        run_id,
        None,
        None,
        Some(&payload),
    )?;
    insert_edge(
        &tx,
        &event_id,
        "input",
        &node_id,
        "gc_unpin_root",
        Some(&ref_name),
    )?;
    tx.execute(
        "INSERT INTO gc_pin_events
         (pin_id, event_id, action, ref_name, node_id, reason, created_at)
         VALUES (?1, ?2, 'unpin', ?3, ?4, ?5, ?6)",
        params![pin_id, event_id, ref_name, node_id, reason, now_millis()],
    )?;
    tx.commit()?;
    Ok(())
}

fn active_gc_pin(conn: &Connection, pin_id: &str) -> AnfsResult<Option<GcPinRow>> {
    let pin = conn
        .query_row(
            "
            WITH latest AS (
                SELECT gpe.pin_id, MAX(es.seq) AS seq
                FROM gc_pin_events gpe
                JOIN event_sequence es ON es.event_id = gpe.event_id
                WHERE gpe.pin_id = ?1
                GROUP BY gpe.pin_id
            )
            SELECT gpe.pin_id, gpe.action, gpe.ref_name, gpe.node_id,
                   gpe.reason, gpe.created_at
            FROM gc_pin_events gpe
            JOIN event_sequence es ON es.event_id = gpe.event_id
            JOIN latest l ON l.pin_id = gpe.pin_id AND l.seq = es.seq
            WHERE gpe.action = 'pin'
            ",
            params![pin_id],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, i64>(5)?,
                ))
            },
        )
        .optional()?;
    Ok(pin)
}

pub(crate) fn gc_pins(conn: &Connection, active_only: bool) -> AnfsResult<Vec<GcPinRow>> {
    let sql = if active_only {
        "
        WITH latest AS (
            SELECT gpe.pin_id, MAX(es.seq) AS seq
            FROM gc_pin_events gpe
            JOIN event_sequence es ON es.event_id = gpe.event_id
            GROUP BY gpe.pin_id
        )
        SELECT gpe.pin_id, gpe.action, gpe.ref_name, gpe.node_id,
               gpe.reason, gpe.created_at
        FROM gc_pin_events gpe
        JOIN event_sequence es ON es.event_id = gpe.event_id
        JOIN latest l ON l.pin_id = gpe.pin_id AND l.seq = es.seq
        WHERE gpe.action = 'pin'
        ORDER BY es.seq, gpe.pin_id
        "
    } else {
        "
        SELECT gpe.pin_id, gpe.action, gpe.ref_name, gpe.node_id,
               gpe.reason, gpe.created_at
        FROM gc_pin_events gpe
        JOIN event_sequence es ON es.event_id = gpe.event_id
        ORDER BY es.seq, gpe.pin_id
        "
    };
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, i64>(5)?,
        ))
    })?;

    let mut pins = Vec::new();
    for row in rows {
        pins.push(row?);
    }
    Ok(pins)
}

pub(crate) fn set_retention_policy(
    conn: &mut Connection,
    policy_name: &str,
    include_workspaces: bool,
    min_age_ms: Option<i64>,
    limit: Option<i64>,
    enabled: bool,
    agent_id: &str,
    run_id: Option<&str>,
) -> AnfsResult<()> {
    validate_retention_policy_name(policy_name)?;
    validate_min_age_ms(min_age_ms)?;
    let _ = normalize_gc_limit(limit)?;
    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let effect = if enabled { "enabled" } else { "disabled" };
    let payload = json!({
        "policy_name": policy_name,
        "effect": effect,
        "include_workspaces": include_workspaces,
        "min_age_ms": min_age_ms,
        "limit": limit,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "retention_policy",
        Some(agent_id),
        run_id,
        None,
        None,
        Some(&payload),
    )?;
    tx.execute(
        "INSERT INTO retention_policy_events
         (policy_name, effect, include_workspaces, min_age_ms, limit_count, event_id, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        params![
            policy_name,
            effect,
            i64::from(include_workspaces),
            min_age_ms,
            limit,
            event_id,
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok(())
}

pub(crate) fn retention_policies(
    conn: &Connection,
    active_only: bool,
) -> AnfsResult<Vec<RetentionPolicyRow>> {
    let sql = if active_only {
        "
        WITH latest AS (
            SELECT rpe.policy_name, MAX(es.seq) AS seq
            FROM retention_policy_events rpe
            JOIN event_sequence es ON es.event_id = rpe.event_id
            GROUP BY rpe.policy_name
        )
        SELECT rpe.policy_name, rpe.effect, rpe.include_workspaces,
               rpe.min_age_ms, rpe.limit_count, rpe.event_id, rpe.created_at
        FROM retention_policy_events rpe
        JOIN event_sequence es ON es.event_id = rpe.event_id
        JOIN latest l ON l.policy_name = rpe.policy_name AND l.seq = es.seq
        WHERE rpe.effect = 'enabled'
        ORDER BY rpe.policy_name
        "
    } else {
        "
        SELECT rpe.policy_name, rpe.effect, rpe.include_workspaces,
               rpe.min_age_ms, rpe.limit_count, rpe.event_id, rpe.created_at
        FROM retention_policy_events rpe
        JOIN event_sequence es ON es.event_id = rpe.event_id
        ORDER BY es.seq, rpe.policy_name
        "
    };
    let mut stmt = conn.prepare(sql)?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, i64>(2)? != 0,
            row.get::<_, Option<i64>>(3)?,
            row.get::<_, Option<i64>>(4)?,
            row.get::<_, String>(5)?,
            row.get::<_, i64>(6)?,
        ))
    })?;
    let mut policies = Vec::new();
    for row in rows {
        policies.push(row?);
    }
    Ok(policies)
}

pub(crate) fn run_retention_policy(
    conn: &mut Connection,
    objects_dir: &Path,
    policy_name: &str,
    dry_run: bool,
    agent_id: &str,
) -> AnfsResult<Vec<GcResultRow>> {
    validate_retention_policy_name(policy_name)?;
    let policy = active_retention_policy(conn, policy_name)?.ok_or_else(|| {
        AnfsError::PolicyDenied(format!("retention policy {policy_name} is not enabled"))
    })?;
    let (_name, _effect, include_workspaces, min_age_ms, limit, _event_id, _created_at) = policy;
    let older_than_ms = min_age_ms.map(|age| now_millis().saturating_sub(age));
    collect_garbage(
        conn,
        objects_dir,
        include_workspaces,
        dry_run,
        agent_id,
        older_than_ms,
        limit,
        Some(policy_name),
    )
}

fn active_retention_policy(
    conn: &Connection,
    policy_name: &str,
) -> AnfsResult<Option<RetentionPolicyRow>> {
    let row = conn
        .query_row(
            "
            WITH latest AS (
                SELECT rpe.policy_name, MAX(es.seq) AS seq
                FROM retention_policy_events rpe
                JOIN event_sequence es ON es.event_id = rpe.event_id
                WHERE rpe.policy_name = ?1
                GROUP BY rpe.policy_name
            )
            SELECT rpe.policy_name, rpe.effect, rpe.include_workspaces,
                   rpe.min_age_ms, rpe.limit_count, rpe.event_id, rpe.created_at
            FROM retention_policy_events rpe
            JOIN event_sequence es ON es.event_id = rpe.event_id
            JOIN latest l ON l.policy_name = rpe.policy_name AND l.seq = es.seq
            WHERE rpe.effect = 'enabled'
            ",
            params![policy_name],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)? != 0,
                    row.get::<_, Option<i64>>(3)?,
                    row.get::<_, Option<i64>>(4)?,
                    row.get::<_, String>(5)?,
                    row.get::<_, i64>(6)?,
                ))
            },
        )
        .optional()?;
    Ok(row)
}

pub(crate) fn snapshot_namespace(
    conn: &mut Connection,
    objects_dir: &Path,
    prefix: &str,
    snapshot_ref: &str,
    agent_id: &str,
    kind: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<String> {
    let normalized_prefix = prefix.trim_end_matches('/');
    if normalized_prefix.is_empty() {
        return Err(AnfsError::PolicyDenied(
            "snapshot namespace prefix must not be empty".to_string(),
        ));
    }

    let tx = conn.transaction()?;
    let mut children = Vec::new();
    {
        let like_pattern = ref_prefix_like_pattern(normalized_prefix);
        let mut stmt = tx.prepare(
            "
            SELECT ref_name, node_id
            FROM refs
            WHERE ref_name LIKE ?1 ESCAPE '\\'
              AND state NOT IN ('deleted', 'archived')
            ORDER BY ref_name
            ",
        )?;
        let rows = stmt.query_map(params![like_pattern], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;

        for row in rows {
            let (ref_name, node_id) = row?;
            let path = snapshot_relative_path(normalized_prefix, &ref_name)?;
            let (digest, media_type, size) = node_manifest_metadata(&tx, &node_id)?;
            children.push(ManifestChild {
                path,
                node_id,
                role: Some("snapshot_child".to_string()),
                media_type,
                digest: Some(digest),
                size: Some(size),
            });
        }
    }

    if children.is_empty() {
        return Err(AnfsError::PolicyDenied(format!(
            "snapshot namespace {normalized_prefix} has no active refs"
        )));
    }

    let child_count = children.len();
    let snapshot_edges: Vec<(String, String)> = children
        .iter()
        .map(|child| (child.path.clone(), child.node_id.clone()))
        .collect();
    let manifest_node_id = insert_manifest_node(
        &tx,
        objects_dir,
        kind,
        children,
        json!({
            "snapshot": true,
            "prefix": normalized_prefix,
            "snapshot_ref": snapshot_ref,
        }),
    )?;

    let event_id = new_event_id();
    let payload = json!({
        "prefix": normalized_prefix,
        "snapshot_ref": snapshot_ref,
        "child_count": child_count,
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "snapshot_namespace",
        Some(agent_id),
        run_id,
        tool_call_id,
        Some(normalized_prefix),
        Some(&payload),
    )?;
    for (idx, (path, child_node_id)) in snapshot_edges.iter().enumerate() {
        let role = format!("snapshot_child:{idx}");
        insert_edge(&tx, &event_id, "input", child_node_id, &role, Some(path))?;
    }
    insert_edge(
        &tx,
        &event_id,
        "output",
        &manifest_node_id,
        "snapshot_manifest",
        Some(snapshot_ref),
    )?;
    upsert_ref(
        &tx,
        snapshot_ref,
        &manifest_node_id,
        kind,
        "published",
        &event_id,
        RefWriteMode::PublishImmutable,
    )?;

    tx.commit()?;
    Ok(manifest_node_id)
}

fn snapshot_relative_path(prefix: &str, ref_name: &str) -> AnfsResult<String> {
    let prefix = prefix.trim_end_matches('/');
    let expected_prefix = format!("{prefix}/");
    let relative = ref_name.strip_prefix(&expected_prefix).ok_or_else(|| {
        AnfsError::StorageCorruption(format!(
            "ref {ref_name} is not inside snapshot prefix {prefix}"
        ))
    })?;
    if relative.is_empty() {
        return Err(AnfsError::StorageCorruption(format!(
            "ref {ref_name} has empty snapshot path"
        )));
    }
    Ok(relative.to_string())
}

pub(crate) fn collect_garbage(
    conn: &mut Connection,
    objects_dir: &Path,
    include_workspaces: bool,
    dry_run: bool,
    agent_id: &str,
    older_than_ms: Option<i64>,
    limit: Option<i64>,
    retention_policy: Option<&str>,
) -> AnfsResult<Vec<GcResultRow>> {
    let candidates = gc_candidates(conn, objects_dir, include_workspaces, older_than_ms, limit)?;
    if dry_run {
        return Ok(candidates
            .into_iter()
            .map(|(hash, size, storage_kind)| {
                let action = if storage_kind == "file" {
                    "would_collect_file"
                } else {
                    "skip_inline"
                };
                (hash, size, storage_kind, action.to_string())
            })
            .collect());
    }

    let tx = conn.transaction()?;
    let event_id = new_event_id();
    let payload = json!({
        "include_workspaces": include_workspaces,
        "older_than_ms": older_than_ms,
        "limit": limit,
        "retention_policy": retention_policy,
        "candidate_count": candidates.len(),
    })
    .to_string();
    insert_event(
        &tx,
        &event_id,
        "gc_collect",
        Some(agent_id),
        None,
        None,
        None,
        Some(&payload),
    )?;

    let mut results = Vec::new();
    for (hash, size, storage_kind) in candidates {
        if storage_kind != "file" {
            results.push((hash, size, storage_kind, "skip_inline".to_string()));
            continue;
        }

        let storage_uri: Option<String> = tx
            .query_row(
                "SELECT storage_uri FROM blobs WHERE hash = ?1",
                params![hash],
                |row| row.get(0),
            )
            .optional()?
            .flatten();
        let storage_uri = storage_uri.ok_or_else(|| {
            AnfsError::StorageCorruption(format!("file blob {hash} has no storage_uri"))
        })?;
        let path = PathBuf::from(&storage_uri);
        let path = if path.is_absolute() {
            path
        } else {
            objects_dir.join(path)
        };

        match fs::read(&path) {
            Ok(bytes) => {
                let actual_hash = sha256_hex(&bytes);
                if actual_hash != hash {
                    return Err(AnfsError::StorageCorruption(format!(
                        "refusing to collect blob {hash}: path {} has hash {actual_hash}",
                        path.display()
                    )));
                }
                fs::remove_file(&path)?;
                tx.execute(
                    "INSERT INTO gc_blob_events
                     (hash, event_id, storage_uri, size, created_at)
                     VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![hash, event_id, storage_uri, size, now_millis()],
                )?;
                results.push((hash, size, storage_kind, "collected_file".to_string()));
            }
            Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                tx.execute(
                    "INSERT INTO gc_blob_events
                     (hash, event_id, storage_uri, size, created_at)
                     VALUES (?1, ?2, ?3, ?4, ?5)",
                    params![hash, event_id, storage_uri, size, now_millis()],
                )?;
                results.push((hash, size, storage_kind, "already_missing".to_string()));
            }
            Err(err) => return Err(AnfsError::Io(err)),
        }
    }

    tx.commit()?;
    Ok(results)
}

pub(crate) fn gc_roots(
    conn: &Connection,
    include_workspaces: bool,
) -> AnfsResult<Vec<(String, String, String)>> {
    let include_workspaces = if include_workspaces { 1 } else { 0 };
    let mut stmt = conn.prepare(
        "
        WITH active_pins AS (
            SELECT gpe.ref_name, gpe.node_id
            FROM gc_pin_events gpe
            JOIN event_sequence es ON es.event_id = gpe.event_id
            JOIN (
                SELECT gpe2.pin_id, MAX(es2.seq) AS seq
                FROM gc_pin_events gpe2
                JOIN event_sequence es2 ON es2.event_id = gpe2.event_id
                GROUP BY gpe2.pin_id
            ) latest ON latest.pin_id = gpe.pin_id AND latest.seq = es.seq
            WHERE gpe.action = 'pin'
        )
        SELECT ref_name, node_id, state
        FROM refs
        WHERE state IN ('published', 'approved')
           OR (?1 = 1 AND ref_kind = 'workspace' AND state = 'draft')
        UNION
        SELECT ref_name, node_id, 'pinned'
        FROM active_pins
        ORDER BY ref_name
        ",
    )?;
    let rows = stmt.query_map(params![include_workspaces], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
        ))
    })?;

    let mut roots = Vec::new();
    for row in rows {
        roots.push(row?);
    }
    Ok(roots)
}

/// Blob hashes required to replay any recorded ref-view checkpoint. GC must
/// never collect these, or a recorded replay proof would no longer verify.
/// Computed by reusing the exact replay view, so it can never drift from what
/// `verify_ref_view_checkpoint` actually needs.
fn checkpoint_protected_hashes(
    conn: &Connection,
) -> AnfsResult<std::collections::HashSet<String>> {
    let mut protected = std::collections::HashSet::new();
    for checkpoint in ref_view_checkpoints(conn)? {
        let (_checkpoint_id, target_event_id, _target_seq, prefix, include_inactive, ..) =
            checkpoint;
        let view = ref_view_at_event(conn, &target_event_id, prefix.as_deref(), include_inactive, &[])?;
        for row in view {
            let node_id = row.1;
            let hash: Option<String> = conn
                .query_row(
                    "SELECT blob_hash FROM nodes WHERE node_id = ?1",
                    params![node_id],
                    |row| row.get(0),
                )
                .optional()?;
            if let Some(hash) = hash {
                protected.insert(hash);
            }
        }
    }
    Ok(protected)
}

pub(crate) fn gc_candidates(
    conn: &Connection,
    objects_dir: &Path,
    include_workspaces: bool,
    older_than_ms: Option<i64>,
    limit: Option<i64>,
) -> AnfsResult<Vec<(String, i64, String)>> {
    let limit = normalize_gc_limit(limit)?;
    // Protect both: blobs a recorded replay checkpoint needs, and chunk blobs
    // referenced only inside chunked manifest nodes (node->blob reachability
    // below cannot see chunk blobs, so GC would otherwise corrupt them).
    let mut protected = checkpoint_protected_hashes(conn)?;
    protected.extend(all_chunked_chunk_hashes(conn, objects_dir)?);
    let include_workspaces = if include_workspaces { 1 } else { 0 };
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE active_pins(node_id) AS (
            SELECT gpe.node_id
            FROM gc_pin_events gpe
            JOIN event_sequence es ON es.event_id = gpe.event_id
            JOIN (
                SELECT gpe2.pin_id, MAX(es2.seq) AS seq
                FROM gc_pin_events gpe2
                JOIN event_sequence es2 ON es2.event_id = gpe2.event_id
                GROUP BY gpe2.pin_id
            ) latest ON latest.pin_id = gpe.pin_id AND latest.seq = es.seq
            WHERE gpe.action = 'pin'
        ),
        roots(node_id) AS (
            SELECT node_id
            FROM refs
            WHERE state IN ('published', 'approved')
               OR (?1 = 1 AND ref_kind = 'workspace' AND state = 'draft')
            UNION
            SELECT node_id FROM active_pins
        ),
        reachable(node_id) AS (
            SELECT node_id FROM roots
            UNION
            SELECT e_in.node_id
            FROM reachable r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        )
        SELECT b.hash, b.size, b.storage_kind, b.storage_uri
        FROM blobs b
        WHERE NOT EXISTS (
            SELECT 1
            FROM nodes n
            JOIN reachable r ON r.node_id = n.node_id
            WHERE n.blob_hash = b.hash
        )
          AND (
            ?2 IS NULL
            OR (
                SELECT MAX(n.created_at)
                FROM nodes n
                WHERE n.blob_hash = b.hash
            ) <= ?2
          )
        ORDER BY b.hash
        ",
    )?;
    let rows = stmt.query_map(params![include_workspaces, older_than_ms], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, i64>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, Option<String>>(3)?,
        ))
    })?;

    let mut candidates = Vec::new();
    for row in rows {
        let (hash, size, storage_kind, storage_uri) = row?;
        // Never collect a blob a checkpoint or a chunked manifest still needs.
        if protected.contains(&hash) {
            continue;
        }
        if is_blob_gc_collected(conn, &hash)? {
            if storage_kind != "file" {
                continue;
            }
            let Some(uri) = storage_uri.as_deref() else {
                continue;
            };
            if !file_blob_path(objects_dir, uri).exists() {
                continue;
            }
        }
        candidates.push((hash, size, storage_kind));
        if limit > 0 && candidates.len() as i64 >= limit {
            break;
        }
    }
    Ok(candidates)
}

fn normalize_gc_limit(limit: Option<i64>) -> AnfsResult<i64> {
    match limit {
        Some(limit) if (1..=10000).contains(&limit) => Ok(limit),
        Some(_) => Err(AnfsError::PolicyDenied(
            "gc limit must be between 1 and 10000".to_string(),
        )),
        None => Ok(-1),
    }
}

fn validate_retention_policy_name(policy_name: &str) -> AnfsResult<()> {
    if policy_name.is_empty()
        || policy_name.len() > 128
        || !policy_name
            .chars()
            .all(|ch| ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' || ch == ':')
    {
        return Err(AnfsError::PolicyDenied(format!(
            "invalid retention policy name {policy_name}"
        )));
    }
    Ok(())
}

fn validate_min_age_ms(min_age_ms: Option<i64>) -> AnfsResult<()> {
    match min_age_ms {
        Some(value) if value < 0 => Err(AnfsError::PolicyDenied(
            "retention min_age_ms must be non-negative".to_string(),
        )),
        _ => Ok(()),
    }
}
