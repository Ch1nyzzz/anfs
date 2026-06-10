//! Conservative byte-span parser for Markdown bodies, YAML frontmatter, and JSON fields. Internal engine for the manifest field-span API; spans must stay byte-exact because fragment policy labels attach to them.

use serde_json::Value;
use std::collections::HashSet;

use crate::{AnfsError, AnfsResult};

#[derive(Clone)]
struct MarkdownAliasAnchor {
    name: String,
    path: String,
}

#[derive(Clone)]
struct MarkdownAliasRef {
    name: String,
    path: String,
    offset: i64,
    length: i64,
}

#[derive(Clone)]
struct MarkdownMergeRef {
    name: String,
    path: String,
    offset: i64,
    length: i64,
}

pub(crate) fn markdown_frontmatter_field_spans(
    text: &str,
    node_id: &str,
) -> AnfsResult<Vec<crate::MarkdownFieldSpanRow>> {
    #[derive(Clone)]
    struct FrontmatterContainer {
        indent: usize,
        path: String,
        start: usize,
        end: usize,
        kind: &'static str,
        item_count: usize,
    }

    fn close_frontmatter_containers(
        containers: &mut Vec<FrontmatterContainer>,
        max_indent: usize,
        spans: &mut Vec<crate::MarkdownFieldSpanRow>,
    ) {
        while containers
            .last()
            .map(|container| container.indent >= max_indent)
            .unwrap_or(false)
        {
            if let Some(container) = containers.pop() {
                if container.end > container.start {
                    spans.push((
                        container.path,
                        container.start as i64,
                        (container.end - container.start) as i64,
                        container.kind.to_string(),
                    ));
                }
            }
        }
    }

    fn active_frontmatter_path(containers: &[FrontmatterContainer], key: &str) -> String {
        let mut path = containers
            .last()
            .map(|container| container.path.clone())
            .unwrap_or_else(|| "frontmatter".to_string());
        push_frontmatter_path_segment(&mut path, key);
        path
    }

    let bytes = text.as_bytes();
    let (first_line, mut cursor) = next_line_bounds(bytes, 0).ok_or_else(|| {
        AnfsError::PolicyDenied(format!(
            "node {node_id} does not contain Markdown frontmatter"
        ))
    })?;
    if trim_line_ascii(&bytes[first_line.0..first_line.1]) != b"---" {
        return Err(AnfsError::PolicyDenied(format!(
            "node {node_id} does not start with Markdown frontmatter"
        )));
    }

    let mut spans = Vec::new();
    let mut containers: Vec<FrontmatterContainer> = Vec::new();
    let mut alias_anchors: Vec<MarkdownAliasAnchor> = Vec::new();
    let mut alias_refs: Vec<MarkdownAliasRef> = Vec::new();
    let mut merge_refs: Vec<MarkdownMergeRef> = Vec::new();
    let mut found_closing_fence = false;
    while let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) {
        cursor = next_cursor;
        let line = &bytes[line_start..line_end];
        let trimmed = trim_line_ascii(line);
        if trimmed == b"---" {
            close_frontmatter_containers(&mut containers, 0, &mut spans);
            found_closing_fence = true;
            break;
        }
        if trimmed.is_empty() || trimmed.starts_with(b"#") {
            if let Some(container) = containers.last_mut() {
                container.end = next_cursor;
            }
            continue;
        }
        let indent = leading_ascii_ws(line);
        if line[..indent].contains(&b'\t') {
            continue;
        }

        if trimmed.starts_with(b"-") {
            close_frontmatter_containers(&mut containers, indent, &mut spans);
            let Some(parent) = containers.last_mut() else {
                continue;
            };
            if indent <= parent.indent {
                continue;
            }
            parent.kind = "array";
            parent.end = next_cursor;
            let item_index = parent.item_count;
            parent.item_count += 1;
            let dash_rel = line.iter().position(|byte| *byte == b'-').unwrap_or(indent);
            let item_value_start_rel = dash_rel + 1 + leading_ascii_ws(&line[(dash_rel + 1)..]);
            let item_value_end_rel = trim_trailing_ascii_ws_end(line, line.len());
            if item_value_start_rel >= item_value_end_rel {
                continue;
            }
            let item_value = &line[item_value_start_rel..item_value_end_rel];
            let (_item_payload_start, item_payload) =
                markdown_frontmatter_value_payload(item_value);
            let item_is_inline_container = markdown_inline_mapping_bounds(item_payload).is_some()
                || markdown_inline_sequence_bounds(item_payload).is_some();
            if !item_is_inline_container {
                let Some(colon_rel) = find_unquoted_byte(item_value, b':') else {
                    let path = format!("{}[{item_index}]", parent.path);
                    push_markdown_frontmatter_value_spans(
                        path.clone(),
                        line_start + item_value_start_rel,
                        item_value,
                        &mut spans,
                    );
                    record_markdown_frontmatter_value_references(
                        &path,
                        line_start + item_value_start_rel,
                        item_value,
                        &mut alias_anchors,
                        &mut alias_refs,
                        &mut merge_refs,
                    );
                    continue;
                };
                let key = trim_line_ascii(&item_value[..colon_rel]);
                let Some(key) = parse_frontmatter_key(key) else {
                    continue;
                };
                let field_value_start_rel = item_value_start_rel
                    + colon_rel
                    + 1
                    + leading_ascii_ws(&item_value[(colon_rel + 1)..]);
                let field_value_end_rel = item_value_end_rel;
                if field_value_start_rel >= field_value_end_rel {
                    continue;
                }
                let mut path = format!("{}[{item_index}]", parent.path);
                push_frontmatter_path_segment(&mut path, &key);
                let value = &line[field_value_start_rel..field_value_end_rel];
                push_markdown_frontmatter_value_spans(
                    path.clone(),
                    line_start + field_value_start_rel,
                    value,
                    &mut spans,
                );
                record_markdown_frontmatter_value_references(
                    &path,
                    line_start + field_value_start_rel,
                    value,
                    &mut alias_anchors,
                    &mut alias_refs,
                    &mut merge_refs,
                );
            } else {
                let path = format!("{}[{item_index}]", parent.path);
                push_markdown_frontmatter_value_spans(
                    path.clone(),
                    line_start + item_value_start_rel,
                    item_value,
                    &mut spans,
                );
                record_markdown_frontmatter_value_references(
                    &path,
                    line_start + item_value_start_rel,
                    item_value,
                    &mut alias_anchors,
                    &mut alias_refs,
                    &mut merge_refs,
                );
            }
            continue;
        }

        let Some(colon_rel) = find_unquoted_byte(line, b':') else {
            continue;
        };
        close_frontmatter_containers(&mut containers, indent, &mut spans);
        if let Some(parent) = containers.last_mut() {
            if indent > parent.indent {
                parent.end = next_cursor;
            }
        }
        let key = trim_line_ascii(&line[..colon_rel]);
        let Some(key) = parse_frontmatter_key(key) else {
            continue;
        };

        let value_line_start = colon_rel + 1;
        let value_start_rel = value_line_start + leading_ascii_ws(&line[value_line_start..]);
        let value_end_rel = trim_trailing_ascii_ws_end(line, line.len());
        let path = active_frontmatter_path(&containers, &key);
        if value_start_rel >= value_end_rel {
            containers.push(FrontmatterContainer {
                indent,
                path,
                start: line_start,
                end: line_end,
                kind: "object",
                item_count: 0,
            });
            continue;
        }

        let value = &line[value_start_rel..value_end_rel];
        if matches!(trim_line_ascii(value), b"|" | b">") {
            let mut block_start = None;
            let mut block_end = None;
            while let Some(((block_line_start, block_line_end), block_next_cursor)) =
                next_line_bounds(bytes, cursor)
            {
                let block_line = &bytes[block_line_start..block_line_end];
                let block_trimmed = trim_line_ascii(block_line);
                if block_trimmed == b"---" {
                    break;
                }
                let block_indent = leading_ascii_ws(block_line);
                if !block_trimmed.is_empty() && block_indent <= indent {
                    break;
                }
                if block_start.is_none() {
                    block_start = Some(block_line_start);
                }
                block_end = Some(block_next_cursor);
                cursor = block_next_cursor;
            }
            if let Some(parent) = containers.last_mut() {
                if indent > parent.indent {
                    parent.end = cursor;
                }
            }
            if let (Some(block_start), Some(block_end)) = (block_start, block_end) {
                if block_end > block_start {
                    spans.push((
                        path,
                        block_start as i64,
                        (block_end - block_start) as i64,
                        "string".to_string(),
                    ));
                }
            }
            continue;
        }

        push_markdown_frontmatter_value_spans(
            path.clone(),
            line_start + value_start_rel,
            value,
            &mut spans,
        );
        record_markdown_frontmatter_value_references(
            &path,
            line_start + value_start_rel,
            value,
            &mut alias_anchors,
            &mut alias_refs,
            &mut merge_refs,
        );
    }

    if !found_closing_fence {
        return Err(AnfsError::PolicyDenied(format!(
            "node {node_id} has unterminated Markdown frontmatter"
        )));
    }
    append_markdown_alias_expansion_spans(&mut spans, &alias_anchors, &alias_refs);
    append_markdown_merge_expansion_spans(&mut spans, &alias_anchors, &merge_refs);
    append_markdown_scalar_alias_target_spans(&mut spans, bytes, &alias_anchors);
    Ok(spans)
}

fn record_markdown_frontmatter_value_references(
    path: &str,
    value_offset: usize,
    value: &[u8],
    alias_anchors: &mut Vec<MarkdownAliasAnchor>,
    alias_refs: &mut Vec<MarkdownAliasRef>,
    merge_refs: &mut Vec<MarkdownMergeRef>,
) {
    if let Some(anchor_name) = markdown_frontmatter_anchor_name(value) {
        alias_anchors.push(MarkdownAliasAnchor {
            name: anchor_name,
            path: path.to_string(),
        });
    }
    if let Some((alias_start, alias_name, alias_len)) = markdown_frontmatter_alias_reference(value)
    {
        alias_refs.push(MarkdownAliasRef {
            name: alias_name,
            path: path.to_string(),
            offset: (value_offset + alias_start) as i64,
            length: alias_len as i64,
        });
    }
    for (merge_start, merge_name, merge_len) in markdown_frontmatter_merge_references(value) {
        merge_refs.push(MarkdownMergeRef {
            name: merge_name,
            path: path.to_string(),
            offset: (value_offset + merge_start) as i64,
            length: merge_len as i64,
        });
    }
}

fn push_markdown_frontmatter_value_spans(
    path: String,
    value_offset: usize,
    value: &[u8],
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
) {
    let (payload_start, payload) = markdown_frontmatter_value_payload(value);
    let payload_offset = value_offset + payload_start;
    if let Some((mapping_start, mapping_end)) = markdown_inline_mapping_bounds(payload) {
        spans.push((
            path.clone(),
            (payload_offset + mapping_start) as i64,
            (mapping_end - mapping_start) as i64,
            markdown_frontmatter_container_kind(value, "object").to_string(),
        ));
        push_markdown_inline_mapping_field_spans(
            &path,
            payload_offset,
            payload,
            mapping_start,
            mapping_end,
            0,
            spans,
        );
    } else if let Some((sequence_start, sequence_end)) = markdown_inline_sequence_bounds(payload) {
        spans.push((
            path.clone(),
            (payload_offset + sequence_start) as i64,
            (sequence_end - sequence_start) as i64,
            markdown_frontmatter_container_kind(value, "array").to_string(),
        ));
        push_markdown_inline_sequence_item_spans(
            &path,
            payload_offset,
            payload,
            sequence_start,
            sequence_end,
            0,
            spans,
        );
    } else {
        let (scalar_payload_start, scalar_payload) = markdown_frontmatter_scalar_payload(value);
        spans.push((
            path,
            (value_offset + scalar_payload_start) as i64,
            scalar_payload.len() as i64,
            markdown_frontmatter_scalar_kind(value, scalar_payload).to_string(),
        ));
    }
}

fn markdown_inline_mapping_bounds(value: &[u8]) -> Option<(usize, usize)> {
    let start = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    if start + 1 < end && value[start] == b'{' && value[end - 1] == b'}' {
        Some((start, end))
    } else {
        None
    }
}

fn markdown_inline_sequence_bounds(value: &[u8]) -> Option<(usize, usize)> {
    let start = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    if start + 1 < end && value[start] == b'[' && value[end - 1] == b']' {
        Some((start, end))
    } else {
        None
    }
}

const MAX_MARKDOWN_INLINE_NESTING_DEPTH: usize = 8;

