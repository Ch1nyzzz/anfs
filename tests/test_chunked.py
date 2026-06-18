"""Canonical chunked storage: large files as content-addressed chunk blobs +
a manifest node, so identical chunks dedupe and GC never collects them (task #6)."""

import sqlite3


def _blob_count(db_path):
    con = sqlite3.connect(db_path)
    n = con.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]
    con.close()
    return n


def test_chunked_roundtrip(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "agent")
    content = b"A" * 1000 + b"B" * 1000 + b"C" * 537
    node = ws.write_chunked("big.bin", content, 1024)
    assert bytes(fs.read_chunked_node(node)) == content


def test_chunked_dedupes_shared_chunks(anfs_paths, anfs_engine):
    db_path, _ = anfs_paths
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "agent")
    cs = 1024
    p, q, r = b"P" * cs, b"Q" * cs, b"R" * cs

    # Two files sharing chunk P. (Count once at the end: opening an external
    # sqlite connection between engine writes is not a supported access pattern.)
    ws.write_chunked("a.bin", p + q, cs)  # chunks P,Q + manifest
    ws.write_chunked("b.bin", p + r, cs)  # shares P; adds R + manifest

    # Distinct blobs = P, Q, R (3 chunks, P shared) + 2 manifests = 5, not 6.
    assert _blob_count(db_path) == 5


def test_gc_never_collects_chunk_blobs(anfs_paths, anfs_engine):
    db_path, _ = anfs_paths
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "agent")
    content = b"Z" * 4096
    node = ws.write_chunked("big.bin", content, 1024)

    con = sqlite3.connect(db_path)
    manifest_hash = con.execute(
        "SELECT blob_hash FROM nodes WHERE node_id = ?", (node,)
    ).fetchone()[0]
    con.close()

    # Even excluding workspace drafts as roots, chunk blobs must be protected.
    candidates = {h for (h, _s, _k) in fs.gc_candidates(False, None, None)}
    # the chunk blobs (not the manifest) are referenced only inside the manifest
    chunk_hashes = {
        h
        for (h, _s, _k) in fs.gc_candidates(True, None, None)  # workspaces as roots
    }
    # With chunk protection, none of the chunk blobs are ever collectable:
    con = sqlite3.connect(db_path)
    all_blobs = {r[0] for r in con.execute("SELECT hash FROM blobs").fetchall()}
    con.close()
    chunk_only = all_blobs - {manifest_hash}
    assert chunk_only.isdisjoint(candidates)
    assert chunk_only.isdisjoint(chunk_hashes)


def test_chunked_keeps_integrity_clean(anfs_engine):
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "agent")
    ws.write_chunked("big.bin", b"Q" * 5000, 1024)
    assert fs.verify_integrity() == []
