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


def test_context_pack_is_relevant_and_token_accurate(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    src = (
        b"pub fn target() -> i64 { 1 }\n\n"
        b"pub fn caller_one() -> i64 { target() }\n\n"
        b"pub fn unrelated() -> i64 { 99 }\n"
    )
    node = ws.write("lib.rs", src, [])
    fs.index_node_fragments(node, "tree-sitter-rust")

    items, tokens = fs.context_pack("target", 10_000)
    names = {item[2] for item in items}
    assert "target" in names  # the definition
    assert "caller_one" in names  # its caller is pulled in
    assert "unrelated" not in names  # irrelevant code is not

    # token estimate equals the packed source size (ceil bytes/4)
    total = sum((len(item[6]) + 3) // 4 for item in items)
    assert tokens == total
    assert tokens <= 10_000


def test_context_pack_respects_budget(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    src = b"".join(b"pub fn f%d() -> i64 { shared() }\n\n" % i for i in range(20))
    src += b"pub fn shared() -> i64 { 0 }\n"
    node = ws.write("many.rs", src, [])
    fs.index_node_fragments(node, "tree-sitter-rust")

    big, big_tokens = fs.context_pack("shared", 100_000)
    small, small_tokens = fs.context_pack("shared", 30)
    assert len(small) <= len(big)
    assert small_tokens <= big_tokens


def test_context_pack_records_audit_event(anfs_paths, anfs_engine):
    import sqlite3

    db_path, _ = anfs_paths
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    b_node = ws.write("b.rs", b"pub fn helper() -> i64 { 1 }\n", [])
    a_node = ws.write("a.rs", b"pub fn run() -> i64 { helper() }\n", [])
    fs.index_node_fragments(a_node, "tree-sitter-rust")
    fs.index_node_fragments(b_node, "tree-sitter-rust")

    # without identity: pure read, no event recorded
    con = sqlite3.connect(db_path)
    before = con.execute(
        "SELECT COUNT(*) FROM events WHERE kind='code_context_query'"
    ).fetchone()[0]
    fs.context_pack("helper", 10_000)
    after_noid = con.execute(
        "SELECT COUNT(*) FROM events WHERE kind='code_context_query'"
    ).fetchone()[0]
    assert after_noid == before  # no identity -> no audit event

    # with identity: a code_context_query event + input edges to packed nodes
    items, _tokens = fs.context_pack("helper", 10_000, "coder_agent", "run-1")
    ev = con.execute(
        "SELECT event_id, agent_id, payload_json FROM events "
        "WHERE kind='code_context_query' ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    assert ev is not None
    event_id, agent_id, payload = ev
    assert agent_id == "coder_agent"
    assert '"seed":"helper"' in payload.replace(" ", "")
    edge_nodes = {
        r[0]
        for r in con.execute(
            "SELECT node_id FROM event_edges WHERE event_id=? AND direction='input'",
            (event_id,),
        ).fetchall()
    }
    packed_nodes = {it[0] for it in items}
    assert edge_nodes == packed_nodes  # input edges == nodes the model saw
    con.close()

    assert fs.verify_integrity() == []  # new event kind keeps integrity clean


def test_fragments_do_not_break_integrity(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:writer", "writer_agent")
    md = ws.write("note.md", b"# H\n\ntext\n", [])
    fs.index_node_fragments(md, "span-markdown")
    # rust indexing also populates fragment_edges; integrity must cover both.
    rs = ws.write("lib.rs", b"pub fn a() { b() }\npub fn b() {}\n", [])
    fs.index_node_fragments(rs, "tree-sitter-rust")
    assert fs.verify_integrity() == []


def _index_chain(fs):
    """top() -> mid() -> leaf(), one symbol per file."""
    ws = fs.open_workspace("ws:coder", "coder_agent")
    for path, src in [
        ("leaf.rs", b"pub fn leaf() -> i64 { 1 }\n"),
        ("mid.rs", b"pub fn mid() -> i64 { leaf() }\n"),
        ("top.rs", b"pub fn top() -> i64 { mid() }\n"),
    ]:
        fs.index_node_fragments(ws.write(path, src, []), "tree-sitter-rust")


def test_call_graph_callees_walks_the_chain(anfs_engine):
    fs = anfs_engine
    _index_chain(fs)
    nodes, edges, est = fs.call_graph("top", "callees", 2, 4, 2000)
    # Reached the whole execution flow, each at its hop distance.
    assert {(n[2], n[6]) for n in nodes} == {("top", 0), ("mid", 1), ("leaf", 2)}
    resolved = {(e[1], e[2]) for e in edges if e[3] is not None}
    assert resolved == {("top", "mid"), ("mid", "leaf")}
    assert est > 0


def test_call_graph_callers_walks_blast_radius_upward(anfs_engine):
    fs = anfs_engine
    _index_chain(fs)
    nodes, _edges, _est = fs.call_graph("leaf", "callers", 2, 4, 2000)
    assert {(n[2], n[6]) for n in nodes} == {("leaf", 0), ("mid", 1), ("top", 2)}


def test_call_graph_depth_bounds_traversal(anfs_engine):
    fs = anfs_engine
    _index_chain(fs)
    nodes, _edges, _est = fs.call_graph("top", "callees", 1, 4, 2000)
    assert {n[2] for n in nodes} == {"top", "mid"}  # leaf is two hops away, excluded


def test_call_graph_fanout_guard_does_not_explode(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "coder_agent")
    # `common` is defined 5 times (> max_fanout); user() calls it once. Bodies
    # must differ — identical content is one content-addressed fragment, not five.
    for i in range(5):
        src = f"pub fn common() -> i64 {{ {i} }}\n".encode()
        fs.index_node_fragments(ws.write(f"d{i}.rs", src, []), "tree-sitter-rust")
    fs.index_node_fragments(ws.write("u.rs", b"pub fn user() { common() }\n", []),
                            "tree-sitter-rust")
    nodes, edges, _est = fs.call_graph("user", "callees", 3, 4, 2000)
    # `common` resolves to 5 > max_fanout(4): edge recorded, but not recursed into.
    assert any(e[2] == "common" and e[3] is None for e in edges)
    assert "common" not in {n[2] for n in nodes}


def test_call_graph_records_audit_event(anfs_engine):
    fs = anfs_engine
    _index_chain(fs)
    fs.call_graph("top", "callees", 2, 4, 2000, agent_id="walker", tool_call_id="tc_cg")
    assert len(fs.events(kind="code_call_graph")) == 1
    assert fs.verify_integrity() == []