fn push_markdown_inline_sequence_item_spans(
    path: &str,
    value_offset: usize,
    value: &[u8],
    sequence_start: usize,
    sequence_end: usize,
    depth: usize,
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
) {
    if depth >= MAX_MARKDOWN_INLINE_NESTING_DEPTH {
        return;
    }
    let mut item_count = 0_i64;
    let mut item_start = sequence_start + 1;
    let inner_end = sequence_end - 1;
    let mut cursor = item_start;
    let mut quote: Option<u8> = None;
    let mut delimiter_depth = 0_i64;
    while cursor <= inner_end {
        let at_separator = cursor == inner_end
            || (quote.is_none() && delimiter_depth == 0 && value[cursor] == b',');
        if at_separator {
            let raw_end = cursor;
            let start = item_start + leading_ascii_ws(&value[item_start..raw_end]);
            let end = trim_trailing_ascii_ws_end(value, raw_end);
            if start < end {
                let item = &value[start..end];
                let item_path = format!("{path}[{item_count}]");
                let (payload_start, payload) = markdown_frontmatter_value_payload(item);
                let item_payload_start = start + payload_start;
                if let Some((mapping_start, mapping_end)) = markdown_inline_mapping_bounds(payload)
                {
                    let mapping_start = item_payload_start + mapping_start;
                    let mapping_end = item_payload_start + mapping_end;
                    spans.push((
                        item_path.clone(),
                        (value_offset + mapping_start) as i64,
                        (mapping_end - mapping_start) as i64,
                        markdown_frontmatter_container_kind(item, "object").to_string(),
                    ));
                    push_markdown_inline_mapping_field_spans(
                        &item_path,
                        value_offset,
                        value,
                        mapping_start,
                        mapping_end,
                        depth + 1,
                        spans,
                    );
                } else {
                    let (scalar_payload_start, scalar_payload) =
                        markdown_frontmatter_scalar_payload(item);
                    spans.push((
                        item_path,
                        (value_offset + start + scalar_payload_start) as i64,
                        scalar_payload.len() as i64,
                        markdown_frontmatter_scalar_kind(item, scalar_payload).to_string(),
                    ));
                }
                item_count += 1;
            }
            item_start = cursor + 1;
        } else {
            if let Some(tag_end) = markdown_yaml_uri_tag_end(value, cursor, inner_end + 1) {
                cursor = tag_end;
            } else if quote == Some(b'"') && value[cursor] == b'\\' && cursor + 1 < inner_end {
                cursor += 1;
            } else {
                match value[cursor] {
                    b'\'' | b'"' if quote.is_none() => quote = Some(value[cursor]),
                    byte if quote == Some(byte) => quote = None,
                    b'{' | b'[' if quote.is_none() => delimiter_depth += 1,
                    b'}' | b']' if quote.is_none() && delimiter_depth > 0 => delimiter_depth -= 1,
                    _ => {}
                }
            }
        }
        cursor += 1;
    }
}

fn push_markdown_inline_mapping_field_spans(
    path: &str,
    value_offset: usize,
    value: &[u8],
    mapping_start: usize,
    mapping_end: usize,
    depth: usize,
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
) {
    if depth >= MAX_MARKDOWN_INLINE_NESTING_DEPTH {
        return;
    }
    let mut field_start = mapping_start + 1;
    let inner_end = mapping_end - 1;
    let mut cursor = field_start;
    let mut quote: Option<u8> = None;
    let mut delimiter_depth = 0_i64;
    while cursor <= inner_end {
        let at_separator = cursor == inner_end
            || (quote.is_none() && delimiter_depth == 0 && value[cursor] == b',');
        if at_separator {
            let raw_end = cursor;
            push_markdown_inline_mapping_field_span(
                path,
                value_offset,
                value,
                field_start,
                raw_end,
                depth,
                spans,
            );
            field_start = cursor + 1;
        } else {
            if let Some(tag_end) = markdown_yaml_uri_tag_end(value, cursor, inner_end + 1) {
                cursor = tag_end;
            } else if quote == Some(b'"') && value[cursor] == b'\\' && cursor + 1 < inner_end {
                cursor += 1;
            } else {
                match value[cursor] {
                    b'\'' | b'"' if quote.is_none() => quote = Some(value[cursor]),
                    byte if quote == Some(byte) => quote = None,
                    b'{' | b'[' if quote.is_none() => delimiter_depth += 1,
                    b'}' | b']' if quote.is_none() && delimiter_depth > 0 => delimiter_depth -= 1,
                    _ => {}
                }
            }
        }
        cursor += 1;
    }
}

fn push_markdown_inline_mapping_field_span(
    path: &str,
    value_offset: usize,
    value: &[u8],
    raw_start: usize,
    raw_end: usize,
    depth: usize,
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
) {
    let start = raw_start + leading_ascii_ws(&value[raw_start..raw_end]);
    let end = trim_trailing_ascii_ws_end(value, raw_end);
    if start >= end {
        return;
    }
    let Some(colon_rel) = find_unquoted_byte(&value[start..end], b':') else {
        return;
    };
    let key = trim_line_ascii(&value[start..start + colon_rel]);
    let Some(key) = parse_frontmatter_key(key) else {
        return;
    };
    let value_start = start + colon_rel + 1 + leading_ascii_ws(&value[start + colon_rel + 1..end]);
    let value_end = trim_trailing_ascii_ws_end(value, end);
    if value_start >= value_end {
        return;
    }
    let field_value = &value[value_start..value_end];
    let (payload_start, payload) = markdown_frontmatter_value_payload(field_value);
    let payload_value_start = value_start + payload_start;
    let mut field_path = path.to_string();
    push_frontmatter_path_segment(&mut field_path, &key);
    if let Some((mapping_start, mapping_end)) = markdown_inline_mapping_bounds(payload) {
        let mapping_start = payload_value_start + mapping_start;
        let mapping_end = payload_value_start + mapping_end;
        spans.push((
            field_path.clone(),
            (value_offset + mapping_start) as i64,
            (mapping_end - mapping_start) as i64,
            markdown_frontmatter_container_kind(field_value, "object").to_string(),
        ));
        push_markdown_inline_mapping_field_spans(
            &field_path,
            value_offset,
            value,
            mapping_start,
            mapping_end,
            depth + 1,
            spans,
        );
        return;
    }
    if let Some((sequence_start, sequence_end)) = markdown_inline_sequence_bounds(payload) {
        let sequence_start = payload_value_start + sequence_start;
        let sequence_end = payload_value_start + sequence_end;
        spans.push((
            field_path.clone(),
            (value_offset + sequence_start) as i64,
            (sequence_end - sequence_start) as i64,
            markdown_frontmatter_container_kind(field_value, "array").to_string(),
        ));
        push_markdown_inline_sequence_item_spans(
            &field_path,
            value_offset,
            value,
            sequence_start,
            sequence_end,
            depth + 1,
            spans,
        );
        return;
    }
    let (scalar_payload_start, scalar_payload) = markdown_frontmatter_scalar_payload(field_value);
    spans.push((
        field_path,
        (value_offset + value_start + scalar_payload_start) as i64,
        scalar_payload.len() as i64,
        markdown_frontmatter_scalar_kind(field_value, scalar_payload).to_string(),
    ));
}

fn markdown_frontmatter_value_payload(value: &[u8]) -> (usize, &[u8]) {
    let mut cursor = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    let original = &value[cursor..end];
    while cursor < end {
        let Some(token_end) = markdown_frontmatter_decorator_end(value, cursor, end) else {
            break;
        };
        let next = token_end + leading_ascii_ws(&value[token_end..end]);
        if next >= end {
            return (cursor, original);
        }
        cursor = next;
    }
    (cursor, &value[cursor..end])
}

fn markdown_frontmatter_scalar_payload(value: &[u8]) -> (usize, &[u8]) {
    let (payload_start, payload) = markdown_frontmatter_value_payload(value);
    if let Some((inner_start, inner_end)) = markdown_frontmatter_quoted_scalar_inner(payload) {
        return (
            payload_start + inner_start,
            &payload[inner_start..inner_end],
        );
    }
    (payload_start, payload)
}

fn markdown_frontmatter_quoted_scalar_inner(value: &[u8]) -> Option<(usize, usize)> {
    let start = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    if start + 1 >= end {
        return None;
    }
    let quote = value[start];
    if !matches!(quote, b'\'' | b'"') || value[end - 1] != quote {
        return None;
    }
    Some((start + 1, end - 1))
}

pub(crate) fn markdown_field_span_value(bytes: &[u8], offset: i64, length: i64) -> AnfsResult<String> {
    if offset < 0 || length < 0 {
        return Err(AnfsError::StorageCorruption(format!(
            "invalid Markdown field span offset={offset} length={length}"
        )));
    }
    let start = offset as usize;
    let end = start
        .checked_add(length as usize)
        .ok_or_else(|| AnfsError::StorageCorruption("Markdown field span overflow".to_string()))?;
    if end > bytes.len() {
        return Err(AnfsError::StorageCorruption(format!(
            "Markdown field span {offset}..{} exceeds node size {}",
            offset + length,
            bytes.len()
        )));
    }
    if start > 0 && end < bytes.len() {
        let quote = bytes[start - 1];
        if matches!(quote, b'\'' | b'"') && bytes[end] == quote {
            return decode_markdown_frontmatter_quoted_scalar(&bytes[start - 1..=end]);
        }
    }
    std::str::from_utf8(&bytes[start..end])
        .map(str::to_string)
        .map_err(|_| AnfsError::StorageCorruption("Markdown field span is not UTF-8".to_string()))
}

fn decode_markdown_frontmatter_quoted_scalar(quoted: &[u8]) -> AnfsResult<String> {
    if quoted.len() < 2 {
        return Err(AnfsError::StorageCorruption(
            "quoted Markdown scalar is too short".to_string(),
        ));
    }
    match (quoted[0], quoted[quoted.len() - 1]) {
        (b'\'', b'\'') => decode_markdown_frontmatter_single_quoted_scalar(quoted),
        (b'"', b'"') => decode_markdown_frontmatter_double_quoted_scalar(quoted),
        _ => Err(AnfsError::StorageCorruption(
            "invalid quoted Markdown scalar delimiters".to_string(),
        )),
    }
}

fn decode_markdown_frontmatter_single_quoted_scalar(quoted: &[u8]) -> AnfsResult<String> {
    let inner = std::str::from_utf8(&quoted[1..quoted.len() - 1]).map_err(|_| {
        AnfsError::StorageCorruption("single-quoted Markdown scalar is not UTF-8".to_string())
    })?;
    let mut decoded = String::new();
    let mut chars = inner.chars().peekable();
    while let Some(ch) = chars.next() {
        if ch == '\'' && chars.peek() == Some(&'\'') {
            chars.next();
            decoded.push('\'');
        } else {
            decoded.push(ch);
        }
    }
    Ok(decoded)
}

fn decode_markdown_frontmatter_double_quoted_scalar(quoted: &[u8]) -> AnfsResult<String> {
    let inner = std::str::from_utf8(&quoted[1..quoted.len() - 1]).map_err(|_| {
        AnfsError::StorageCorruption("double-quoted Markdown scalar is not UTF-8".to_string())
    })?;
    let mut decoded = String::new();
    let mut chars = inner.chars();
    while let Some(ch) = chars.next() {
        if ch != '\\' {
            decoded.push(ch);
            continue;
        }
        let escaped = chars.next().ok_or_else(|| {
            AnfsError::StorageCorruption(
                "unterminated escape in double-quoted Markdown scalar".to_string(),
            )
        })?;
        match escaped {
            '0' => decoded.push('\0'),
            'a' => decoded.push('\u{0007}'),
            'b' => decoded.push('\u{0008}'),
            't' | '\t' => decoded.push('\t'),
            'n' => decoded.push('\n'),
            'v' => decoded.push('\u{000B}'),
            'f' => decoded.push('\u{000C}'),
            'r' => decoded.push('\r'),
            'e' => decoded.push('\u{001B}'),
            '"' => decoded.push('"'),
            '\'' => decoded.push('\''),
            '\\' => decoded.push('\\'),
            '/' => decoded.push('/'),
            'x' => decoded.push(markdown_frontmatter_hex_escape(&mut chars, 2)?),
            'u' => decoded.push(markdown_frontmatter_hex_escape(&mut chars, 4)?),
            'U' => decoded.push(markdown_frontmatter_hex_escape(&mut chars, 8)?),
            other => {
                return Err(AnfsError::StorageCorruption(format!(
                    "unsupported YAML escape \\{other} in Markdown scalar"
                )))
            }
        }
    }
    Ok(decoded)
}

fn markdown_frontmatter_hex_escape(
    chars: &mut std::str::Chars<'_>,
    digits: usize,
) -> AnfsResult<char> {
    let mut value = 0_u32;
    for _ in 0..digits {
        let ch = chars.next().ok_or_else(|| {
            AnfsError::StorageCorruption("truncated YAML hex escape in Markdown scalar".to_string())
        })?;
        let digit = ch.to_digit(16).ok_or_else(|| {
            AnfsError::StorageCorruption(format!(
                "invalid YAML hex escape digit {ch} in Markdown scalar"
            ))
        })?;
        value = value * 16 + digit;
    }
    char::from_u32(value).ok_or_else(|| {
        AnfsError::StorageCorruption(format!(
            "invalid YAML unicode escape value {value} in Markdown scalar"
        ))
    })
}

fn markdown_frontmatter_decorator_end(value: &[u8], cursor: usize, end: usize) -> Option<usize> {
    match value.get(cursor).copied()? {
        b'!' => {
            if cursor + 1 < end && value[cursor + 1] == b'<' {
                let mut idx = cursor + 2;
                while idx < end {
                    if value[idx] == b'>' {
                        return Some(idx + 1);
                    }
                    idx += 1;
                }
                None
            } else {
                let mut idx = cursor + 1;
                while idx < end && !value[idx].is_ascii_whitespace() {
                    idx += 1;
                }
                if idx > cursor + 1 {
                    Some(idx)
                } else {
                    None
                }
            }
        }
        b'&' => {
            let mut idx = cursor + 1;
            while idx < end && !value[idx].is_ascii_whitespace() {
                idx += 1;
            }
            if idx > cursor + 1 {
                Some(idx)
            } else {
                None
            }
        }
        _ => None,
    }
}

fn markdown_yaml_uri_tag_end(value: &[u8], cursor: usize, end: usize) -> Option<usize> {
    if cursor + 1 >= end || value[cursor] != b'!' || value[cursor + 1] != b'<' {
        return None;
    }
    let mut idx = cursor + 2;
    while idx < end {
        if value[idx] == b'>' {
            return Some(idx);
        }
        idx += 1;
    }
    None
}

fn markdown_frontmatter_anchor_name(value: &[u8]) -> Option<String> {
    let mut cursor = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    let mut anchor = None;
    while cursor < end {
        if value.get(cursor) == Some(&b'&') {
            let token_end = markdown_frontmatter_decorator_end(value, cursor, end)?;
            let name = std::str::from_utf8(&value[cursor + 1..token_end]).ok()?;
            anchor = Some(name.to_string());
            cursor = token_end + leading_ascii_ws(&value[token_end..end]);
            continue;
        }
        let Some(token_end) = markdown_frontmatter_decorator_end(value, cursor, end) else {
            break;
        };
        cursor = token_end + leading_ascii_ws(&value[token_end..end]);
    }
    anchor
}

