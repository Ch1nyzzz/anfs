use rusqlite::{params, Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

use crate::{
    events::insert_event_sequence_if_missing, insert_blob, materialize_blob, now_millis,
    object_path, query::collect_rows, read_blob_bytes, read_node_bytes, sha256_hex, AnfsError,
    AnfsResult, ArchivalReadinessRow, BundleBlob, BundleEvent, BundleEventEdge, BundleNode,
    BundleRefEvent, BundleSignature, EventBundle, ExportBundleResult, ImportBundleResult,
    VisibilityPolicy, MANIFEST_MEDIA_TYPE,
};

pub(crate) fn export_event_bundle(
    conn: &Connection,
    objects_dir: &Path,
    event_id: &str,
    output_dir: &Path,
    include_blobs: bool,
    policy_label_excludes: &[String],
    signing_key: Option<&str>,
    signer_id: Option<&str>,
) -> AnfsResult<ExportBundleResult> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let root_seq: Option<i64> = conn
        .query_row(
            "SELECT seq FROM event_sequence WHERE event_id = ?1",
            params![event_id],
            |row| row.get(0),
        )
        .optional()?;
    let root_seq = root_seq.ok_or_else(|| AnfsError::EventNotFound(event_id.to_string()))?;

    let events = bundle_events(conn, event_id, root_seq)?;
    let event_edges = bundle_event_edges(conn, event_id, root_seq)?;
    let nodes = bundle_nodes(conn, event_id, root_seq)?;
    let blobs = bundle_blobs(conn, event_id, root_seq)?;
    let ref_events = bundle_ref_events(conn, event_id, root_seq)?;
    ensure_bundle_policy_labels_allowed(conn, &nodes, &ref_events, &visibility)?;

    write_bundle(
        conn,
        objects_dir,
        event_id,
        output_dir,
        include_blobs,
        events,
        event_edges,
        nodes,
        blobs,
        ref_events,
        signing_key,
        signer_id,
    )
}

pub(crate) fn export_run_bundle(
    conn: &Connection,
    objects_dir: &Path,
    run_id: &str,
    output_dir: &Path,
    include_blobs: bool,
    policy_label_excludes: &[String],
    signing_key: Option<&str>,
    signer_id: Option<&str>,
) -> AnfsResult<ExportBundleResult> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let root: Option<(String, i64)> = conn
        .query_row(
            "
            SELECT e.event_id, es.seq
            FROM events e
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1
            ORDER BY es.seq DESC
            LIMIT 1
            ",
            params![run_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?;
    let (root_event_id, root_seq) =
        root.ok_or_else(|| AnfsError::EventNotFound(format!("run_id {run_id}")))?;

    let events = run_bundle_events(conn, run_id, root_seq)?;
    let event_edges = run_bundle_event_edges(conn, run_id, root_seq)?;
    let nodes = run_bundle_nodes(conn, run_id, root_seq)?;
    let blobs = run_bundle_blobs(conn, run_id, root_seq)?;
    let ref_events = run_bundle_ref_events(conn, run_id, root_seq)?;
    ensure_bundle_policy_labels_allowed(conn, &nodes, &ref_events, &visibility)?;

    write_bundle(
        conn,
        objects_dir,
        &root_event_id,
        output_dir,
        include_blobs,
        events,
        event_edges,
        nodes,
        blobs,
        ref_events,
        signing_key,
        signer_id,
    )
}

pub(crate) fn export_history_archive(
    conn: &Connection,
    objects_dir: &Path,
    output_dir: &Path,
    include_blobs: bool,
    policy_label_excludes: &[String],
    signing_key: Option<&str>,
    signer_id: Option<&str>,
) -> AnfsResult<ExportBundleResult> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let root: Option<(String, i64)> = conn
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
    let (root_event_id, root_seq) =
        root.ok_or_else(|| AnfsError::EventNotFound("history archive root".to_string()))?;

    let events = history_archive_events(conn, root_seq)?;
    let event_edges = history_archive_event_edges(conn, root_seq)?;
    let nodes = history_archive_nodes(conn, root_seq)?;
    let blobs = history_archive_blobs(conn, root_seq)?;
    let ref_events = history_archive_ref_events(conn, root_seq)?;
    ensure_bundle_policy_labels_allowed(conn, &nodes, &ref_events, &visibility)?;

    write_bundle(
        conn,
        objects_dir,
        &root_event_id,
        output_dir,
        include_blobs,
        events,
        event_edges,
        nodes,
        blobs,
        ref_events,
        signing_key,
        signer_id,
    )
}

fn write_bundle(
    conn: &Connection,
    objects_dir: &Path,
    root_event_id: &str,
    output_dir: &Path,
    include_blobs: bool,
    events: Vec<BundleEvent>,
    event_edges: Vec<BundleEventEdge>,
    nodes: Vec<BundleNode>,
    mut blobs: Vec<BundleBlob>,
    ref_events: Vec<BundleRefEvent>,
    signing_key: Option<&str>,
    signer_id: Option<&str>,
) -> AnfsResult<ExportBundleResult> {
    let signing_key = validate_bundle_signing_key(signing_key)?;
    let signer_id = validate_bundle_signer_id(signer_id)?;
    fs::create_dir_all(output_dir)?;

    if include_blobs {
        let object_root = output_dir.join("objects");
        for blob in &mut blobs {
            let bytes = read_blob_bytes(conn, objects_dir, &blob.hash)?;
            let object_path = object_path(&object_root, &blob.hash);
            if let Some(parent) = object_path.parent() {
                fs::create_dir_all(parent)?;
            }
            fs::write(&object_path, &bytes)?;
            blob.object_path = Some(
                object_path
                    .strip_prefix(output_dir)
                    .unwrap_or(&object_path)
                    .to_string_lossy()
                    .to_string(),
            );
        }
    }

    let mut bundle = EventBundle {
        schema: "anfs.event_bundle.v1".to_string(),
        root_event_id: root_event_id.to_string(),
        exported_at: now_millis(),
        bundle_checksum: None,
        bundle_signature: None,
        events,
        event_edges,
        nodes,
        blobs,
        ref_events,
    };
    let event_count = bundle.events.len() as i64;
    let node_count = bundle.nodes.len() as i64;
    let blob_count = bundle.blobs.len() as i64;
    let bundle_path = output_dir.join("bundle.json");
    let canonical_bundle_bytes = serde_json::to_vec_pretty(&bundle).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize event bundle: {err}"))
    })?;
    let payload_sha256 = sha256_hex(&canonical_bundle_bytes);
    bundle.bundle_checksum = Some(payload_sha256.clone());
    if let Some(signing_key) = signing_key {
        bundle.bundle_signature = Some(BundleSignature {
            scheme: "hmac-sha256".to_string(),
            signer_id: signer_id.map(str::to_string),
            payload_sha256,
            signature: hmac_sha256_hex(signing_key.as_bytes(), &canonical_bundle_bytes),
        });
    }
    let bundle_bytes = serde_json::to_vec_pretty(&bundle).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize event bundle: {err}"))
    })?;
    fs::write(&bundle_path, bundle_bytes)?;

    Ok((
        bundle_path.to_string_lossy().to_string(),
        event_count,
        node_count,
        blob_count,
    ))
}

