use rusqlite::{params, Connection, OptionalExtension, Transaction};
use serde_json::{json, Value};
use std::fs;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

use crate::{
    collect_json_field_spans, ensure_node_fragments_visible, ensure_node_range_visible,
    file_blob_path, gc_snapshot::is_blob_gc_collected, insert_blob, markdown_body_section_spans,
    markdown_field_span_value, markdown_frontmatter_field_spans, materialize_blob, new_node_id,
    now_millis, sha256_hex, AnfsError, AnfsResult, ManifestChild, ManifestChildRecord, ManifestDoc,
    MANIFEST_MEDIA_TYPE,
};

pub(crate) fn read_blob_bytes(
    conn: &Connection,
    objects_dir: &Path,
    hash: &str,
) -> AnfsResult<Vec<u8>> {
    let row: Option<(String, Option<String>, Option<Vec<u8>>)> = conn
        .query_row(
            "SELECT storage_kind, storage_uri, inline_content FROM blobs WHERE hash = ?1",
            params![hash],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let (storage_kind, storage_uri, inline_content) = row.ok_or_else(|| {
        AnfsError::StorageCorruption(format!("bundle blob {hash} is missing from blobs table"))
    })?;
    match storage_kind.as_str() {
        "inline" => inline_content.ok_or_else(|| {
            AnfsError::StorageCorruption(format!("inline blob {hash} has no inline content"))
        }),
        "file" => {
            let uri = storage_uri.ok_or_else(|| {
                AnfsError::StorageCorruption(format!("file blob {hash} has no storage uri"))
            })?;
            let path = file_blob_path(objects_dir, &uri);
            let bytes = match fs::read(&path) {
                Ok(bytes) => bytes,
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    if is_blob_gc_collected(conn, hash)? {
                        return Err(AnfsError::StorageCorruption(format!(
                            "blob {hash} was physically collected by GC"
                        )));
                    }
                    return Err(AnfsError::Io(err));
                }
                Err(err) => return Err(AnfsError::Io(err)),
            };
            let actual_hash = sha256_hex(&bytes);
            if actual_hash != hash {
                return Err(AnfsError::StorageCorruption(format!(
                    "blob hash mismatch for {hash}: got {actual_hash}"
                )));
            }
            Ok(bytes)
        }
        other => Err(AnfsError::StorageCorruption(format!(
            "unknown storage_kind {other} for blob {hash}"
        ))),
    }
}