fn markdown_frontmatter_alias_reference(value: &[u8]) -> Option<(usize, String, usize)> {
    let (payload_start, payload) = markdown_frontmatter_value_payload(value);
    if !valid_markdown_frontmatter_alias(payload) {
        return None;
    }
    let name = std::str::from_utf8(&payload[1..]).ok()?.to_string();
    Some((payload_start, name, payload.len()))
}

fn markdown_frontmatter_merge_references(value: &[u8]) -> Vec<(usize, String, usize)> {
    let mut refs = Vec::new();
    let (payload_start, payload) = markdown_frontmatter_value_payload(value);
    let Some((mapping_start, mapping_end)) = markdown_inline_mapping_bounds(payload) else {
        return refs;
    };
    let mut field_start = mapping_start + 1;
    let inner_end = mapping_end - 1;
    let mut cursor = field_start;
    let mut quote: Option<u8> = None;
    let mut delimiter_depth = 0_i64;
    while cursor <= inner_end {
        let at_separator = cursor == inner_end
            || (quote.is_none() && delimiter_depth == 0 && payload[cursor] == b',');
        if at_separator {
            let raw_end = cursor;
            let start = field_start + leading_ascii_ws(&payload[field_start..raw_end]);
            let end = trim_trailing_ascii_ws_end(payload, raw_end);
            if start < end {
                if let Some(colon_rel) = find_unquoted_byte(&payload[start..end], b':') {
                    let key = trim_line_ascii(&payload[start..start + colon_rel]);
                    if key == b"<<" {
                        let value_start = start
                            + colon_rel
                            + 1
                            + leading_ascii_ws(&payload[start + colon_rel + 1..end]);
                        let value_end = trim_trailing_ascii_ws_end(payload, end);
                        if value_start < value_end {
                            let field_value = &payload[value_start..value_end];
                            push_markdown_merge_alias_references(
                                field_value,
                                payload_start + value_start,
                                &mut refs,
                            );
                        }
                    }
                }
            }
            field_start = cursor + 1;
        } else {
            match payload[cursor] {
                b'\'' | b'"' if quote.is_none() => quote = Some(payload[cursor]),
                byte if quote == Some(byte) => quote = None,
                b'{' | b'[' if quote.is_none() => delimiter_depth += 1,
                b'}' | b']' if quote.is_none() && delimiter_depth > 0 => delimiter_depth -= 1,
                _ => {}
            }
        }
        cursor += 1;
    }
    refs
}

fn push_markdown_merge_alias_references(
    value: &[u8],
    value_offset: usize,
    refs: &mut Vec<(usize, String, usize)>,
) {
    let (payload_start, payload) = markdown_frontmatter_value_payload(value);
    let payload_offset = value_offset + payload_start;
    if valid_markdown_frontmatter_alias(payload) {
        if let Ok(name) = std::str::from_utf8(&payload[1..]) {
            refs.push((payload_offset, name.to_string(), payload.len()));
        }
        return;
    }
    let Some((sequence_start, sequence_end)) = markdown_inline_sequence_bounds(payload) else {
        return;
    };
    let mut item_start = sequence_start + 1;
    let inner_end = sequence_end - 1;
    let mut cursor = item_start;
    let mut quote: Option<u8> = None;
    let mut delimiter_depth = 0_i64;
    while cursor <= inner_end {
        let at_separator = cursor == inner_end
            || (quote.is_none() && delimiter_depth == 0 && payload[cursor] == b',');
        if at_separator {
            let raw_end = cursor;
            let start = item_start + leading_ascii_ws(&payload[item_start..raw_end]);
            let end = trim_trailing_ascii_ws_end(payload, raw_end);
            if start < end {
                let item = &payload[start..end];
                let (item_payload_start, item_payload) = markdown_frontmatter_value_payload(item);
                if valid_markdown_frontmatter_alias(item_payload) {
                    if let Ok(name) = std::str::from_utf8(&item_payload[1..]) {
                        refs.push((
                            payload_offset + start + item_payload_start,
                            name.to_string(),
                            item_payload.len(),
                        ));
                    }
                }
            }
            item_start = cursor + 1;
        } else {
            match payload[cursor] {
                b'\'' | b'"' if quote.is_none() => quote = Some(payload[cursor]),
                byte if quote == Some(byte) => quote = None,
                b'{' | b'[' if quote.is_none() => delimiter_depth += 1,
                b'}' | b']' if quote.is_none() && delimiter_depth > 0 => delimiter_depth -= 1,
                _ => {}
            }
        }
        cursor += 1;
    }
}

fn append_markdown_alias_expansion_spans(
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
    anchors: &[MarkdownAliasAnchor],
    aliases: &[MarkdownAliasRef],
) {
    let mut iterations = 0;
    loop {
        let mut expanded = Vec::new();
        for (path, _offset, _length, _kind) in spans.iter() {
            for alias in aliases {
                let Some(anchor) = anchors
                    .iter()
                    .rev()
                    .find(|anchor| anchor.name == alias.name)
                else {
                    continue;
                };
                let Some(suffix) = markdown_alias_suffix(path, &anchor.path) else {
                    continue;
                };
                let alias_path = format!("{}{suffix}", alias.path);
                if spans
                    .iter()
                    .chain(expanded.iter())
                    .any(|(path, _offset, _length, _kind)| path == &alias_path)
                {
                    continue;
                }
                expanded.push((alias_path, alias.offset, alias.length, "alias".to_string()));
            }
        }
        if expanded.is_empty() {
            break;
        }
        spans.extend(expanded);
        iterations += 1;
        if iterations >= anchors.len().saturating_add(aliases.len()).max(1) {
            break;
        }
    }
}

fn markdown_alias_suffix(path: &str, anchor_path: &str) -> Option<String> {
    let source_prefix = format!("{anchor_path}.");
    let source_bracket_prefix = format!("{anchor_path}[");
    if let Some(suffix) = path.strip_prefix(&source_prefix) {
        Some(format!(".{suffix}"))
    } else {
        path.strip_prefix(&source_bracket_prefix)
            .map(|suffix| format!("[{suffix}"))
    }
}

fn append_markdown_merge_expansion_spans(
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
    anchors: &[MarkdownAliasAnchor],
    merges: &[MarkdownMergeRef],
) {
    let mut iterations = 0;
    loop {
        let mut expanded = Vec::new();
        for (path, _offset, _length, _kind) in spans.iter() {
            for merge in merges {
                let Some(anchor) = anchors
                    .iter()
                    .rev()
                    .find(|anchor| anchor.name == merge.name)
                else {
                    continue;
                };
                let Some(suffix) = markdown_alias_suffix(path, &anchor.path) else {
                    continue;
                };
                let merge_path = format!("{}{suffix}", merge.path);
                if spans
                    .iter()
                    .chain(expanded.iter())
                    .any(|(path, _offset, _length, _kind)| path == &merge_path)
                {
                    continue;
                }
                expanded.push((merge_path, merge.offset, merge.length, "alias".to_string()));
            }
        }
        if expanded.is_empty() {
            break;
        }
        spans.extend(expanded);
        iterations += 1;
        if iterations >= anchors.len().saturating_add(merges.len()).max(1) {
            break;
        }
    }
}

fn append_markdown_scalar_alias_target_spans(
    spans: &mut Vec<crate::MarkdownFieldSpanRow>,
    bytes: &[u8],
    anchors: &[MarkdownAliasAnchor],
) {
    let alias_spans: Vec<crate::MarkdownFieldSpanRow> = spans
        .iter()
        .filter(|(_path, _offset, _length, kind)| kind == "alias")
        .cloned()
        .collect();
    for (alias_path, alias_offset, alias_length, _kind) in alias_spans {
        let Ok(start) = usize::try_from(alias_offset) else {
            continue;
        };
        let Ok(length) = usize::try_from(alias_length) else {
            continue;
        };
        let Some(end) = start.checked_add(length) else {
            continue;
        };
        let Some(alias_value) = bytes.get(start..end) else {
            continue;
        };
        if !valid_markdown_frontmatter_alias(alias_value) {
            continue;
        }
        let Some(alias_name) = std::str::from_utf8(&alias_value[1..]).ok() else {
            continue;
        };
        let Some((_target_path, offset, length, kind)) =
            resolve_markdown_scalar_alias_target(spans, bytes, anchors, alias_name)
        else {
            continue;
        };
        let target_path = format!("{alias_path}.__target");
        if spans
            .iter()
            .any(|(path, _offset, _length, _kind)| path == &target_path)
        {
            continue;
        }
        spans.push((target_path, offset, length, kind));
    }
}

fn resolve_markdown_scalar_alias_target(
    spans: &[crate::MarkdownFieldSpanRow],
    bytes: &[u8],
    anchors: &[MarkdownAliasAnchor],
    alias_name: &str,
) -> Option<crate::MarkdownFieldSpanRow> {
    let mut current_name = alias_name.to_string();
    let mut visited = HashSet::new();
    loop {
        if !visited.insert(current_name.clone()) {
            return None;
        }
        let anchor = anchors
            .iter()
            .rev()
            .find(|anchor| anchor.name == current_name)?;
        let target = spans
            .iter()
            .find(|(path, _offset, _length, _kind)| path == &anchor.path)?;
        if !matches!(target.3.as_str(), "alias") {
            if matches!(target.3.as_str(), "object" | "array") {
                return None;
            }
            return Some(target.clone());
        }
        let Ok(start) = usize::try_from(target.1) else {
            return None;
        };
        let Ok(length) = usize::try_from(target.2) else {
            return None;
        };
        let end = start.checked_add(length)?;
        let alias_value = bytes.get(start..end)?;
        if !valid_markdown_frontmatter_alias(alias_value) {
            return None;
        }
        current_name = std::str::from_utf8(&alias_value[1..]).ok()?.to_string();
    }
}

fn find_unquoted_byte(value: &[u8], target: u8) -> Option<usize> {
    let mut quote: Option<u8> = None;
    let mut escaped = false;
    for (idx, byte) in value.iter().enumerate() {
        if quote == Some(b'"') && escaped {
            escaped = false;
            continue;
        }
        match *byte {
            b'\\' if quote == Some(b'"') => escaped = true,
            b'\'' | b'"' if quote.is_none() => quote = Some(*byte),
            byte if quote == Some(byte) => quote = None,
            byte if quote.is_none() && byte == target => return Some(idx),
            _ => {}
        }
    }
    None
}

fn parse_frontmatter_key(key: &[u8]) -> Option<String> {
    if valid_frontmatter_bare_key(key) {
        return std::str::from_utf8(key).ok().map(str::to_string);
    }
    let quote = key.first().copied()?;
    if !matches!(quote, b'\'' | b'"') || key.last().copied() != Some(quote) || key.len() < 2 {
        return None;
    }
    let decoded = if quote == b'"' {
        serde_json::from_slice::<String>(key).ok()?
    } else {
        let inner = std::str::from_utf8(&key[1..key.len() - 1]).ok()?;
        let mut decoded = String::new();
        let mut chars = inner.chars().peekable();
        while let Some(ch) = chars.next() {
            if ch == '\'' && chars.peek() == Some(&'\'') {
                chars.next();
                decoded.push('\'');
            } else {
                decoded.push(ch);
            }
        }
        decoded
    };
    if decoded.is_empty() {
        None
    } else {
        Some(decoded)
    }
}

fn push_frontmatter_path_segment(path: &mut String, key: &str) {
    if valid_frontmatter_bare_key(key.as_bytes()) {
        path.push('.');
        path.push_str(key);
    } else {
        path.push('[');
        path.push_str(&serde_json::to_string(key).unwrap_or_else(|_| "\"\"".to_string()));
        path.push(']');
    }
}

fn valid_frontmatter_bare_key(key: &[u8]) -> bool {
    !key.is_empty()
        && key
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

pub(crate) fn markdown_body_section_spans(
    text: &str,
    node_id: &str,
) -> AnfsResult<Vec<crate::MarkdownSectionSpanRow>> {
    let bytes = text.as_bytes();
    let mut cursor = markdown_body_start(bytes);
    let mut headings = Vec::new();
    let mut in_fence: Option<&[u8]> = None;
    while let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) {
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if trimmed_start <= 3 {
            if let Some(fence) = in_fence {
                if trimmed.starts_with(fence) {
                    in_fence = None;
                }
                cursor = next_cursor;
                continue;
            }
            if trimmed.starts_with(b"```") {
                in_fence = Some(b"```");
                cursor = next_cursor;
                continue;
            }
            if trimmed.starts_with(b"~~~") {
                in_fence = Some(b"~~~");
                cursor = next_cursor;
                continue;
            }
        }

        if in_fence.is_none() && trimmed_start <= 3 {
            if markdown_html_block_start(trimmed_start, trimmed) {
                cursor = next_cursor;
                while let Some(((html_start, html_end), html_next_cursor)) =
                    next_line_bounds(bytes, cursor)
                {
                    if trim_line_ascii(&bytes[html_start..html_end]).is_empty() {
                        break;
                    }
                    cursor = html_next_cursor;
                }
                continue;
            }
            if let Some((level, title)) = markdown_atx_heading(trimmed) {
                headings.push((level, line_start, title.to_string()));
            } else if let Some((level, title_start, title)) =
                markdown_setext_heading_at(bytes, line_start, line_end, next_cursor)
            {
                headings.push((level, title_start, title.to_string()));
            }
        }
        cursor = next_cursor;
    }

    let mut seen_paths: Vec<String> = Vec::new();
    let headings: Vec<(usize, usize, String, String)> = headings
        .into_iter()
        .map(|(level, start, title)| {
            let path = unique_markdown_section_path(&mut seen_paths, &title);
            (level, start, title, path)
        })
        .collect();

    let mut spans = Vec::new();
    for (idx, (level, start, _title, path)) in headings.iter().enumerate() {
        let mut end = bytes.len();
        for (next_level, next_start, _next_title, _next_path) in headings.iter().skip(idx + 1) {
            if next_level <= level {
                end = *next_start;
                break;
            }
        }
        spans.push((
            path.clone(),
            *start as i64,
            (end - start) as i64,
            format!("h{level}"),
        ));
    }

    spans.extend(markdown_body_block_spans(bytes, &headings));

    if spans.is_empty() {
        return Err(AnfsError::PolicyDenied(format!(
            "node {node_id} does not contain Markdown body headings or body blocks"
        )));
    }
    Ok(spans)
}

