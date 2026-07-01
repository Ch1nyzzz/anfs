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
use crate::manifest::{
    json_field_spans, markdown_field_spans, markdown_section_spans, read_node_bytes,
};
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

/// (fragment_id, kind, name, path, byte_start, byte_end, parent_fragment_id) —
/// outline row; `parent_fragment_id` gives the containment tree.
pub(crate) type FragmentRow = (String, String, Option<String>, String, i64, i64, Option<String>);

/// (src_fragment_id, src_kind, src_name, src_node_id, evidence_start, evidence_end).
pub(crate) type CallerRow = (String, String, Option<String>, String, i64, i64);

/// (node_id, fragment_id, name, kind, byte_start, byte_end, source_text).
pub(crate) type ContextItemRow = (String, String, Option<String>, String, i64, i64, String);

/// (fragment_id, node_id, name, kind, byte_start, byte_end, depth, source_text).
/// A symbol reached while walking the call graph, with its policy-filtered source.
pub(crate) type CallGraphNodeRow = (String, String, Option<String>, String, i64, i64, i64, String);

/// (src_fragment_id, src_name, dst_name, dst_fragment_id, evidence_node_id,
/// evidence_start, evidence_end). An edge always means "src calls dst".
/// `dst_fragment_id` is None when the callee name resolves to no indexed
/// definition (external); a name with several definitions yields one edge per
/// candidate — the same multiplicity-as-evidence rule as fragment_callers.
pub(crate) type CallGraphEdgeRow =
    (String, Option<String>, String, Option<String>, String, i64, i64);

/// (fragment_id, node_id, name, kind, byte_start, byte_end) — a resolved
/// definition, the working unit of the call-graph walk.
type DefRow = (String, String, Option<String>, String, i64, i64);

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
        "span-markdown" => {
            // One Markdown slice: frontmatter fields + body sections/blocks, in
            // the same `fragments` table policy reads from. Each family errors
            // when its region is absent; a doc needs at least one.
            let mut spans = markdown_field_spans(conn, objects_dir, node_id).unwrap_or_default();
            spans.extend(markdown_section_spans(conn, objects_dir, node_id).unwrap_or_default());
            if spans.is_empty() {
                return Err(AnfsError::PolicyDenied(format!(
                    "node {node_id} has no Markdown frontmatter fields or body blocks"
                )));
            }
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let edges = markdown_reference_edges(&spans, &bytes);
            Ok((spans, edges))
        }
        "span-json" => {
            let spans = json_field_spans(conn, objects_dir, node_id)?;
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let edges = json_reference_edges(&spans, &bytes);
            Ok((spans, edges))
        }
        "tree-sitter-rust" => {
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let text = std::str::from_utf8(&bytes).map_err(|err| {
                AnfsError::PolicyDenied(format!(
                    "node {node_id} is not valid utf-8 Rust source: {err}"
                ))
            })?;
            parse_rust(text)
        }
        "tree-sitter-python" => {
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let text = std::str::from_utf8(&bytes).map_err(|err| {
                AnfsError::PolicyDenied(format!(
                    "node {node_id} is not valid utf-8 Python source: {err}"
                ))
            })?;
            parse_python(text)
        }
        "tree-sitter-typescript" | "tree-sitter-go" | "tree-sitter-java"
        | "tree-sitter-swift" => {
            let bytes = read_node_bytes(conn, objects_dir, node_id)?;
            let text = std::str::from_utf8(&bytes).map_err(|err| {
                AnfsError::PolicyDenied(format!("node {node_id} is not valid utf-8: {err}"))
            })?;
            parse_tree_sitter(parser, text)
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
        "tree-sitter-python" => Some("python"),
        "tree-sitter-typescript" => Some("typescript"),
        "tree-sitter-go" => Some("go"),
        "tree-sitter-java" => Some("java"),
        "tree-sitter-swift" => Some("swift"),
        _ => None,
    }
}

