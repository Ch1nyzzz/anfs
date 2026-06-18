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

use crate::events::{insert_edge, insert_event};
use crate::manifest::{json_field_spans, markdown_section_spans, read_node_bytes};
use crate::visibility::node_range_hidden;
use crate::{new_event_id, now_millis, sha256_hex, AnfsError, AnfsResult};

/// Bumped when a parser's output shape changes so stale rows can be detected.
const PARSER_VERSION: &str = "1";

/// (path, byte_offset, byte_length, kind) — the shape every span parser emits.
type SpanRow = (String, i64, i64, String);

/// (src_path, src_start, src_end, edge_kind, dst_name, evidence_start, evidence_end).
/// The parser records one edge per name reference at a site; cross-node
/// resolution to the candidate set happens at query time, never written back.
type EdgeRow = (String, i64, i64, String, String, i64, i64);

/// (fragment_id, kind, name, path, byte_start, byte_end) — outline row.
pub(crate) type FragmentRow = (String, String, Option<String>, String, i64, i64);

/// (src_fragment_id, src_kind, src_name, src_node_id, evidence_start, evidence_end).
pub(crate) type CallerRow = (String, String, Option<String>, String, i64, i64);

/// (node_id, fragment_id, name, kind, byte_start, byte_end, source_text).
pub(crate) type ContextItemRow = (String, String, Option<String>, String, i64, i64, String);

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
) -> AnfsResult<(Vec<SpanRow>, Vec<EdgeRow>)> {
    match parser {
        "span-markdown" => Ok((markdown_section_spans(conn, objects_dir, node_id)?, Vec::new())),
        "span-json" => Ok((json_field_spans(conn, objects_dir, node_id)?, Vec::new())),
        "tree-sitter-rust" => {
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let text = std::str::from_utf8(&bytes).map_err(|err| {
                AnfsError::PolicyDenied(format!(
                    "node {node_id} is not valid utf-8 Rust source: {err}"
                ))
            })?;
            parse_rust(text)
        }
        other => Err(AnfsError::PolicyDenied(format!(
            "unknown fragment parser {other}"
        ))),
    }
}

fn parser_language(parser: &str) -> Option<&'static str> {
    match parser {
        "span-markdown" => Some("markdown"),
        "span-json" => Some("json"),
        "tree-sitter-rust" => Some("rust"),
        _ => None,
    }
}

/// Extract top-level (and impl/trait/mod member) Rust symbols as fragments.
/// Edges (calls/imports) come in a later slice; this slice yields the outline.
fn parse_rust(text: &str) -> AnfsResult<(Vec<SpanRow>, Vec<EdgeRow>)> {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_rust::language())
        .map_err(|err| {
            AnfsError::StorageCorruption(format!("tree-sitter rust init failed: {err}"))
        })?;
    let tree = parser
        .parse(text, None)
        .ok_or_else(|| AnfsError::PolicyDenied("failed to parse Rust source".to_string()))?;
    let mut spans = Vec::new();
    let mut edges = Vec::new();
    collect_rust_symbols(tree.root_node(), text.as_bytes(), &mut spans, &mut edges);
    Ok((spans, edges))
}

fn node_text(node: tree_sitter::Node, src: &[u8]) -> Option<String> {
    src.get(node.start_byte()..node.end_byte())
        .and_then(|bytes| std::str::from_utf8(bytes).ok())
        .map(|text| text.to_string())
}

fn rust_node_name(node: tree_sitter::Node, src: &[u8]) -> Option<String> {
    if let Some(name) = node.child_by_field_name("name") {
        return node_text(name, src);
    }
    match node.kind() {
        "impl_item" => node
            .child_by_field_name("type")
            .and_then(|n| node_text(n, src)),
        "use_declaration" => node
            .child_by_field_name("argument")
            .and_then(|n| node_text(n, src)),
        _ => None,
    }
}

fn rust_kind(kind: &str) -> Option<&'static str> {
    match kind {
        "function_item" => Some("function"),
        "struct_item" => Some("struct"),
        "enum_item" => Some("enum"),
        "trait_item" => Some("trait"),
        "mod_item" => Some("module"),
        "const_item" => Some("const"),
        "static_item" => Some("static"),
        "type_item" => Some("type"),
        "macro_definition" => Some("macro"),
        "impl_item" => Some("impl"),
        "use_declaration" => Some("import"),
        _ => None,
    }
}