#[derive(Clone)]
struct MarkdownLinkReferenceDefinition {
    normalized_label: String,
    destination_start: usize,
    destination_end: usize,
    title_span: Option<(usize, usize)>,
}

fn markdown_body_block_spans(
    bytes: &[u8],
    headings: &[(usize, usize, String, String)],
) -> Vec<crate::MarkdownSectionSpanRow> {
    let mut spans = Vec::new();
    let mut counters: Vec<(String, String, i64)> = Vec::new();
    let link_references = markdown_link_reference_definitions(bytes);
    let mut cursor = markdown_body_start(bytes);
    while let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) {
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if trim_line_ascii(line).is_empty() {
            cursor = next_cursor;
            continue;
        }
        if trimmed_start <= 3 && markdown_atx_heading(trimmed).is_some() {
            cursor = next_cursor;
            continue;
        }
        if let Some((_level, _title_start, _title)) =
            markdown_setext_heading_at(bytes, line_start, line_end, next_cursor)
        {
            if let Some((_underline_bounds, after_underline)) = next_line_bounds(bytes, next_cursor)
            {
                cursor = after_underline;
            } else {
                cursor = next_cursor;
            }
            continue;
        }
        if markdown_indented_code_line(trimmed_start, line) {
            let block_start = line_start;
            let mut block_end = next_cursor;
            cursor = next_cursor;
            while let Some(((code_start, code_end), code_next_cursor)) =
                next_line_bounds(bytes, cursor)
            {
                let code_line = &bytes[code_start..code_end];
                let code_trimmed_start = leading_ascii_ws(code_line);
                if !markdown_indented_code_line(code_trimmed_start, code_line) {
                    break;
                }
                block_end = code_next_cursor;
                cursor = code_next_cursor;
            }
            let scope = nearest_markdown_heading_path(headings, block_start);
            let path = next_markdown_body_block_path(&mut counters, scope.as_deref(), "code");
            spans.push((
                path,
                block_start as i64,
                (block_end - block_start) as i64,
                "code".to_string(),
            ));
            continue;
        }
        if markdown_link_reference_line(trimmed_start, trimmed) {
            let mut block_end = next_cursor;
            if let Some(parts) = markdown_link_reference_definition_parts(trimmed) {
                if parts.title_span.is_none() {
                    if let Some((_title_start, _title_end, continuation_end)) =
                        markdown_link_reference_continuation_title(bytes, next_cursor)
                    {
                        block_end = continuation_end;
                    }
                }
            }
            let scope = nearest_markdown_heading_path(headings, line_start);
            let path =
                next_markdown_body_block_path(&mut counters, scope.as_deref(), "link-reference");
            let span_path = path.clone();
            spans.push((
                path,
                line_start as i64,
                (block_end - line_start) as i64,
                "link-reference".to_string(),
            ));
            push_markdown_link_reference_component_spans(
                bytes, line_start, block_end, &span_path, &mut spans,
            );
            cursor = block_end;
            continue;
        }
        if markdown_thematic_break_line(trimmed_start, trimmed) {
            let scope = nearest_markdown_heading_path(headings, line_start);
            let path =
                next_markdown_body_block_path(&mut counters, scope.as_deref(), "thematic-break");
            spans.push((
                path,
                line_start as i64,
                (next_cursor - line_start) as i64,
                "thematic-break".to_string(),
            ));
            cursor = next_cursor;
            continue;
        }

        if markdown_html_block_start(trimmed_start, trimmed) {
            let block_start = line_start;
            let mut block_end = next_cursor;
            cursor = next_cursor;
            while let Some(((html_start, html_end), html_next_cursor)) =
                next_line_bounds(bytes, cursor)
            {
                let html_line = &bytes[html_start..html_end];
                if trim_line_ascii(html_line).is_empty() {
                    break;
                }
                block_end = html_next_cursor;
                cursor = html_next_cursor;
            }
            let scope = nearest_markdown_heading_path(headings, block_start);
            let path = next_markdown_body_block_path(&mut counters, scope.as_deref(), "html");
            spans.push((
                path,
                block_start as i64,
                (block_end - block_start) as i64,
                "html".to_string(),
            ));
            continue;
        }

        if let Some(fence) = markdown_fence_marker(trimmed_start, trimmed) {
            let block_start = line_start;
            let mut block_end = next_cursor;
            cursor = next_cursor;
            while let Some(((code_start, code_end), code_next_cursor)) =
                next_line_bounds(bytes, cursor)
            {
                let code_line = &bytes[code_start..code_end];
                let code_trimmed_start = leading_ascii_ws(code_line);
                let code_trimmed = &code_line[code_trimmed_start..];
                block_end = code_next_cursor;
                cursor = code_next_cursor;
                if markdown_fence_marker(code_trimmed_start, code_trimmed) == Some(fence) {
                    break;
                }
            }
            let scope = nearest_markdown_heading_path(headings, block_start);
            let path = next_markdown_body_block_path(&mut counters, scope.as_deref(), "code");
            spans.push((
                path,
                block_start as i64,
                (block_end - block_start) as i64,
                "code".to_string(),
            ));
            continue;
        }

        let kind = markdown_body_block_kind(trimmed);
        let block_start = line_start;
        let mut block_end = next_cursor;
        cursor = next_cursor;
        while let Some(((next_line_start, next_line_end), following_cursor)) =
            next_line_bounds(bytes, cursor)
        {
            let next_line = &bytes[next_line_start..next_line_end];
            let next_trimmed_start = leading_ascii_ws(next_line);
            let next_trimmed = &next_line[next_trimmed_start..];
            if trim_line_ascii(next_line).is_empty()
                || (next_trimmed_start <= 3 && markdown_atx_heading(next_trimmed).is_some())
                || markdown_setext_heading_at(
                    bytes,
                    next_line_start,
                    next_line_end,
                    following_cursor,
                )
                .is_some()
                || markdown_indented_code_line(next_trimmed_start, next_line)
                || markdown_link_reference_line(next_trimmed_start, next_trimmed)
                || markdown_thematic_break_line(next_trimmed_start, next_trimmed)
                || markdown_html_block_start(next_trimmed_start, next_trimmed)
                || markdown_fence_marker(next_trimmed_start, next_trimmed).is_some()
            {
                break;
            }
            let next_kind = markdown_body_block_kind(next_trimmed);
            if next_kind != kind {
                break;
            }
            block_end = following_cursor;
            cursor = following_cursor;
        }

        let scope = nearest_markdown_heading_path(headings, block_start);
        let path = next_markdown_body_block_path(&mut counters, scope.as_deref(), kind);
        let span_path = path.clone();
        spans.push((
            path,
            block_start as i64,
            (block_end - block_start) as i64,
            kind.to_string(),
        ));
        if kind == "list" {
            push_markdown_list_item_spans(bytes, block_start, block_end, &span_path, &mut spans);
        } else if kind == "table" {
            push_markdown_table_row_spans(bytes, block_start, block_end, &span_path, &mut spans);
        } else if kind == "blockquote" {
            push_markdown_blockquote_line_spans(
                bytes,
                block_start,
                block_end,
                &span_path,
                &mut spans,
            );
        } else if kind == "paragraph" {
            push_markdown_paragraph_line_spans(
                bytes,
                block_start,
                block_end,
                &span_path,
                &link_references,
                &mut spans,
            );
        }
    }
    spans
}

fn push_markdown_list_item_spans(
    bytes: &[u8],
    block_start: usize,
    block_end: usize,
    list_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let mut cursor = block_start;
    let mut item_start: Option<(usize, i64)> = None;
    let mut item_end = block_start;
    let mut item_count = 0_i64;
    while cursor < block_end {
        let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) else {
            break;
        };
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if markdown_list_marker(trimmed) {
            if let Some((start, item_number)) = item_start {
                spans.push((
                    format!("{list_path}.item.{item_number}"),
                    start as i64,
                    (item_end - start) as i64,
                    "list-item".to_string(),
                ));
            }
            item_count += 1;
            if let Some((checkbox_start, checkbox_end)) = markdown_task_checkbox_marker(trimmed) {
                spans.push((
                    format!("{list_path}.item.{item_count}.checkbox"),
                    (line_start + trimmed_start + checkbox_start) as i64,
                    (checkbox_end - checkbox_start) as i64,
                    "task-checkbox".to_string(),
                ));
            }
            item_start = Some((line_start, item_count));
        }
        item_end = next_cursor.min(block_end);
        cursor = next_cursor;
    }
    if let Some((start, item_number)) = item_start {
        spans.push((
            format!("{list_path}.item.{item_number}"),
            start as i64,
            (item_end - start) as i64,
            "list-item".to_string(),
        ));
    }
}

fn push_markdown_table_row_spans(
    bytes: &[u8],
    block_start: usize,
    block_end: usize,
    table_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let mut cursor = block_start;
    let mut row_count = 0_i64;
    while cursor < block_end {
        let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) else {
            break;
        };
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if markdown_table_separator_line(trimmed) {
            push_markdown_table_alignment_spans(bytes, line_start, line_end, table_path, spans);
        } else if markdown_table_line(trimmed) {
            row_count += 1;
            let row_path = format!("{table_path}.row.{row_count}");
            spans.push((
                row_path.clone(),
                line_start as i64,
                (next_cursor.min(block_end) - line_start) as i64,
                "table-row".to_string(),
            ));
            push_markdown_table_cell_spans(bytes, line_start, line_end, &row_path, spans);
        }
        cursor = next_cursor;
    }
}

fn push_markdown_table_alignment_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    table_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut align_count = 0_i64;
    let mut cell_start = 0;
    for idx in 0..=line.len() {
        if idx < line.len() && !markdown_unescaped_pipe_at(line, idx) {
            continue;
        }
        let raw_start = cell_start;
        let raw_end = idx;
        cell_start = idx + 1;
        let value_start = raw_start + leading_ascii_ws(&line[raw_start..raw_end]);
        let value_end = trim_trailing_ascii_ws_end(line, raw_end);
        if value_start >= value_end {
            continue;
        }
        let value = &line[value_start..value_end];
        if !markdown_table_separator_cell(value) {
            continue;
        }
        align_count += 1;
        spans.push((
            format!("{table_path}.align.{align_count}"),
            (line_start + value_start) as i64,
            (value_end - value_start) as i64,
            "table-align".to_string(),
        ));
    }
}

fn push_markdown_table_cell_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    row_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cell_count = 0_i64;
    let mut cell_start = 0;
    for idx in 0..=line.len() {
        if idx < line.len() && !markdown_unescaped_pipe_at(line, idx) {
            continue;
        }
        let raw_start = cell_start;
        let raw_end = idx;
        cell_start = idx + 1;
        if raw_start == raw_end && (idx == 0 || idx == line.len()) {
            continue;
        }
        let value_start = raw_start + leading_ascii_ws(&line[raw_start..raw_end]);
        let value_end = trim_trailing_ascii_ws_end(line, raw_end);
        if value_start >= value_end {
            continue;
        }
        cell_count += 1;
        spans.push((
            format!("{row_path}.cell.{cell_count}"),
            (line_start + value_start) as i64,
            (value_end - value_start) as i64,
            "table-cell".to_string(),
        ));
    }
}

fn push_markdown_blockquote_line_spans(
    bytes: &[u8],
    block_start: usize,
    block_end: usize,
    blockquote_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let mut cursor = block_start;
    let mut line_count = 0_i64;
    while cursor < block_end {
        let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) else {
            break;
        };
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if trimmed.starts_with(b">") {
            line_count += 1;
            spans.push((
                format!("{blockquote_path}.line.{line_count}"),
                line_start as i64,
                (next_cursor.min(block_end) - line_start) as i64,
                "blockquote-line".to_string(),
            ));
        }
        cursor = next_cursor;
    }
}

fn push_markdown_paragraph_line_spans(
    bytes: &[u8],
    block_start: usize,
    block_end: usize,
    paragraph_path: &str,
    link_references: &[MarkdownLinkReferenceDefinition],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let mut cursor = block_start;
    let mut line_count = 0_i64;
    while cursor < block_end {
        let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) else {
            break;
        };
        let line = &bytes[line_start..line_end];
        if !trim_line_ascii(line).is_empty() {
            line_count += 1;
            let line_path = format!("{paragraph_path}.line.{line_count}");
            spans.push((
                line_path.clone(),
                line_start as i64,
                (next_cursor.min(block_end) - line_start) as i64,
                "paragraph-line".to_string(),
            ));
            let inline_code_ranges = markdown_inline_code_ranges(&bytes[line_start..line_end]);
            push_markdown_inline_image_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                spans,
            );
            push_markdown_reference_image_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                link_references,
                spans,
            );
            push_markdown_inline_link_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                spans,
            );
            push_markdown_reference_link_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                link_references,
                spans,
            );
            push_markdown_emphasis_and_strong_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                spans,
            );
            push_markdown_inline_code_spans(line_start, &line_path, &inline_code_ranges, spans);
            push_markdown_autolink_spans(
                bytes,
                line_start,
                line_end,
                &line_path,
                &inline_code_ranges,
                spans,
            );
        }
        cursor = next_cursor;
    }
}

#[derive(Clone, Copy)]
struct MarkdownInlineDelimitedSpan {
    start: usize,
    end: usize,
    text_start: usize,
    text_end: usize,
}

