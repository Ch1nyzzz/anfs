"""GC must never collect blobs a recorded ref-view checkpoint still needs,
otherwise the replay proof would stop verifying (task #9)."""

import sqlite3


def test_gc_protects_ref_view_checkpoint_nodes(anfs_paths, anfs_engine):
    db_path, _ = anfs_paths
    fs = anfs_engine
    ws = fs.open_workspace("ws:coder", "agent")
    node = ws.write("draft.txt", b"checkpoint me", [])

    con = sqlite3.connect(db_path)
    blob_hash = con.execute(
        "SELECT blob_hash FROM nodes WHERE node_id = ?", (node,)
    ).fetchone()[0]
    con.close()

    # Control: an orphan draft (not a root when workspaces are excluded) is a
    # GC candidate.
    before = {h for (h, _size, _kind) in fs.gc_candidates(False, None, None)}
    assert blob_hash in before

    # Record a replay checkpoint covering this workspace prefix.
    checkpoint = fs.create_ref_view_checkpoint(None, "ws:coder", False, "agent")
    checkpoint_id = checkpoint[0]

    # Now the same query must NOT offer that blob — the checkpoint protects it.
    after = {h for (h, _size, _kind) in fs.gc_candidates(False, None, None)}
    assert blob_hash not in after

    # And the checkpoint still verifies.
    assert fs.verify_ref_view_checkpoint(checkpoint_id) is not None
    assert fs.verify_integrity() == []