/// Best-effort callee name from a call_expression: the final identifier of the
/// callee (`foo()` -> foo, `a.bar()` -> bar, `A::baz()` -> baz).
fn call_name(call: tree_sitter::Node, src: &[u8]) -> Option<String> {
    let func = call.child_by_field_name("function")?;
    match func.kind() {
        "identifier" => node_text(func, src),
        "field_expression" => func
            .child_by_field_name("field")
            .and_then(|n| node_text(n, src)),
        "scoped_identifier" => func
            .child_by_field_name("name")
            .and_then(|n| node_text(n, src)),
        _ => node_text(func, src),
    }
}

/// Record one `calls` edge per call site inside a function body. dst is left as
/// a name; resolution to the candidate set happens at query time.
fn collect_calls(
    node: tree_sitter::Node,
    src: &[u8],
    src_path: &str,
    src_start: i64,
    src_end: i64,
    out: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "call_expression" {
            if let Some(name) = call_name(child, src) {
                out.push((
                    src_path.to_string(),
                    src_start,
                    src_end,
                    "calls".to_string(),
                    name,
                    child.start_byte() as i64,
                    child.end_byte() as i64,
                ));
            }
        }
        collect_calls(child, src, src_path, src_start, src_end, out);
    }
}

fn collect_rust_symbols(
    node: tree_sitter::Node,
    src: &[u8],
    out: &mut Vec<SpanRow>,
    edges: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        let kind = child.kind();
        // declaration_list (impl/trait/mod body) is a container: recurse, no row.
        if kind == "declaration_list" {
            collect_rust_symbols(child, src, out, edges);
            continue;
        }
        if let Some(frag_kind) = rust_kind(kind) {
            let name = rust_node_name(child, src);
            let path = name.clone().unwrap_or_else(|| frag_kind.to_string());
            let start = child.start_byte() as i64;
            let end = child.end_byte() as i64;
            out.push((path.clone(), start, end - start, frag_kind.to_string()));
            if kind == "function_item" {
                collect_calls(child, src, &path, start, end, edges);
            }
            // Recurse into containers to capture methods / associated items,
            // but not into function bodies (avoids local-fn noise).
            if matches!(kind, "impl_item" | "mod_item" | "trait_item") {
                collect_rust_symbols(child, src, out, edges);
            }
        }
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
    let (spans, edge_rows) = run_parser(conn, objects_dir, node_id, parser)?;

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

    let mut edge_count = 0i64;
    for (src_path, src_start, src_end, edge_kind, dst_name, evidence_start, evidence_end) in
        &edge_rows
    {
        let src_fragment_id = fragment_id(&blob_hash, parser, src_path, *src_start, *src_end);
        tx.execute(
            "INSERT OR REPLACE INTO fragment_edges
             (src_fragment_id, edge_kind, dst_name, dst_fragment_id,
              evidence_node_id, evidence_start, evidence_end)
             VALUES (?1, ?2, ?3, NULL, ?4, ?5, ?6)",
            params![
                src_fragment_id,
                edge_kind,
                dst_name,
                node_id,
                evidence_start,
                evidence_end
            ],
        )?;
        edge_count += 1;
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
            edge_count,
            "ok",
            now_millis()
        ],
    )?;
    tx.commit()?;
    Ok((fragment_count, edge_count))
}