fn ensure_bundle_policy_labels_allowed(
    conn: &Connection,
    nodes: &[BundleNode],
    ref_events: &[BundleRefEvent],
    visibility: &VisibilityPolicy<'_>,
) -> AnfsResult<()> {
    if visibility.is_unrestricted_for(conn)? {
        return Ok(());
    }

    for node in nodes {
        visibility.ensure_ref_node_visible(conn, "", &node.node_id, "bundle export")?;
    }

    for ref_event in ref_events {
        for node_id in [&ref_event.old_node_id, &ref_event.new_node_id]
            .into_iter()
            .flatten()
        {
            visibility.ensure_ref_node_visible(
                conn,
                &ref_event.ref_name,
                node_id,
                "bundle export",
            )?;
        }
    }

    Ok(())
}

pub(crate) fn import_event_bundle(
    conn: &mut Connection,
    objects_dir: &Path,
    bundle_path: &Path,
    bundle_objects_dir: Option<&Path>,
    policy_label_excludes: &[String],
    signature_key: Option<&str>,
    require_signature: bool,
) -> AnfsResult<ImportBundleResult> {
    let visibility = VisibilityPolicy::new(policy_label_excludes)?;
    let bundle_bytes = fs::read(bundle_path)?;
    let mut bundle: EventBundle = serde_json::from_slice(&bundle_bytes).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "failed to parse event bundle {}: {err}",
            bundle_path.display()
        ))
    })?;
    if bundle.schema != "anfs.event_bundle.v1" {
        return Err(AnfsError::StorageCorruption(format!(
            "unsupported event bundle schema {}",
            bundle.schema
        )));
    }
    verify_bundle_authentication(&mut bundle, signature_key, require_signature)?;
    validate_event_bundle_references(&bundle)?;
    ensure_import_bundle_policy_labels_allowed(conn, &bundle, &visibility)?;

    let object_source_root = bundle_objects_dir
        .map(Path::to_path_buf)
        .or_else(|| bundle_path.parent().map(|parent| parent.join("objects")));

    let tx = conn.transaction()?;
    for blob in &bundle.blobs {
        if blob.object_path.is_none() && existing_bundle_blob_matches(&tx, objects_dir, blob)? {
            continue;
        }
        let bytes = import_bundle_blob_bytes(blob, object_source_root.as_deref())?;
        let actual_hash = sha256_hex(&bytes);
        if actual_hash != blob.hash {
            return Err(AnfsError::StorageCorruption(format!(
                "bundle object hash mismatch for {}: got {actual_hash}",
                blob.hash
            )));
        }
        if bytes.len() as i64 != blob.size {
            return Err(AnfsError::StorageCorruption(format!(
                "bundle object size mismatch for {}: expected {} got {}",
                blob.hash,
                blob.size,
                bytes.len()
            )));
        }
        let storage = materialize_blob(objects_dir, &blob.hash, &bytes)?;
        insert_blob(&tx, &blob.hash, blob.size, &storage)?;
    }

    for node in &bundle.nodes {
        tx.execute(
            "INSERT OR IGNORE INTO nodes
             (node_id, blob_hash, kind, media_type, metadata_json, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                node.node_id,
                node.blob_hash,
                node.kind,
                node.media_type,
                node.metadata_json,
                node.created_at
            ],
        )?;
        if tx.changes() == 0 {
            ensure_imported_node_matches(&tx, node)?;
        }
        ensure_imported_node_fts(&tx, objects_dir, node)?;
    }

    let event_order = ordered_bundle_event_indices(&bundle.events);
    for event_idx in event_order {
        let event = &bundle.events[event_idx];
        tx.execute(
            "INSERT OR IGNORE INTO events
             (event_id, kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                event.event_id,
                event.kind,
                event.agent_id,
                event.run_id,
                event.tool_call_id,
                event.workspace_id,
                event.payload_json,
                event.created_at
            ],
        )?;
        if tx.changes() == 0 {
            ensure_imported_event_matches(&tx, event)?;
            insert_event_sequence_if_missing(&tx, &event.event_id)?;
        } else {
            insert_event_sequence_if_missing(&tx, &event.event_id)?;
        }
    }

    for edge in &bundle.event_edges {
        tx.execute(
            "INSERT OR IGNORE INTO event_edges
             (event_id, direction, node_id, role, logical_path)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                edge.event_id,
                edge.direction,
                edge.node_id,
                edge.role,
                edge.logical_path
            ],
        )?;
        if tx.changes() == 0 {
            ensure_imported_event_edge_matches(&tx, edge)?;
        }
    }

    for ref_event in &bundle.ref_events {
        tx.execute(
            "INSERT OR IGNORE INTO ref_events
             (ref_name, event_id, old_node_id, new_node_id, old_state, new_state)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![
                ref_event.ref_name,
                ref_event.event_id,
                ref_event.old_node_id,
                ref_event.new_node_id,
                ref_event.old_state,
                ref_event.new_state
            ],
        )?;
        if tx.changes() == 0 {
            ensure_imported_ref_event_matches(&tx, ref_event)?;
        }
    }

    tx.commit()?;
    Ok((
        bundle.root_event_id,
        bundle.events.len() as i64,
        bundle.nodes.len() as i64,
        bundle.blobs.len() as i64,
    ))
}