fn push_markdown_emphasis_and_strong_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut strong_candidates = Vec::new();
    collect_markdown_delimited_spans(line, b'*', 2, excluded_ranges, &mut strong_candidates);
    collect_markdown_delimited_spans(line, b'_', 2, excluded_ranges, &mut strong_candidates);
    strong_candidates.sort_by_key(|candidate| (candidate.start, candidate.end));

    let mut strong_ranges = Vec::new();
    let mut strong_count = 0_i64;
    for candidate in strong_candidates {
        if markdown_range_overlaps(candidate.start, candidate.end, &strong_ranges) {
            continue;
        }
        strong_count += 1;
        let strong_path = format!("{line_path}.strong.{strong_count}");
        spans.push((
            strong_path.clone(),
            (line_start + candidate.start) as i64,
            (candidate.end - candidate.start) as i64,
            "inline-strong".to_string(),
        ));
        spans.push((
            format!("{strong_path}.text"),
            (line_start + candidate.text_start) as i64,
            (candidate.text_end - candidate.text_start) as i64,
            "inline-strong-text".to_string(),
        ));
        strong_ranges.push((candidate.start, candidate.end));
    }

    let mut emphasis_excluded_ranges = excluded_ranges.to_vec();
    emphasis_excluded_ranges.extend(strong_ranges.iter().copied());
    let mut emphasis_candidates = Vec::new();
    collect_markdown_delimited_spans(
        line,
        b'*',
        1,
        &emphasis_excluded_ranges,
        &mut emphasis_candidates,
    );
    collect_markdown_delimited_spans(
        line,
        b'_',
        1,
        &emphasis_excluded_ranges,
        &mut emphasis_candidates,
    );
    emphasis_candidates.sort_by_key(|candidate| (candidate.start, candidate.end));

    let mut emphasis_ranges = Vec::new();
    let mut emphasis_count = 0_i64;
    for candidate in emphasis_candidates {
        if markdown_range_overlaps(candidate.start, candidate.end, &emphasis_ranges) {
            continue;
        }
        emphasis_count += 1;
        let emphasis_path = format!("{line_path}.emphasis.{emphasis_count}");
        spans.push((
            emphasis_path.clone(),
            (line_start + candidate.start) as i64,
            (candidate.end - candidate.start) as i64,
            "inline-emphasis".to_string(),
        ));
        spans.push((
            format!("{emphasis_path}.text"),
            (line_start + candidate.text_start) as i64,
            (candidate.text_end - candidate.text_start) as i64,
            "inline-emphasis-text".to_string(),
        ));
        emphasis_ranges.push((candidate.start, candidate.end));
    }
}

fn collect_markdown_delimited_spans(
    line: &[u8],
    marker: u8,
    marker_len: usize,
    excluded_ranges: &[(usize, usize)],
    spans: &mut Vec<MarkdownInlineDelimitedSpan>,
) {
    let mut cursor = 0;
    while cursor + marker_len <= line.len() {
        let Some(open) = markdown_find_unescaped_byte(line, cursor, marker) else {
            break;
        };
        if !markdown_delimiter_at(line, open, marker, marker_len)
            || !markdown_delimiter_boundary_ok(line, open, marker, marker_len)
        {
            cursor = open + 1;
            continue;
        }

        let search_start = open + marker_len;
        let mut close_cursor = search_start;
        let mut matched_close = None;
        while close_cursor + marker_len <= line.len() {
            let Some(close) = markdown_find_unescaped_byte(line, close_cursor, marker) else {
                break;
            };
            if markdown_delimiter_at(line, close, marker, marker_len)
                && markdown_delimiter_boundary_ok(line, close, marker, marker_len)
            {
                matched_close = Some(close);
                break;
            }
            close_cursor = close + 1;
        }

        let Some(close) = matched_close else {
            break;
        };
        let end = close + marker_len;
        if close > search_start
            && !trim_line_ascii(&line[search_start..close]).is_empty()
            && !markdown_range_overlaps(open, end, excluded_ranges)
        {
            spans.push(MarkdownInlineDelimitedSpan {
                start: open,
                end,
                text_start: search_start,
                text_end: close,
            });
        }
        cursor = end;
    }
}

fn markdown_delimiter_at(line: &[u8], offset: usize, marker: u8, marker_len: usize) -> bool {
    offset + marker_len <= line.len()
        && !markdown_byte_is_escaped(line, offset)
        && line[offset..offset + marker_len]
            .iter()
            .all(|byte| *byte == marker)
}

fn markdown_delimiter_boundary_ok(
    line: &[u8],
    offset: usize,
    marker: u8,
    marker_len: usize,
) -> bool {
    if offset > 0 && line[offset - 1] == marker {
        return false;
    }
    if line.get(offset + marker_len) == Some(&marker) {
        return false;
    }
    if marker == b'_' {
        let before = offset.checked_sub(1).and_then(|idx| line.get(idx)).copied();
        let after = line.get(offset + marker_len).copied();
        if before.is_some_and(|byte| byte.is_ascii_alphanumeric())
            && after.is_some_and(|byte| byte.is_ascii_alphanumeric())
        {
            return false;
        }
    }
    true
}

fn push_markdown_reference_image_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    link_references: &[MarkdownLinkReferenceDefinition],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cursor = 0;
    let mut image_count = 0_i64;
    while cursor + 1 < line.len() {
        let Some(bang) = markdown_find_unescaped_byte(line, cursor, b'!') else {
            break;
        };
        if line.get(bang + 1) != Some(&b'[') {
            cursor = bang + 1;
            continue;
        }
        let open = bang + 1;
        let Some(alt_close) = markdown_find_unescaped_byte(line, open + 1, b']') else {
            break;
        };
        if alt_close == open + 1 || line.get(alt_close + 1) == Some(&b'(') {
            cursor = alt_close + 1;
            continue;
        }
        let (reference_open, reference_close, span_end, reference_bytes) =
            if line.get(alt_close + 1) == Some(&b'[') {
                let reference_open = alt_close + 1;
                let Some(reference_close) =
                    markdown_find_unescaped_byte(line, reference_open + 1, b']')
                else {
                    cursor = reference_open + 1;
                    continue;
                };
                let reference_bytes = if reference_close == reference_open + 1 {
                    &line[open + 1..alt_close]
                } else {
                    &line[reference_open + 1..reference_close]
                };
                (
                    reference_open,
                    reference_close,
                    reference_close + 1,
                    reference_bytes,
                )
            } else {
                (
                    alt_close + 1,
                    alt_close + 1,
                    alt_close + 1,
                    &line[open + 1..alt_close],
                )
            };
        if markdown_range_overlaps(bang, span_end, excluded_ranges) {
            cursor = span_end;
            continue;
        }
        let Some(reference_label) = markdown_normalized_reference_label(reference_bytes) else {
            cursor = span_end;
            continue;
        };
        let Some(definition) = link_references
            .iter()
            .find(|definition| definition.normalized_label == reference_label)
        else {
            cursor = span_end;
            continue;
        };
        image_count += 1;
        let image_path = format!("{line_path}.reference-image.{image_count}");
        spans.push((
            image_path.clone(),
            (line_start + bang) as i64,
            (span_end - bang) as i64,
            "reference-image".to_string(),
        ));
        spans.push((
            format!("{image_path}.alt"),
            (line_start + open + 1) as i64,
            (alt_close - open - 1) as i64,
            "reference-image-alt".to_string(),
        ));
        spans.push((
            format!("{image_path}.reference"),
            (line_start + reference_open + 1) as i64,
            reference_close.saturating_sub(reference_open + 1) as i64,
            "reference-image-reference".to_string(),
        ));
        spans.push((
            format!("{image_path}.resolved-destination"),
            definition.destination_start as i64,
            (definition.destination_end - definition.destination_start) as i64,
            "reference-image-resolved-destination".to_string(),
        ));
        if let Some((title_start, title_end)) = definition.title_span {
            spans.push((
                format!("{image_path}.resolved-title"),
                title_start as i64,
                (title_end - title_start) as i64,
                "reference-image-resolved-title".to_string(),
            ));
        }
        cursor = span_end;
    }
}

fn push_markdown_reference_link_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    link_references: &[MarkdownLinkReferenceDefinition],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cursor = 0;
    let mut link_count = 0_i64;
    while cursor < line.len() {
        let Some(open) = markdown_find_unescaped_byte(line, cursor, b'[') else {
            break;
        };
        if open > 0 && line[open - 1] == b'!' && !markdown_byte_is_escaped(line, open - 1) {
            cursor = open + 1;
            continue;
        }
        let Some(label_close) = markdown_find_unescaped_byte(line, open + 1, b']') else {
            break;
        };
        if label_close == open + 1 || line.get(label_close + 1) == Some(&b'(') {
            cursor = label_close + 1;
            continue;
        }
        let (reference_open, reference_close, span_end, reference_bytes) =
            if line.get(label_close + 1) == Some(&b'[') {
                let reference_open = label_close + 1;
                let Some(reference_close) =
                    markdown_find_unescaped_byte(line, reference_open + 1, b']')
                else {
                    cursor = reference_open + 1;
                    continue;
                };
                let reference_bytes = if reference_close == reference_open + 1 {
                    &line[open + 1..label_close]
                } else {
                    &line[reference_open + 1..reference_close]
                };
                (
                    reference_open,
                    reference_close,
                    reference_close + 1,
                    reference_bytes,
                )
            } else {
                if open > 0 && line[open - 1] == b']' {
                    cursor = label_close + 1;
                    continue;
                }
                (
                    label_close + 1,
                    label_close + 1,
                    label_close + 1,
                    &line[open + 1..label_close],
                )
            };
        if markdown_range_overlaps(open, span_end, excluded_ranges) {
            cursor = span_end;
            continue;
        }
        let Some(reference_label) = markdown_normalized_reference_label(reference_bytes) else {
            cursor = span_end;
            continue;
        };
        let Some(definition) = link_references
            .iter()
            .find(|definition| definition.normalized_label == reference_label)
        else {
            cursor = span_end;
            continue;
        };
        link_count += 1;
        let link_path = format!("{line_path}.reference-link.{link_count}");
        spans.push((
            link_path.clone(),
            (line_start + open) as i64,
            (span_end - open) as i64,
            "reference-link".to_string(),
        ));
        spans.push((
            format!("{link_path}.label"),
            (line_start + open + 1) as i64,
            (label_close - open - 1) as i64,
            "reference-link-label".to_string(),
        ));
        spans.push((
            format!("{link_path}.reference"),
            (line_start + reference_open + 1) as i64,
            reference_close.saturating_sub(reference_open + 1) as i64,
            "reference-link-reference".to_string(),
        ));
        spans.push((
            format!("{link_path}.resolved-destination"),
            definition.destination_start as i64,
            (definition.destination_end - definition.destination_start) as i64,
            "reference-link-resolved-destination".to_string(),
        ));
        if let Some((title_start, title_end)) = definition.title_span {
            spans.push((
                format!("{link_path}.resolved-title"),
                title_start as i64,
                (title_end - title_start) as i64,
                "reference-link-resolved-title".to_string(),
            ));
        }
        cursor = span_end;
    }
}

fn push_markdown_inline_image_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cursor = 0;
    let mut image_count = 0_i64;
    while cursor + 1 < line.len() {
        let Some(bang) = markdown_find_unescaped_byte(line, cursor, b'!') else {
            break;
        };
        if line.get(bang + 1) != Some(&b'[') {
            cursor = bang + 1;
            continue;
        }
        let open = bang + 1;
        let Some(alt_close) = markdown_find_unescaped_byte(line, open + 1, b']') else {
            break;
        };
        if line.get(alt_close + 1) != Some(&b'(') {
            cursor = alt_close + 1;
            continue;
        }
        let destination_start = alt_close + 2;
        let Some(close) = markdown_find_inline_destination_close(line, destination_start) else {
            cursor = destination_start;
            continue;
        };
        if trim_line_ascii(&line[destination_start..close]).is_empty() {
            cursor = close + 1;
            continue;
        }
        let span_end = close + 1;
        if markdown_range_overlaps(bang, span_end, excluded_ranges) {
            cursor = span_end;
            continue;
        }
        let Some((destination_trim_start, destination_trim_end, title_span)) =
            markdown_inline_destination_and_title_spans(line, destination_start, close)
        else {
            cursor = close + 1;
            continue;
        };
        image_count += 1;
        let image_path = format!("{line_path}.image.{image_count}");
        spans.push((
            image_path.clone(),
            (line_start + bang) as i64,
            (span_end - bang) as i64,
            "inline-image".to_string(),
        ));
        if alt_close > open + 1 {
            spans.push((
                format!("{image_path}.alt"),
                (line_start + open + 1) as i64,
                (alt_close - open - 1) as i64,
                "inline-image-alt".to_string(),
            ));
        }
        spans.push((
            format!("{image_path}.destination"),
            (line_start + destination_trim_start) as i64,
            (destination_trim_end - destination_trim_start) as i64,
            "inline-image-destination".to_string(),
        ));
        if let Some((title_start, title_end)) = title_span {
            spans.push((
                format!("{image_path}.title"),
                (line_start + title_start) as i64,
                (title_end - title_start) as i64,
                "inline-image-title".to_string(),
            ));
        }
        cursor = span_end;
    }
}

