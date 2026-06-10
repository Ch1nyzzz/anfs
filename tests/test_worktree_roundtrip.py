"""C4 compatibility (mechanism): worktree round-trip is byte-faithful.

Deterministic, no-LLM core of the compatibility claim: an arbitrary edit
battery applied to a materialized worktree (modify, add, add-nested,
add-binary, delete) must commit back and re-materialize byte-identically,
with integrity clean. The real coding-agent version is
benchmarks/coding_agent_compat_benchmark.py.
"""

import os
import tempfile

import anfs_core

BASE = "resource:repo/main@v1"


def _read_tree(root):
    tree = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if rel == ".anfs-worktree-manifest.json":
                continue  # reserved adapter metadata, not a workspace file
            with open(full, "rb") as h:
                tree[rel] = h.read()
    return tree


def test_worktree_roundtrip_is_byte_faithful_across_edit_battery():
    base = {
        "src/mod_000.py": b"# keep\nx = 1\n",
        "src/mod_001.py": b"# delete me\ny = 2\n",
        "README.md": b"# repo\n",
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        fs = anfs_core.AnfsEngine(
            os.path.join(tmpdir, "anfs.db"), os.path.join(tmpdir, "objs")
        )
        importer = fs.open_workspace("ws:importer", "importer_agent")
        for name, content in base.items():
            importer.write(name, content, [])
            importer.publish(name, f"{BASE}/{name}", "resource")

        fs.checkout("ws:coder", "coder_agent", BASE)
        work = os.path.join(tmpdir, "work")
        fs.materialize_workspace("ws:coder", work)

        # Edit battery with ordinary filesystem operations.
        with open(os.path.join(work, "src/mod_000.py"), "ab") as h:
            h.write(b"x += 1\n")  # modify
        os.makedirs(os.path.join(work, "src/new"), exist_ok=True)
        with open(os.path.join(work, "src/new/added.py"), "wb") as h:
            h.write(b"# added\nz = 3\n")  # add nested
        with open(os.path.join(work, "data.bin"), "wb") as h:
            h.write(bytes(range(256)))  # add binary (non-utf8)
        os.remove(os.path.join(work, "src/mod_001.py"))  # delete

        fs.commit_worktree(
            "ws:coder", work, "coder_agent", require_manifest_match=True
        )

        fresh = os.path.join(tmpdir, "fresh")
        fs.materialize_workspace("ws:coder", fresh)
        got = _read_tree(fresh)

        expected = {
            "src/mod_000.py": b"# keep\nx = 1\nx += 1\n",
            "README.md": b"# repo\n",
            "src/new/added.py": b"# added\nz = 3\n",
            "data.bin": bytes(range(256)),
        }
        assert got == expected
        assert fs.verify_integrity() == []