pub(crate) fn verify_history_archive_bundle(
    conn: &Connection,
    bundle_path: &Path,
    signature_key: Option<&str>,
    require_signature: bool,
) -> AnfsResult<ArchivalReadinessRow> {
    let result = (|| -> AnfsResult<(String, i64)> {
        let bundle_bytes = fs::read(bundle_path)?;
        let mut bundle: EventBundle = serde_json::from_slice(&bundle_bytes).map_err(|err| {
            AnfsError::StorageCorruption(format!(
                "failed to parse event bundle {}: {err}",
                bundle_path.display()
            ))
        })?;
        if bundle.schema != "anfs.event_bundle.v1" {
            return Err(AnfsError::StorageCorruption(format!(
                "unsupported event bundle schema {}",
                bundle.schema
            )));
        }
        verify_bundle_authentication(&mut bundle, signature_key, require_signature)?;
        validate_event_bundle_references(&bundle)?;

        let current_events = current_archive_events(conn)?;
        if current_events.is_empty() {
            return Err(AnfsError::EventNotFound("history archive root".to_string()));
        }
        let bundle_events: Vec<(String, Option<i64>)> = bundle
            .events
            .iter()
            .map(|event| (event.event_id.clone(), event.source_seq))
            .collect();
        let expected_events: Vec<(String, Option<i64>)> = current_events
            .iter()
            .map(|(event_id, seq)| (event_id.clone(), Some(*seq)))
            .collect();
        if bundle_events != expected_events {
            return Ok((
                format!(
                    "bundle event history mismatch: expected {} current events, got {} bundle events",
                    expected_events.len(),
                    bundle_events.len()
                ),
                bundle_events.len() as i64,
            ));
        }
        let latest_event_id = current_events.last().map(|(event_id, _)| event_id).unwrap();
        if bundle.root_event_id.as_str() != latest_event_id.as_str() {
            return Ok((
                format!(
                    "bundle root_event_id {} is not current latest event {}",
                    bundle.root_event_id, latest_event_id
                ),
                bundle.events.len() as i64,
            ));
        }

        let expected_edges = current_archive_event_edges(conn)?;
        let bundle_edges: Vec<(String, String, String, String, Option<String>)> = bundle
            .event_edges
            .iter()
            .map(|edge| {
                (
                    edge.event_id.clone(),
                    edge.direction.clone(),
                    edge.node_id.clone(),
                    edge.role.clone(),
                    edge.logical_path.clone(),
                )
            })
            .collect();
        if bundle_edges != expected_edges {
            return Ok((
                format!(
                    "bundle event edge mismatch: expected {} current edges, got {} bundle edges",
                    expected_edges.len(),
                    bundle_edges.len()
                ),
                bundle.events.len() as i64,
            ));
        }

        let expected_ref_events = current_archive_ref_events(conn)?;
        let bundle_ref_events: Vec<(
            String,
            String,
            Option<String>,
            Option<String>,
            Option<String>,
            Option<String>,
        )> = bundle
            .ref_events
            .iter()
            .map(|ref_event| {
                (
                    ref_event.ref_name.clone(),
                    ref_event.event_id.clone(),
                    ref_event.old_node_id.clone(),
                    ref_event.new_node_id.clone(),
                    ref_event.old_state.clone(),
                    ref_event.new_state.clone(),
                )
            })
            .collect();
        if bundle_ref_events != expected_ref_events {
            return Ok((
                format!(
                    "bundle ref audit mismatch: expected {} current ref events, got {} bundle ref events",
                    expected_ref_events.len(),
                    bundle_ref_events.len()
                ),
                bundle.events.len() as i64,
            ));
        }

        Ok((
            format!(
                "history archive bundle covers {} events, {} event edges, and {} ref audit rows",
                bundle.events.len(),
                bundle.event_edges.len(),
                bundle.ref_events.len()
            ),
            bundle.events.len() as i64,
        ))
    })();

    Ok(match result {
        Ok((detail, count)) => (
            "history_archive_bundle".to_string(),
            detail.starts_with("history archive bundle covers"),
            detail,
            count,
        ),
        Err(err) => (
            "history_archive_bundle".to_string(),
            false,
            format!("history archive bundle verification failed: {err:?}"),
            0,
        ),
    })
}

fn ensure_import_bundle_policy_labels_allowed(
    conn: &Connection,
    bundle: &EventBundle,
    visibility: &VisibilityPolicy<'_>,
) -> AnfsResult<()> {
    if visibility.is_unrestricted_for(conn)? {
        return Ok(());
    }

    for node in &bundle.nodes {
        if visibility.ref_node_blocked(conn, "", &node.node_id)? {
            return Err(AnfsError::PolicyDenied(format!(
                "bundle import blocked by policy label on node {}",
                node.node_id
            )));
        }
    }

    for ref_event in &bundle.ref_events {
        for node_id in [&ref_event.old_node_id, &ref_event.new_node_id]
            .into_iter()
            .flatten()
        {
            visibility.ensure_ref_node_visible(
                conn,
                &ref_event.ref_name,
                node_id,
                "bundle import",
            )?;
        }
    }

    Ok(())
}

fn verify_bundle_authentication(
    bundle: &mut EventBundle,
    signature_key: Option<&str>,
    require_signature: bool,
) -> AnfsResult<()> {
    let signature_key = validate_bundle_signing_key(signature_key)?;
    if require_signature && signature_key.is_none() {
        return Err(AnfsError::PolicyDenied(
            "signature_key is required when require_signature is true".to_string(),
        ));
    }
    let expected_checksum = bundle.bundle_checksum.take();
    let bundle_signature = bundle.bundle_signature.take();
    let canonical_bytes = serde_json::to_vec_pretty(bundle).map_err(|err| {
        AnfsError::StorageCorruption(format!(
            "failed to serialize event bundle for checksum verification: {err}"
        ))
    })?;
    let actual = sha256_hex(&canonical_bytes);
    if let Some(expected_checksum) = expected_checksum {
        if actual != expected_checksum {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle checksum mismatch: expected={expected_checksum} actual={actual}"
            )));
        }
    }
    match bundle_signature {
        Some(signature) => {
            verify_bundle_signature(&signature, signature_key, &actual, &canonical_bytes)?;
        }
        None if require_signature => {
            return Err(AnfsError::StorageCorruption(
                "event bundle signature is required but missing".to_string(),
            ));
        }
        None => {}
    }
    Ok(())
}