fn push_markdown_inline_link_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cursor = 0;
    let mut link_count = 0_i64;
    while cursor < line.len() {
        let Some(open) = markdown_find_unescaped_byte(line, cursor, b'[') else {
            break;
        };
        if open > 0 && line[open - 1] == b'!' && !markdown_byte_is_escaped(line, open - 1) {
            cursor = open + 1;
            continue;
        }
        let Some(label_close) = markdown_find_unescaped_byte(line, open + 1, b']') else {
            break;
        };
        if label_close == open + 1 || line.get(label_close + 1) != Some(&b'(') {
            cursor = label_close + 1;
            continue;
        }
        let destination_start = label_close + 2;
        let Some(close) = markdown_find_inline_destination_close(line, destination_start) else {
            cursor = destination_start;
            continue;
        };
        if trim_line_ascii(&line[destination_start..close]).is_empty() {
            cursor = close + 1;
            continue;
        }
        let span_end = close + 1;
        if markdown_range_overlaps(open, span_end, excluded_ranges) {
            cursor = span_end;
            continue;
        }
        let Some((destination_trim_start, destination_trim_end, title_span)) =
            markdown_inline_destination_and_title_spans(line, destination_start, close)
        else {
            cursor = close + 1;
            continue;
        };
        link_count += 1;
        let link_path = format!("{line_path}.link.{link_count}");
        spans.push((
            link_path.clone(),
            (line_start + open) as i64,
            (span_end - open) as i64,
            "inline-link".to_string(),
        ));
        spans.push((
            format!("{link_path}.label"),
            (line_start + open + 1) as i64,
            (label_close - open - 1) as i64,
            "inline-link-label".to_string(),
        ));
        spans.push((
            format!("{link_path}.destination"),
            (line_start + destination_trim_start) as i64,
            (destination_trim_end - destination_trim_start) as i64,
            "inline-link-destination".to_string(),
        ));
        if let Some((title_start, title_end)) = title_span {
            spans.push((
                format!("{link_path}.title"),
                (line_start + title_start) as i64,
                (title_end - title_start) as i64,
                "inline-link-title".to_string(),
            ));
        }
        cursor = span_end;
    }
}

fn markdown_inline_destination_and_title_spans(
    line: &[u8],
    content_start: usize,
    content_end: usize,
) -> Option<(usize, usize, Option<(usize, usize)>)> {
    let destination_start = content_start + leading_ascii_ws(&line[content_start..content_end]);
    let mut destination_end = trim_trailing_ascii_ws_end(line, content_end);
    if destination_start >= destination_end {
        return None;
    }
    let mut title_span = None;
    let title_quote = line[destination_end - 1];
    if matches!(title_quote, b'"' | b'\'') {
        let title_end = destination_end - 1;
        if let Some(open) =
            markdown_rfind_unescaped_byte(line, destination_start, destination_end - 1, title_quote)
        {
            if open > destination_start
                && line[open - 1].is_ascii_whitespace()
                && open + 1 < destination_end - 1
            {
                let candidate_destination_end = trim_trailing_ascii_ws_end(line, open);
                if destination_start < candidate_destination_end {
                    destination_end = candidate_destination_end;
                    title_span = Some((open + 1, title_end));
                }
            }
        }
    } else if title_quote == b')' && !markdown_byte_is_escaped(line, destination_end - 1) {
        let title_end = destination_end - 1;
        if let Some(open) =
            markdown_find_parenthesized_title_open(line, destination_start, title_end)
        {
            if open > destination_start
                && line[open - 1].is_ascii_whitespace()
                && open + 1 < title_end
            {
                let candidate_destination_end = trim_trailing_ascii_ws_end(line, open);
                if destination_start < candidate_destination_end {
                    destination_end = candidate_destination_end;
                    title_span = Some((open + 1, title_end));
                }
            }
        }
    }
    if line[destination_start] == b'<' && !markdown_byte_is_escaped(line, destination_start) {
        let angle_close_end = trim_trailing_ascii_ws_end(line, destination_end);
        if angle_close_end > destination_start + 1
            && line[angle_close_end - 1] == b'>'
            && !markdown_byte_is_escaped(line, angle_close_end - 1)
        {
            return Some((destination_start + 1, angle_close_end - 1, title_span));
        }
    }
    Some((destination_start, destination_end, title_span))
}

fn markdown_find_parenthesized_title_open(
    line: &[u8],
    start: usize,
    title_close: usize,
) -> Option<usize> {
    let mut cursor = title_close;
    let mut depth = 0_usize;
    while cursor > start {
        cursor -= 1;
        if markdown_byte_is_escaped(line, cursor) {
            continue;
        }
        match line[cursor] {
            b')' => depth += 1,
            b'(' => {
                if depth == 0 {
                    return Some(cursor);
                }
                depth -= 1;
            }
            _ => {}
        }
    }
    None
}

fn markdown_find_inline_destination_close(line: &[u8], start: usize) -> Option<usize> {
    let mut cursor = start;
    let mut paren_depth = 0_usize;
    while cursor < line.len() {
        if markdown_byte_is_escaped(line, cursor) {
            cursor += 1;
            continue;
        }
        match line[cursor] {
            b'(' => {
                paren_depth += 1;
                cursor += 1;
            }
            b')' => {
                if paren_depth == 0 {
                    return Some(cursor);
                }
                paren_depth -= 1;
                cursor += 1;
            }
            _ => cursor += 1,
        }
    }
    None
}

fn push_markdown_inline_code_spans(
    line_start: usize,
    line_path: &str,
    code_ranges: &[(usize, usize)],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let mut code_count = 0_i64;
    for &(open, close) in code_ranges {
        code_count += 1;
        spans.push((
            format!("{line_path}.code.{code_count}"),
            (line_start + open) as i64,
            (close - open) as i64,
            "inline-code".to_string(),
        ));
    }
}

fn markdown_inline_code_ranges(line: &[u8]) -> Vec<(usize, usize)> {
    let mut ranges = Vec::new();
    let mut cursor = 0;
    while cursor < line.len() {
        let Some(open) = markdown_find_unescaped_byte(line, cursor, b'`') else {
            break;
        };
        let marker_len = markdown_backtick_run_len(line, open);
        let search_start = open + marker_len;
        let Some(close) = markdown_find_matching_backtick_run(line, search_start, marker_len)
        else {
            break;
        };
        if close == search_start {
            cursor = close + marker_len;
            continue;
        }
        ranges.push((open, close + marker_len));
        cursor = close + marker_len;
    }
    ranges
}

fn markdown_backtick_run_len(line: &[u8], start: usize) -> usize {
    let mut cursor = start;
    while cursor < line.len() && line[cursor] == b'`' {
        cursor += 1;
    }
    cursor - start
}

fn markdown_find_matching_backtick_run(
    line: &[u8],
    start: usize,
    marker_len: usize,
) -> Option<usize> {
    let mut cursor = start;
    while cursor < line.len() {
        let candidate = markdown_find_unescaped_byte(line, cursor, b'`')?;
        if markdown_backtick_run_len(line, candidate) == marker_len {
            return Some(candidate);
        }
        cursor = candidate + markdown_backtick_run_len(line, candidate);
    }
    None
}

fn markdown_range_overlaps(start: usize, end: usize, ranges: &[(usize, usize)]) -> bool {
    ranges
        .iter()
        .any(|(range_start, range_end)| start < *range_end && end > *range_start)
}

fn markdown_byte_is_escaped(line: &[u8], offset: usize) -> bool {
    let mut cursor = offset;
    let mut slash_count = 0;
    while cursor > 0 && line[cursor - 1] == b'\\' {
        slash_count += 1;
        cursor -= 1;
    }
    slash_count % 2 == 1
}

fn markdown_find_unescaped_byte(line: &[u8], start: usize, needle: u8) -> Option<usize> {
    let mut cursor = start;
    while cursor < line.len() {
        if line[cursor] == needle && !markdown_byte_is_escaped(line, cursor) {
            return Some(cursor);
        }
        cursor += 1;
    }
    None
}

fn markdown_rfind_unescaped_byte(
    line: &[u8],
    start: usize,
    end: usize,
    needle: u8,
) -> Option<usize> {
    let mut cursor = end;
    while cursor > start {
        cursor -= 1;
        if line[cursor] == needle && !markdown_byte_is_escaped(line, cursor) {
            return Some(cursor);
        }
    }
    None
}

fn push_markdown_autolink_spans(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    line_path: &str,
    excluded_ranges: &[(usize, usize)],
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let line = &bytes[line_start..line_end];
    let mut cursor = 0;
    let mut autolink_count = 0_i64;
    while cursor < line.len() {
        let Some(open) = markdown_find_unescaped_byte(line, cursor, b'<') else {
            break;
        };
        let Some(close) = markdown_find_unescaped_byte(line, open + 1, b'>') else {
            break;
        };
        let target = &line[open + 1..close];
        if markdown_autolink_target(target)
            && !markdown_range_overlaps(open, close + 1, excluded_ranges)
        {
            autolink_count += 1;
            let autolink_path = format!("{line_path}.autolink.{autolink_count}");
            spans.push((
                autolink_path.clone(),
                (line_start + open) as i64,
                (close + 1 - open) as i64,
                "autolink".to_string(),
            ));
            spans.push((
                format!("{autolink_path}.target"),
                (line_start + open + 1) as i64,
                (close - open - 1) as i64,
                "autolink-target".to_string(),
            ));
        }
        cursor = close + 1;
    }
}

fn markdown_autolink_target(target: &[u8]) -> bool {
    if target.is_empty() || target.iter().any(|byte| byte.is_ascii_whitespace()) {
        return false;
    }
    target.starts_with(b"http://")
        || target.starts_with(b"https://")
        || target.starts_with(b"mailto:")
}

fn markdown_body_start(bytes: &[u8]) -> usize {
    let Some((first_line, mut cursor)) = next_line_bounds(bytes, 0) else {
        return 0;
    };
    if trim_line_ascii(&bytes[first_line.0..first_line.1]) != b"---" {
        return 0;
    }
    while let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) {
        cursor = next_cursor;
        if trim_line_ascii(&bytes[line_start..line_end]) == b"---" {
            return cursor;
        }
    }
    0
}

fn markdown_atx_heading(line: &[u8]) -> Option<(usize, &str)> {
    let level = line.iter().take_while(|byte| **byte == b'#').count();
    if !(1..=6).contains(&level) {
        return None;
    }
    if line.len() > level && !line[level].is_ascii_whitespace() {
        return None;
    }
    let mut title = trim_line_ascii(&line[level..]);
    while title.ends_with(b"#") {
        title = trim_trailing_ascii_ws_end(&title[..title.len() - 1], title.len() - 1)
            .checked_sub(0)
            .map(|end| &title[..end])?;
    }
    let title = trim_line_ascii(title);
    if title.is_empty() {
        return None;
    }
    std::str::from_utf8(title).ok().map(|title| (level, title))
}

fn markdown_setext_heading_at(
    bytes: &[u8],
    line_start: usize,
    line_end: usize,
    next_cursor: usize,
) -> Option<(usize, usize, &str)> {
    let line = &bytes[line_start..line_end];
    let trimmed_start = leading_ascii_ws(line);
    let trimmed = &line[trimmed_start..];
    let title = markdown_setext_title(trimmed_start, trimmed)?;
    let ((underline_start, underline_end), _after_underline) =
        next_line_bounds(bytes, next_cursor)?;
    let underline = &bytes[underline_start..underline_end];
    let underline_trimmed_start = leading_ascii_ws(underline);
    let underline_trimmed = &underline[underline_trimmed_start..];
    let level = markdown_setext_underline(underline_trimmed_start, underline_trimmed)?;
    Some((level, line_start, title))
}

fn markdown_setext_title(indent: usize, trimmed: &[u8]) -> Option<&str> {
    if indent > 3
        || trimmed.is_empty()
        || markdown_atx_heading(trimmed).is_some()
        || markdown_fence_marker(indent, trimmed).is_some()
        || markdown_link_reference_line(indent, trimmed)
        || markdown_body_block_kind(trimmed) != "paragraph"
    {
        return None;
    }
    std::str::from_utf8(trim_line_ascii(trimmed))
        .ok()
        .filter(|title| !title.is_empty())
}

fn markdown_setext_underline(indent: usize, trimmed: &[u8]) -> Option<usize> {
    if indent > 3 || trimmed.len() < 3 {
        return None;
    }
    if trimmed.iter().all(|byte| *byte == b'=') {
        Some(1)
    } else if trimmed.iter().all(|byte| *byte == b'-') {
        Some(2)
    } else {
        None
    }
}

fn markdown_fence_marker(indent: usize, trimmed: &[u8]) -> Option<&'static [u8]> {
    if indent > 3 {
        return None;
    }
    if trimmed.starts_with(b"```") {
        Some(b"```")
    } else if trimmed.starts_with(b"~~~") {
        Some(b"~~~")
    } else {
        None
    }
}

fn markdown_body_block_kind(trimmed: &[u8]) -> &'static str {
    if trimmed.starts_with(b">") {
        "blockquote"
    } else if markdown_list_marker(trimmed) {
        "list"
    } else if markdown_table_line(trimmed) {
        "table"
    } else {
        "paragraph"
    }
}

fn markdown_indented_code_line(indent: usize, line: &[u8]) -> bool {
    indent >= 4 && !trim_line_ascii(line).is_empty()
}

fn markdown_link_reference_line(indent: usize, trimmed: &[u8]) -> bool {
    if indent > 3 {
        return false;
    }
    markdown_link_reference_definition_parts(trimmed).is_some()
}

fn markdown_link_reference_definitions(bytes: &[u8]) -> Vec<MarkdownLinkReferenceDefinition> {
    let mut definitions = Vec::new();
    let mut cursor = markdown_body_start(bytes);
    let mut in_fence: Option<&[u8]> = None;
    while let Some(((line_start, line_end), next_cursor)) = next_line_bounds(bytes, cursor) {
        let line = &bytes[line_start..line_end];
        let trimmed_start = leading_ascii_ws(line);
        let trimmed = &line[trimmed_start..];
        if trimmed_start <= 3 {
            if let Some(fence) = in_fence {
                if markdown_fence_marker(trimmed_start, trimmed) == Some(fence) {
                    in_fence = None;
                }
                cursor = next_cursor;
                continue;
            }
            if let Some(fence) = markdown_fence_marker(trimmed_start, trimmed) {
                in_fence = Some(fence);
                cursor = next_cursor;
                continue;
            }
        }
        if markdown_indented_code_line(trimmed_start, line) {
            cursor = next_cursor;
            continue;
        }
        if let Some(parts) = markdown_link_reference_definition_parts(trimmed) {
            let mut consumed_cursor = next_cursor;
            let mut title_span = parts.title_span.map(|(title_start, title_end)| {
                (
                    line_start + trimmed_start + title_start,
                    line_start + trimmed_start + title_end,
                )
            });
            if title_span.is_none() {
                if let Some((title_start, title_end, continuation_end)) =
                    markdown_link_reference_continuation_title(bytes, next_cursor)
                {
                    title_span = Some((title_start, title_end));
                    consumed_cursor = continuation_end;
                }
            }
            if !definitions
                .iter()
                .any(|definition: &MarkdownLinkReferenceDefinition| {
                    definition.normalized_label == parts.normalized_label
                })
            {
                definitions.push(MarkdownLinkReferenceDefinition {
                    normalized_label: parts.normalized_label,
                    destination_start: line_start + trimmed_start + parts.destination_start,
                    destination_end: line_start + trimmed_start + parts.destination_end,
                    title_span,
                });
            }
            cursor = consumed_cursor;
            continue;
        }
        cursor = next_cursor;
    }
    definitions
}

