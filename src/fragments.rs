//! Unified structural projection: one parser per format turns a node's blob
//! bytes into rows in the generic `fragments` / `fragment_edges` tables.
//!
//! This is the first cut of task #5's "unified parser contract": the existing
//! conservative Markdown/JSON span parser is registered as the first parser and
//! persists into the same generic tables a future tree-sitter code parser will
//! use. Everything downstream stays format-blind. Fragments are rebuildable
//! projections bound to `(node_id, blob_hash, parser, parser_version)`.

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension};

use crate::manifest::{json_field_spans, markdown_section_spans};
use crate::{now_millis, sha256_hex, AnfsError, AnfsResult};

/// Bumped when a parser's output shape changes so stale rows can be detected.
const PARSER_VERSION: &str = "1";

/// (path, byte_offset, byte_length, kind) — the shape every span parser emits.
type SpanRow = (String, i64, i64, String);

/// (fragment_id, kind, name, path, byte_start, byte_end) — outline row.
pub(crate) type FragmentRow = (String, String, Option<String>, String, i64, i64);

fn node_blob_hash(conn: &Connection, node_id: &str) -> AnfsResult<String> {
    conn.query_row(
        "SELECT blob_hash FROM nodes WHERE node_id = ?1",
        params![node_id],
        |row| row.get(0),
    )
    .optional()?
    .ok_or_else(|| AnfsError::NodeNotFound(node_id.to_string()))
}

/// The parser registry. v1 is caller-selected; a future media_type registry
/// can pick automatically. Each arm reduces a format to the same SpanRow shape.
fn run_parser(
    conn: &Connection,
    objects_dir: &Path,
    node_id: &str,
    parser: &str,
) -> AnfsResult<Vec<SpanRow>> {
    match parser {
        "span-markdown" => markdown_section_spans(conn, objects_dir, node_id),
        "span-json" => json_field_spans(conn, objects_dir, node_id),
        other => Err(AnfsError::PolicyDenied(format!(
            "unknown fragment parser {other}"
        ))),
    }
}

fn parser_language(parser: &str) -> Option<&'static str> {
    match parser {
        "span-markdown" => Some("markdown"),
        "span-json" => Some("json"),
        _ => None,
    }
}

/// Content-addressed fragment id: stable across rebuilds while content is
/// unchanged, so the same byte range keeps the same id across workspaces and
/// time-travel views.
fn fragment_id(blob_hash: &str, parser: &str, path: &str, start: i64, end: i64) -> String {
    let key = format!("{blob_hash}\0{parser}\0{path}\0{start}\0{end}");
    format!("frag:{}", sha256_hex(key.as_bytes()))
}

/// Best-effort symbol name from a hierarchical span path: the last dotted
/// segment with any trailing array index stripped (e.g. `$.items[0].name` ->
/// `name`, `body.heading-1` -> `heading-1`).
fn name_from_path(path: &str) -> Option<String> {
    let seg = path.rsplit('.').next().unwrap_or(path);
    let seg = seg.split('[').next().unwrap_or(seg);
    if seg.is_empty() {
        None
    } else {
        Some(seg.to_string())
    }
}

/// Parse `node_id` with `parser` and persist its fragments. Idempotent and
/// incremental: if the node was already indexed at the current blob hash and
/// parser version, returns the cached `(fragment_count, edge_count)` untouched.
pub(crate) fn index_node_fragments(
    conn: &mut Connection,
    objects_dir: &Path,
    node_id: &str,
    parser: &str,
) -> AnfsResult<(i64, i64)> {
    let blob_hash = node_blob_hash(conn, node_id)?;

    let existing: Option<(String, String, i64, i64)> = conn
        .query_row(
            "SELECT blob_hash, parser_version, fragment_count, edge_count
             FROM fragment_index_runs WHERE node_id = ?1 AND parser = ?2",
            params![node_id, parser],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .optional()?;
    if let Some((indexed_hash, indexed_version, fragment_count, edge_count)) = existing {
        if indexed_hash == blob_hash && indexed_version == PARSER_VERSION {
            return Ok((fragment_count, edge_count));
        }
    }

    // Parse (read-only) before opening the write transaction.
    let spans = run_parser(conn, objects_dir, node_id, parser)?;

    let tx = conn.transaction()?;
    tx.execute(
        "DELETE FROM fragment_edges
         WHERE src_fragment_id IN
             (SELECT fragment_id FROM fragments WHERE node_id = ?1 AND parser = ?2)",
        params![node_id, parser],
    )?;
    tx.execute(
        "DELETE FROM fragments WHERE node_id = ?1 AND parser = ?2",
        params![node_id, parser],
    )?;

    let mut fragment_count = 0i64;
    for (path, offset, length, kind) in &spans {
        let start = *offset;
        let end = *offset + *length;
        let fragment_id = fragment_id(&blob_hash, parser, path, start, end);
        let name = name_from_path(path);
        tx.execute(
            "INSERT OR REPLACE INTO fragments
             (fragment_id, node_id, blob_hash, parser, parser_version,
              kind, name, path, byte_start, byte_end, parent_fragment_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, NULL)",
            params![
                fragment_id,
                node_id,
                blob_hash,
                parser,
                PARSER_VERSION,
                kind,
                name,
                path,
                start,
                end
            ],
        )?;
        fragment_count += 1;
    }

    tx.execute(
        "INSERT OR REPLACE INTO fragment_index_runs
         (node_id, parser, blob_hash, parser_version, language,
          fragment_count, edge_count, status, indexed_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
        params![
            node_id,
            parser,
            blob_hash,
            PARSER_VERSION,
            parser_language(parser),
            fragment_count,
            0i64,
            "ok",
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok((fragment_count, 0))
}

/// Outline of a node: its fragments ordered by byte position.
pub(crate) fn node_fragments(conn: &Connection, node_id: &str) -> AnfsResult<Vec<FragmentRow>> {
    let mut stmt = conn.prepare(
        "SELECT fragment_id, kind, name, path, byte_start, byte_end
         FROM fragments WHERE node_id = ?1
         ORDER BY byte_start, byte_end",
    )?;
    let rows = stmt.query_map(params![node_id], |row| {
        Ok((
            row.get(0)?,
            row.get(1)?,
            row.get(2)?,
            row.get(3)?,
            row.get(4)?,
            row.get(5)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}