fn verify_bundle_signature(
    signature: &BundleSignature,
    signature_key: Option<&str>,
    payload_sha256: &str,
    canonical_bytes: &[u8],
) -> AnfsResult<()> {
    if signature.scheme != "hmac-sha256" {
        return Err(AnfsError::StorageCorruption(format!(
            "unsupported event bundle signature scheme {}",
            signature.scheme
        )));
    }
    if signature.payload_sha256 != payload_sha256 {
        return Err(AnfsError::StorageCorruption(format!(
            "event bundle signature payload mismatch: expected={} actual={payload_sha256}",
            signature.payload_sha256
        )));
    }
    let Some(signature_key) = signature_key else {
        return Ok(());
    };
    let expected = hmac_sha256_hex(signature_key.as_bytes(), canonical_bytes);
    if !constant_time_eq(expected.as_bytes(), signature.signature.as_bytes()) {
        return Err(AnfsError::StorageCorruption(
            "event bundle signature mismatch".to_string(),
        ));
    }
    Ok(())
}

fn validate_bundle_signing_key(signing_key: Option<&str>) -> AnfsResult<Option<&str>> {
    match signing_key {
        Some(key) => {
            let key = key.trim();
            if key.is_empty() {
                return Err(AnfsError::PolicyDenied(
                    "bundle signing key must not be empty".to_string(),
                ));
            }
            Ok(Some(key))
        }
        None => Ok(None),
    }
}

fn validate_bundle_signer_id(signer_id: Option<&str>) -> AnfsResult<Option<&str>> {
    match signer_id {
        Some(signer_id) => {
            let signer_id = signer_id.trim();
            if signer_id.is_empty() {
                return Err(AnfsError::PolicyDenied(
                    "bundle signer_id must not be empty".to_string(),
                ));
            }
            Ok(Some(signer_id))
        }
        None => Ok(None),
    }
}

fn hmac_sha256_hex(key: &[u8], message: &[u8]) -> String {
    const BLOCK_SIZE: usize = 64;
    let mut key_block = [0_u8; BLOCK_SIZE];
    if key.len() > BLOCK_SIZE {
        let hashed_key = Sha256::digest(key);
        key_block[..hashed_key.len()].copy_from_slice(&hashed_key);
    } else {
        key_block[..key.len()].copy_from_slice(key);
    }

    let mut ipad = [0x36_u8; BLOCK_SIZE];
    let mut opad = [0x5c_u8; BLOCK_SIZE];
    for index in 0..BLOCK_SIZE {
        ipad[index] ^= key_block[index];
        opad[index] ^= key_block[index];
    }

    let mut inner = Sha256::new();
    inner.update(ipad);
    inner.update(message);
    let inner_digest = inner.finalize();

    let mut outer = Sha256::new();
    outer.update(opad);
    outer.update(inner_digest);
    hex::encode(outer.finalize())
}

fn constant_time_eq(left: &[u8], right: &[u8]) -> bool {
    if left.len() != right.len() {
        return false;
    }
    let mut diff = 0_u8;
    for (left_byte, right_byte) in left.iter().zip(right.iter()) {
        diff |= left_byte ^ right_byte;
    }
    diff == 0
}

fn validate_event_bundle_references(bundle: &EventBundle) -> AnfsResult<()> {
    let mut event_ids = HashSet::new();
    let mut source_seqs = HashSet::new();
    let mut has_source_seq = false;
    let mut missing_source_seq = false;
    let mut root_source_seq: Option<i64> = None;
    let mut max_source_seq: Option<i64> = None;
    for event in &bundle.events {
        if !event_ids.insert(event.event_id.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle contains duplicate event {}",
                event.event_id
            )));
        }
        match event.source_seq {
            Some(source_seq) if source_seq > 0 => {
                has_source_seq = true;
                if !source_seqs.insert(source_seq) {
                    return Err(AnfsError::StorageCorruption(format!(
                        "event bundle contains duplicate source_seq {source_seq}"
                    )));
                }
                if event.event_id == bundle.root_event_id {
                    root_source_seq = Some(source_seq);
                }
                max_source_seq = Some(max_source_seq.map_or(source_seq, |max| max.max(source_seq)));
            }
            Some(source_seq) => {
                return Err(AnfsError::StorageCorruption(format!(
                    "event bundle event {} has invalid source_seq {source_seq}",
                    event.event_id
                )));
            }
            None => missing_source_seq = true,
        }
    }
    if has_source_seq && missing_source_seq {
        return Err(AnfsError::StorageCorruption(
            "event bundle mixes events with and without source_seq".to_string(),
        ));
    }
    if !event_ids.contains(bundle.root_event_id.as_str()) {
        return Err(AnfsError::StorageCorruption(format!(
            "event bundle root_event_id {} is missing from events",
            bundle.root_event_id
        )));
    }
    if has_source_seq && root_source_seq != max_source_seq {
        return Err(AnfsError::StorageCorruption(format!(
            "event bundle root_event_id {} does not have the maximum source_seq",
            bundle.root_event_id
        )));
    }

    let mut blob_hashes = HashSet::new();
    for blob in &bundle.blobs {
        if !blob_hashes.insert(blob.hash.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle contains duplicate blob {}",
                blob.hash
            )));
        }
    }

    let mut node_ids = HashSet::new();
    for node in &bundle.nodes {
        if !node_ids.insert(node.node_id.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle contains duplicate node {}",
                node.node_id
            )));
        }
        if !blob_hashes.contains(node.blob_hash.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle node {} references missing blob {}",
                node.node_id, node.blob_hash
            )));
        }
    }

    let mut edge_keys = HashSet::new();
    for edge in &bundle.event_edges {
        let key = format!(
            "{}\u{1f}{}\u{1f}{}\u{1f}{}",
            edge.event_id, edge.direction, edge.node_id, edge.role
        );
        if !edge_keys.insert(key) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle contains duplicate event edge {}/{}/{}/{}",
                edge.event_id, edge.direction, edge.node_id, edge.role
            )));
        }
        if !event_ids.contains(edge.event_id.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle edge {}/{}/{}/{} references missing event",
                edge.event_id, edge.direction, edge.node_id, edge.role
            )));
        }
        if !node_ids.contains(edge.node_id.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle edge {}/{}/{}/{} references missing node",
                edge.event_id, edge.direction, edge.node_id, edge.role
            )));
        }
    }

    let mut ref_event_keys = HashSet::new();
    for ref_event in &bundle.ref_events {
        let key = format!("{}\u{1f}{}", ref_event.ref_name, ref_event.event_id);
        if !ref_event_keys.insert(key) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle contains duplicate ref event {}/{}",
                ref_event.ref_name, ref_event.event_id
            )));
        }
        if !event_ids.contains(ref_event.event_id.as_str()) {
            return Err(AnfsError::StorageCorruption(format!(
                "event bundle ref event {}/{} references missing event",
                ref_event.ref_name, ref_event.event_id
            )));
        }
        if let Some(old_node_id) = ref_event.old_node_id.as_deref() {
            if !node_ids.contains(old_node_id) {
                return Err(AnfsError::StorageCorruption(format!(
                    "event bundle ref event {}/{} references missing old node {}",
                    ref_event.ref_name, ref_event.event_id, old_node_id
                )));
            }
        }
        if let Some(new_node_id) = ref_event.new_node_id.as_deref() {
            if !node_ids.contains(new_node_id) {
                return Err(AnfsError::StorageCorruption(format!(
                    "event bundle ref event {}/{} references missing new node {}",
                    ref_event.ref_name, ref_event.event_id, new_node_id
                )));
            }
        }
    }

    Ok(())
}