struct MarkdownLinkReferenceDefinitionParts {
    normalized_label: String,
    label_start: usize,
    label_end: usize,
    destination_start: usize,
    destination_end: usize,
    title_span: Option<(usize, usize)>,
}

fn push_markdown_link_reference_component_spans(
    bytes: &[u8],
    line_start: usize,
    block_end: usize,
    reference_path: &str,
    spans: &mut Vec<crate::MarkdownSectionSpanRow>,
) {
    let Some(((first_line_start, line_end), next_cursor)) = next_line_bounds(bytes, line_start)
    else {
        return;
    };
    debug_assert_eq!(first_line_start, line_start);
    let line = &bytes[line_start..line_end];
    let trimmed_start = leading_ascii_ws(line);
    let trimmed = &line[trimmed_start..];
    let Some(parts) = markdown_link_reference_definition_parts(trimmed) else {
        return;
    };
    spans.push((
        format!("{reference_path}.label"),
        (line_start + trimmed_start + parts.label_start) as i64,
        (parts.label_end - parts.label_start) as i64,
        "link-reference-label".to_string(),
    ));
    spans.push((
        format!("{reference_path}.destination"),
        (line_start + trimmed_start + parts.destination_start) as i64,
        (parts.destination_end - parts.destination_start) as i64,
        "link-reference-destination".to_string(),
    ));
    if let Some((title_start, title_end)) = parts.title_span {
        spans.push((
            format!("{reference_path}.title"),
            (line_start + trimmed_start + title_start) as i64,
            (title_end - title_start) as i64,
            "link-reference-title".to_string(),
        ));
    } else if next_cursor < block_end {
        if let Some((title_start, title_end, _continuation_end)) =
            markdown_link_reference_continuation_title(bytes, next_cursor)
        {
            spans.push((
                format!("{reference_path}.title"),
                title_start as i64,
                (title_end - title_start) as i64,
                "link-reference-title".to_string(),
            ));
        }
    }
}

fn markdown_link_reference_definition_parts(
    line: &[u8],
) -> Option<MarkdownLinkReferenceDefinitionParts> {
    let line = trim_line_ascii(line);
    if !line.starts_with(b"[") {
        return None;
    }
    let close_idx = markdown_find_unescaped_byte(line, 1, b']')?;
    if close_idx == 1 || line.get(close_idx + 1) != Some(&b':') {
        return None;
    }
    let normalized_label = markdown_normalized_reference_label(&line[1..close_idx])?;
    let (destination_start, destination_end, title_span) =
        markdown_inline_destination_and_title_spans(line, close_idx + 2, line.len())?;
    Some(MarkdownLinkReferenceDefinitionParts {
        normalized_label,
        label_start: 1,
        label_end: close_idx,
        destination_start,
        destination_end,
        title_span,
    })
}

fn markdown_link_reference_continuation_title(
    bytes: &[u8],
    cursor: usize,
) -> Option<(usize, usize, usize)> {
    let ((line_start, line_end), next_cursor) = next_line_bounds(bytes, cursor)?;
    let line = &bytes[line_start..line_end];
    let title_start = leading_ascii_ws(line);
    if title_start > 3 {
        return None;
    }
    let title_end = trim_trailing_ascii_ws_end(line, line.len());
    let (payload_start, payload_end) =
        markdown_reference_title_only_payload_span(line, title_start, title_end)?;
    Some((
        line_start + payload_start,
        line_start + payload_end,
        next_cursor,
    ))
}

fn markdown_reference_title_only_payload_span(
    line: &[u8],
    title_start: usize,
    title_end: usize,
) -> Option<(usize, usize)> {
    if title_start >= title_end {
        return None;
    }
    let open = line[title_start];
    let close = line[title_end - 1];
    if matches!(open, b'"' | b'\'')
        && close == open
        && !markdown_byte_is_escaped(line, title_end - 1)
        && title_start + 1 < title_end - 1 {
            return Some((title_start + 1, title_end - 1));
        }
    if open == b'(' && close == b')' && !markdown_byte_is_escaped(line, title_end - 1)
        && title_start + 1 < title_end - 1 {
            return Some((title_start + 1, title_end - 1));
        }
    None
}

fn markdown_normalized_reference_label(label: &[u8]) -> Option<String> {
    let label = trim_line_ascii(label);
    if label.is_empty() {
        return None;
    }
    let label = markdown_unescaped_reference_label_bytes(label);
    let label = std::str::from_utf8(&label).ok()?;
    let mut normalized = String::new();
    let mut pending_space = false;
    for ch in label.chars() {
        if ch.is_ascii_whitespace() {
            pending_space = true;
        } else {
            if pending_space && !normalized.is_empty() {
                normalized.push(' ');
            }
            normalized.push(ch.to_ascii_lowercase());
            pending_space = false;
        }
    }
    if normalized.is_empty() {
        None
    } else {
        Some(normalized)
    }
}

fn markdown_unescaped_reference_label_bytes(label: &[u8]) -> Vec<u8> {
    let mut unescaped = Vec::with_capacity(label.len());
    let mut cursor = 0;
    while cursor < label.len() {
        if label[cursor] == b'\\'
            && cursor + 1 < label.len()
            && label[cursor + 1].is_ascii_punctuation()
        {
            unescaped.push(label[cursor + 1]);
            cursor += 2;
            continue;
        }
        unescaped.push(label[cursor]);
        cursor += 1;
    }
    unescaped
}

fn markdown_thematic_break_line(indent: usize, trimmed: &[u8]) -> bool {
    if indent > 3 {
        return false;
    }
    let trimmed = trim_line_ascii(trimmed);
    let mut marker = None;
    let mut count = 0;
    for byte in trimmed {
        if byte.is_ascii_whitespace() {
            continue;
        }
        if !matches!(*byte, b'-' | b'_' | b'*') {
            return false;
        }
        match marker {
            Some(existing) if existing != *byte => return false,
            Some(_) => {}
            None => marker = Some(*byte),
        }
        count += 1;
    }
    count >= 3
}

fn markdown_html_block_start(indent: usize, trimmed: &[u8]) -> bool {
    if indent > 3 {
        return false;
    }
    let trimmed = trim_line_ascii(trimmed);
    if trimmed.starts_with(b"<!--") || trimmed.starts_with(b"<?") || trimmed.starts_with(b"<!") {
        return true;
    }
    let Some(mut cursor) = trimmed.strip_prefix(b"<") else {
        return false;
    };
    if let Some(rest) = cursor.strip_prefix(b"/") {
        cursor = rest;
    }
    let name_len = cursor
        .iter()
        .take_while(|byte| byte.is_ascii_alphanumeric())
        .count();
    if name_len == 0 {
        return false;
    }
    let tag = cursor[..name_len]
        .iter()
        .map(|byte| byte.to_ascii_lowercase())
        .collect::<Vec<_>>();
    markdown_html_block_tag(&tag)
}

fn markdown_html_block_tag(tag: &[u8]) -> bool {
    matches!(
        tag,
        b"address"
            | b"article"
            | b"aside"
            | b"base"
            | b"basefont"
            | b"blockquote"
            | b"body"
            | b"caption"
            | b"center"
            | b"col"
            | b"colgroup"
            | b"dd"
            | b"details"
            | b"dialog"
            | b"dir"
            | b"div"
            | b"dl"
            | b"dt"
            | b"fieldset"
            | b"figcaption"
            | b"figure"
            | b"footer"
            | b"form"
            | b"frame"
            | b"frameset"
            | b"h1"
            | b"h2"
            | b"h3"
            | b"h4"
            | b"h5"
            | b"h6"
            | b"head"
            | b"header"
            | b"hr"
            | b"html"
            | b"iframe"
            | b"legend"
            | b"li"
            | b"link"
            | b"main"
            | b"menu"
            | b"menuitem"
            | b"nav"
            | b"noframes"
            | b"ol"
            | b"optgroup"
            | b"option"
            | b"p"
            | b"param"
            | b"search"
            | b"section"
            | b"summary"
            | b"table"
            | b"tbody"
            | b"td"
            | b"tfoot"
            | b"th"
            | b"thead"
            | b"title"
            | b"tr"
            | b"track"
            | b"ul"
            | b"script"
            | b"style"
            | b"pre"
    )
}

fn markdown_list_marker(trimmed: &[u8]) -> bool {
    markdown_list_marker_content_start(trimmed).is_some()
}

fn markdown_list_marker_content_start(trimmed: &[u8]) -> Option<usize> {
    if trimmed.len() >= 2
        && matches!(trimmed[0], b'-' | b'*' | b'+')
        && trimmed[1].is_ascii_whitespace()
    {
        return Some(
            1 + trimmed[1..]
                .iter()
                .take_while(|byte| byte.is_ascii_whitespace())
                .count(),
        );
    }
    let digit_count = trimmed
        .iter()
        .take_while(|byte| byte.is_ascii_digit())
        .count();
    if digit_count > 0
        && digit_count + 1 < trimmed.len()
        && matches!(trimmed[digit_count], b'.' | b')')
        && trimmed[digit_count + 1].is_ascii_whitespace()
    {
        Some(
            digit_count
                + 1
                + trimmed[digit_count + 1..]
                    .iter()
                    .take_while(|byte| byte.is_ascii_whitespace())
                    .count(),
        )
    } else {
        None
    }
}

fn markdown_task_checkbox_marker(trimmed: &[u8]) -> Option<(usize, usize)> {
    let start = markdown_list_marker_content_start(trimmed)?;
    let rest = &trimmed[start..];
    if rest.len() < 3
        || rest[0] != b'['
        || !matches!(rest[1], b' ' | b'x' | b'X')
        || rest[2] != b']'
        || rest.get(3).is_some_and(|byte| !byte.is_ascii_whitespace())
    {
        return None;
    }
    Some((start, start + 3))
}

fn markdown_table_line(trimmed: &[u8]) -> bool {
    trimmed
        .iter()
        .enumerate()
        .filter(|(idx, _byte)| markdown_unescaped_pipe_at(trimmed, *idx))
        .count()
        >= 2
}

fn markdown_table_separator_line(trimmed: &[u8]) -> bool {
    if !markdown_table_line(trimmed) {
        return false;
    }
    let mut saw_cell = false;
    let mut cell_start = 0;
    for idx in 0..=trimmed.len() {
        if idx < trimmed.len() && !markdown_unescaped_pipe_at(trimmed, idx) {
            continue;
        }
        let cell = &trimmed[cell_start..idx];
        cell_start = idx + 1;
        let cell = trim_line_ascii(cell);
        if cell.is_empty() {
            continue;
        }
        saw_cell = true;
        if !markdown_table_separator_cell(cell) {
            return false;
        }
    }
    saw_cell
}

fn markdown_table_separator_cell(cell: &[u8]) -> bool {
    let mut hyphen_count = 0;
    for (idx, byte) in cell.iter().enumerate() {
        match *byte {
            b'-' => hyphen_count += 1,
            b':' if idx == 0 || idx + 1 == cell.len() => {}
            _ => return false,
        }
    }
    hyphen_count >= 3
}

fn markdown_unescaped_pipe_at(line: &[u8], idx: usize) -> bool {
    if line.get(idx) != Some(&b'|') {
        return false;
    }
    let mut backslash_count = 0;
    let mut cursor = idx;
    while cursor > 0 && line[cursor - 1] == b'\\' {
        backslash_count += 1;
        cursor -= 1;
    }
    backslash_count % 2 == 0
}

fn nearest_markdown_heading_path(
    headings: &[(usize, usize, String, String)],
    offset: usize,
) -> Option<String> {
    headings
        .iter()
        .take_while(|(_level, start, _title, _path)| *start < offset)
        .last()
        .map(|(_level, _start, _title, path)| path.clone())
}

fn next_markdown_body_block_path(
    counters: &mut Vec<(String, String, i64)>,
    scope: Option<&str>,
    kind: &str,
) -> String {
    let scope = scope.unwrap_or("body").to_string();
    let count = if let Some((_scope, _kind, count)) =
        counters
            .iter_mut()
            .find(|(existing_scope, existing_kind, _count)| {
                existing_scope == &scope && existing_kind == kind
            }) {
        *count += 1;
        *count
    } else {
        counters.push((scope.clone(), kind.to_string(), 1));
        1
    };
    format!("{scope}.{kind}.{count}")
}

fn unique_markdown_section_path(seen_paths: &mut Vec<String>, title: &str) -> String {
    let slug = markdown_section_slug(title);
    let base = format!("body.{slug}");
    if !seen_paths.iter().any(|path| path == &base) {
        seen_paths.push(base.clone());
        return base;
    }
    let mut idx = 2;
    loop {
        let candidate = format!("{base}.{idx}");
        if !seen_paths.iter().any(|path| path == &candidate) {
            seen_paths.push(candidate.clone());
            return candidate;
        }
        idx += 1;
    }
}