pub(crate) fn read_node_bytes(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<u8>> {
    let row: Option<(String, String, Option<String>, Option<Vec<u8>>)> = conn
        .query_row(
            "SELECT b.hash, b.storage_kind, b.storage_uri, b.inline_content
             FROM nodes n
             JOIN blobs b ON b.hash = n.blob_hash
             WHERE n.node_id = ?1",
            params![node_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;

    let (hash, storage_kind, storage_uri, inline_content) =
        row.ok_or_else(|| AnfsError::NodeNotFound(node_id.to_string()))?;

    match storage_kind.as_str() {
        "inline" => inline_content.ok_or_else(|| {
            AnfsError::StorageCorruption(format!("inline blob {hash} has no inline content"))
        }),
        "file" => {
            let uri = storage_uri.ok_or_else(|| {
                AnfsError::StorageCorruption(format!("file blob {hash} has no storage uri"))
            })?;
            let path = file_blob_path(objects_dir, &uri);
            let bytes = match fs::read(&path) {
                Ok(bytes) => bytes,
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    if is_blob_gc_collected(conn, &hash)? {
                        return Err(AnfsError::StorageCorruption(format!(
                            "blob {hash} was physically collected by GC"
                        )));
                    }
                    return Err(AnfsError::Io(err));
                }
                Err(err) => return Err(AnfsError::Io(err)),
            };
            let actual_hash = sha256_hex(&bytes);
            if actual_hash != hash {
                return Err(AnfsError::StorageCorruption(format!(
                    "blob hash mismatch for {hash}: got {actual_hash}"
                )));
            }
            Ok(bytes)
        }
        other => Err(AnfsError::StorageCorruption(format!(
            "unknown storage_kind {other} for blob {hash}"
        ))),
    }
}

pub(crate) fn read_node_range(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
    offset: i64,
    length: i64,
) -> AnfsResult<Vec<u8>> {
    if offset < 0 {
        return Err(AnfsError::PolicyDenied(
            "range offset must be non-negative".to_string(),
        ));
    }
    if length <= 0 {
        return Err(AnfsError::PolicyDenied(
            "range length must be positive".to_string(),
        ));
    }
    let (hash, size, storage_kind, storage_uri, inline_content) = node_blob_row(conn, node_id)?;
    if offset >= size {
        return Ok(Vec::new());
    }
    let read_len = std::cmp::min(length, size - offset) as usize;
    ensure_node_range_visible(conn, node_id, offset, read_len as i64, || {
        format!("node range read blocked by fragment policy label on node {node_id}")
    })?;
    match storage_kind.as_str() {
        "inline" => {
            let bytes = inline_content.ok_or_else(|| {
                AnfsError::StorageCorruption(format!("inline blob {hash} has no inline content"))
            })?;
            Ok(bytes[offset as usize..offset as usize + read_len].to_vec())
        }
        "file" => {
            let uri = storage_uri.ok_or_else(|| {
                AnfsError::StorageCorruption(format!("file blob {hash} has no storage uri"))
            })?;
            let path = file_blob_path(objects_dir, &uri);
            let mut file = match fs::File::open(&path) {
                Ok(file) => file,
                Err(err) if err.kind() == std::io::ErrorKind::NotFound => {
                    if is_blob_gc_collected(conn, &hash)? {
                        return Err(AnfsError::StorageCorruption(format!(
                            "blob {hash} was physically collected by GC"
                        )));
                    }
                    return Err(AnfsError::Io(err));
                }
                Err(err) => return Err(AnfsError::Io(err)),
            };
            let metadata_len = file.metadata()?.len() as i64;
            if metadata_len != size {
                return Err(AnfsError::StorageCorruption(format!(
                    "file blob {hash} size mismatch: expected {size} got {metadata_len}"
                )));
            }
            file.seek(SeekFrom::Start(offset as u64))?;
            let mut bytes = vec![0; read_len];
            file.read_exact(&mut bytes)?;
            Ok(bytes)
        }
        other => Err(AnfsError::StorageCorruption(format!(
            "unknown storage_kind {other} for blob {hash}"
        ))),
    }
}

pub(crate) fn node_chunks(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
    chunk_size: i64,
) -> AnfsResult<Vec<crate::NodeChunkRow>> {
    validate_chunk_size(chunk_size)?;
    let (_hash, size, _storage_kind, _storage_uri, _inline_content) = node_blob_row(conn, node_id)?;
    let mut chunks = Vec::new();
    let mut offset = 0;
    let mut chunk_index = 0;
    while offset < size {
        let bytes = read_node_range(conn, objects_dir, node_id, offset, chunk_size)?;
        let chunk_len = bytes.len() as i64;
        chunks.push((chunk_index, offset, chunk_len, sha256_hex(&bytes)));
        offset += chunk_len;
        chunk_index += 1;
    }
    Ok(chunks)
}

pub(crate) fn cache_node_chunks(
    conn: &mut Connection,
    objects_dir: &Path,
    node_id: &str,
    chunk_size: i64,
) -> AnfsResult<Vec<crate::NodeChunkRow>> {
    validate_chunk_size(chunk_size)?;
    let (hash, blob_size, _storage_kind, _storage_uri, _inline_content) =
        node_blob_row(conn, node_id)?;
    let chunks = node_chunks(conn, objects_dir, node_id, chunk_size)?;
    let tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM node_chunk_index WHERE node_id = ?1 AND chunk_size = ?2",
        params![node_id, chunk_size],
    )?;
    tx.execute(
        "DELETE FROM node_chunk_indexes WHERE node_id = ?1 AND chunk_size = ?2",
        params![node_id, chunk_size],
    )?;
    tx.execute(
        "INSERT INTO node_chunk_indexes
         (node_id, chunk_size, blob_hash, blob_size, chunk_count, indexed_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            node_id,
            chunk_size,
            hash,
            blob_size,
            chunks.len() as i64,
            now_millis()
        ],
    )?;
    for (chunk_index, offset, size, digest) in &chunks {
        tx.execute(
            "INSERT INTO node_chunk_index
             (node_id, chunk_size, chunk_index, offset, size, sha256)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            params![node_id, chunk_size, chunk_index, offset, size, digest],
        )?;
    }
    tx.commit()?;
    Ok(chunks)
}

pub(crate) fn cached_node_chunks(
    conn: &Connection,
    node_id: &str,
    chunk_size: i64,
) -> AnfsResult<Vec<crate::NodeChunkRow>> {
    validate_chunk_size(chunk_size)?;
    ensure_node_fragments_visible(conn, node_id, || {
        format!("cached node chunks blocked by fragment policy label on node {node_id}")
    })?;
    let (hash, blob_size, _storage_kind, _storage_uri, _inline_content) =
        node_blob_row(conn, node_id)?;
    let meta: Option<(String, i64, i64)> = conn
        .query_row(
            "SELECT blob_hash, blob_size, chunk_count
             FROM node_chunk_indexes
             WHERE node_id = ?1 AND chunk_size = ?2",
            params![node_id, chunk_size],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;
    let Some((cached_hash, cached_size, chunk_count)) = meta else {
        return Ok(Vec::new());
    };
    if cached_hash != hash || cached_size != blob_size {
        return Err(AnfsError::StorageCorruption(format!(
            "node chunk cache for {node_id} chunk_size {chunk_size} does not match node blob"
        )));
    }
    let mut stmt = conn.prepare(
        "SELECT chunk_index, offset, size, sha256
         FROM node_chunk_index
         WHERE node_id = ?1 AND chunk_size = ?2
         ORDER BY chunk_index",
    )?;
    let rows = stmt.query_map(params![node_id, chunk_size], |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
    })?;
    let mut chunks = Vec::new();
    for row in rows {
        chunks.push(row?);
    }
    if chunks.len() as i64 != chunk_count {
        return Err(AnfsError::StorageCorruption(format!(
            "node chunk cache for {node_id} chunk_size {chunk_size} expected {chunk_count} rows, found {}",
            chunks.len()
        )));
    }
    Ok(chunks)
}

pub(crate) fn rebuild_derived_indexes(
    conn: &mut Connection,
    objects_dir: &Path,
) -> AnfsResult<crate::DerivedIndexRepairResult> {
    let rows = searchable_node_bodies(conn, objects_dir)?;
    let chunk_indexes_cleared: i64 =
        conn.query_row("SELECT COUNT(*) FROM node_chunk_indexes", [], |row| {
            row.get(0)
        })?;

    let tx = conn.transaction()?;
    tx.execute("DELETE FROM node_fts", [])?;
    for (node_id, body) in &rows {
        tx.execute(
            "INSERT INTO node_fts (node_id, body) VALUES (?1, ?2)",
            params![node_id, body],
        )?;
    }
    tx.execute("DELETE FROM node_chunk_embeddings", [])?;
    tx.execute("DELETE FROM node_chunk_index", [])?;
    tx.execute("DELETE FROM node_chunk_indexes", [])?;
    tx.commit()?;
    Ok((rows.len() as i64, chunk_indexes_cleared))
}

fn searchable_node_bodies(
    conn: &Connection,
    objects_dir: &Path,
) -> AnfsResult<Vec<(String, String)>> {
    let mut stmt = conn.prepare(
        "
        SELECT n.node_id
        FROM nodes n
        WHERE n.media_type LIKE 'text/%'
           OR n.media_type = 'application/json'
           OR n.media_type = ?1
        ORDER BY n.node_id
        ",
    )?;
    let node_rows = stmt.query_map(params![MANIFEST_MEDIA_TYPE], |row| row.get::<_, String>(0))?;
    let mut out = Vec::new();
    for row in node_rows {
        let node_id = row?;
        let bytes = read_node_bytes(conn, objects_dir, &node_id)?;
        let body = String::from_utf8_lossy(&bytes).to_string();
        out.push((node_id, body));
    }
    Ok(out)
}

pub(crate) fn json_field_spans(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<crate::JsonFieldSpanRow>> {
    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let text = std::str::from_utf8(&bytes).map_err(|err| {
        AnfsError::PolicyDenied(format!(
            "node {node_id} is not valid utf-8 JSON text: {err}"
        ))
    })?;
    let parsed: Value = serde_json::from_str(text).map_err(|err| {
        AnfsError::PolicyDenied(format!("node {node_id} is not valid JSON: {err}"))
    })?;
    let mut spans = Vec::new();
    collect_json_field_spans(text, &parsed, 0, "$", &mut spans)?;
    Ok(spans)
}

pub(crate) fn markdown_field_spans(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<crate::MarkdownFieldSpanRow>> {
    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let text = std::str::from_utf8(&bytes).map_err(|err| {
        AnfsError::PolicyDenied(format!(
            "node {node_id} is not valid utf-8 Markdown text: {err}"
        ))
    })?;
    markdown_frontmatter_field_spans(text, node_id)
}

pub(crate) fn markdown_field_values(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<crate::MarkdownFieldValueRow>> {
    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let text = std::str::from_utf8(&bytes).map_err(|err| {
        AnfsError::PolicyDenied(format!(
            "node {node_id} is not valid utf-8 Markdown text: {err}"
        ))
    })?;
    markdown_frontmatter_field_spans(text, node_id)?
        .into_iter()
        .map(|(path, offset, length, kind)| {
            let value = markdown_field_span_value(text.as_bytes(), offset, length)?;
            Ok((path, value, kind))
        })
        .collect()
}

pub(crate) fn markdown_section_spans(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<crate::MarkdownSectionSpanRow>> {
    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let text = std::str::from_utf8(&bytes).map_err(|err| {
        AnfsError::PolicyDenied(format!(
            "node {node_id} is not valid utf-8 Markdown text: {err}"
        ))
    })?;
    markdown_body_section_spans(text, node_id)
}

fn validate_chunk_size(chunk_size: i64) -> AnfsResult<()> {
    if chunk_size <= 0 || chunk_size > 16 * 1024 * 1024 {
        return Err(AnfsError::PolicyDenied(
            "chunk_size must be between 1 and 16777216".to_string(),
        ));
    }
    Ok(())
}

fn node_blob_row(
    conn: &Connection,
    node_id: &str,
) -> AnfsResult<(String, i64, String, Option<String>, Option<Vec<u8>>)> {
    conn.query_row(
        "SELECT b.hash, b.size, b.storage_kind, b.storage_uri, b.inline_content
         FROM nodes n
         JOIN blobs b ON b.hash = n.blob_hash
         WHERE n.node_id = ?1",
        params![node_id],
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
    .optional()?
    .ok_or_else(|| AnfsError::NodeNotFound(node_id.to_string()))
}

pub(crate) fn manifest_children(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<(String, String)>> {
    Ok(manifest_child_records(conn, objects_dir, node_id)?
        .into_iter()
        .map(|(path, node_id, _role, _media_type, _digest, _size)| (path, node_id))
        .collect())
}

pub(crate) fn manifest_child_records(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
) -> AnfsResult<Vec<ManifestChildRecord>> {
    let media_type: Option<String> = conn
        .query_row(
            "SELECT media_type FROM nodes WHERE node_id = ?1",
            params![node_id],
            |row| row.get(0),
        )
        .optional()?;
    if media_type.as_deref() != Some(MANIFEST_MEDIA_TYPE) {
        return Ok(Vec::new());
    }

    let bytes = read_node_bytes(conn, objects_dir, node_id)?;
    let manifest: ManifestDoc = serde_json::from_slice(&bytes).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to parse manifest node {node_id}: {err}"))
    })?;
    if manifest.schema != "anfs.manifest.v1" {
        return Err(AnfsError::StorageCorruption(format!(
            "unsupported manifest schema {} for node {node_id}",
            manifest.schema
        )));
    }
    Ok(manifest
        .children
        .into_iter()
        .map(|child| {
            (
                child.path,
                child.node_id,
                child.role,
                child.media_type,
                child.digest,
                child.size,
            )
        })
        .collect())
}

pub(crate) fn node_manifest_metadata(
    conn: &Connection,
    node_id: &str,
) -> AnfsResult<(String, Option<String>, i64)> {
    conn.query_row(
        "SELECT n.blob_hash, n.media_type, b.size
         FROM nodes n
         JOIN blobs b ON b.hash = n.blob_hash
         WHERE n.node_id = ?1",
        params![node_id],
        |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
    )
    .optional()?
    .ok_or_else(|| AnfsError::NodeNotFound(node_id.to_string()))
}

pub(crate) fn insert_manifest_node(
    tx: &Transaction<'_>,
    objects_dir: &Path,
    kind: &str,
    children: Vec<ManifestChild>,
    metadata_extra: serde_json::Value,
) -> AnfsResult<String> {
    let manifest = ManifestDoc {
        schema: "anfs.manifest.v1".to_string(),
        kind: kind.to_string(),
        children,
    };
    let manifest_bytes = serde_json::to_vec(&manifest).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to serialize manifest: {err}"))
    })?;
    let hash = sha256_hex(&manifest_bytes);
    let blob_storage = materialize_blob(objects_dir, &hash, &manifest_bytes)?;
    insert_blob(tx, &hash, manifest_bytes.len() as i64, &blob_storage)?;

    let manifest_node_id = new_node_id();
    let metadata = json!({
        "manifest": true,
        "schema": "anfs.manifest.v1",
        "child_count": manifest.children.len(),
        "extra": metadata_extra,
    })
    .to_string();
    tx.execute(
        "INSERT INTO nodes (node_id, blob_hash, kind, media_type, metadata_json, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
        params![
            manifest_node_id,
            hash,
            kind,
            MANIFEST_MEDIA_TYPE,
            metadata,
            now_millis()
        ],
    )?;
    tx.execute(
        "INSERT INTO node_fts (node_id, body) VALUES (?1, ?2)",
        params![
            manifest_node_id,
            String::from_utf8_lossy(&manifest_bytes).as_ref()
        ],
    )?;
    Ok(manifest_node_id)
}
