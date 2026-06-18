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


def test_fragments_do_not_break_integrity(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:writer", "writer_agent")
    node_id = ws.write("note.md", b"# H\n\ntext\n", [])
    fs.index_node_fragments(node_id, "span-markdown")
    assert fs.verify_integrity() == []
