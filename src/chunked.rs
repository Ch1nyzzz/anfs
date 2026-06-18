//! Canonical chunked storage for large files (task #6 / backlog item 8).
//!
//! A chunked file is a small manifest node whose blob lists ordered,
//! content-addressed chunk blobs. Identical chunks dedupe across files and edits
//! through CAS, so a small in-place edit re-stores only the affected chunk(s).
//!
//! Chunk blobs are first-class blobs, so existing blob integrity checks already
//! cover their hash/size. The one extra invariant — a chunked node's referenced
//! chunk blobs must exist and must not be GC'd — is enforced in `integrity` and
//! `gc_snapshot` via the helpers here.
//!
//! This first slice is an explicit, additive capability: it does not replace the
//! default whole-file write path and does not run lock/policy propagation.

use std::collections::HashSet;
use std::path::Path;

use rusqlite::{params, Connection, Transaction};
use serde_json::{json, Value};

use crate::manifest::{read_blob_bytes, read_node_bytes};
use crate::{
    insert_blob, insert_edge, insert_event, materialize_blob, new_event_id, new_node_id,
    now_millis, sha256_hex, upsert_ref, workspace_ref_name, AnfsError, AnfsResult, RefWriteMode,
    CHUNKED_MEDIA_TYPE,
};

const CHUNKED_SCHEMA: &str = "anfs.chunked.v1";

fn validate_chunk_size(chunk_size: i64) -> AnfsResult<()> {
    if !(1..=(16 * 1024 * 1024)).contains(&chunk_size) {
        return Err(AnfsError::PolicyDenied(
            "chunk size must be between 1 byte and 16 MiB".to_string(),
        ));
    }
    Ok(())
}

/// Store `content` as content-addressed chunk blobs plus a manifest node, set a
/// workspace draft ref, and record a write event. Returns the manifest node id.
#[allow(clippy::too_many_arguments)]
pub(crate) fn write_chunked_in_tx(
    tx: &Transaction<'_>,
    objects_dir: &Path,
    workspace_name: &str,
    agent_id: &str,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
    logical_path: &str,
    content: &[u8],
    chunk_size: i64,
) -> AnfsResult<String> {
    validate_chunk_size(chunk_size)?;
    let ref_name = workspace_ref_name(workspace_name, logical_path);

    let mut chunks = Vec::new();
    for piece in content.chunks(chunk_size as usize) {
        let hash = sha256_hex(piece);
        let storage = materialize_blob(objects_dir, &hash, piece)?;
        insert_blob(tx, &hash, piece.len() as i64, &storage)?;
        chunks.push(json!({"hash": hash, "size": piece.len()}));
    }
    let manifest = json!({
        "schema": CHUNKED_SCHEMA,
        "chunk_size": chunk_size,
        "total_size": content.len(),
        "chunks": chunks,
    });
    let manifest_bytes = serde_json::to_vec(&manifest).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize chunk manifest: {err}"))
    })?;
    let manifest_hash = sha256_hex(&manifest_bytes);
    let manifest_storage = materialize_blob(objects_dir, &manifest_hash, &manifest_bytes)?;
    insert_blob(tx, &manifest_hash, manifest_bytes.len() as i64, &manifest_storage)?;

    let node_id = new_node_id();
    tx.execute(
        "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
         VALUES (?1, ?2, 'artifact', ?3, '{}', ?4)",
        params![node_id, manifest_hash, CHUNKED_MEDIA_TYPE, now_millis()],
    )?;

    // The "write" event payload is the logical path (kernel convention checked
    // by integrity's ref_event linkage); chunk metadata lives in the node blob.
    let event_id = new_event_id();
    insert_event(
        tx,
        &event_id,
        "write",
        Some(agent_id),
        run_id,
        tool_call_id,
        Some(workspace_name),
        Some(logical_path),
    )?;
    insert_edge(tx, &event_id, "output", &node_id, "result", Some(logical_path))?;
    upsert_ref(
        tx,
        &ref_name,
        &node_id,
        "workspace",
        "draft",
        &event_id,
        RefWriteMode::WorkspaceDraft,
    )?;
    Ok(node_id)
}

/// Ordered chunk-blob hashes referenced by a chunked manifest node.
pub(crate) fn chunked_node_chunk_hashes(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<String>> {
    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let manifest: Value = serde_json::from_slice(&bytes).map_err(|err| {
        AnfsError::StorageCorruption(format!("chunked node {node_id} has invalid manifest: {err}"))
    })?;
    if manifest.get("schema").and_then(Value::as_str) != Some(CHUNKED_SCHEMA) {
        return Err(AnfsError::StorageCorruption(format!(
            "node {node_id} is not a chunked manifest"
        )));
    }
    let chunks = manifest
        .get("chunks")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            AnfsError::StorageCorruption(format!("chunked node {node_id} manifest has no chunks"))
        })?;
    let mut hashes = Vec::with_capacity(chunks.len());
    for chunk in chunks {
        let hash = chunk.get("hash").and_then(Value::as_str).ok_or_else(|| {
            AnfsError::StorageCorruption(format!("chunked node {node_id} has a chunk without hash"))
        })?;
        hashes.push(hash.to_string());
    }
    Ok(hashes)
}

/// Reassemble the full content of a chunked node from its chunk blobs.
pub(crate) fn read_chunked_node(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<u8>> {
    let mut out = Vec::new();
    for hash in chunked_node_chunk_hashes(conn, objects_dir, node_id)? {
        let bytes = read_blob_bytes(conn, objects_dir, &hash)?;
        out.extend_from_slice(&bytes);
    }
    Ok(out)
}

/// Every chunk-blob hash referenced by any chunked node. GC must keep these even
/// though no node points at them through `nodes.blob_hash` directly.
pub(crate) fn all_chunked_chunk_hashes(
    conn: &Connection,
    objects_dir: &Path,
) -> AnfsResult<HashSet<String>> {
    let mut node_stmt =
        conn.prepare("SELECT node_id FROM nodes WHERE media_type = ?1 ORDER BY node_id")?;
    let node_ids: Vec<String> = node_stmt
        .query_map(params![CHUNKED_MEDIA_TYPE], |row| row.get(0))?
        .collect::<Result<_, _>>()?;
    let mut protected = HashSet::new();
    for node_id in node_ids {
        for hash in chunked_node_chunk_hashes(conn, objects_dir, &node_id)? {
            protected.insert(hash);
        }
    }
    Ok(protected)
}