fn current_archive_events(conn: &Connection) -> AnfsResult<Vec<(String, i64)>> {
    let mut stmt = conn.prepare(
        "
        SELECT e.event_id, es.seq
        FROM events e
        JOIN event_sequence es ON es.event_id = e.event_id
        ORDER BY es.seq
        ",
    )?;
    let rows = stmt.query_map([], |row| Ok((row.get(0)?, row.get(1)?)))?;
    collect_rows(rows)
}

fn current_archive_event_edges(
    conn: &Connection,
) -> AnfsResult<Vec<(String, String, String, String, Option<String>)>> {
    let mut stmt = conn.prepare(
        "
        SELECT ee.event_id, ee.direction, ee.node_id, ee.role, ee.logical_path
        FROM event_edges ee
        JOIN event_sequence es ON es.event_id = ee.event_id
        ORDER BY es.seq, ee.direction, ee.role, ee.node_id
        ",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, String>(3)?,
            row.get::<_, Option<String>>(4)?,
        ))
    })?;
    collect_rows(rows)
}

fn current_archive_ref_events(
    conn: &Connection,
) -> AnfsResult<
    Vec<(
        String,
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    )>,
> {
    let mut stmt = conn.prepare(
        "
        SELECT re.ref_name, re.event_id, re.old_node_id, re.new_node_id,
               re.old_state, re.new_state
        FROM ref_events re
        JOIN event_sequence es ON es.event_id = re.event_id
        ORDER BY es.seq, re.rowid
        ",
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, String>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, Option<String>>(2)?,
            row.get::<_, Option<String>>(3)?,
            row.get::<_, Option<String>>(4)?,
            row.get::<_, Option<String>>(5)?,
        ))
    })?;
    collect_rows(rows)
}

fn ordered_bundle_event_indices(events: &[BundleEvent]) -> Vec<usize> {
    let mut indices: Vec<usize> = (0..events.len()).collect();
    indices.sort_by_key(|idx| events[*idx].source_seq.unwrap_or(*idx as i64 + 1));
    indices
}

fn import_bundle_blob_bytes(blob: &BundleBlob, object_root: Option<&Path>) -> AnfsResult<Vec<u8>> {
    let object_root = object_root.ok_or_else(|| {
        AnfsError::StorageCorruption(format!(
            "bundle blob {} has no object source directory",
            blob.hash
        ))
    })?;
    let object_path = match blob.object_path.as_deref() {
        Some(path) => object_root
            .parent()
            .unwrap_or(object_root)
            .join(import_bundle_relative_path(path)?),
        None => object_path(object_root, &blob.hash),
    };
    fs::read(&object_path).map_err(|err| {
        AnfsError::Io(std::io::Error::new(
            err.kind(),
            format!(
                "failed to read bundle object {}: {err}",
                object_path.display()
            ),
        ))
    })
}

fn existing_bundle_blob_matches(
    conn: &Connection,
    objects_dir: &Path,
    blob: &BundleBlob,
) -> AnfsResult<bool> {
    let exists: Option<i64> = conn
        .query_row(
            "SELECT 1 FROM blobs WHERE hash = ?1 LIMIT 1",
            params![blob.hash],
            |row| row.get(0),
        )
        .optional()?;
    if exists.is_none() {
        return Ok(false);
    }

    let bytes = read_blob_bytes(conn, objects_dir, &blob.hash)?;
    if bytes.len() as i64 != blob.size {
        return Err(AnfsError::StorageCorruption(format!(
            "existing blob {} size mismatch for metadata-only bundle: expected {} got {}",
            blob.hash,
            blob.size,
            bytes.len()
        )));
    }
    Ok(true)
}

fn import_bundle_relative_path(path: &str) -> AnfsResult<PathBuf> {
    let path = Path::new(path);
    if path.is_absolute() {
        return Err(AnfsError::PolicyDenied(format!(
            "unsafe absolute bundle object path {}",
            path.display()
        )));
    }
    let mut safe_path = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::Normal(segment) => safe_path.push(segment),
            _ => {
                return Err(AnfsError::PolicyDenied(format!(
                    "unsafe bundle object path {}",
                    path.display()
                )))
            }
        }
    }
    Ok(safe_path)
}

fn ensure_imported_node_matches(conn: &Connection, node: &BundleNode) -> AnfsResult<()> {
    let existing: Option<(String, String, Option<String>, Option<String>, i64)> = conn
        .query_row(
            "SELECT blob_hash, kind, media_type, metadata_json, created_at
             FROM nodes WHERE node_id = ?1",
            params![node.node_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                ))
            },
        )
        .optional()?;
    if existing
        != Some((
            node.blob_hash.clone(),
            node.kind.clone(),
            node.media_type.clone(),
            node.metadata_json.clone(),
            node.created_at,
        ))
    {
        return Err(AnfsError::RefConflict(format!(
            "imported node {} conflicts with existing node",
            node.node_id
        )));
    }
    Ok(())
}

fn ensure_imported_node_fts(
    conn: &Connection,
    objects_dir: &Path,
    node: &BundleNode,
) -> AnfsResult<()> {
    if !is_searchable_media_type(node.media_type.as_deref()) {
        return Ok(());
    }
    let existing_count: i64 = conn.query_row(
        "SELECT COUNT(*) FROM node_fts WHERE node_id = ?1",
        params![node.node_id],
        |row| row.get(0),
    )?;
    if existing_count > 0 {
        return Ok(());
    }
    let bytes = read_node_bytes(conn, objects_dir, &node.node_id)?;
    let body = String::from_utf8_lossy(&bytes);
    conn.execute(
        "INSERT INTO node_fts (node_id, body) VALUES (?1, ?2)",
        params![node.node_id, body.as_ref()],
    )?;
    Ok(())
}