/// `references` edges for JSON: a `$ref` string value points at the last segment
/// of its pointer (`"#/definitions/Owner"` -> `Owner`). The *holder* object (the
/// field carrying the `$ref`) is the source, so `callees("owner")` reaches
/// `Owner`; the pointer string bytes are the evidence. Reuses the field spans
/// already computed — no re-parse. Conservative: only string `$ref` keys whose
/// holder is a named span.
fn json_reference_edges(spans: &[SpanRow], bytes: &[u8]) -> Vec<EdgeRow> {
    let mut edges = Vec::new();
    for (path, offset, length, kind) in spans {
        let holder_path = match (kind == "string").then(|| path.strip_suffix(".[\"$ref\"]")).flatten()
        {
            Some(holder) => holder,
            None => continue,
        };
        let holder = match spans.iter().find(|(p, _, _, _)| p == holder_path) {
            Some((p, off, len, _)) => (p, *off, *off + *len),
            None => continue,
        };
        let (start, end) = (*offset, *offset + *length);
        let raw = match bytes
            .get(start as usize..end as usize)
            .and_then(|b| std::str::from_utf8(b).ok())
        {
            Some(text) => text.trim().trim_matches('"'),
            None => continue,
        };
        let target = raw.rsplit('/').next().unwrap_or(raw);
        if target.is_empty() {
            continue;
        }
        edges.push((
            holder.0.clone(),
            holder.1,
            holder.2,
            "references".to_string(),
            target.to_string(),
            start,
            end,
        ));
    }
    edges
}

fn is_heading_kind(kind: &str) -> bool {
    let bytes = kind.as_bytes();
    bytes.len() == 2 && bytes[0] == b'h' && bytes[1].is_ascii_digit()
}

/// The tightest heading span whose byte range covers `pos`, or None when the
/// position sits above the first heading.
fn enclosing_heading<'a>(headings: &[&'a SpanRow], pos: i64) -> Option<&'a SpanRow> {
    let mut best: Option<&SpanRow> = None;
    for heading in headings {
        let (start, end) = (heading.1, heading.1 + heading.2);
        if start <= pos && pos < end && best.map_or(true, |b| heading.2 < b.2) {
            best = Some(heading);
        }
    }
    best
}

/// `references` edges for Markdown: each inline `[text](dest)` link is an edge
/// from its enclosing heading section to the destination (anchor `#name` -> the
/// section it targets; otherwise the raw destination as an external name). The
/// whole `[..](..)` span is the evidence. Fenced code and images are skipped.
fn markdown_reference_edges(spans: &[SpanRow], bytes: &[u8]) -> Vec<EdgeRow> {
    let headings: Vec<&SpanRow> = spans.iter().filter(|s| is_heading_kind(&s.3)).collect();
    let mut edges = Vec::new();
    for (dst_name, ev_start, ev_end) in scan_markdown_links(bytes) {
        if let Some(section) = enclosing_heading(&headings, ev_start as i64) {
            edges.push((
                section.0.clone(),
                section.1,
                section.1 + section.2,
                "references".to_string(),
                dst_name,
                ev_start as i64,
                ev_end as i64,
            ));
        }
    }
    edges
}

/// Destination -> a resolvable name: `#anchor` yields the anchor, everything
/// else yields the first whitespace-delimited token (URL/path), stripped of
/// angle brackets and any trailing link title.
fn link_dest_name(dest: &str) -> String {
    let token = dest.split_whitespace().next().unwrap_or(dest);
    let token = token.trim_start_matches('<').trim_end_matches('>');
    token.strip_prefix('#').unwrap_or(token).to_string()
}