/// Cross-node "who calls `name`": every `calls` edge targeting that name,
/// joined to its source fragment. This is the candidate-set resolution done at
/// the (here, whole-store) composition layer — no probability, just the exact
/// set of call sites across indexed nodes.
pub(crate) fn fragment_callers(conn: &Connection, name: &str) -> AnfsResult<Vec<CallerRow>> {
    let mut stmt = conn.prepare(
        "SELECT f.fragment_id, f.kind, f.name, f.node_id, e.evidence_start, e.evidence_end
         FROM fragment_edges e
         JOIN fragments f ON f.fragment_id = e.src_fragment_id
         WHERE e.edge_kind = 'calls' AND e.dst_name = ?1
         ORDER BY f.node_id, e.evidence_start",
    )?;
    let rows = stmt.query_map(params![name], |row| {
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

/// Conservative token estimate (ceil(bytes / 4)), matching the kernel's
/// existing answer-accounting estimator family.
fn estimate_tokens(byte_len: usize) -> i64 {
    ((byte_len + 3) / 4) as i64
}

/// The token-saving entrypoint: instead of reading whole files, pack the source
/// of the symbol named `seed_name` plus its callers, greedily, up to
/// `token_budget`. Policy-hidden byte ranges are never included
/// (`node_range_hidden`). Returns the packed items and the total token estimate.
pub(crate) fn context_pack(
    conn: &mut Connection,
    objects_dir: &Path,
    seed_name: &str,
    token_budget: i64,
    agent_id: Option<&str>,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<(Vec<ContextItemRow>, i64)> {
    // Working set: the definition(s) of seed_name, then its callers' fragments.
    let mut targets: Vec<(String, String, Option<String>, String, i64, i64)> = Vec::new();
    {
        let mut stmt = conn.prepare(
            "SELECT node_id, fragment_id, name, kind, byte_start, byte_end
             FROM fragments WHERE name = ?1 ORDER BY node_id, byte_start",
        )?;
        let rows = stmt.query_map(params![seed_name], |row| {
            Ok((
                row.get(0)?,
                row.get(1)?,
                row.get(2)?,
                row.get(3)?,
                row.get(4)?,
                row.get(5)?,
            ))
        })?;
        for row in rows {
            targets.push(row?);
        }
    }
    for caller in fragment_callers(conn, seed_name)? {
        let frag: Option<(String, String, Option<String>, String, i64, i64)> = conn
            .query_row(
                "SELECT node_id, fragment_id, name, kind, byte_start, byte_end
                 FROM fragments WHERE fragment_id = ?1",
                params![caller.0],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                        row.get(5)?,
                    ))
                },
            )
            .optional()?;
        if let Some(frag) = frag {
            targets.push(frag);
        }
    }

    let mut seen = std::collections::HashSet::new();
    let mut packed: Vec<ContextItemRow> = Vec::new();
    let mut tokens = 0i64;
    for (node_id, fragment_id, name, kind, start, end) in targets {
        if !seen.insert(fragment_id.clone()) {
            continue;
        }
        // Policy: never pack bytes the caller may not see.
        if node_range_hidden(conn, &node_id, start, end - start)? {
            continue;
        }
        let bytes = read_node_bytes(conn, objects_dir, &node_id)?;
        let source = match bytes
            .get(start as usize..end as usize)
            .and_then(|slice| std::str::from_utf8(slice).ok())
        {
            Some(text) => text.to_string(),
            None => continue,
        };
        let cost = estimate_tokens(source.len());
        // Always include at least one item; otherwise stop at the budget.
        if !packed.is_empty() && tokens + cost > token_budget {
            break;
        }
        tokens += cost;
        packed.push((node_id, fragment_id, name, kind, start, end, source));
        if tokens >= token_budget {
            break;
        }
    }

    // ANFS-native differentiator: when an agent identity is supplied, the
    // context request itself becomes an auditable event with input edges to the
    // packed nodes — so "what did the model see, and how many tokens" is
    // replayable, exactly like search/query/answer.
    if let Some(agent_id) = agent_id {
        let mut node_seen = std::collections::HashSet::new();
        let mut context_nodes: Vec<String> = Vec::new();
        for item in &packed {
            if node_seen.insert(item.0.clone()) {
                context_nodes.push(item.0.clone());
            }
        }
        let payload = serde_json::json!({
            "seed": seed_name,
            "token_budget": token_budget,
            "token_estimate": tokens,
            "item_count": packed.len(),
            "nodes": context_nodes,
        })
        .to_string();
        let event_id = new_event_id();
        let tx = conn.transaction()?;
        insert_event(
            &tx,
            &event_id,
            "code_context_query",
            Some(agent_id),
            run_id,
            tool_call_id,
            None,
            Some(&payload),
        )?;
        for (index, node_id) in context_nodes.iter().enumerate() {
            insert_edge(
                &tx,
                &event_id,
                "input",
                node_id,
                &format!("context_node:{index}"),
                None,
            )?;
        }
        tx.commit()?;
    }

    Ok((packed, tokens))
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