fn is_searchable_media_type(media_type: Option<&str>) -> bool {
    matches!(
        media_type,
        Some(media_type)
            if media_type.starts_with("text/")
                || media_type == "application/json"
                || media_type == MANIFEST_MEDIA_TYPE
    )
}

fn ensure_imported_event_matches(conn: &Connection, event: &BundleEvent) -> AnfsResult<()> {
    let existing: Option<(
        String,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
        i64,
    )> = conn
        .query_row(
            "SELECT kind, agent_id, run_id, tool_call_id, workspace_id, payload_json, created_at
             FROM events WHERE event_id = ?1",
            params![event.event_id],
            |row| {
                Ok((
                    row.get(0)?,
                    row.get(1)?,
                    row.get(2)?,
                    row.get(3)?,
                    row.get(4)?,
                    row.get(5)?,
                    row.get(6)?,
                ))
            },
        )
        .optional()?;
    if existing
        != Some((
            event.kind.clone(),
            event.agent_id.clone(),
            event.run_id.clone(),
            event.tool_call_id.clone(),
            event.workspace_id.clone(),
            event.payload_json.clone(),
            event.created_at,
        ))
    {
        return Err(AnfsError::RefConflict(format!(
            "imported event {} conflicts with existing event",
            event.event_id
        )));
    }
    Ok(())
}

fn ensure_imported_event_edge_matches(conn: &Connection, edge: &BundleEventEdge) -> AnfsResult<()> {
    let existing: Option<Option<String>> = conn
        .query_row(
            "SELECT logical_path
             FROM event_edges
             WHERE event_id = ?1
               AND direction = ?2
               AND node_id = ?3
               AND role = ?4",
            params![edge.event_id, edge.direction, edge.node_id, edge.role],
            |row| row.get(0),
        )
        .optional()?;
    if existing != Some(edge.logical_path.clone()) {
        return Err(AnfsError::RefConflict(format!(
            "imported event edge {}/{}/{}/{} conflicts with existing edge",
            edge.event_id, edge.direction, edge.node_id, edge.role
        )));
    }
    Ok(())
}