/// Conservative inline-link scan: line-based, skips fenced code blocks, and for
/// each unescaped `[text](dest)` (not an `![image]`) records
/// `(dest_name, link_start, link_end)`. Destinations end at the first `)`.
fn scan_markdown_links(bytes: &[u8]) -> Vec<(String, usize, usize)> {
    let mut out = Vec::new();
    let mut in_fence = false;
    let mut line_start = 0usize;
    let n = bytes.len();
    let mut i = 0usize;
    while i <= n {
        if i == n || bytes[i] == b'\n' {
            let line = &bytes[line_start..i];
            let indent = line.iter().take_while(|b| **b == b' ' || **b == b'\t').count();
            let trimmed = &line[indent..];
            if indent <= 3 && (trimmed.starts_with(b"```") || trimmed.starts_with(b"~~~")) {
                in_fence = !in_fence;
            } else if !in_fence {
                scan_line_links(bytes, line_start, i, &mut out);
            }
            line_start = i + 1;
        }
        i += 1;
    }
    out
}

fn scan_line_links(bytes: &[u8], start: usize, end: usize, out: &mut Vec<(String, usize, usize)>) {
    let mut i = start;
    while i < end {
        match bytes[i] {
            // Skip an inline code span so its contents can't look like a link.
            b'`' => {
                let run = bytes[i..end].iter().take_while(|b| **b == b'`').count();
                let mut j = i + run;
                while j + run <= end {
                    if bytes[j] == b'`' && bytes[j..].iter().take_while(|b| **b == b'`').count() == run {
                        break;
                    }
                    j += 1;
                }
                i = (j + run).min(end);
            }
            b'[' if i == start || bytes[i - 1] != b'\\' => {
                // An image `![alt](src)` is not a link edge.
                if i > start && bytes[i - 1] == b'!' {
                    i += 1;
                    continue;
                }
                if let Some(link_end) = parse_inline_link(bytes, i, end, out) {
                    i = link_end;
                    continue;
                }
                i += 1;
            }
            _ => i += 1,
        }
    }
}

/// Parse `[text](dest)` starting at `open` (`bytes[open] == b'['`). On success
/// pushes `(dest_name, open, close+1)` and returns the byte after `)`.
fn parse_inline_link(
    bytes: &[u8],
    open: usize,
    end: usize,
    out: &mut Vec<(String, usize, usize)>,
) -> Option<usize> {
    let text_close = bytes[open + 1..end].iter().position(|b| *b == b']')? + open + 1;
    if bytes.get(text_close + 1) != Some(&b'(') {
        return None;
    }
    let dest_start = text_close + 2;
    let dest_close = bytes[dest_start..end].iter().position(|b| *b == b')')? + dest_start;
    let dest = std::str::from_utf8(&bytes[dest_start..dest_close]).ok()?.trim();
    let name = link_dest_name(dest);
    if !name.is_empty() {
        out.push((name, open, dest_close + 1));
    }
    Some(dest_close + 1)
}

/// The parent of each span: the tightest *strictly* larger span that contains
/// it, by byte range. Format-agnostic — code impl/method, JSON object/field,
/// Markdown section/subsection all nest, so containment is the one rule. Equal
/// ranges never nest (avoids self/cycles); ties break deterministically.
fn derive_parents(blob_hash: &str, parser: &str, spans: &[SpanRow]) -> Vec<Option<String>> {
    let ranges: Vec<(i64, i64)> = spans.iter().map(|(_, off, len, _)| (*off, *off + *len)).collect();
    let ids: Vec<String> = spans
        .iter()
        .map(|(path, off, len, _)| fragment_id(blob_hash, parser, path, *off, *off + *len))
        .collect();
    (0..spans.len())
        .map(|i| {
            let (fs, fe) = ranges[i];
            let fw = fe - fs;
            let mut best: Option<usize> = None;
            for j in 0..spans.len() {
                if i == j {
                    continue;
                }
                let (ps, pe) = ranges[j];
                if ps <= fs && pe >= fe && (pe - ps) > fw && better_parent(&ranges, &ids, j, best) {
                    best = Some(j);
                }
            }
            best.map(|b| ids[b].clone())
        })
        .collect()
}