fn markdown_section_slug(title: &str) -> String {
    let mut out = String::new();
    let mut pending_dash = false;
    for ch in title.chars() {
        if ch.is_ascii_alphanumeric() {
            if pending_dash && !out.is_empty() {
                out.push('-');
            }
            out.push(ch.to_ascii_lowercase());
            pending_dash = false;
        } else if ch.is_ascii_whitespace() || matches!(ch, '-' | '_' | ':' | '/' | '.') {
            pending_dash = true;
        }
    }
    if out.is_empty() {
        "section".to_string()
    } else {
        out
    }
}

fn next_line_bounds(bytes: &[u8], start: usize) -> Option<((usize, usize), usize)> {
    if start >= bytes.len() {
        return None;
    }
    let mut end = start;
    while end < bytes.len() && bytes[end] != b'\n' {
        end += 1;
    }
    let line_end = if end > start && bytes[end - 1] == b'\r' {
        end - 1
    } else {
        end
    };
    let next = if end < bytes.len() { end + 1 } else { end };
    Some(((start, line_end), next))
}

fn trim_line_ascii(line: &[u8]) -> &[u8] {
    let start = leading_ascii_ws(line);
    let end = trim_trailing_ascii_ws_end(line, line.len());
    if start >= end {
        &line[0..0]
    } else {
        &line[start..end]
    }
}

fn leading_ascii_ws(line: &[u8]) -> usize {
    line.iter()
        .take_while(|byte| byte.is_ascii_whitespace())
        .count()
}

fn trim_trailing_ascii_ws_end(line: &[u8], mut end: usize) -> usize {
    while end > 0 && line[end - 1].is_ascii_whitespace() {
        end -= 1;
    }
    end
}

fn markdown_frontmatter_value_kind(value: &[u8]) -> &'static str {
    let trimmed = trim_line_ascii(value);
    if valid_markdown_frontmatter_alias(trimmed) {
        "alias"
    } else if matches!(trimmed, b"null" | b"Null" | b"NULL" | b"~") {
        "null"
    } else if matches!(trimmed, b"true" | b"false") {
        "bool"
    } else if markdown_frontmatter_implicit_timestamp(trimmed) {
        "timestamp"
    } else if std::str::from_utf8(trimmed)
        .ok()
        .and_then(|value| value.parse::<f64>().ok())
        .is_some()
    {
        "number"
    } else {
        "string"
    }
}

fn markdown_frontmatter_implicit_timestamp(value: &[u8]) -> bool {
    if value.is_empty()
        || matches!(value.first(), Some(b'\'' | b'"'))
        || matches!(value.last(), Some(b'\'' | b'"'))
    {
        return false;
    }
    let Some(mut cursor) = parse_markdown_frontmatter_iso_date(value, 0) else {
        return false;
    };
    if cursor == value.len() {
        return true;
    }
    if !matches!(value.get(cursor), Some(b'T' | b't' | b' ')) {
        return false;
    }
    cursor += 1;
    let Some(cursor) = parse_markdown_frontmatter_iso_time(value, cursor) else {
        return false;
    };
    cursor == value.len()
}

fn parse_markdown_frontmatter_iso_date(value: &[u8], start: usize) -> Option<usize> {
    if start + 10 > value.len() {
        return None;
    }
    if !value[start..start + 4].iter().all(u8::is_ascii_digit)
        || value[start + 4] != b'-'
        || !value[start + 5..start + 7].iter().all(u8::is_ascii_digit)
        || value[start + 7] != b'-'
        || !value[start + 8..start + 10].iter().all(u8::is_ascii_digit)
    {
        return None;
    }
    let month = (value[start + 5] - b'0') * 10 + (value[start + 6] - b'0');
    let day = (value[start + 8] - b'0') * 10 + (value[start + 9] - b'0');
    if !(1..=12).contains(&month) || !(1..=31).contains(&day) {
        return None;
    }
    Some(start + 10)
}

fn parse_markdown_frontmatter_iso_time(value: &[u8], start: usize) -> Option<usize> {
    if start + 8 > value.len()
        || !value[start..start + 2].iter().all(u8::is_ascii_digit)
        || value[start + 2] != b':'
        || !value[start + 3..start + 5].iter().all(u8::is_ascii_digit)
        || value[start + 5] != b':'
        || !value[start + 6..start + 8].iter().all(u8::is_ascii_digit)
    {
        return None;
    }
    let hour = (value[start] - b'0') * 10 + (value[start + 1] - b'0');
    let minute = (value[start + 3] - b'0') * 10 + (value[start + 4] - b'0');
    let second = (value[start + 6] - b'0') * 10 + (value[start + 7] - b'0');
    if hour > 23 || minute > 59 || second > 59 {
        return None;
    }
    let mut cursor = start + 8;
    if value.get(cursor) == Some(&b'.') {
        cursor += 1;
        let fraction_start = cursor;
        while cursor < value.len() && value[cursor].is_ascii_digit() {
            cursor += 1;
        }
        if cursor == fraction_start {
            return None;
        }
    }
    if matches!(value.get(cursor), Some(b'Z' | b'z')) {
        return Some(cursor + 1);
    }
    if matches!(value.get(cursor), Some(b'+' | b'-')) {
        if cursor + 6 > value.len()
            || !value[cursor + 1..cursor + 3].iter().all(u8::is_ascii_digit)
            || value[cursor + 3] != b':'
            || !value[cursor + 4..cursor + 6].iter().all(u8::is_ascii_digit)
        {
            return None;
        }
        let tz_hour = (value[cursor + 1] - b'0') * 10 + (value[cursor + 2] - b'0');
        let tz_minute = (value[cursor + 4] - b'0') * 10 + (value[cursor + 5] - b'0');
        if tz_hour > 23 || tz_minute > 59 {
            return None;
        }
        return Some(cursor + 6);
    }
    Some(cursor)
}

fn markdown_frontmatter_scalar_kind(value: &[u8], payload: &[u8]) -> &'static str {
    markdown_frontmatter_forced_kind(value).unwrap_or_else(|| {
        let (_, raw_payload) = markdown_frontmatter_value_payload(value);
        if markdown_frontmatter_quoted_scalar_inner(raw_payload).is_some() {
            "string"
        } else {
            markdown_frontmatter_value_kind(payload)
        }
    })
}

fn markdown_frontmatter_container_kind(value: &[u8], default_kind: &'static str) -> &'static str {
    match markdown_frontmatter_forced_kind(value) {
        Some("set") => "set",
        Some("omap") => "omap",
        _ => default_kind,
    }
}

fn markdown_frontmatter_forced_kind(value: &[u8]) -> Option<&'static str> {
    let mut cursor = leading_ascii_ws(value);
    let end = trim_trailing_ascii_ws_end(value, value.len());
    let mut forced_kind = None;
    while cursor < end {
        let Some(token_end) = markdown_frontmatter_decorator_end(value, cursor, end) else {
            break;
        };
        if value.get(cursor) == Some(&b'!') {
            if let Some(kind) = markdown_frontmatter_tag_kind(&value[cursor..token_end]) {
                forced_kind = Some(kind);
            }
        }
        let next = token_end + leading_ascii_ws(&value[token_end..end]);
        if next >= end {
            break;
        }
        cursor = next;
    }
    forced_kind
}

fn markdown_frontmatter_tag_kind(token: &[u8]) -> Option<&'static str> {
    let suffix = if token.starts_with(b"!<") && token.ends_with(b">") && token.len() > 3 {
        &token[2..token.len() - 1]
    } else if token.starts_with(b"!!") && token.len() > 2 {
        &token[2..]
    } else if token.starts_with(b"!") && token.len() > 1 {
        &token[1..]
    } else {
        return None;
    };
    match suffix {
        b"str" | b"tag:yaml.org,2002:str" => Some("string"),
        b"int" | b"float" | b"tag:yaml.org,2002:int" | b"tag:yaml.org,2002:float" => Some("number"),
        b"bool" | b"tag:yaml.org,2002:bool" => Some("bool"),
        b"null" | b"tag:yaml.org,2002:null" => Some("null"),
        b"timestamp" | b"tag:yaml.org,2002:timestamp" => Some("timestamp"),
        b"binary" | b"tag:yaml.org,2002:binary" => Some("binary"),
        b"set" | b"tag:yaml.org,2002:set" => Some("set"),
        b"omap" | b"tag:yaml.org,2002:omap" => Some("omap"),
        _ => None,
    }
}

fn valid_markdown_frontmatter_alias(value: &[u8]) -> bool {
    value.len() > 1
        && value.first() == Some(&b'*')
        && value[1..]
            .iter()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
}

pub(crate) fn collect_json_field_spans(
    text: &str,
    value: &Value,
    start: usize,
    path: &str,
    spans: &mut Vec<crate::JsonFieldSpanRow>,
) -> AnfsResult<usize> {
    let start = skip_json_ws(text, start);
    let end = json_value_end(text, start)?;
    if path != "$" {
        spans.push((
            path.to_string(),
            start as i64,
            (end - start) as i64,
            json_value_kind(value).to_string(),
        ));
    }

    match value {
        Value::Object(map) => {
            for (key, child) in map {
                let mut cursor = find_json_object_key(text, start + 1, key)?;
                cursor = skip_json_ws(text, cursor);
                if text.as_bytes().get(cursor) != Some(&b':') {
                    return Err(AnfsError::StorageCorruption(format!(
                        "failed to locate ':' after JSON key {key}"
                    )));
                }
                let child_start = skip_json_ws(text, cursor + 1);
                let child_path = format!("{}.{}", path, json_path_segment(key));
                let child_end =
                    collect_json_field_spans(text, child, child_start, &child_path, spans)?;
                let _ = child_end;
            }
        }
        Value::Array(items) => {
            let mut cursor = start + 1;
            for (idx, child) in items.iter().enumerate() {
                let child_start = skip_json_ws(text, cursor);
                let child_path = format!("{path}[{idx}]");
                let child_end =
                    collect_json_field_spans(text, child, child_start, &child_path, spans)?;
                cursor = skip_json_ws(text, child_end);
                if text.as_bytes().get(cursor) == Some(&b',') {
                    cursor += 1;
                }
            }
        }
        _ => {}
    }
    Ok(end)
}

fn skip_json_ws(text: &str, mut offset: usize) -> usize {
    while let Some(byte) = text.as_bytes().get(offset) {
        if !byte.is_ascii_whitespace() {
            break;
        }
        offset += 1;
    }
    offset
}

fn json_value_end(text: &str, start: usize) -> AnfsResult<usize> {
    let bytes = text.as_bytes();
    match bytes.get(start).copied() {
        Some(b'"') => json_string_end(text, start),
        Some(b'{') => json_balanced_end(text, start, b'{', b'}'),
        Some(b'[') => json_balanced_end(text, start, b'[', b']'),
        Some(_) => {
            let mut end = start;
            while let Some(byte) = bytes.get(end) {
                if *byte == b',' || *byte == b'}' || *byte == b']' || byte.is_ascii_whitespace() {
                    break;
                }
                end += 1;
            }
            Ok(end)
        }
        None => Err(AnfsError::StorageCorruption(
            "failed to locate JSON value start".to_string(),
        )),
    }
}

fn json_string_end(text: &str, start: usize) -> AnfsResult<usize> {
    let bytes = text.as_bytes();
    let mut cursor = start + 1;
    let mut escaped = false;
    while let Some(byte) = bytes.get(cursor) {
        if escaped {
            escaped = false;
        } else if *byte == b'\\' {
            escaped = true;
        } else if *byte == b'"' {
            return Ok(cursor + 1);
        }
        cursor += 1;
    }
    Err(AnfsError::StorageCorruption(
        "unterminated JSON string".to_string(),
    ))
}

fn json_balanced_end(text: &str, start: usize, open: u8, close: u8) -> AnfsResult<usize> {
    let bytes = text.as_bytes();
    let mut cursor = start;
    let mut depth = 0;
    while let Some(byte) = bytes.get(cursor) {
        match *byte {
            b'"' => {
                cursor = json_string_end(text, cursor)?;
                continue;
            }
            value if value == open => depth += 1,
            value if value == close => {
                depth -= 1;
                if depth == 0 {
                    return Ok(cursor + 1);
                }
            }
            _ => {}
        }
        cursor += 1;
    }
    Err(AnfsError::StorageCorruption(
        "unterminated JSON container".to_string(),
    ))
}

fn find_json_object_key(text: &str, mut cursor: usize, key: &str) -> AnfsResult<usize> {
    let encoded_key = serde_json::to_string(key).map_err(|err| {
        AnfsError::StorageCorruption(format!("failed to encode JSON key {key}: {err}"))
    })?;
    let bytes = text.as_bytes();
    let mut depth = 0;
    loop {
        cursor = skip_json_ws(text, cursor);
        match bytes.get(cursor).copied() {
            Some(b'"') => {
                if depth == 0 && text[cursor..].starts_with(&encoded_key) {
                    let after_key = skip_json_ws(text, cursor + encoded_key.len());
                    if bytes.get(after_key) == Some(&b':') {
                        return Ok(cursor + encoded_key.len());
                    }
                }
                cursor = json_string_end(text, cursor)?;
                continue;
            }
            Some(b'{') | Some(b'[') => {
                depth += 1;
            }
            Some(b'}') if depth == 0 => {
                break;
            }
            Some(b'}') | Some(b']') => {
                depth -= 1;
            }
            Some(_) => {}
            None => break,
        }
        cursor += 1;
    }
    Err(AnfsError::StorageCorruption(format!(
        "failed to locate JSON key {key}"
    )))
}

fn json_path_segment(key: &str) -> String {
    if key
        .chars()
        .all(|ch| ch.is_ascii_alphanumeric() || ch == '_')
        && !key.is_empty()
    {
        key.to_string()
    } else {
        format!(
            "[{}]",
            serde_json::to_string(key).unwrap_or_else(|_| "\"\"".to_string())
        )
    }
}

fn json_value_kind(value: &Value) -> &'static str {
    match value {
        Value::Null => "null",
        Value::Bool(_) => "bool",
        Value::Number(_) => "number",
        Value::String(_) => "string",
        Value::Array(_) => "array",
        Value::Object(_) => "object",
    }
}