fn ensure_imported_ref_event_matches(
    conn: &Connection,
    ref_event: &BundleRefEvent,
) -> AnfsResult<()> {
    let existing: Option<(
        Option<String>,
        Option<String>,
        Option<String>,
        Option<String>,
    )> = conn
        .query_row(
            "SELECT old_node_id, new_node_id, old_state, new_state
             FROM ref_events
             WHERE ref_name = ?1 AND event_id = ?2",
            params![ref_event.ref_name, ref_event.event_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    if existing
        != Some((
            ref_event.old_node_id.clone(),
            ref_event.new_node_id.clone(),
            ref_event.old_state.clone(),
            ref_event.new_state.clone(),
        ))
    {
        return Err(AnfsError::RefConflict(format!(
            "imported ref event {}/{} conflicts with existing ref event",
            ref_event.ref_name, ref_event.event_id
        )));
    }
    Ok(())
}

fn history_archive_events(conn: &Connection, root_seq: i64) -> AnfsResult<Vec<BundleEvent>> {
    let mut stmt = conn.prepare(
        "
        SELECT e.event_id, es.seq, e.kind, e.agent_id, e.run_id, e.tool_call_id,
               e.workspace_id, e.payload_json, e.created_at
        FROM events e
        JOIN event_sequence es ON es.event_id = e.event_id
        WHERE es.seq <= ?1
        ORDER BY es.seq
        ",
    )?;
    let rows = stmt.query_map(params![root_seq], |row| {
        Ok(BundleEvent {
            event_id: row.get(0)?,
            source_seq: Some(row.get(1)?),
            kind: row.get(2)?,
            agent_id: row.get(3)?,
            run_id: row.get(4)?,
            tool_call_id: row.get(5)?,
            workspace_id: row.get(6)?,
            payload_json: row.get(7)?,
            created_at: row.get(8)?,
        })
    })?;
    collect_rows(rows)
}

fn history_archive_event_edges(
    conn: &Connection,
    root_seq: i64,
) -> AnfsResult<Vec<BundleEventEdge>> {
    let mut stmt = conn.prepare(
        "
        SELECT ee.event_id, ee.direction, ee.node_id, ee.role, ee.logical_path
        FROM event_edges ee
        JOIN event_sequence es ON es.event_id = ee.event_id
        WHERE es.seq <= ?1
        ORDER BY es.seq, ee.direction, ee.role, ee.node_id
        ",
    )?;
    let rows = stmt.query_map(params![root_seq], |row| {
        Ok(BundleEventEdge {
            event_id: row.get(0)?,
            direction: row.get(1)?,
            node_id: row.get(2)?,
            role: row.get(3)?,
            logical_path: row.get(4)?,
        })
    })?;
    collect_rows(rows)
}

fn history_archive_nodes(conn: &Connection, root_seq: i64) -> AnfsResult<Vec<BundleNode>> {
    let mut stmt = conn.prepare(
        "
        WITH included_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id
            WHERE es.seq <= ?1
            UNION
            SELECT re.old_node_id
            FROM ref_events re
            JOIN event_sequence es ON es.event_id = re.event_id
            WHERE es.seq <= ?1 AND re.old_node_id IS NOT NULL
            UNION
            SELECT re.new_node_id
            FROM ref_events re
            JOIN event_sequence es ON es.event_id = re.event_id
            WHERE es.seq <= ?1 AND re.new_node_id IS NOT NULL
        )
        SELECT n.node_id, n.blob_hash, n.kind, n.media_type, n.metadata_json, n.created_at
        FROM nodes n
        JOIN included_nodes inode ON inode.node_id = n.node_id
        ORDER BY n.created_at, n.node_id
        ",
    )?;
    let rows = stmt.query_map(params![root_seq], |row| {
        Ok(BundleNode {
            node_id: row.get(0)?,
            blob_hash: row.get(1)?,
            kind: row.get(2)?,
            media_type: row.get(3)?,
            metadata_json: row.get(4)?,
            created_at: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}

fn history_archive_blobs(conn: &Connection, root_seq: i64) -> AnfsResult<Vec<BundleBlob>> {
    let mut stmt = conn.prepare(
        "
        WITH included_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id
            WHERE es.seq <= ?1
            UNION
            SELECT re.old_node_id
            FROM ref_events re
            JOIN event_sequence es ON es.event_id = re.event_id
            WHERE es.seq <= ?1 AND re.old_node_id IS NOT NULL
            UNION
            SELECT re.new_node_id
            FROM ref_events re
            JOIN event_sequence es ON es.event_id = re.event_id
            WHERE es.seq <= ?1 AND re.new_node_id IS NOT NULL
        )
        SELECT DISTINCT b.hash, b.size, b.storage_kind
        FROM blobs b
        JOIN nodes n ON n.blob_hash = b.hash
        JOIN included_nodes inode ON inode.node_id = n.node_id
        ORDER BY b.hash
        ",
    )?;
    let rows = stmt.query_map(params![root_seq], |row| {
        Ok(BundleBlob {
            hash: row.get(0)?,
            size: row.get(1)?,
            storage_kind: row.get(2)?,
            object_path: None,
        })
    })?;
    collect_rows(rows)
}

fn history_archive_ref_events(conn: &Connection, root_seq: i64) -> AnfsResult<Vec<BundleRefEvent>> {
    let mut stmt = conn.prepare(
        "
        SELECT re.ref_name, re.event_id, re.old_node_id, re.new_node_id,
               re.old_state, re.new_state
        FROM ref_events re
        JOIN event_sequence es ON es.event_id = re.event_id
        WHERE es.seq <= ?1
        ORDER BY es.seq, re.rowid
        ",
    )?;
    let rows = stmt.query_map(params![root_seq], |row| {
        Ok(BundleRefEvent {
            ref_name: row.get(0)?,
            event_id: row.get(1)?,
            old_node_id: row.get(2)?,
            new_node_id: row.get(3)?,
            old_state: row.get(4)?,
            new_state: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}

fn bundle_events(conn: &Connection, event_id: &str, root_seq: i64) -> AnfsResult<Vec<BundleEvent>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT node_id
            FROM event_edges
            WHERE event_id = ?1
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN events e
                ON e.event_id = e_out.event_id
            JOIN event_sequence es
                ON es.event_id = e.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT ?1
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT e.event_id, es.seq, e.kind, e.agent_id, e.run_id, e.tool_call_id,
               e.workspace_id, e.payload_json, e.created_at
        FROM events e
        JOIN event_sequence es ON es.event_id = e.event_id
        JOIN reachable_events re ON re.event_id = e.event_id
        ORDER BY es.seq
        ",
    )?;
    let rows = stmt.query_map(params![event_id, root_seq], |row| {
        Ok(BundleEvent {
            event_id: row.get(0)?,
            source_seq: Some(row.get(1)?),
            kind: row.get(2)?,
            agent_id: row.get(3)?,
            run_id: row.get(4)?,
            tool_call_id: row.get(5)?,
            workspace_id: row.get(6)?,
            payload_json: row.get(7)?,
            created_at: row.get(8)?,
        })
    })?;
    collect_rows(rows)
}

fn bundle_event_edges(
    conn: &Connection,
    event_id: &str,
    root_seq: i64,
) -> AnfsResult<Vec<BundleEventEdge>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT node_id
            FROM event_edges
            WHERE event_id = ?1
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN events e
                ON e.event_id = e_out.event_id
            JOIN event_sequence es
                ON es.event_id = e.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT ?1
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT ee.event_id, ee.direction, ee.node_id, ee.role, ee.logical_path
        FROM event_edges ee
        JOIN events e ON e.event_id = ee.event_id
        JOIN event_sequence es ON es.event_id = e.event_id
        JOIN reachable_events re ON re.event_id = ee.event_id
        ORDER BY es.seq, ee.direction, ee.role, ee.node_id
        ",
    )?;
    let rows = stmt.query_map(params![event_id, root_seq], |row| {
        Ok(BundleEventEdge {
            event_id: row.get(0)?,
            direction: row.get(1)?,
            node_id: row.get(2)?,
            role: row.get(3)?,
            logical_path: row.get(4)?,
        })
    })?;
    collect_rows(rows)
}

fn bundle_nodes(conn: &Connection, event_id: &str, root_seq: i64) -> AnfsResult<Vec<BundleNode>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT node_id
            FROM event_edges
            WHERE event_id = ?1
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN events e
                ON e.event_id = e_out.event_id
            JOIN event_sequence es
                ON es.event_id = e.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        )
        SELECT n.node_id, n.blob_hash, n.kind, n.media_type, n.metadata_json, n.created_at
        FROM nodes n
        JOIN reachable_nodes rn ON rn.node_id = n.node_id
        ORDER BY n.created_at, n.node_id
        ",
    )?;
    let rows = stmt.query_map(params![event_id, root_seq], |row| {
        Ok(BundleNode {
            node_id: row.get(0)?,
            blob_hash: row.get(1)?,
            kind: row.get(2)?,
            media_type: row.get(3)?,
            metadata_json: row.get(4)?,
            created_at: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}

fn bundle_blobs(conn: &Connection, event_id: &str, root_seq: i64) -> AnfsResult<Vec<BundleBlob>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT node_id
            FROM event_edges
            WHERE event_id = ?1
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN events e
                ON e.event_id = e_out.event_id
            JOIN event_sequence es
                ON es.event_id = e.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        )
        SELECT DISTINCT b.hash, b.size, b.storage_kind
        FROM blobs b
        JOIN nodes n ON n.blob_hash = b.hash
        JOIN reachable_nodes rn ON rn.node_id = n.node_id
        ORDER BY b.hash
        ",
    )?;
    let rows = stmt.query_map(params![event_id, root_seq], |row| {
        Ok(BundleBlob {
            hash: row.get(0)?,
            size: row.get(1)?,
            storage_kind: row.get(2)?,
            object_path: None,
        })
    })?;
    collect_rows(rows)
}

fn bundle_ref_events(
    conn: &Connection,
    event_id: &str,
    root_seq: i64,
) -> AnfsResult<Vec<BundleRefEvent>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT node_id
            FROM event_edges
            WHERE event_id = ?1
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN events e
                ON e.event_id = e_out.event_id
            JOIN event_sequence es
                ON es.event_id = e.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT ?1
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT re.ref_name, re.event_id, re.old_node_id, re.new_node_id,
               re.old_state, re.new_state
        FROM ref_events re
        JOIN events e ON e.event_id = re.event_id
        JOIN event_sequence es ON es.event_id = e.event_id
        JOIN reachable_events rev ON rev.event_id = re.event_id
        ORDER BY es.seq, re.rowid
        ",
    )?;
    let rows = stmt.query_map(params![event_id, root_seq], |row| {
        Ok(BundleRefEvent {
            ref_name: row.get(0)?,
            event_id: row.get(1)?,
            old_node_id: row.get(2)?,
            new_node_id: row.get(3)?,
            old_state: row.get(4)?,
            new_state: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}

fn run_bundle_events(
    conn: &Connection,
    run_id: &str,
    root_seq: i64,
) -> AnfsResult<Vec<BundleEvent>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE
        root_events(event_id) AS (
            SELECT e.event_id
            FROM events e
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1 AND es.seq <= ?2
        ),
        reachable_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN root_events re ON re.event_id = ee.event_id
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_sequence es
                ON es.event_id = e_out.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT event_id FROM root_events
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT e.event_id, es.seq, e.kind, e.agent_id, e.run_id, e.tool_call_id,
               e.workspace_id, e.payload_json, e.created_at
        FROM events e
        JOIN event_sequence es ON es.event_id = e.event_id
        JOIN reachable_events re ON re.event_id = e.event_id
        ORDER BY es.seq
        ",
    )?;
    let rows = stmt.query_map(params![run_id, root_seq], |row| {
        Ok(BundleEvent {
            event_id: row.get(0)?,
            source_seq: Some(row.get(1)?),
            kind: row.get(2)?,
            agent_id: row.get(3)?,
            run_id: row.get(4)?,
            tool_call_id: row.get(5)?,
            workspace_id: row.get(6)?,
            payload_json: row.get(7)?,
            created_at: row.get(8)?,
        })
    })?;
    collect_rows(rows)
}

fn run_bundle_event_edges(
    conn: &Connection,
    run_id: &str,
    root_seq: i64,
) -> AnfsResult<Vec<BundleEventEdge>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE
        root_events(event_id) AS (
            SELECT e.event_id
            FROM events e
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1 AND es.seq <= ?2
        ),
        reachable_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN root_events re ON re.event_id = ee.event_id
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_sequence es
                ON es.event_id = e_out.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT event_id FROM root_events
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT ee.event_id, ee.direction, ee.node_id, ee.role, ee.logical_path
        FROM event_edges ee
        JOIN event_sequence es ON es.event_id = ee.event_id
        JOIN reachable_events re ON re.event_id = ee.event_id
        ORDER BY es.seq, ee.direction, ee.role, ee.node_id
        ",
    )?;
    let rows = stmt.query_map(params![run_id, root_seq], |row| {
        Ok(BundleEventEdge {
            event_id: row.get(0)?,
            direction: row.get(1)?,
            node_id: row.get(2)?,
            role: row.get(3)?,
            logical_path: row.get(4)?,
        })
    })?;
    collect_rows(rows)
}