/// Prefer the tighter (smaller-width) container; on equal width prefer the later
/// start, then the earlier end, then the smaller id — total and deterministic.
fn better_parent(ranges: &[(i64, i64)], ids: &[String], cand: usize, best: Option<usize>) -> bool {
    let best = match best {
        None => return true,
        Some(b) => b,
    };
    let (cs, ce) = ranges[cand];
    let (bs, be) = ranges[best];
    (ce - cs, bs, be, &ids[best]) < (be - bs, cs, ce, &ids[cand])
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

/// Extract Python symbols (functions, classes, methods) as fragments and
/// `calls` edges, mirroring the Rust slice. Same generic tables downstream.
fn parse_python(text: &str) -> AnfsResult<(Vec<SpanRow>, Vec<EdgeRow>)> {
    let mut parser = tree_sitter::Parser::new();
    parser
        .set_language(&tree_sitter_python::language())
        .map_err(|err| {
            AnfsError::StorageCorruption(format!("tree-sitter python init failed: {err}"))
        })?;
    let tree = parser
        .parse(text, None)
        .ok_or_else(|| AnfsError::PolicyDenied("failed to parse Python source".to_string()))?;
    let mut spans = Vec::new();
    let mut edges = Vec::new();
    collect_python_symbols(tree.root_node(), text.as_bytes(), &mut spans, &mut edges);
    Ok((spans, edges))
}

fn python_kind(kind: &str) -> Option<&'static str> {
    match kind {
        "function_definition" => Some("function"),
        "class_definition" => Some("class"),
        _ => None,
    }
}

/// Callee name from a Python `call`: `foo()` -> foo, `a.bar()` -> bar.
fn python_call_name(call: tree_sitter::Node, src: &[u8]) -> Option<String> {
    let func = call.child_by_field_name("function")?;
    match func.kind() {
        "identifier" => node_text(func, src),
        "attribute" => func
            .child_by_field_name("attribute")
            .and_then(|n| node_text(n, src)),
        _ => None,
    }
}

fn collect_python_calls(
    node: tree_sitter::Node,
    src: &[u8],
    src_path: &str,
    src_start: i64,
    src_end: i64,
    out: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == "call" {
            if let Some(name) = python_call_name(child, src) {
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
        collect_python_calls(child, src, src_path, src_start, src_end, out);
    }
}

fn collect_python_symbols(
    node: tree_sitter::Node,
    src: &[u8],
    out: &mut Vec<SpanRow>,
    edges: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        let kind = child.kind();
        if let Some(frag_kind) = python_kind(kind) {
            let name = child
                .child_by_field_name("name")
                .and_then(|n| node_text(n, src));
            let path = name.unwrap_or_else(|| frag_kind.to_string());
            let start = child.start_byte() as i64;
            let end = child.end_byte() as i64;
            out.push((path.clone(), start, end - start, frag_kind.to_string()));
            if kind == "function_definition" {
                collect_python_calls(child, src, &path, start, end, edges);
            }
            // Recurse into classes for methods/nested classes, but not into
            // function bodies (avoids local-fn noise), mirroring the Rust slice.
            if kind == "class_definition" {
                collect_python_symbols(child, src, out, edges);
            }
        } else {
            // Descend through containers (module, block, decorated_definition,
            // control flow) to reach the definitions nested inside them.
            collect_python_symbols(child, src, out, edges);
        }
    }
}

/// A defined symbol's role in traversal: a function-like node collects its
/// calls and is not descended for more symbols; a container (class/interface)
/// is descended to reach its members.
enum DefRole {
    Func,
    Container,
}

