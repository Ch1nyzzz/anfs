"""Unified fragment pipeline: existing span parsers feed the generic
`fragments` table; agents read an outline instead of whole files."""


def test_markdown_fragments_indexed_and_outlined(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:writer", "writer_agent")
    content = (
        b"# Title\n\n"
        b"Some body paragraph mentioning renewal.\n\n"
        b"## Section Two\n\n"
        b"More text.\n"
    )
    node_id = ws.write("note.md", content, [])

    frag_count, edge_count = fs.index_node_fragments(node_id, "span-markdown")
    assert frag_count > 0
    assert edge_count == 0

    frags = fs.node_fragments(node_id)
    assert len(frags) == frag_count
    # outline is ordered by byte position
    starts = [row[4] for row in frags]
    assert starts == sorted(starts)
    # every fragment is content-addressed
    assert all(row[0].startswith("frag:") for row in frags)
    # byte ranges are well formed and inside the blob
    for _fid, _kind, _name, _path, start, end in frags:
        assert 0 <= start <= end <= len(content)


def test_index_is_incremental_skip(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:writer", "writer_agent")
    node_id = ws.write("a.md", b"# A\n\nbody\n", [])
    first = fs.index_node_fragments(node_id, "span-markdown")
    # same blob + parser version -> cached counts, no rework
    second = fs.index_node_fragments(node_id, "span-markdown")
    assert first == second
    assert fs.node_fragments(node_id)


def test_json_fragments_named_by_field(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    node_id = ws.write("data.json", b'{"name": "x", "items": [1, 2]}', [])
    frag_count, _ = fs.index_node_fragments(node_id, "span-json")
    assert frag_count > 0
    names = {row[2] for row in fs.node_fragments(node_id)}
    assert "name" in names or "items" in names


def test_unknown_parser_rejected(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    node_id = ws.write("x.txt", b"hello", [])
    try:
        fs.index_node_fragments(node_id, "span-rust")
    except Exception as exc:  # noqa: BLE001
        assert "span-rust" in str(exc) or "unknown" in str(exc).lower()
    else:
        raise AssertionError("expected unknown-parser rejection")


def test_rust_fragments_extract_symbols(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    src = (
        b"use std::fmt;\n\n"
        b"pub struct Point { x: i64, y: i64 }\n\n"
        b"pub fn add(a: i64, b: i64) -> i64 { a + b }\n\n"
        b"impl Point {\n"
        b"    pub fn norm(&self) -> i64 { self.x + self.y }\n"
        b"}\n"
    )
    node = ws.write("lib.rs", src, [])
    frag_count, edge_count = fs.index_node_fragments(node, "tree-sitter-rust")
    assert frag_count > 0
    assert edge_count == 0

    by_kind = {}
    for _fid, kind, name, _path, start, end in fs.node_fragments(node):
        by_kind.setdefault(kind, set()).add(name)
        assert src[start:end]  # span slices to real bytes

    assert "add" in by_kind.get("function", set())
    assert "norm" in by_kind.get("function", set())  # method inside impl block
    assert "Point" in by_kind.get("struct", set())
    assert by_kind.get("import")  # use std::fmt;


def test_cross_file_callers_resolved(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    # helper defined in b.rs; run() in a.rs calls it twice.
    b_node = ws.write("b.rs", b"pub fn helper() -> i64 { 42 }\n", [])
    a_node = ws.write("a.rs", b"pub fn run() -> i64 { helper() + helper() }\n", [])
    _frags, edges = fs.index_node_fragments(a_node, "tree-sitter-rust")
    fs.index_node_fragments(b_node, "tree-sitter-rust")
    assert edges == 2  # two call sites recorded as name references

    callers = fs.fragment_callers("helper")
    assert len(callers) == 2  # exact candidate set, no probability
    assert all(row[2] == "run" for row in callers)  # src_name
    assert all(row[1] == "function" for row in callers)  # src_kind
    assert {row[3] for row in callers} == {a_node}  # resolved to a.rs's node


def test_callers_empty_for_unknown_name(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    node = ws.write("c.rs", b"pub fn lone() {}\n", [])
    fs.index_node_fragments(node, "tree-sitter-rust")
    assert fs.fragment_callers("does_not_exist") == []


def test_fragments_do_not_break_integrity(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:writer", "writer_agent")
    node_id = ws.write("note.md", b"# H\n\ntext\n", [])
    fs.index_node_fragments(node_id, "span-markdown")
    assert fs.verify_integrity() == []