fn run_bundle_nodes(conn: &Connection, run_id: &str, root_seq: i64) -> AnfsResult<Vec<BundleNode>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN events e ON e.event_id = ee.event_id
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1 AND es.seq <= ?2
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_sequence es
                ON es.event_id = e_out.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        )
        SELECT n.node_id, n.blob_hash, n.kind, n.media_type, n.metadata_json, n.created_at
        FROM nodes n
        JOIN reachable_nodes rn ON rn.node_id = n.node_id
        ORDER BY n.created_at, n.node_id
        ",
    )?;
    let rows = stmt.query_map(params![run_id, root_seq], |row| {
        Ok(BundleNode {
            node_id: row.get(0)?,
            blob_hash: row.get(1)?,
            kind: row.get(2)?,
            media_type: row.get(3)?,
            metadata_json: row.get(4)?,
            created_at: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}

fn run_bundle_blobs(conn: &Connection, run_id: &str, root_seq: i64) -> AnfsResult<Vec<BundleBlob>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE reachable_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN events e ON e.event_id = ee.event_id
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1 AND es.seq <= ?2
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_sequence es
                ON es.event_id = e_out.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        )
        SELECT DISTINCT b.hash, b.size, b.storage_kind
        FROM blobs b
        JOIN nodes n ON n.blob_hash = b.hash
        JOIN reachable_nodes rn ON rn.node_id = n.node_id
        ORDER BY b.hash
        ",
    )?;
    let rows = stmt.query_map(params![run_id, root_seq], |row| {
        Ok(BundleBlob {
            hash: row.get(0)?,
            size: row.get(1)?,
            storage_kind: row.get(2)?,
            object_path: None,
        })
    })?;
    collect_rows(rows)
}

fn run_bundle_ref_events(
    conn: &Connection,
    run_id: &str,
    root_seq: i64,
) -> AnfsResult<Vec<BundleRefEvent>> {
    let mut stmt = conn.prepare(
        "
        WITH RECURSIVE
        root_events(event_id) AS (
            SELECT e.event_id
            FROM events e
            JOIN event_sequence es ON es.event_id = e.event_id
            WHERE e.run_id = ?1 AND es.seq <= ?2
        ),
        reachable_nodes(node_id) AS (
            SELECT ee.node_id
            FROM event_edges ee
            JOIN root_events re ON re.event_id = ee.event_id
            UNION
            SELECT e_in.node_id
            FROM reachable_nodes r
            JOIN event_edges e_out
                ON e_out.node_id = r.node_id
               AND e_out.direction = 'output'
            JOIN event_sequence es
                ON es.event_id = e_out.event_id
               AND es.seq <= ?2
            JOIN event_edges e_in
                ON e_in.event_id = e_out.event_id
               AND e_in.direction = 'input'
        ),
        reachable_events(event_id) AS (
            SELECT event_id FROM root_events
            UNION
            SELECT DISTINCT ee.event_id
            FROM event_edges ee
            JOIN event_sequence es ON es.event_id = ee.event_id AND es.seq <= ?2
            JOIN reachable_nodes rn ON rn.node_id = ee.node_id
        )
        SELECT re.ref_name, re.event_id, re.old_node_id, re.new_node_id,
               re.old_state, re.new_state
        FROM ref_events re
        JOIN event_sequence es ON es.event_id = re.event_id
        JOIN reachable_events rev ON rev.event_id = re.event_id
        ORDER BY es.seq, re.rowid
        ",
    )?;
    let rows = stmt.query_map(params![run_id, root_seq], |row| {
        Ok(BundleRefEvent {
            ref_name: row.get(0)?,
            event_id: row.get(1)?,
            old_node_id: row.get(2)?,
            new_node_id: row.get(3)?,
            old_state: row.get(4)?,
            new_state: row.get(5)?,
        })
    })?;
    collect_rows(rows)
}