/// Per-language definition node kinds for the generic tree-sitter slice.
fn def_role(parser: &str, kind: &str) -> Option<(&'static str, DefRole)> {
    match parser {
        "tree-sitter-typescript" => match kind {
            "function_declaration" => Some(("function", DefRole::Func)),
            "method_definition" => Some(("method", DefRole::Func)),
            "class_declaration" => Some(("class", DefRole::Container)),
            "interface_declaration" => Some(("interface", DefRole::Container)),
            _ => None,
        },
        "tree-sitter-go" => match kind {
            "function_declaration" => Some(("function", DefRole::Func)),
            "method_declaration" => Some(("method", DefRole::Func)),
            "type_spec" => Some(("type", DefRole::Container)),
            _ => None,
        },
        "tree-sitter-java" => match kind {
            "method_declaration" => Some(("method", DefRole::Func)),
            "constructor_declaration" => Some(("constructor", DefRole::Func)),
            "class_declaration" => Some(("class", DefRole::Container)),
            "interface_declaration" => Some(("interface", DefRole::Container)),
            "enum_declaration" => Some(("enum", DefRole::Container)),
            _ => None,
        },
        "tree-sitter-swift" => match kind {
            "function_declaration" => Some(("function", DefRole::Func)),
            "init_declaration" => Some(("constructor", DefRole::Func)),
            "class_declaration" => Some(("class", DefRole::Container)),
            "protocol_declaration" => Some(("protocol", DefRole::Container)),
            _ => None,
        },
        _ => None,
    }
}

fn is_call_node(parser: &str, kind: &str) -> bool {
    match parser {
        "tree-sitter-java" => kind == "method_invocation",
        _ => kind == "call_expression",
    }
}

/// Callee name from a call node, per language.
fn generic_call_name(parser: &str, call: tree_sitter::Node, src: &[u8]) -> Option<String> {
    if parser == "tree-sitter-java" {
        return call
            .child_by_field_name("name")
            .and_then(|n| node_text(n, src));
    }
    if parser == "tree-sitter-swift" {
        // Swift call_expression: first child is the callee — a simple_identifier
        // (`foo(...)`) or a navigation_expression (`a.foo(...)`) whose last
        // navigation_suffix names the method.
        let callee = call.named_child(0)?;
        return match callee.kind() {
            "simple_identifier" => node_text(callee, src),
            _ => {
                // walk to the last simple_identifier in the callee expression
                let mut cursor = callee.walk();
                let mut last = None;
                for d in callee.named_children(&mut cursor) {
                    if d.kind() == "simple_identifier" {
                        last = Some(d);
                    } else if d.kind() == "navigation_suffix" {
                        if let Some(id) = d.child_by_field_name("suffix") {
                            last = Some(id);
                        }
                    }
                }
                last.and_then(|n| node_text(n, src))
            }
        };
    }
    let func = call.child_by_field_name("function")?;
    match func.kind() {
        "identifier" => node_text(func, src),
        // TS `a.b()` and Go `a.b()`
        "member_expression" => func
            .child_by_field_name("property")
            .and_then(|n| node_text(n, src)),
        "selector_expression" => func
            .child_by_field_name("field")
            .and_then(|n| node_text(n, src)),
        _ => node_text(func, src),
    }
}

fn parse_tree_sitter(parser: &str, text: &str) -> AnfsResult<(Vec<SpanRow>, Vec<EdgeRow>)> {
    let language = match parser {
        "tree-sitter-typescript" => tree_sitter_typescript::language_tsx(),
        "tree-sitter-go" => tree_sitter_go::language(),
        "tree-sitter-java" => tree_sitter_java::language(),
        "tree-sitter-swift" => tree_sitter_swift::language(),
        other => {
            return Err(AnfsError::PolicyDenied(format!("unknown parser {other}")));
        }
    };
    let mut p = tree_sitter::Parser::new();
    p.set_language(&language)
        .map_err(|err| AnfsError::StorageCorruption(format!("tree-sitter init failed: {err}")))?;
    let tree = p
        .parse(text, None)
        .ok_or_else(|| AnfsError::PolicyDenied(format!("failed to parse {parser} source")))?;
    let mut spans = Vec::new();
    let mut edges = Vec::new();
    collect_generic(parser, tree.root_node(), text.as_bytes(), &mut spans, &mut edges);
    Ok((spans, edges))
}

fn collect_generic_calls(
    parser: &str,
    node: tree_sitter::Node,
    src: &[u8],
    src_path: &str,
    src_start: i64,
    src_end: i64,
    out: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if is_call_node(parser, child.kind()) {
            if let Some(name) = generic_call_name(parser, child, src) {
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
        collect_generic_calls(parser, child, src, src_path, src_start, src_end, out);
    }
}

/// TypeScript/JS: `const f = () => {}` and class field `f = () => {}` are the
/// dominant function form (React); capture the declarator as a function whose
/// body is the arrow/function value.
fn ts_value_function<'a>(
    node: tree_sitter::Node<'a>,
    src: &[u8],
) -> Option<(String, tree_sitter::Node<'a>)> {
    if !matches!(node.kind(), "variable_declarator" | "public_field_definition") {
        return None;
    }
    let value = node.child_by_field_name("value")?;
    if !matches!(
        value.kind(),
        "arrow_function" | "function" | "function_expression"
    ) {
        return None;
    }
    let name = node
        .child_by_field_name("name")
        .and_then(|n| node_text(n, src))?;
    Some((name, value))
}

fn collect_generic(
    parser: &str,
    node: tree_sitter::Node,
    src: &[u8],
    out: &mut Vec<SpanRow>,
    edges: &mut Vec<EdgeRow>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if parser == "tree-sitter-typescript" {
            if let Some((name, body)) = ts_value_function(child, src) {
                let start = child.start_byte() as i64;
                let end = child.end_byte() as i64;
                out.push((name, start, end - start, "function".to_string()));
                let path = node_text(child, src).unwrap_or_default();
                // calls live in the arrow/function body; attribute them to this symbol
                let sym = child
                    .child_by_field_name("name")
                    .and_then(|n| node_text(n, src))
                    .unwrap_or(path);
                collect_generic_calls(parser, body, src, &sym, start, end, edges);
                continue;
            }
        }
        if let Some((frag_kind, role)) = def_role(parser, child.kind()) {
            let name = child
                .child_by_field_name("name")
                .and_then(|n| node_text(n, src));
            let path = name.unwrap_or_else(|| frag_kind.to_string());
            let start = child.start_byte() as i64;
            let end = child.end_byte() as i64;
            out.push((path.clone(), start, end - start, frag_kind.to_string()));
            match role {
                DefRole::Func => {
                    collect_generic_calls(parser, child, src, &path, start, end, edges)
                }
                DefRole::Container => collect_generic(parser, child, src, out, edges),
            }
        } else {
            collect_generic(parser, child, src, out, edges);
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

    let parents = derive_parents(&blob_hash, parser, &spans);
    let mut fragment_count = 0i64;
    for ((path, offset, length, kind), parent) in spans.iter().zip(&parents) {
        let start = *offset;
        let end = *offset + *length;
        let fragment_id = fragment_id(&blob_hash, parser, path, start, end);
        let name = name_from_path(path);
        tx.execute(
            "INSERT OR REPLACE INTO fragments
             (fragment_id, node_id, blob_hash, parser, parser_version,
              kind, name, path, byte_start, byte_end, parent_fragment_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
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
                end,
                parent
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
         WHERE e.dst_name = ?1
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

/// Definition fragment(s) of a symbol name, the working units of the walk.
fn fragment_defs_by_name(conn: &Connection, name: &str) -> AnfsResult<Vec<DefRow>> {
    let mut stmt = conn.prepare(
        "SELECT fragment_id, node_id, name, kind, byte_start, byte_end
         FROM fragments WHERE name = ?1 ORDER BY node_id, byte_start",
    )?;
    let rows = stmt.query_map(params![name], |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// A single definition fragment by id (used to surface a caller as a graph node).
fn fragment_by_id(conn: &Connection, fragment_id: &str) -> AnfsResult<Option<DefRow>> {
    Ok(conn
        .query_row(
            "SELECT fragment_id, node_id, name, kind, byte_start, byte_end
             FROM fragments WHERE fragment_id = ?1",
            params![fragment_id],
            |row| {
                Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?, row.get(5)?))
            },
        )
        .optional()?)
}

/// Outgoing reference edges of a fragment (`calls` for code, `references` for
/// docs/data): `(dst_name, evidence_node_id, start, end)`.
fn fragment_callees(
    conn: &Connection,
    src_fragment_id: &str,
) -> AnfsResult<Vec<(String, String, i64, i64)>> {
    let mut stmt = conn.prepare(
        "SELECT dst_name, evidence_node_id, evidence_start, evidence_end
         FROM fragment_edges
         WHERE src_fragment_id = ?1
         ORDER BY evidence_start",
    )?;
    let rows = stmt.query_map(params![src_fragment_id], |row| {
        Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}

/// Walk the call graph from `seed_name`, returning the reachable symbols (with
/// their policy-filtered source) and the call edges between them, in ONE call.
///
/// This is the higher-level retrieval `context_pack` (one hop) and
/// `fragment_callers` (reverse, one hop) could not give: a depth-bounded,
/// direction-aware traversal of the `calls` edges, so an agent gets a whole
/// mechanism's skeleton — `spawn -> schedule -> push -> wake -> poll` — in a
/// single auditable payload instead of dozens of outline/read probes.
///
/// `direction` is "callees" (follow the execution flow downward) or "callers"
/// (the multi-hop blast radius upward). Traversal is breadth-first and
/// cycle-safe (each fragment is placed once); `token_budget` greedily bounds
/// the packed source exactly as `context_pack` does; policy-hidden ranges are
/// never surfaced. Like `context_pack`, an `agent_id` makes the request an
/// auditable `code_call_graph` event with input edges to the touched nodes.
#[allow(clippy::too_many_arguments)]
pub(crate) fn call_graph(
    conn: &mut Connection,
    objects_dir: &Path,
    seed_name: &str,
    direction: &str,
    max_depth: i64,
    max_fanout: i64,
    token_budget: i64,
    agent_id: Option<&str>,
    run_id: Option<&str>,
    tool_call_id: Option<&str>,
) -> AnfsResult<(Vec<CallGraphNodeRow>, Vec<CallGraphEdgeRow>, i64)> {
    if direction != "callees" && direction != "callers" {
        return Err(AnfsError::PolicyDenied(
            "direction must be 'callees' or 'callers'".to_string(),
        ));
    }
    let max_depth = max_depth.max(0);
    let max_fanout = max_fanout.max(1);

    let mut nodes: Vec<CallGraphNodeRow> = Vec::new();
    let mut edges: Vec<CallGraphEdgeRow> = Vec::new();
    let mut visited: std::collections::HashSet<String> = std::collections::HashSet::new();
    let mut tokens = 0i64;
    let mut budget_reached = false;

    let mut frontier: Vec<DefRow> = fragment_defs_by_name(conn, seed_name)?;
    let mut depth = 0i64;

    while !frontier.is_empty() && !budget_reached {
        let mut next: Vec<DefRow> = Vec::new();
        for (fragment_id, node_id, name, kind, start, end) in std::mem::take(&mut frontier) {
            if !visited.insert(fragment_id.clone()) {
                continue;
            }
            // Policy: never surface bytes the caller may not see.
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
            // Always include the seed; otherwise stop at the budget.
            if !nodes.is_empty() && tokens + cost > token_budget {
                budget_reached = true;
                break;
            }
            tokens += cost;
            nodes.push((
                fragment_id.clone(),
                node_id.clone(),
                name.clone(),
                kind,
                start,
                end,
                depth,
                source,
            ));
            if tokens >= token_budget {
                budget_reached = true;
            }
            if depth >= max_depth || budget_reached {
                continue;
            }

            // Expand one hop in the requested direction.
            if direction == "callees" {
                for (dst_name, ev_node, ev_start, ev_end) in fragment_callees(conn, &fragment_id)? {
                    let defs = fragment_defs_by_name(conn, &dst_name)?;
                    if defs.is_empty() || defs.len() as i64 > max_fanout {
                        // Unresolved (external) OR too ambiguous to follow without
                        // type info — `clone`/`next`/`poll` match dozens of unrelated
                        // defs. Record one edge so the call is visible, but don't
                        // recurse into the fan-out (a None target marks both cases).
                        edges.push((
                            fragment_id.clone(),
                            name.clone(),
                            dst_name,
                            None,
                            ev_node,
                            ev_start,
                            ev_end,
                        ));
                    } else {
                        for def in &defs {
                            edges.push((
                                fragment_id.clone(),
                                name.clone(),
                                dst_name.clone(),
                                Some(def.0.clone()),
                                ev_node.clone(),
                                ev_start,
                                ev_end,
                            ));
                            if !visited.contains(&def.0) {
                                next.push(def.clone());
                            }
                        }
                    }
                }
            } else {
                let here = name.clone().unwrap_or_default();
                for (c_frag, _c_kind, c_name, c_node, ev_start, ev_end) in
                    fragment_callers(conn, &here)?
                {
                    edges.push((
                        c_frag.clone(),
                        c_name,
                        here.clone(),
                        Some(fragment_id.clone()),
                        c_node,
                        ev_start,
                        ev_end,
                    ));
                    if !visited.contains(&c_frag) {
                        if let Some(def) = fragment_by_id(conn, &c_frag)? {
                            next.push(def);
                        }
                    }
                }
            }
        }
        frontier = next;
        depth += 1;
        if depth > max_depth {
            break;
        }
    }

    // ANFS-native: the traversal itself is an auditable, replayable event with
    // input edges to every node whose source it surfaced.
    if let Some(agent_id) = agent_id {
        let mut node_seen = std::collections::HashSet::new();
        let mut context_nodes: Vec<String> = Vec::new();
        for item in &nodes {
            if node_seen.insert(item.1.clone()) {
                context_nodes.push(item.1.clone());
            }
        }
        let payload = serde_json::json!({
            "seed": seed_name,
            "direction": direction,
            "max_depth": max_depth,
            "token_budget": token_budget,
            "token_estimate": tokens,
            "node_count": nodes.len(),
            "edge_count": edges.len(),
            "nodes": context_nodes,
        })
        .to_string();
        let event_id = new_event_id();
        let tx = conn.transaction()?;
        insert_event(
            &tx,
            &event_id,
            "code_call_graph",
            Some(agent_id),
            run_id,
            tool_call_id,
            None,
            Some(&payload),
        )?;
        for (index, node_id) in context_nodes.iter().enumerate() {
            insert_edge(&tx, &event_id, "input", node_id, &format!("context_node:{index}"), None)?;
        }
        tx.commit()?;
    }

    Ok((nodes, edges, tokens))
}

/// Byte range `(offset, length)` of the fragment at `path` for `parser`,
/// indexing the node on demand (idempotent) so a field is sliced once — by the
/// parser into `fragments` — and policy labels ride that same canonical slice
/// instead of re-parsing the file per label.
pub(crate) fn fragment_span_by_path(
    conn: &mut Connection,
    objects_dir: &Path,
    node_id: &str,
    parser: &str,
    path: &str,
) -> AnfsResult<(i64, i64)> {
    index_node_fragments(conn, objects_dir, node_id, parser)?;
    conn.query_row(
        "SELECT byte_start, byte_end FROM fragments
         WHERE node_id = ?1 AND parser = ?2 AND path = ?3",
        params![node_id, parser, path],
        |row| {
            let start: i64 = row.get(0)?;
            let end: i64 = row.get(1)?;
            Ok((start, end - start))
        },
    )
    .optional()?
    .ok_or_else(|| {
        AnfsError::PolicyDenied(format!(
            "fragment path {path} not found in node {node_id} ({parser})"
        ))
    })
}

/// Outline of a node: its fragments ordered by byte position.
pub(crate) fn node_fragments(conn: &Connection, node_id: &str) -> AnfsResult<Vec<FragmentRow>> {
    let mut stmt = conn.prepare(
        "SELECT fragment_id, kind, name, path, byte_start, byte_end, parent_fragment_id
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
            row.get(6)?,
        ))
    })?;
    let mut out = Vec::new();
    for row in rows {
        out.push(row?);
    }
    Ok(out)
}
