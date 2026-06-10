#!/usr/bin/env python3
import argparse
import fcntl
import hashlib
import json
import multiprocessing
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import anfs_core


BASE_REF = "resource:repo/main@v1"
NEEDLE = "needle_agent_memory_benchmark"


def millis(start):
    return (time.perf_counter() - start) * 1000.0


def per_second(count, elapsed_ms):
    if elapsed_ms <= 0:
        return 0.0
    return count / (elapsed_ms / 1000.0)


def matching_file_count(files_per_agent, needle_every):
    return sum(1 for file_idx in range(files_per_agent) if file_idx % needle_every == 0)


def benchmark_content(worker_idx, file_idx, payload_bytes, needle_every):
    lines = [
        f"agent={worker_idx}",
        f"file={file_idx}",
    ]
    if file_idx % needle_every == 0:
        lines.append(f"{NEEDLE} agent={worker_idx} file={file_idx}")
    else:
        lines.append(f"plain agent={worker_idx} file={file_idx}")
    content = ("\n".join(lines) + "\n").encode("utf-8")
    if payload_bytes > len(content):
        padding = b"x" * (payload_bytes - len(content))
        content += padding
    return content


def large_content(size):
    pattern = b"0123456789abcdef"
    return (pattern * ((size + len(pattern) - 1) // len(pattern)))[:size]


def expected_chunk_count(size, chunk_size):
    if size <= 0:
        return 0
    return (size + chunk_size - 1) // chunk_size


def write_file(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def iter_files(root):
    if not root.exists():
        return
    for path in root.rglob("*"):
        if path.is_file():
            yield path


def file_count(root, exclude_names=None):
    exclude_names = set(exclude_names or [])
    return sum(
        1
        for path in iter_files(Path(root))
        if path.name not in exclude_names
    )


def anfs_worker(
    db_path,
    objs_dir,
    workspace,
    agent_id,
    worker_idx,
    files_per_agent,
    payload_bytes,
    needle_every,
    queue,
):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        ws = fs.open_workspace(workspace, agent_id, run_id=f"run:{agent_id}")
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            path = f"agents/{worker_idx}/file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            ws.write(path, content, [], tool_call_id=f"write:{worker_idx}:{file_idx}")
            if bytes(ws.read(path, tool_call_id=f"read:{worker_idx}:{file_idx}")) == content:
                read_ok += 1
        grep_rows = ws.grep(
            NEEDLE,
            f"agents/{worker_idx}",
            tool_call_id=f"grep:{worker_idx}",
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": len(grep_rows),
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def native_record_event(
    audit_dir,
    kind,
    workspace=None,
    payload=None,
    input_edges=0,
    output_edges=0,
    ref_events=0,
):
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    lock_path = audit_dir / "audit.lock"
    seq_path = audit_dir / "seq.txt"
    events_path = audit_dir / "events.jsonl"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            if seq_path.exists():
                seq = int(seq_path.read_text(encoding="utf-8"))
            else:
                seq = 0
            seq += 1
            seq_path.write_text(str(seq), encoding="utf-8")
            event = {
                "seq": seq,
                "event_id": f"event:native:{seq}",
                "kind": kind,
                "workspace": workspace,
                "payload": payload or {},
                "input_edges": input_edges,
                "output_edges": output_edges,
                "ref_events": ref_events,
                "created_ns": time.time_ns(),
            }
            with events_path.open("a", encoding="utf-8") as events_file:
                events_file.write(json.dumps(event, sort_keys=True) + "\n")
            return event["event_id"]
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def native_audit_counts(audit_dir):
    events_path = Path(audit_dir) / "events.jsonl"
    events = []
    if events_path.exists():
        with events_path.open("r", encoding="utf-8") as file:
            events = [json.loads(line) for line in file if line.strip()]
    seqs = [event["seq"] for event in events]
    event_kinds = {}
    for event in events:
        event_kinds[event["kind"]] = event_kinds.get(event["kind"], 0) + 1
    return {
        "seq_count": len(seqs),
        "max_seq": max(seqs, default=0),
        "distinct_seq": len(set(seqs)),
        "event_count": len(events),
        "edge_count": sum(
            event.get("input_edges", 0) + event.get("output_edges", 0)
            for event in events
        ),
        "ref_event_count": sum(event.get("ref_events", 0) for event in events),
        "event_kinds": event_kinds,
    }


def markdownfs_record_event(
    audit_dir,
    kind,
    workspace=None,
    payload=None,
    input_edges=0,
    output_edges=0,
    ref_events=0,
):
    audit_dir = Path(audit_dir)
    events_dir = audit_dir / "events"
    events_dir.mkdir(parents=True, exist_ok=True)
    lock_path = audit_dir / "audit.lock"
    seq_path = audit_dir / "seq.txt"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            seq = int(seq_path.read_text(encoding="utf-8")) if seq_path.exists() else 0
            seq += 1
            seq_path.write_text(str(seq), encoding="utf-8")
            event_id = f"event:markdownfs:{seq}"
            event_path = events_dir / f"{seq:08d}-{kind}.md"
            body = [
                "---",
                f"seq: {seq}",
                f"event_id: {event_id}",
                f"kind: {kind}",
                f"workspace: {workspace or ''}",
                f"input_edges: {input_edges}",
                f"output_edges: {output_edges}",
                f"ref_events: {ref_events}",
                f"created_ns: {time.time_ns()}",
                "---",
                "",
                "```json",
                json.dumps(payload or {}, sort_keys=True),
                "```",
                "",
            ]
            event_path.write_text("\n".join(body), encoding="utf-8")
            return event_id
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def markdownfs_audit_counts(audit_dir):
    events_dir = Path(audit_dir) / "events"
    events = []
    if events_dir.exists():
        for event_path in sorted(events_dir.glob("*.md")):
            metadata = {}
            in_frontmatter = False
            for line in event_path.read_text(encoding="utf-8").splitlines():
                if line == "---":
                    if not in_frontmatter:
                        in_frontmatter = True
                        continue
                    break
                if in_frontmatter and ":" in line:
                    key, value = line.split(":", 1)
                    metadata[key.strip()] = value.strip()
            if metadata:
                events.append(metadata)
    seqs = [int(event["seq"]) for event in events]
    event_kinds = {}
    for event in events:
        event_kinds[event["kind"]] = event_kinds.get(event["kind"], 0) + 1
    return {
        "seq_count": len(seqs),
        "max_seq": max(seqs, default=0),
        "distinct_seq": len(set(seqs)),
        "event_count": len(events),
        "edge_count": sum(
            int(event.get("input_edges", 0)) + int(event.get("output_edges", 0))
            for event in events
        ),
        "ref_event_count": sum(int(event.get("ref_events", 0)) for event in events),
        "event_kinds": event_kinds,
    }


def rag_jsonl_record_event(
    audit_dir,
    kind,
    workspace=None,
    payload=None,
    input_edges=0,
    output_edges=0,
    ref_events=0,
):
    audit_dir = Path(audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    lock_path = audit_dir / "audit.lock"
    seq_path = audit_dir / "seq.txt"
    events_path = audit_dir / "events.jsonl"
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            seq = int(seq_path.read_text(encoding="utf-8")) if seq_path.exists() else 0
            seq += 1
            seq_path.write_text(str(seq), encoding="utf-8")
            event = {
                "seq": seq,
                "event_id": f"event:rag-jsonl:{seq}",
                "kind": kind,
                "workspace": workspace,
                "payload": payload or {},
                "input_edges": input_edges,
                "output_edges": output_edges,
                "ref_events": ref_events,
                "created_ns": time.time_ns(),
            }
            with events_path.open("a", encoding="utf-8") as events_file:
                events_file.write(json.dumps(event, sort_keys=True) + "\n")
            return event["event_id"]
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def rag_jsonl_tokens(text):
    tokens = []
    token = []
    for char in text.lower():
        if char.isalnum() or char == "_":
            token.append(char)
        elif token:
            tokens.append("".join(token))
            token = []
    if token:
        tokens.append("".join(token))
    return sorted(set(tokens))


def rag_jsonl_index_document(index_dir, workspace, path, content):
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    lock_path = index_dir / "index.lock"
    docs_path = index_dir / "documents.jsonl"
    text = content.decode("utf-8", errors="replace")
    record = {
        "workspace": workspace,
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "tokens": rag_jsonl_tokens(text),
        "text": text,
        "indexed_ns": time.time_ns(),
    }
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with docs_path.open("a", encoding="utf-8") as docs_file:
                docs_file.write(json.dumps(record, sort_keys=True) + "\n")
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def rag_jsonl_search(index_dir, workspace, prefix, query):
    docs_path = Path(index_dir) / "documents.jsonl"
    if not docs_path.exists():
        return []
    query_tokens = set(rag_jsonl_tokens(query))
    latest = {}
    with docs_path.open("r", encoding="utf-8") as docs_file:
        for line in docs_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["workspace"] != workspace or not record["path"].startswith(prefix):
                continue
            latest[(record["workspace"], record["path"])] = record
    rows = []
    for record in latest.values():
        text = record["text"]
        tokens = set(record["tokens"])
        lexical_hit = query in text
        token_overlap = len(query_tokens & tokens)
        if lexical_hit or token_overlap:
            score = (1000 if lexical_hit else 0) + token_overlap
            rows.append((score, record["path"], record["sha256"]))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return rows


def rag_jsonl_index_counts(index_dir):
    docs_path = Path(index_dir) / "documents.jsonl"
    if not docs_path.exists():
        return {"document_rows": 0, "unique_documents": 0, "token_rows": 0}
    rows = 0
    latest = {}
    with docs_path.open("r", encoding="utf-8") as docs_file:
        for line in docs_file:
            if not line.strip():
                continue
            rows += 1
            record = json.loads(line)
            latest[(record["workspace"], record["path"])] = record
    return {
        "document_rows": rows,
        "unique_documents": len(latest),
        "token_rows": sum(len(record["tokens"]) for record in latest.values()),
    }


def external_vector_tokens(text):
    return rag_jsonl_tokens(text)


def external_vector_embedding(text, dimensions=64):
    vector = [0.0] * dimensions
    for token in external_vector_tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = sum(value * value for value in vector) ** 0.5
    if norm:
        vector = [value / norm for value in vector]
    return vector


def external_vector_index_document(index_dir, workspace, path, content):
    index_dir = Path(index_dir)
    index_dir.mkdir(parents=True, exist_ok=True)
    lock_path = index_dir / "index.lock"
    docs_path = index_dir / "vectors.jsonl"
    text = content.decode("utf-8", errors="replace")
    record = {
        "workspace": workspace,
        "path": str(path),
        "sha256": hashlib.sha256(content).hexdigest(),
        "dimensions": 64,
        "embedding": external_vector_embedding(text),
        "text": text,
        "indexed_ns": time.time_ns(),
    }
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            with docs_path.open("a", encoding="utf-8") as docs_file:
                docs_file.write(json.dumps(record, sort_keys=True) + "\n")
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def external_vector_search(index_dir, workspace, prefix, query):
    docs_path = Path(index_dir) / "vectors.jsonl"
    if not docs_path.exists():
        return []
    query_vector = external_vector_embedding(query)
    latest = {}
    with docs_path.open("r", encoding="utf-8") as docs_file:
        for line in docs_file:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["workspace"] != workspace or not record["path"].startswith(prefix):
                continue
            latest[(record["workspace"], record["path"])] = record
    rows = []
    for record in latest.values():
        score = sum(a * b for a, b in zip(query_vector, record["embedding"]))
        if score > 0:
            rows.append((score, record["path"], record["sha256"]))
    rows.sort(key=lambda row: (-row[0], row[1]))
    return rows


def external_vector_index_counts(index_dir):
    docs_path = Path(index_dir) / "vectors.jsonl"
    if not docs_path.exists():
        return {"vector_rows": 0, "unique_documents": 0, "dimensions": 64}
    rows = 0
    latest = {}
    dimensions = 64
    with docs_path.open("r", encoding="utf-8") as docs_file:
        for line in docs_file:
            if not line.strip():
                continue
            rows += 1
            record = json.loads(line)
            latest[(record["workspace"], record["path"])] = record
            dimensions = record.get("dimensions", dimensions)
    return {
        "vector_rows": rows,
        "unique_documents": len(latest),
        "dimensions": dimensions,
    }


def sqlite_memory_connect(db_path):
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            workspace TEXT NOT NULL,
            path TEXT NOT NULL,
            content BLOB NOT NULL,
            updated_ns INTEGER NOT NULL,
            PRIMARY KEY (workspace, path)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lineage (
            evidence_ref TEXT PRIMARY KEY,
            target_ref TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS audit_events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL,
            workspace TEXT,
            payload_json TEXT NOT NULL,
            input_edges INTEGER NOT NULL DEFAULT 0,
            output_edges INTEGER NOT NULL DEFAULT 0,
            ref_events INTEGER NOT NULL DEFAULT 0,
            created_ns INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS working_set_cache (
            cache_key TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            file_count INTEGER NOT NULL,
            materialized_ns INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS large_files (
            name TEXT PRIMARY KEY,
            content BLOB NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS large_file_chunks (
            name TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            offset INTEGER NOT NULL,
            size INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            PRIMARY KEY (name, chunk_index)
        )
        """
    )
    conn.commit()
    return conn


def sqlite_memory_record_event(
    conn,
    kind,
    workspace=None,
    payload=None,
    input_edges=0,
    output_edges=0,
    ref_events=0,
):
    conn.execute(
        """
        INSERT INTO audit_events
        (kind, workspace, payload_json, input_edges, output_edges, ref_events, created_ns)
        VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
        """,
        (
            kind,
            workspace,
            json.dumps(payload or {}, sort_keys=True),
            input_edges,
            output_edges,
            ref_events,
            time.time_ns(),
        ),
    )


def sqlite_memory_counts(db_path):
    with sqlite_memory_connect(db_path) as conn:
        seq_count, max_seq, distinct_seq = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0), COUNT(DISTINCT seq) FROM audit_events"
        ).fetchone()
        event_kinds = dict(
            conn.execute(
                "SELECT kind, COUNT(*) FROM audit_events GROUP BY kind ORDER BY kind"
            ).fetchall()
        )
        edge_count = conn.execute(
            "SELECT COALESCE(SUM(input_edges + output_edges), 0) FROM audit_events"
        ).fetchone()[0]
        ref_event_count = conn.execute(
            "SELECT COALESCE(SUM(ref_events), 0) FROM audit_events"
        ).fetchone()[0]
    return {
        "seq_count": seq_count,
        "max_seq": max_seq,
        "distinct_seq": distinct_seq,
        "event_count": seq_count,
        "edge_count": edge_count,
        "ref_event_count": ref_event_count,
        "event_kinds": event_kinds,
    }


def sqlite_memory_worker(
    db_path,
    worker_idx,
    files_per_agent,
    payload_bytes,
    needle_every,
    queue,
):
    try:
        workspace = f"ws:agent-{worker_idx}"
        conn = sqlite_memory_connect(db_path)
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            path = f"agents/{worker_idx}/file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            with conn:
                conn.execute(
                    """
                    INSERT INTO files (workspace, path, content, updated_ns)
                    VALUES (?1, ?2, ?3, ?4)
                    ON CONFLICT(workspace, path) DO UPDATE SET
                        content = excluded.content,
                        updated_ns = excluded.updated_ns
                    """,
                    (workspace, path, content, time.time_ns()),
                )
                sqlite_memory_record_event(
                    conn,
                    "write",
                    workspace=workspace,
                    payload={"path": path},
                    output_edges=1,
                    ref_events=1,
                )
            row = conn.execute(
                "SELECT content FROM files WHERE workspace = ?1 AND path = ?2",
                (workspace, path),
            ).fetchone()
            if row and row[0] == content:
                read_ok += 1
            with conn:
                sqlite_memory_record_event(
                    conn,
                    "read_ref",
                    workspace=workspace,
                    payload={"path": path},
                    input_edges=1,
                )

        grep_results = 0
        prefix = f"agents/{worker_idx}/%"
        for (content,) in conn.execute(
            "SELECT content FROM files WHERE workspace = ?1 AND path LIKE ?2",
            (workspace, prefix),
        ):
            if NEEDLE in content.decode("utf-8", errors="replace"):
                grep_results += 1
        with conn:
            sqlite_memory_record_event(
                conn,
                "search",
                workspace=workspace,
                payload={"query": NEEDLE, "result_count": grep_results},
                input_edges=grep_results,
            )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": grep_results,
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def native_worker(root, audit_dir, worker_idx, files_per_agent, payload_bytes, needle_every, queue):
    try:
        workspace = f"ws:agent-{worker_idx}"
        workspace_dir = Path(root) / "workspaces" / workspace
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            rel_path = Path("agents") / str(worker_idx) / f"file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            write_file(workspace_dir / rel_path, content)
            native_record_event(
                audit_dir,
                "write",
                workspace=workspace,
                payload={"path": str(rel_path)},
                output_edges=1,
                ref_events=1,
            )
            if (workspace_dir / rel_path).read_bytes() == content:
                read_ok += 1
            native_record_event(
                audit_dir,
                "read_ref",
                workspace=workspace,
                payload={"path": str(rel_path)},
                input_edges=1,
            )

        grep_results = 0
        grep_root = workspace_dir / "agents" / str(worker_idx)
        for path in iter_files(grep_root):
            for line in path.read_text(encoding="utf-8").splitlines():
                if NEEDLE in line:
                    grep_results += 1
        native_record_event(
            audit_dir,
            "search",
            workspace=workspace,
            payload={"query": NEEDLE, "result_count": grep_results},
            input_edges=grep_results,
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": grep_results,
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def markdownfs_worker(root, audit_dir, worker_idx, files_per_agent, payload_bytes, needle_every, queue):
    try:
        workspace = f"ws:agent-{worker_idx}"
        workspace_dir = Path(root) / "workspaces" / workspace
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            rel_path = Path("agents") / str(worker_idx) / f"file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            write_file(workspace_dir / rel_path, content)
            markdownfs_record_event(
                audit_dir,
                "write",
                workspace=workspace,
                payload={"path": str(rel_path)},
                output_edges=1,
                ref_events=1,
            )
            if (workspace_dir / rel_path).read_bytes() == content:
                read_ok += 1
            markdownfs_record_event(
                audit_dir,
                "read_ref",
                workspace=workspace,
                payload={"path": str(rel_path)},
                input_edges=1,
            )

        grep_results = 0
        grep_root = workspace_dir / "agents" / str(worker_idx)
        for path in iter_files(grep_root):
            for line in path.read_text(encoding="utf-8").splitlines():
                if NEEDLE in line:
                    grep_results += 1
        markdownfs_record_event(
            audit_dir,
            "search",
            workspace=workspace,
            payload={"query": NEEDLE, "result_count": grep_results},
            input_edges=grep_results,
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": grep_results,
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def rag_jsonl_worker(
    root,
    audit_dir,
    index_dir,
    worker_idx,
    files_per_agent,
    payload_bytes,
    needle_every,
    queue,
):
    try:
        workspace = f"ws:agent-{worker_idx}"
        workspace_dir = Path(root) / "workspaces" / workspace
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            rel_path = Path("agents") / str(worker_idx) / f"file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            write_file(workspace_dir / rel_path, content)
            rag_jsonl_index_document(index_dir, workspace, rel_path, content)
            rag_jsonl_record_event(
                audit_dir,
                "write",
                workspace=workspace,
                payload={"path": str(rel_path), "indexed": True},
                output_edges=1,
                ref_events=1,
            )
            if (workspace_dir / rel_path).read_bytes() == content:
                read_ok += 1
            rag_jsonl_record_event(
                audit_dir,
                "read_ref",
                workspace=workspace,
                payload={"path": str(rel_path)},
                input_edges=1,
            )

        results = rag_jsonl_search(index_dir, workspace, f"agents/{worker_idx}/", NEEDLE)
        rag_jsonl_record_event(
            audit_dir,
            "retrieval_search",
            workspace=workspace,
            payload={
                "query": NEEDLE,
                "result_count": len(results),
                "index": "rag-jsonl",
            },
            input_edges=len(results),
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": len(results),
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def external_vector_worker(
    root,
    audit_dir,
    index_dir,
    worker_idx,
    files_per_agent,
    payload_bytes,
    needle_every,
    queue,
):
    try:
        workspace = f"ws:agent-{worker_idx}"
        workspace_dir = Path(root) / "workspaces" / workspace
        start = time.perf_counter()
        read_ok = 0
        for file_idx in range(files_per_agent):
            rel_path = Path("agents") / str(worker_idx) / f"file-{file_idx:04d}.md"
            content = benchmark_content(worker_idx, file_idx, payload_bytes, needle_every)
            write_file(workspace_dir / rel_path, content)
            external_vector_index_document(index_dir, workspace, rel_path, content)
            rag_jsonl_record_event(
                audit_dir,
                "write",
                workspace=workspace,
                payload={"path": str(rel_path), "indexed": True, "index": "external-vector-jsonl"},
                output_edges=1,
                ref_events=1,
            )
            if (workspace_dir / rel_path).read_bytes() == content:
                read_ok += 1
            rag_jsonl_record_event(
                audit_dir,
                "read_ref",
                workspace=workspace,
                payload={"path": str(rel_path)},
                input_edges=1,
            )

        results = external_vector_search(index_dir, workspace, f"agents/{worker_idx}/", NEEDLE)
        rag_jsonl_record_event(
            audit_dir,
            "vector_search",
            workspace=workspace,
            payload={
                "query": NEEDLE,
                "result_count": len(results),
                "index": "external-vector-jsonl",
            },
            input_edges=len(results),
        )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "writes": files_per_agent,
                "read_ok": read_ok,
                "grep_results": len(results),
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "writes": 0,
                "read_ok": 0,
                "grep_results": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def anfs_cache_worker(db_path, objs_dir, workspace, cache_root, worker_idx, repeats, shared_key, queue):
    try:
        fs = anfs_core.AnfsEngine(db_path, objs_dir)
        cache_key = "agent-0-shared" if shared_key else f"agent-0-worker-{worker_idx}"
        rows = []
        start = time.perf_counter()
        for _idx in range(repeats):
            rows.append(
                fs.cache_materialized_workspace(
                    workspace,
                    cache_root,
                    cache_key=cache_key,
                )
            )
        elapsed_ms = millis(start)
        hits = sum(1 for _key, _output_dir, _file_count, reused in rows if reused)
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": elapsed_ms,
                "misses": len(rows) - hits,
                "hits": hits,
                "file_count": rows[-1][2] if rows else 0,
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "misses": 0,
                "hits": 0,
                "file_count": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def native_cache_worker(root, worker_idx, repeats, shared_key, queue):
    try:
        root = Path(root)
        workspace_dir = root / "workspaces" / "ws:agent-0"
        cache_name = "agent-0-shared" if shared_key else f"agent-0-worker-{worker_idx}"
        cache_dir = root / "working-set-cache-contention" / cache_name
        manifest_path = cache_dir / ".native-cache-manifest.json"
        lock_path = root / "working-set-cache-contention" / f"{cache_name}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        hits = 0
        misses = 0
        start = time.perf_counter()
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            for _idx in range(repeats):
                if shared_key:
                    fcntl.flock(lock_file, fcntl.LOCK_EX)
                try:
                    current_count = file_count(workspace_dir)
                    if (
                        cache_dir.is_dir()
                        and manifest_path.is_file()
                        and json.loads(manifest_path.read_text(encoding="utf-8")).get("file_count")
                        == current_count
                    ):
                        hits += 1
                        continue
                    misses += 1
                    if cache_dir.exists():
                        shutil.rmtree(cache_dir)
                    shutil.copytree(workspace_dir, cache_dir)
                    manifest_path.write_text(
                        json.dumps({"file_count": current_count}, sort_keys=True),
                        encoding="utf-8",
                    )
                finally:
                    if shared_key:
                        fcntl.flock(lock_file, fcntl.LOCK_UN)
        elapsed_ms = millis(start)
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": elapsed_ms,
                "misses": misses,
                "hits": hits,
                "file_count": file_count(
                    cache_dir,
                    exclude_names={".native-cache-manifest.json"},
                ),
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "misses": 0,
                "hits": 0,
                "file_count": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def sqlite_counts(db_path):
    with sqlite3.connect(db_path) as conn:
        seq_count, max_seq, distinct_seq = conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(seq), 0), COUNT(DISTINCT seq) FROM event_sequence"
        ).fetchone()
        event_count = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        edge_count = conn.execute("SELECT COUNT(*) FROM event_edges").fetchone()[0]
        ref_event_count = conn.execute("SELECT COUNT(*) FROM ref_events").fetchone()[0]
        latest_event_id = conn.execute(
            "SELECT event_id FROM event_sequence ORDER BY seq DESC LIMIT 1"
        ).fetchone()[0]
        event_kinds = dict(
            conn.execute(
                "SELECT kind, COUNT(*) FROM events GROUP BY kind ORDER BY kind"
            ).fetchall()
        )
    return {
        "seq_count": seq_count,
        "max_seq": max_seq,
        "distinct_seq": distinct_seq,
        "event_count": event_count,
        "edge_count": edge_count,
        "ref_event_count": ref_event_count,
        "latest_event_id": latest_event_id,
        "event_kinds": event_kinds,
    }


def run_anfs_working_set_cache(fs, root, args):
    if args.cache_repeats <= 0:
        return {
            "enabled": False,
            "repeat_count": 0,
            "misses": 0,
            "hits": 0,
            "file_count": 0,
            "elapsed_ms": 0.0,
            "passed": True,
        }

    cache_root = Path(root) / "working-set-cache"
    workspace = "ws:agent-0"
    cache_key = "agent-0"
    start = time.perf_counter()
    rows = []
    for _idx in range(args.cache_repeats):
        rows.append(
            fs.cache_materialized_workspace(
                workspace,
                str(cache_root),
                cache_key=cache_key,
            )
        )
    elapsed_ms = millis(start)
    hits = sum(1 for _cache_key, _output_dir, _file_count, reused in rows if reused)
    misses = len(rows) - hits
    expected_file_count = args.base_files + args.files_per_agent
    listed = fs.cached_working_sets(workspace)
    return {
        "enabled": True,
        "workspace": workspace,
        "cache_key": cache_key,
        "repeat_count": args.cache_repeats,
        "misses": misses,
        "hits": hits,
        "file_count": rows[-1][2] if rows else 0,
        "expected_file_count": expected_file_count,
        "elapsed_ms": elapsed_ms,
        "per_repeat_ms": elapsed_ms / args.cache_repeats,
        "listed_cache_count": len(listed),
        "passed": (
            misses == 1
            and hits == args.cache_repeats - 1
            and rows[-1][2] == expected_file_count
            and len(listed) == 1
            and listed[0][0] == cache_key
            and listed[0][1] == workspace
            and listed[0][4] == expected_file_count
        ),
    }


def run_native_working_set_cache(root, args):
    if args.cache_repeats <= 0:
        return {
            "enabled": False,
            "repeat_count": 0,
            "misses": 0,
            "hits": 0,
            "file_count": 0,
            "elapsed_ms": 0.0,
            "passed": True,
        }

    root = Path(root)
    workspace_dir = root / "workspaces" / "ws:agent-0"
    cache_root = root / "working-set-cache"
    cache_dir = cache_root / "agent-0"
    manifest_path = cache_dir / ".native-cache-manifest.json"
    expected_file_count = args.base_files + args.files_per_agent
    hits = 0
    misses = 0
    start = time.perf_counter()
    for _idx in range(args.cache_repeats):
        current_count = file_count(workspace_dir)
        if (
            cache_dir.is_dir()
            and manifest_path.is_file()
            and json.loads(manifest_path.read_text(encoding="utf-8")).get("file_count")
            == current_count
        ):
            hits += 1
            continue
        misses += 1
        if cache_dir.exists():
            shutil.rmtree(cache_dir)
        shutil.copytree(workspace_dir, cache_dir)
        manifest_path.write_text(
            json.dumps({"file_count": current_count}, sort_keys=True),
            encoding="utf-8",
        )
    elapsed_ms = millis(start)
    cached_count = file_count(cache_dir, exclude_names={".native-cache-manifest.json"})
    return {
        "enabled": True,
        "workspace": "ws:agent-0",
        "cache_key": "agent-0",
        "repeat_count": args.cache_repeats,
        "misses": misses,
        "hits": hits,
        "file_count": cached_count,
        "expected_file_count": expected_file_count,
        "elapsed_ms": elapsed_ms,
        "per_repeat_ms": elapsed_ms / args.cache_repeats,
        "passed": (
            misses == 1
            and hits == args.cache_repeats - 1
            and cached_count == expected_file_count
        ),
        "semantic_limitations": [
            "cache reuse is based only on file-count metadata",
            "no manifest hash over ANFS refs and node ids",
            "no kernel integrity verification over cached files",
        ],
    }


def run_anfs_cache_contention(fs, db_path, objs_dir, root, args):
    if args.cache_workers <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    workspace = "ws:agent-0"
    cache_root = str(Path(root) / "working-set-cache-contention")
    repeats = args.cache_worker_repeats
    expected_file_count = args.base_files + args.files_per_agent
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=anfs_cache_worker,
            args=(
                db_path,
                objs_dir,
                workspace,
                cache_root,
                worker_idx,
                repeats,
                args.cache_shared_key,
                queue,
            ),
        )
        for worker_idx in range(args.cache_workers)
    ]
    start = time.perf_counter()
    for process in processes:
        process.start()
    for process in processes:
        process.join(args.worker_timeout_s)
    elapsed_ms = millis(start)
    worker_results = []
    while not queue.empty():
        worker_results.append(queue.get())
    worker_results.sort(key=lambda row: row["worker_idx"])
    exitcodes = [process.exitcode for process in processes]
    failures = [row["failure"] for row in worker_results if row["failure"]]
    total_attempts = args.cache_workers * repeats
    expected_misses = args.cache_workers
    expected_hits = args.cache_workers * (repeats - 1)
    listed = fs.cached_working_sets(workspace)
    contention_keys = (
        {"agent-0-shared"}
        if args.cache_shared_key
        else {f"agent-0-worker-{worker_idx}" for worker_idx in range(args.cache_workers)}
    )
    listed_contention_keys = {row[0] for row in listed if row[0] in contention_keys}
    misses = sum(row["misses"] for row in worker_results)
    hits = sum(row["hits"] for row in worker_results)
    return {
        "enabled": True,
        "workspace": workspace,
        "worker_count": args.cache_workers,
        "repeat_count": repeats,
        "shared_key": args.cache_shared_key,
        "elapsed_ms": elapsed_ms,
        "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
        "process_exitcodes": exitcodes,
        "failures": failures,
        "misses": misses,
        "expected_misses": None if args.cache_shared_key else expected_misses,
        "hits": hits,
        "expected_hits": None if args.cache_shared_key else expected_hits,
        "total_attempts": total_attempts,
        "file_counts": [row["file_count"] for row in worker_results],
        "expected_file_count": expected_file_count,
        "listed_contention_cache_count": len(listed_contention_keys),
        "passed": (
            exitcodes == [0] * args.cache_workers
            and len(worker_results) == args.cache_workers
            and not failures
            and (misses + hits) == total_attempts
            and (args.cache_shared_key or misses == expected_misses)
            and (args.cache_shared_key or hits == expected_hits)
            and (not args.cache_shared_key or misses >= 1)
            and all(row["file_count"] == expected_file_count for row in worker_results)
            and len(listed_contention_keys) == len(contention_keys)
        ),
    }


def run_native_cache_contention(root, args):
    if args.cache_workers <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    repeats = args.cache_worker_repeats
    expected_file_count = args.base_files + args.files_per_agent
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=native_cache_worker,
            args=(str(root), worker_idx, repeats, args.cache_shared_key, queue),
        )
        for worker_idx in range(args.cache_workers)
    ]
    start = time.perf_counter()
    for process in processes:
        process.start()
    for process in processes:
        process.join(args.worker_timeout_s)
    elapsed_ms = millis(start)
    worker_results = []
    while not queue.empty():
        worker_results.append(queue.get())
    worker_results.sort(key=lambda row: row["worker_idx"])
    exitcodes = [process.exitcode for process in processes]
    failures = [row["failure"] for row in worker_results if row["failure"]]
    total_attempts = args.cache_workers * repeats
    expected_misses = args.cache_workers
    expected_hits = args.cache_workers * (repeats - 1)
    misses = sum(row["misses"] for row in worker_results)
    hits = sum(row["hits"] for row in worker_results)
    return {
        "enabled": True,
        "workspace": "ws:agent-0",
        "worker_count": args.cache_workers,
        "repeat_count": repeats,
        "shared_key": args.cache_shared_key,
        "elapsed_ms": elapsed_ms,
        "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
        "process_exitcodes": exitcodes,
        "failures": failures,
        "misses": misses,
        "expected_misses": None if args.cache_shared_key else expected_misses,
        "hits": hits,
        "expected_hits": None if args.cache_shared_key else expected_hits,
        "total_attempts": total_attempts,
        "file_counts": [row["file_count"] for row in worker_results],
        "expected_file_count": expected_file_count,
        "passed": (
            exitcodes == [0] * args.cache_workers
            and len(worker_results) == args.cache_workers
            and not failures
            and (misses + hits) == total_attempts
            and (args.cache_shared_key or misses == expected_misses)
            and (args.cache_shared_key or hits == expected_hits)
            and (not args.cache_shared_key or misses >= 1)
            and all(row["file_count"] == expected_file_count for row in worker_results)
        ),
        "semantic_limitations": [
            "workers use separate cache directories",
            "cache reuse is based only on file-count metadata",
            "no kernel integrity verification over cached files",
        ],
    }


def run_anfs_large_file_benchmark(fs, args):
    if args.large_file_bytes <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    content = large_content(args.large_file_bytes)
    ws = fs.open_workspace("ws:large", "large_agent", run_id="run:large-file")
    start = time.perf_counter()
    node_id = ws.write("large.bin", content, [])
    write_ms = millis(start)

    range_offsets = [
        (idx * args.large_range_stride) % args.large_file_bytes
        for idx in range(args.range_read_count)
    ]
    start = time.perf_counter()
    range_ok = 0
    for offset in range_offsets:
        expected = content[offset : offset + args.range_size]
        if bytes(fs.read_node_range(node_id, offset, args.range_size)) == expected:
            range_ok += 1
    range_ms = millis(start)

    start = time.perf_counter()
    derived_chunks = fs.node_chunks(node_id, args.chunk_size)
    derive_ms = millis(start)

    start = time.perf_counter()
    cached_chunks = fs.cache_node_chunks(node_id, args.chunk_size)
    cache_build_ms = millis(start)

    start = time.perf_counter()
    stored_chunks = fs.cached_node_chunks(node_id, args.chunk_size)
    cache_read_ms = millis(start)

    expected_chunks = expected_chunk_count(args.large_file_bytes, args.chunk_size)
    return {
        "enabled": True,
        "node_id": node_id,
        "bytes": args.large_file_bytes,
        "range_read_count": args.range_read_count,
        "range_size": args.range_size,
        "chunk_size": args.chunk_size,
        "expected_chunks": expected_chunks,
        "derived_chunks": len(derived_chunks),
        "cached_chunks": len(stored_chunks),
        "range_ok": range_ok,
        "timings_ms": {
            "write": write_ms,
            "range_reads": range_ms,
            "derive_chunks": derive_ms,
            "cache_build": cache_build_ms,
            "cache_read": cache_read_ms,
        },
        "throughput": {
            "range_reads_per_s": per_second(args.range_read_count, range_ms),
            "chunk_derive_mb_per_s": per_second(args.large_file_bytes / (1024 * 1024), derive_ms),
            "chunk_cache_read_rows_per_s": per_second(len(stored_chunks), cache_read_ms),
        },
        "passed": (
            range_ok == args.range_read_count
            and len(derived_chunks) == expected_chunks
            and cached_chunks == derived_chunks
            and stored_chunks == derived_chunks
        ),
    }


def run_native_large_file_benchmark(root, args):
    if args.large_file_bytes <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    root = Path(root)
    large_dir = root / "large"
    large_dir.mkdir(parents=True, exist_ok=True)
    large_path = large_dir / "large.bin"
    index_path = large_dir / "large.chunk-index.json"
    content = large_content(args.large_file_bytes)

    start = time.perf_counter()
    large_path.write_bytes(content)
    write_ms = millis(start)

    range_offsets = [
        (idx * args.large_range_stride) % args.large_file_bytes
        for idx in range(args.range_read_count)
    ]
    start = time.perf_counter()
    range_ok = 0
    with large_path.open("rb") as file:
        for offset in range_offsets:
            file.seek(offset)
            expected = content[offset : offset + args.range_size]
            if file.read(args.range_size) == expected:
                range_ok += 1
    range_ms = millis(start)

    start = time.perf_counter()
    chunks = []
    for chunk_index, offset in enumerate(range(0, args.large_file_bytes, args.chunk_size)):
        chunk = content[offset : offset + args.chunk_size]
        chunks.append(
            {
                "chunk_index": chunk_index,
                "offset": offset,
                "size": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
        )
    derive_ms = millis(start)

    start = time.perf_counter()
    index_path.write_text(json.dumps(chunks, sort_keys=True), encoding="utf-8")
    cache_build_ms = millis(start)

    start = time.perf_counter()
    stored_chunks = json.loads(index_path.read_text(encoding="utf-8"))
    cache_read_ms = millis(start)

    expected_chunks = expected_chunk_count(args.large_file_bytes, args.chunk_size)
    return {
        "enabled": True,
        "bytes": args.large_file_bytes,
        "range_read_count": args.range_read_count,
        "range_size": args.range_size,
        "chunk_size": args.chunk_size,
        "expected_chunks": expected_chunks,
        "derived_chunks": len(chunks),
        "cached_chunks": len(stored_chunks),
        "range_ok": range_ok,
        "timings_ms": {
            "write": write_ms,
            "range_reads": range_ms,
            "derive_chunks": derive_ms,
            "cache_build": cache_build_ms,
            "cache_read": cache_read_ms,
        },
        "throughput": {
            "range_reads_per_s": per_second(args.range_read_count, range_ms),
            "chunk_derive_mb_per_s": per_second(args.large_file_bytes / (1024 * 1024), derive_ms),
            "chunk_cache_read_rows_per_s": per_second(len(stored_chunks), cache_read_ms),
        },
        "passed": (
            range_ok == args.range_read_count
            and len(chunks) == expected_chunks
            and len(stored_chunks) == expected_chunks
            and stored_chunks == chunks
        ),
        "semantic_limitations": [
            "chunk index is sidecar JSON, not a kernel-verified derived table",
            "range reads do not pass through fragment policy gates",
            "no immutable node id ties the chunk index to file content",
        ],
    }


def run_sqlite_memory_working_set_cache(db_path, args):
    if args.cache_repeats <= 0:
        return {
            "enabled": False,
            "repeat_count": 0,
            "misses": 0,
            "hits": 0,
            "file_count": 0,
            "elapsed_ms": 0.0,
            "passed": True,
        }

    workspace = "ws:agent-0"
    cache_key = "agent-0"
    conn = sqlite_memory_connect(db_path)
    hits = 0
    misses = 0
    start = time.perf_counter()
    for _idx in range(args.cache_repeats):
        current_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE workspace = ?1",
            (workspace,),
        ).fetchone()[0]
        row = conn.execute(
            "SELECT file_count FROM working_set_cache WHERE cache_key = ?1",
            (cache_key,),
        ).fetchone()
        if row and row[0] == current_count:
            hits += 1
            continue
        misses += 1
        with conn:
            conn.execute(
                """
                INSERT INTO working_set_cache
                (cache_key, workspace, file_count, materialized_ns)
                VALUES (?1, ?2, ?3, ?4)
                ON CONFLICT(cache_key) DO UPDATE SET
                    workspace = excluded.workspace,
                    file_count = excluded.file_count,
                    materialized_ns = excluded.materialized_ns
                """,
                (cache_key, workspace, current_count, time.time_ns()),
            )
    elapsed_ms = millis(start)
    expected_file_count = args.base_files + args.files_per_agent
    return {
        "enabled": True,
        "workspace": workspace,
        "cache_key": cache_key,
        "repeat_count": args.cache_repeats,
        "misses": misses,
        "hits": hits,
        "file_count": current_count,
        "expected_file_count": expected_file_count,
        "elapsed_ms": elapsed_ms,
        "per_repeat_ms": elapsed_ms / args.cache_repeats,
        "passed": (
            misses == 1
            and hits == args.cache_repeats - 1
            and current_count == expected_file_count
        ),
        "semantic_limitations": [
            "cache reuse is based only on row-count metadata",
            "no manifest hash over refs and immutable node ids",
            "no kernel integrity verification over cached working sets",
        ],
    }


def sqlite_memory_cache_worker(db_path, worker_idx, repeats, shared_key, queue):
    try:
        workspace = "ws:agent-0"
        cache_key = "agent-0-shared" if shared_key else f"agent-0-worker-{worker_idx}"
        conn = sqlite_memory_connect(db_path)
        hits = 0
        misses = 0
        file_count_value = 0
        start = time.perf_counter()
        for _idx in range(repeats):
            file_count_value = conn.execute(
                "SELECT COUNT(*) FROM files WHERE workspace = ?1",
                (workspace,),
            ).fetchone()[0]
            with conn:
                row = conn.execute(
                    "SELECT file_count FROM working_set_cache WHERE cache_key = ?1",
                    (cache_key,),
                ).fetchone()
                if row and row[0] == file_count_value:
                    hits += 1
                    continue
                misses += 1
                conn.execute(
                    """
                    INSERT INTO working_set_cache
                    (cache_key, workspace, file_count, materialized_ns)
                    VALUES (?1, ?2, ?3, ?4)
                    ON CONFLICT(cache_key) DO UPDATE SET
                        workspace = excluded.workspace,
                        file_count = excluded.file_count,
                        materialized_ns = excluded.materialized_ns
                    """,
                    (cache_key, workspace, file_count_value, time.time_ns()),
                )
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": millis(start),
                "misses": misses,
                "hits": hits,
                "file_count": file_count_value,
                "failure": None,
            }
        )
    except Exception as exc:
        queue.put(
            {
                "worker_idx": worker_idx,
                "elapsed_ms": None,
                "misses": 0,
                "hits": 0,
                "file_count": 0,
                "failure": f"{type(exc).__name__}: {exc}",
            }
        )


def run_sqlite_memory_cache_contention(db_path, args):
    if args.cache_workers <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    repeats = args.cache_worker_repeats
    expected_file_count = args.base_files + args.files_per_agent
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=sqlite_memory_cache_worker,
            args=(db_path, worker_idx, repeats, args.cache_shared_key, queue),
        )
        for worker_idx in range(args.cache_workers)
    ]
    start = time.perf_counter()
    for process in processes:
        process.start()
    for process in processes:
        process.join(args.worker_timeout_s)
    elapsed_ms = millis(start)
    worker_results = []
    while not queue.empty():
        worker_results.append(queue.get())
    worker_results.sort(key=lambda row: row["worker_idx"])
    exitcodes = [process.exitcode for process in processes]
    failures = [row["failure"] for row in worker_results if row["failure"]]
    total_attempts = args.cache_workers * repeats
    expected_misses = args.cache_workers
    expected_hits = args.cache_workers * (repeats - 1)
    misses = sum(row["misses"] for row in worker_results)
    hits = sum(row["hits"] for row in worker_results)
    return {
        "enabled": True,
        "workspace": "ws:agent-0",
        "worker_count": args.cache_workers,
        "repeat_count": repeats,
        "shared_key": args.cache_shared_key,
        "elapsed_ms": elapsed_ms,
        "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
        "process_exitcodes": exitcodes,
        "failures": failures,
        "misses": misses,
        "expected_misses": None if args.cache_shared_key else expected_misses,
        "hits": hits,
        "expected_hits": None if args.cache_shared_key else expected_hits,
        "total_attempts": total_attempts,
        "file_counts": [row["file_count"] for row in worker_results],
        "expected_file_count": expected_file_count,
        "passed": (
            exitcodes == [0] * args.cache_workers
            and len(worker_results) == args.cache_workers
            and not failures
            and (misses + hits) == total_attempts
            and (args.cache_shared_key or misses == expected_misses)
            and (args.cache_shared_key or hits == expected_hits)
            and (not args.cache_shared_key or misses >= 1)
            and all(row["file_count"] == expected_file_count for row in worker_results)
        ),
        "semantic_limitations": [
            "cache rows store counts, not materialized immutable working sets",
            "shared-key contention relies on SQLite row locking only",
        ],
    }


def run_sqlite_memory_large_file_benchmark(db_path, args):
    if args.large_file_bytes <= 0:
        return {
            "enabled": False,
            "passed": True,
        }

    conn = sqlite_memory_connect(db_path)
    content = large_content(args.large_file_bytes)
    start = time.perf_counter()
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO large_files (name, content) VALUES (?1, ?2)",
            ("large.bin", content),
        )
    write_ms = millis(start)

    range_offsets = [
        (idx * args.large_range_stride) % args.large_file_bytes
        for idx in range(args.range_read_count)
    ]
    start = time.perf_counter()
    range_ok = 0
    for offset in range_offsets:
        row = conn.execute(
            "SELECT substr(content, ?1, ?2) FROM large_files WHERE name = 'large.bin'",
            (offset + 1, args.range_size),
        ).fetchone()
        expected = content[offset : offset + args.range_size]
        if row and row[0] == expected:
            range_ok += 1
    range_ms = millis(start)

    start = time.perf_counter()
    chunks = []
    for chunk_index, offset in enumerate(range(0, args.large_file_bytes, args.chunk_size)):
        chunk = content[offset : offset + args.chunk_size]
        chunks.append((chunk_index, offset, len(chunk), hashlib.sha256(chunk).hexdigest()))
    derive_ms = millis(start)

    start = time.perf_counter()
    with conn:
        conn.execute("DELETE FROM large_file_chunks WHERE name = 'large.bin'")
        conn.executemany(
            """
            INSERT INTO large_file_chunks
            (name, chunk_index, offset, size, sha256)
            VALUES ('large.bin', ?1, ?2, ?3, ?4)
            """,
            chunks,
        )
    cache_build_ms = millis(start)

    start = time.perf_counter()
    stored_chunks = conn.execute(
        """
        SELECT chunk_index, offset, size, sha256
        FROM large_file_chunks
        WHERE name = 'large.bin'
        ORDER BY chunk_index
        """
    ).fetchall()
    cache_read_ms = millis(start)

    expected_chunks = expected_chunk_count(args.large_file_bytes, args.chunk_size)
    return {
        "enabled": True,
        "bytes": args.large_file_bytes,
        "range_read_count": args.range_read_count,
        "range_size": args.range_size,
        "chunk_size": args.chunk_size,
        "expected_chunks": expected_chunks,
        "derived_chunks": len(chunks),
        "cached_chunks": len(stored_chunks),
        "range_ok": range_ok,
        "timings_ms": {
            "write": write_ms,
            "range_reads": range_ms,
            "derive_chunks": derive_ms,
            "cache_build": cache_build_ms,
            "cache_read": cache_read_ms,
        },
        "throughput": {
            "range_reads_per_s": per_second(args.range_read_count, range_ms),
            "chunk_derive_mb_per_s": per_second(args.large_file_bytes / (1024 * 1024), derive_ms),
            "chunk_cache_read_rows_per_s": per_second(len(stored_chunks), cache_read_ms),
        },
        "passed": (
            range_ok == args.range_read_count
            and len(chunks) == expected_chunks
            and len(stored_chunks) == expected_chunks
            and stored_chunks == chunks
        ),
        "semantic_limitations": [
            "large blob is mutable table content, not an immutable node",
            "range reads do not pass through fragment policy gates",
            "chunk rows are application-maintained, not kernel-repairable projections",
        ],
    }


def run_anfs(args):
    owned_tmpdir = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmpdir = tempfile.mkdtemp(prefix="anfs-agent-memory-bench-")
        root = Path(owned_tmpdir)

    db_path = str(root / "anfs.db")
    objs_dir = str(root / "objs")
    replay_dir = str(root / "replay")

    try:
        total_start = time.perf_counter()
        fs = anfs_core.AnfsEngine(db_path, objs_dir)

        start = time.perf_counter()
        importer_ws = fs.open_workspace("ws:importer", "importer_agent", run_id="run:setup")
        for base_idx in range(args.base_files):
            path = f"shared/base-{base_idx:04d}.md"
            importer_ws.write(path, f"base file {base_idx}\n".encode("utf-8"), [])
            importer_ws.publish(path, f"{BASE_REF}/{path}", "resource")
        for worker_idx in range(args.agents):
            fs.checkout(
                f"ws:agent-{worker_idx}",
                f"agent_{worker_idx}",
                BASE_REF,
            )
        setup_ms = millis(start)

        start = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = []
        for worker_idx in range(args.agents):
            processes.append(
                ctx.Process(
                    target=anfs_worker,
                    args=(
                        db_path,
                        objs_dir,
                        f"ws:agent-{worker_idx}",
                        f"agent_{worker_idx}",
                        worker_idx,
                        args.files_per_agent,
                        args.payload_bytes,
                        args.needle_every,
                        queue,
                    ),
                )
            )
        for process in processes:
            process.start()
        for process in processes:
            process.join(args.worker_timeout_s)
        concurrent_ms = millis(start)

        worker_results = []
        while not queue.empty():
            worker_results.append(queue.get())
        worker_results.sort(key=lambda row: row["worker_idx"])
        process_exitcodes = [process.exitcode for process in processes]

        start = time.perf_counter()
        merged_rows = []
        for worker_idx in range(args.agents):
            merged_rows.extend(
                fs.merge_workspace(
                    BASE_REF,
                    f"ws:agent-{worker_idx}",
                    "merge_agent",
                    run_id="run:merge",
                    tool_call_id=f"merge:{worker_idx}",
                )
            )
        merge_ms = millis(start)

        working_set_cache = run_anfs_working_set_cache(fs, root, args)
        cache_contention = run_anfs_cache_contention(fs, db_path, objs_dir, root, args)
        large_file = run_anfs_large_file_benchmark(fs, args)

        start = time.perf_counter()
        producer_ws = fs.open_workspace("ws:producer", "producer_agent", run_id="run:produce")
        tester_ws = fs.open_workspace("ws:tester", "tester_agent", run_id="run:test")
        approved_refs = []
        for approval_idx in range(args.approvals):
            patch_path = f"approval/patch-{approval_idx:04d}.txt"
            evidence_path = f"approval/test-{approval_idx:04d}.txt"
            patch_ref = f"artifact:patch-{approval_idx:04d}@v1"
            evidence_ref = f"artifact:test-{approval_idx:04d}@v1"
            patch_node = producer_ws.write(
                patch_path,
                f"patch {approval_idx}\n".encode("utf-8"),
                [],
            )
            producer_ws.publish(patch_path, patch_ref)
            tester_ws.write(
                evidence_path,
                f"PASS {approval_idx}\n".encode("utf-8"),
                [patch_node],
            )
            tester_ws.publish(evidence_path, evidence_ref)
            fs.approve(
                patch_ref,
                [evidence_ref],
                "reviewer_agent",
                run_id="run:review",
                tool_call_id=f"approve:{approval_idx}",
            )
            if fs.get_ref(patch_ref)[3] == "approved":
                approved_refs.append(patch_ref)
        approve_ms = millis(start)

        start = time.perf_counter()
        counts_before_replay = sqlite_counts(db_path)
        replay_rows = fs.materialize_ref_view_at_event(
            counts_before_replay["latest_event_id"],
            replay_dir,
            prefix=BASE_REF,
            overwrite=True,
        )
        replay_ms = millis(start)

        start = time.perf_counter()
        integrity_issues = fs.verify_integrity()
        integrity_ms = millis(start)
        counts = sqlite_counts(db_path)

        expected_agent_files = args.agents * args.files_per_agent
        expected_grep_results = args.agents * matching_file_count(
            args.files_per_agent,
            args.needle_every,
        )
        expected_replay_files = args.base_files + expected_agent_files
        failures = [row["failure"] for row in worker_results if row["failure"]]

        accuracy = {
            "process_exitcodes_ok": process_exitcodes == [0] * args.agents,
            "worker_result_count_ok": len(worker_results) == args.agents,
            "worker_failures": failures,
            "read_recall": sum(row["read_ok"] for row in worker_results),
            "read_expected": expected_agent_files,
            "grep_recall": sum(row["grep_results"] for row in worker_results),
            "grep_expected": expected_grep_results,
            "merge_rows": len(merged_rows),
            "merge_expected": expected_agent_files,
            "approved_refs": len(approved_refs),
            "approvals_expected": args.approvals,
            "replay_rows": len(replay_rows),
            "replay_expected": expected_replay_files,
            "integrity_issues": integrity_issues,
            "event_sequence_contiguous": counts["seq_count"]
            == counts["max_seq"]
            == counts["distinct_seq"],
            "working_set_cache_passed": working_set_cache["passed"],
            "cache_contention_passed": cache_contention["passed"],
            "large_file_passed": large_file["passed"],
        }
        accuracy["passed"] = (
            accuracy["process_exitcodes_ok"]
            and accuracy["worker_result_count_ok"]
            and not accuracy["worker_failures"]
            and accuracy["read_recall"] == accuracy["read_expected"]
            and accuracy["grep_recall"] == accuracy["grep_expected"]
            and accuracy["merge_rows"] == accuracy["merge_expected"]
            and accuracy["approved_refs"] == accuracy["approvals_expected"]
            and accuracy["replay_rows"] == accuracy["replay_expected"]
            and not accuracy["integrity_issues"]
            and accuracy["event_sequence_contiguous"]
            and accuracy["working_set_cache_passed"]
            and accuracy["cache_contention_passed"]
            and accuracy["large_file_passed"]
        )

        return {
            "backend": "anfs",
            "parameters": {
                "agents": args.agents,
                "files_per_agent": args.files_per_agent,
                "base_files": args.base_files,
                "approvals": args.approvals,
                "payload_bytes": args.payload_bytes,
                "needle_every": args.needle_every,
                "cache_repeats": args.cache_repeats,
                "cache_workers": args.cache_workers,
                "cache_worker_repeats": args.cache_worker_repeats,
                "cache_shared_key": args.cache_shared_key,
                "large_file_bytes": args.large_file_bytes,
                "range_read_count": args.range_read_count,
                "range_size": args.range_size,
                "chunk_size": args.chunk_size,
                "large_range_stride": args.large_range_stride,
            },
            "timings_ms": {
                "setup_checkout": setup_ms,
                "concurrent_read_write_grep": concurrent_ms,
                "merge": merge_ms,
                "working_set_cache": working_set_cache["elapsed_ms"],
                "cache_contention": cache_contention.get("elapsed_ms", 0.0),
                "large_file": large_file.get("timings_ms", {}).get("write", 0.0)
                + large_file.get("timings_ms", {}).get("range_reads", 0.0)
                + large_file.get("timings_ms", {}).get("derive_chunks", 0.0)
                + large_file.get("timings_ms", {}).get("cache_build", 0.0)
                + large_file.get("timings_ms", {}).get("cache_read", 0.0),
                "approve": approve_ms,
                "replay": replay_ms,
                "integrity": integrity_ms,
                "total": millis(total_start),
                "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
            },
            "throughput": {
                "concurrent_writes_per_s": per_second(expected_agent_files, concurrent_ms),
                "merge_refs_per_s": per_second(len(merged_rows), merge_ms),
                "replay_files_per_s": per_second(len(replay_rows), replay_ms),
            },
            "working_set_cache": working_set_cache,
            "cache_contention": cache_contention,
            "large_file": large_file,
            "accuracy": accuracy,
            "audit": {
                "event_count": counts["event_count"],
                "edge_count": counts["edge_count"],
                "ref_event_count": counts["ref_event_count"],
                "seq_count": counts["seq_count"],
                "event_kinds": counts["event_kinds"],
            },
            "paths": {
                "workdir": str(root),
                "db_path": db_path,
                "objs_dir": objs_dir,
                "replay_dir": replay_dir,
            },
        }
    finally:
        if owned_tmpdir and not args.keep:
            shutil.rmtree(owned_tmpdir, ignore_errors=True)


def run_native_jsonl(args):
    owned_tmpdir = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmpdir = tempfile.mkdtemp(prefix="native-jsonl-agent-memory-bench-")
        root = Path(owned_tmpdir)

    base_dir = root / "base"
    audit_dir = root / "audit"
    replay_dir = root / "replay"

    try:
        total_start = time.perf_counter()

        start = time.perf_counter()
        for base_idx in range(args.base_files):
            rel_path = Path("shared") / f"base-{base_idx:04d}.md"
            write_file(base_dir / rel_path, f"base file {base_idx}\n".encode("utf-8"))
            native_record_event(
                audit_dir,
                "write",
                workspace="ws:importer",
                payload={"path": str(rel_path)},
                output_edges=1,
                ref_events=1,
            )
            native_record_event(
                audit_dir,
                "publish",
                workspace="ws:importer",
                payload={"ref": f"{BASE_REF}/{rel_path}"},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            workspace_dir = root / "workspaces" / workspace
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
            shutil.copytree(base_dir, workspace_dir)
            native_record_event(
                audit_dir,
                "checkout",
                workspace=workspace,
                payload={"base": BASE_REF},
                input_edges=args.base_files,
                output_edges=args.base_files,
                ref_events=args.base_files,
            )
        setup_ms = millis(start)

        start = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=native_worker,
                args=(
                    str(root),
                    str(audit_dir),
                    worker_idx,
                    args.files_per_agent,
                    args.payload_bytes,
                    args.needle_every,
                    queue,
                ),
            )
            for worker_idx in range(args.agents)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(args.worker_timeout_s)
        concurrent_ms = millis(start)

        worker_results = []
        while not queue.empty():
            worker_results.append(queue.get())
        worker_results.sort(key=lambda row: row["worker_idx"])
        process_exitcodes = [process.exitcode for process in processes]

        start = time.perf_counter()
        merged_rows = []
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            source_root = root / "workspaces" / workspace / "agents" / str(worker_idx)
            merged_for_workspace = 0
            for source in iter_files(source_root):
                rel_path = source.relative_to(root / "workspaces" / workspace)
                destination = base_dir / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                merged_rows.append(str(rel_path))
                merged_for_workspace += 1
            native_record_event(
                audit_dir,
                "merge_workspace",
                workspace=workspace,
                payload={"base": BASE_REF, "merged": merged_for_workspace},
                input_edges=merged_for_workspace,
                output_edges=merged_for_workspace,
                ref_events=merged_for_workspace,
            )
        merge_ms = millis(start)

        working_set_cache = run_native_working_set_cache(root, args)
        cache_contention = run_native_cache_contention(root, args)
        large_file = run_native_large_file_benchmark(root, args)

        start = time.perf_counter()
        approved_refs = []
        lineage = {}
        for approval_idx in range(args.approvals):
            patch_ref = f"artifact:patch-{approval_idx:04d}@v1"
            evidence_ref = f"artifact:test-{approval_idx:04d}@v1"
            patch_path = root / "artifacts" / f"patch-{approval_idx:04d}.txt"
            evidence_path = root / "artifacts" / f"test-{approval_idx:04d}.txt"
            write_file(patch_path, f"patch {approval_idx}\n".encode("utf-8"))
            native_record_event(
                audit_dir,
                "write",
                workspace="ws:producer",
                payload={"path": str(patch_path.relative_to(root))},
                output_edges=1,
                ref_events=1,
            )
            native_record_event(
                audit_dir,
                "publish",
                workspace="ws:producer",
                payload={"ref": patch_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            write_file(evidence_path, f"PASS {approval_idx}\n".encode("utf-8"))
            lineage[evidence_ref] = patch_ref
            native_record_event(
                audit_dir,
                "write",
                workspace="ws:tester",
                payload={
                    "path": str(evidence_path.relative_to(root)),
                    "derived_from": patch_ref,
                },
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            native_record_event(
                audit_dir,
                "publish",
                workspace="ws:tester",
                payload={"ref": evidence_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            decision = lineage.get(evidence_ref) == patch_ref and evidence_path.exists()
            native_record_event(
                audit_dir,
                "policy_decision",
                workspace=None,
                payload={
                    "policy": "lineage_approval",
                    "target_ref": patch_ref,
                    "evidence_ref": evidence_ref,
                    "decision": "allow" if decision else "deny",
                },
            )
            if decision:
                approved_refs.append(patch_ref)
                native_record_event(
                    audit_dir,
                    "approve",
                    workspace=None,
                    payload={"target_ref": patch_ref, "evidence_ref": evidence_ref},
                    input_edges=2,
                    output_edges=1,
                    ref_events=1,
                )
        approve_ms = millis(start)

        start = time.perf_counter()
        if replay_dir.exists():
            shutil.rmtree(replay_dir)
        shutil.copytree(base_dir, replay_dir)
        replay_rows = [path for path in iter_files(replay_dir)]
        replay_ms = millis(start)

        start = time.perf_counter()
        counts = native_audit_counts(audit_dir)
        expected_agent_files = args.agents * args.files_per_agent
        expected_grep_results = args.agents * matching_file_count(
            args.files_per_agent,
            args.needle_every,
        )
        expected_replay_files = args.base_files + expected_agent_files
        failures = [row["failure"] for row in worker_results if row["failure"]]
        integrity_issues = []
        if counts["seq_count"] != counts["max_seq"] or counts["seq_count"] != counts["distinct_seq"]:
            integrity_issues.append("audit sequence is not contiguous")
        if len(list(iter_files(base_dir))) != expected_replay_files:
            integrity_issues.append("base directory file count does not match expected replay set")
        integrity_ms = millis(start)

        accuracy = {
            "process_exitcodes_ok": process_exitcodes == [0] * args.agents,
            "worker_result_count_ok": len(worker_results) == args.agents,
            "worker_failures": failures,
            "read_recall": sum(row["read_ok"] for row in worker_results),
            "read_expected": expected_agent_files,
            "grep_recall": sum(row["grep_results"] for row in worker_results),
            "grep_expected": expected_grep_results,
            "merge_rows": len(merged_rows),
            "merge_expected": expected_agent_files,
            "approved_refs": len(approved_refs),
            "approvals_expected": args.approvals,
            "replay_rows": len(replay_rows),
            "replay_expected": expected_replay_files,
            "integrity_issues": integrity_issues,
            "event_sequence_contiguous": counts["seq_count"]
            == counts["max_seq"]
            == counts["distinct_seq"],
            "working_set_cache_passed": working_set_cache["passed"],
            "cache_contention_passed": cache_contention["passed"],
            "large_file_passed": large_file["passed"],
        }
        accuracy["passed"] = (
            accuracy["process_exitcodes_ok"]
            and accuracy["worker_result_count_ok"]
            and not accuracy["worker_failures"]
            and accuracy["read_recall"] == accuracy["read_expected"]
            and accuracy["grep_recall"] == accuracy["grep_expected"]
            and accuracy["merge_rows"] == accuracy["merge_expected"]
            and accuracy["approved_refs"] == accuracy["approvals_expected"]
            and accuracy["replay_rows"] == accuracy["replay_expected"]
            and not accuracy["integrity_issues"]
            and accuracy["event_sequence_contiguous"]
            and accuracy["working_set_cache_passed"]
            and accuracy["cache_contention_passed"]
            and accuracy["large_file_passed"]
        )

        return {
            "backend": "native-jsonl",
            "parameters": {
                "agents": args.agents,
                "files_per_agent": args.files_per_agent,
                "base_files": args.base_files,
                "approvals": args.approvals,
                "payload_bytes": args.payload_bytes,
                "needle_every": args.needle_every,
                "cache_repeats": args.cache_repeats,
                "cache_workers": args.cache_workers,
                "cache_worker_repeats": args.cache_worker_repeats,
                "cache_shared_key": args.cache_shared_key,
                "large_file_bytes": args.large_file_bytes,
                "range_read_count": args.range_read_count,
                "range_size": args.range_size,
                "chunk_size": args.chunk_size,
                "large_range_stride": args.large_range_stride,
            },
            "timings_ms": {
                "setup_checkout": setup_ms,
                "concurrent_read_write_grep": concurrent_ms,
                "merge": merge_ms,
                "working_set_cache": working_set_cache["elapsed_ms"],
                "cache_contention": cache_contention.get("elapsed_ms", 0.0),
                "large_file": large_file.get("timings_ms", {}).get("write", 0.0)
                + large_file.get("timings_ms", {}).get("range_reads", 0.0)
                + large_file.get("timings_ms", {}).get("derive_chunks", 0.0)
                + large_file.get("timings_ms", {}).get("cache_build", 0.0)
                + large_file.get("timings_ms", {}).get("cache_read", 0.0),
                "approve": approve_ms,
                "replay": replay_ms,
                "integrity": integrity_ms,
                "total": millis(total_start),
                "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
            },
            "throughput": {
                "concurrent_writes_per_s": per_second(expected_agent_files, concurrent_ms),
                "merge_refs_per_s": per_second(len(merged_rows), merge_ms),
                "replay_files_per_s": per_second(len(replay_rows), replay_ms),
            },
            "working_set_cache": working_set_cache,
            "cache_contention": cache_contention,
            "large_file": large_file,
            "accuracy": accuracy,
            "audit": {
                "event_count": counts["event_count"],
                "edge_count": counts["edge_count"],
                "ref_event_count": counts["ref_event_count"],
                "seq_count": counts["seq_count"],
                "event_kinds": counts["event_kinds"],
                "semantic_limitations": [
                    "audit log is separate from file mutations",
                    "no immutable CAS node graph",
                    "lineage approval is simulated in Python metadata",
                    "replay is current-directory copy, not event-time reconstruction",
                ],
            },
            "paths": {
                "workdir": str(root),
                "base_dir": str(base_dir),
                "audit_dir": str(audit_dir),
                "replay_dir": str(replay_dir),
            },
        }
    finally:
        if owned_tmpdir and not args.keep:
            shutil.rmtree(owned_tmpdir, ignore_errors=True)


def run_rag_jsonl(args, external_vector=False):
    backend_name = "external-vector-jsonl" if external_vector else "rag-jsonl"
    index_name = backend_name
    index_document = external_vector_index_document if external_vector else rag_jsonl_index_document
    index_counts_fn = external_vector_index_counts if external_vector else rag_jsonl_index_counts
    worker_target = external_vector_worker if external_vector else rag_jsonl_worker
    owned_tmpdir = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmpdir = tempfile.mkdtemp(prefix=f"{backend_name}-agent-memory-bench-")
        root = Path(owned_tmpdir)

    base_dir = root / "base"
    audit_dir = root / "audit"
    index_dir = root / ("vector_index" if external_vector else "retrieval_index")
    replay_dir = root / "replay"

    try:
        total_start = time.perf_counter()

        start = time.perf_counter()
        for base_idx in range(args.base_files):
            rel_path = Path("shared") / f"base-{base_idx:04d}.md"
            content = f"base file {base_idx}\n".encode("utf-8")
            write_file(base_dir / rel_path, content)
            index_document(index_dir, "__base__", rel_path, content)
            rag_jsonl_record_event(
                audit_dir,
                "write",
                workspace="ws:importer",
                payload={"path": str(rel_path), "indexed": True, "index": index_name},
                output_edges=1,
                ref_events=1,
            )
            rag_jsonl_record_event(
                audit_dir,
                "publish",
                workspace="ws:importer",
                payload={"ref": f"{BASE_REF}/{rel_path}"},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            workspace_dir = root / "workspaces" / workspace
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
            shutil.copytree(base_dir, workspace_dir)
            for source in iter_files(workspace_dir):
                rel_path = source.relative_to(workspace_dir)
                index_document(index_dir, workspace, rel_path, source.read_bytes())
            rag_jsonl_record_event(
                audit_dir,
                "checkout",
                workspace=workspace,
                payload={"base": BASE_REF, "indexed": True, "index": index_name},
                input_edges=args.base_files,
                output_edges=args.base_files,
                ref_events=args.base_files,
            )
        setup_ms = millis(start)

        start = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=worker_target,
                args=(
                    str(root),
                    str(audit_dir),
                    str(index_dir),
                    worker_idx,
                    args.files_per_agent,
                    args.payload_bytes,
                    args.needle_every,
                    queue,
                ),
            )
            for worker_idx in range(args.agents)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(args.worker_timeout_s)
        concurrent_ms = millis(start)

        worker_results = []
        while not queue.empty():
            worker_results.append(queue.get())
        worker_results.sort(key=lambda row: row["worker_idx"])
        process_exitcodes = [process.exitcode for process in processes]

        start = time.perf_counter()
        merged_rows = []
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            source_root = root / "workspaces" / workspace / "agents" / str(worker_idx)
            merged_for_workspace = 0
            for source in iter_files(source_root):
                rel_path = source.relative_to(root / "workspaces" / workspace)
                destination = base_dir / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                index_document(index_dir, "__base__", rel_path, source.read_bytes())
                merged_rows.append(str(rel_path))
                merged_for_workspace += 1
            rag_jsonl_record_event(
                audit_dir,
                "merge_workspace",
                workspace=workspace,
                payload={
                    "base": BASE_REF,
                    "merged": merged_for_workspace,
                    "indexed": True,
                    "index": index_name,
                },
                input_edges=merged_for_workspace,
                output_edges=merged_for_workspace,
                ref_events=merged_for_workspace,
            )
        merge_ms = millis(start)

        working_set_cache = run_native_working_set_cache(root, args)
        cache_contention = run_native_cache_contention(root, args)
        large_file = run_native_large_file_benchmark(root, args)

        start = time.perf_counter()
        approved_refs = []
        lineage = {}
        for approval_idx in range(args.approvals):
            patch_ref = f"artifact:patch-{approval_idx:04d}@v1"
            evidence_ref = f"artifact:test-{approval_idx:04d}@v1"
            patch_path = root / "artifacts" / f"patch-{approval_idx:04d}.txt"
            evidence_path = root / "artifacts" / f"test-{approval_idx:04d}.txt"
            patch_content = f"patch {approval_idx}\n".encode("utf-8")
            evidence_content = f"PASS {approval_idx}\n".encode("utf-8")
            write_file(patch_path, patch_content)
            index_document(index_dir, "artifacts", patch_ref, patch_content)
            rag_jsonl_record_event(
                audit_dir,
                "write",
                workspace="ws:producer",
                payload={
                    "path": str(patch_path.relative_to(root)),
                    "indexed": True,
                    "index": index_name,
                },
                output_edges=1,
                ref_events=1,
            )
            rag_jsonl_record_event(
                audit_dir,
                "publish",
                workspace="ws:producer",
                payload={"ref": patch_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            write_file(evidence_path, evidence_content)
            index_document(index_dir, "artifacts", evidence_ref, evidence_content)
            lineage[evidence_ref] = patch_ref
            rag_jsonl_record_event(
                audit_dir,
                "write",
                workspace="ws:tester",
                payload={
                    "path": str(evidence_path.relative_to(root)),
                    "derived_from": patch_ref,
                    "indexed": True,
                    "index": index_name,
                },
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            rag_jsonl_record_event(
                audit_dir,
                "publish",
                workspace="ws:tester",
                payload={"ref": evidence_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            decision = lineage.get(evidence_ref) == patch_ref and evidence_path.exists()
            rag_jsonl_record_event(
                audit_dir,
                "policy_decision",
                workspace=None,
                payload={
                    "policy": "lineage_approval",
                    "target_ref": patch_ref,
                    "evidence_ref": evidence_ref,
                    "decision": "allow" if decision else "deny",
                },
            )
            if decision:
                approved_refs.append(patch_ref)
                rag_jsonl_record_event(
                    audit_dir,
                    "approve",
                    workspace=None,
                    payload={"target_ref": patch_ref, "evidence_ref": evidence_ref},
                    input_edges=2,
                    output_edges=1,
                    ref_events=1,
                )
        approve_ms = millis(start)

        start = time.perf_counter()
        if replay_dir.exists():
            shutil.rmtree(replay_dir)
        shutil.copytree(base_dir, replay_dir)
        replay_rows = [path for path in iter_files(replay_dir)]
        replay_ms = millis(start)

        start = time.perf_counter()
        counts = native_audit_counts(audit_dir)
        index_counts = index_counts_fn(index_dir)
        expected_agent_files = args.agents * args.files_per_agent
        expected_grep_results = args.agents * matching_file_count(
            args.files_per_agent,
            args.needle_every,
        )
        expected_replay_files = args.base_files + expected_agent_files
        failures = [row["failure"] for row in worker_results if row["failure"]]
        integrity_issues = []
        if counts["seq_count"] != counts["max_seq"] or counts["seq_count"] != counts["distinct_seq"]:
            integrity_issues.append(f"{backend_name} audit sequence is not contiguous")
        if len(list(iter_files(base_dir))) != expected_replay_files:
            integrity_issues.append("base directory file count does not match expected replay set")
        if index_counts["unique_documents"] < expected_agent_files:
            integrity_issues.append(f"{index_name} index has fewer documents than agent writes")
        integrity_ms = millis(start)

        accuracy = {
            "process_exitcodes_ok": process_exitcodes == [0] * args.agents,
            "worker_result_count_ok": len(worker_results) == args.agents,
            "worker_failures": failures,
            "read_recall": sum(row["read_ok"] for row in worker_results),
            "read_expected": expected_agent_files,
            "grep_recall": sum(row["grep_results"] for row in worker_results),
            "grep_expected": expected_grep_results,
            "merge_rows": len(merged_rows),
            "merge_expected": expected_agent_files,
            "approved_refs": len(approved_refs),
            "approvals_expected": args.approvals,
            "replay_rows": len(replay_rows),
            "replay_expected": expected_replay_files,
            "integrity_issues": integrity_issues,
            "event_sequence_contiguous": counts["seq_count"]
            == counts["max_seq"]
            == counts["distinct_seq"],
            "working_set_cache_passed": working_set_cache["passed"],
            "cache_contention_passed": cache_contention["passed"],
            "large_file_passed": large_file["passed"],
            "retrieval_index_documents": index_counts["unique_documents"],
        }
        accuracy["passed"] = (
            accuracy["process_exitcodes_ok"]
            and accuracy["worker_result_count_ok"]
            and not accuracy["worker_failures"]
            and accuracy["read_recall"] == accuracy["read_expected"]
            and accuracy["grep_recall"] == accuracy["grep_expected"]
            and accuracy["merge_rows"] == accuracy["merge_expected"]
            and accuracy["approved_refs"] == accuracy["approvals_expected"]
            and accuracy["replay_rows"] == accuracy["replay_expected"]
            and not accuracy["integrity_issues"]
            and accuracy["event_sequence_contiguous"]
            and accuracy["working_set_cache_passed"]
            and accuracy["cache_contention_passed"]
            and accuracy["large_file_passed"]
        )

        return {
            "backend": backend_name,
            "parameters": {
                "agents": args.agents,
                "files_per_agent": args.files_per_agent,
                "base_files": args.base_files,
                "approvals": args.approvals,
                "payload_bytes": args.payload_bytes,
                "needle_every": args.needle_every,
                "cache_repeats": args.cache_repeats,
                "cache_workers": args.cache_workers,
                "cache_worker_repeats": args.cache_worker_repeats,
                "cache_shared_key": args.cache_shared_key,
                "large_file_bytes": args.large_file_bytes,
                "range_read_count": args.range_read_count,
                "range_size": args.range_size,
                "chunk_size": args.chunk_size,
                "large_range_stride": args.large_range_stride,
            },
            "timings_ms": {
                "setup_checkout": setup_ms,
                "concurrent_read_write_grep": concurrent_ms,
                "merge": merge_ms,
                "working_set_cache": working_set_cache["elapsed_ms"],
                "cache_contention": cache_contention.get("elapsed_ms", 0.0),
                "large_file": large_file.get("timings_ms", {}).get("write", 0.0)
                + large_file.get("timings_ms", {}).get("range_reads", 0.0)
                + large_file.get("timings_ms", {}).get("derive_chunks", 0.0)
                + large_file.get("timings_ms", {}).get("cache_build", 0.0)
                + large_file.get("timings_ms", {}).get("cache_read", 0.0),
                "approve": approve_ms,
                "replay": replay_ms,
                "integrity": integrity_ms,
                "total": millis(total_start),
                "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
            },
            "throughput": {
                "concurrent_writes_per_s": per_second(expected_agent_files, concurrent_ms),
                "merge_refs_per_s": per_second(len(merged_rows), merge_ms),
                "replay_files_per_s": per_second(len(replay_rows), replay_ms),
            },
            "working_set_cache": working_set_cache,
            "cache_contention": cache_contention,
            "large_file": large_file,
            "retrieval_index": index_counts,
            "accuracy": accuracy,
            "audit": {
                "event_count": counts["event_count"],
                "edge_count": counts["edge_count"],
                "ref_event_count": counts["ref_event_count"],
                "seq_count": counts["seq_count"],
                "event_kinds": counts["event_kinds"],
                "semantic_limitations": [
                    (
                        "external vector index is append-only JSONL outside the file mutation primitive"
                        if external_vector
                        else "retrieval index is append-only JSONL outside the file mutation primitive"
                    ),
                    (
                        "vector search uses deterministic hashed embeddings and cosine similarity, not a managed vector database service"
                        if external_vector
                        else "retrieval search is lexical token matching, not vector similarity"
                    ),
                    "no immutable CAS node graph",
                    "lineage approval is simulated in Python metadata",
                    "replay is current-directory copy, not event-time reconstruction",
                    "no integrated policy or fragment visibility semantics",
                ],
            },
            "paths": {
                "workdir": str(root),
                "base_dir": str(base_dir),
                "audit_dir": str(audit_dir),
                "index_dir": str(index_dir),
                "replay_dir": str(replay_dir),
            },
        }
    finally:
        if owned_tmpdir and not args.keep:
            shutil.rmtree(owned_tmpdir, ignore_errors=True)


def run_markdownfs(args):
    owned_tmpdir = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmpdir = tempfile.mkdtemp(prefix="markdownfs-agent-memory-bench-")
        root = Path(owned_tmpdir)

    base_dir = root / "base"
    audit_dir = root / "audit"
    replay_dir = root / "replay"

    try:
        total_start = time.perf_counter()

        start = time.perf_counter()
        for base_idx in range(args.base_files):
            rel_path = Path("shared") / f"base-{base_idx:04d}.md"
            write_file(base_dir / rel_path, f"base file {base_idx}\n".encode("utf-8"))
            markdownfs_record_event(
                audit_dir,
                "write",
                workspace="ws:importer",
                payload={"path": str(rel_path)},
                output_edges=1,
                ref_events=1,
            )
            markdownfs_record_event(
                audit_dir,
                "publish",
                workspace="ws:importer",
                payload={"ref": f"{BASE_REF}/{rel_path}"},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            workspace_dir = root / "workspaces" / workspace
            if workspace_dir.exists():
                shutil.rmtree(workspace_dir)
            shutil.copytree(base_dir, workspace_dir)
            markdownfs_record_event(
                audit_dir,
                "checkout",
                workspace=workspace,
                payload={"base": BASE_REF},
                input_edges=args.base_files,
                output_edges=args.base_files,
                ref_events=args.base_files,
            )
        setup_ms = millis(start)

        start = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=markdownfs_worker,
                args=(
                    str(root),
                    str(audit_dir),
                    worker_idx,
                    args.files_per_agent,
                    args.payload_bytes,
                    args.needle_every,
                    queue,
                ),
            )
            for worker_idx in range(args.agents)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(args.worker_timeout_s)
        concurrent_ms = millis(start)

        worker_results = []
        while not queue.empty():
            worker_results.append(queue.get())
        worker_results.sort(key=lambda row: row["worker_idx"])
        process_exitcodes = [process.exitcode for process in processes]

        start = time.perf_counter()
        merged_rows = []
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            source_root = root / "workspaces" / workspace / "agents" / str(worker_idx)
            merged_for_workspace = 0
            for source in iter_files(source_root):
                rel_path = source.relative_to(root / "workspaces" / workspace)
                destination = base_dir / rel_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                merged_rows.append(str(rel_path))
                merged_for_workspace += 1
            markdownfs_record_event(
                audit_dir,
                "merge_workspace",
                workspace=workspace,
                payload={"base": BASE_REF, "merged": merged_for_workspace},
                input_edges=merged_for_workspace,
                output_edges=merged_for_workspace,
                ref_events=merged_for_workspace,
            )
        merge_ms = millis(start)

        working_set_cache = run_native_working_set_cache(root, args)
        cache_contention = run_native_cache_contention(root, args)
        large_file = run_native_large_file_benchmark(root, args)

        start = time.perf_counter()
        approved_refs = []
        lineage = {}
        for approval_idx in range(args.approvals):
            patch_ref = f"artifact:patch-{approval_idx:04d}@v1"
            evidence_ref = f"artifact:test-{approval_idx:04d}@v1"
            patch_path = root / "artifacts" / f"patch-{approval_idx:04d}.md"
            evidence_path = root / "artifacts" / f"test-{approval_idx:04d}.md"
            write_file(patch_path, f"# Patch {approval_idx}\n\npatch {approval_idx}\n".encode("utf-8"))
            markdownfs_record_event(
                audit_dir,
                "write",
                workspace="ws:producer",
                payload={"path": str(patch_path.relative_to(root))},
                output_edges=1,
                ref_events=1,
            )
            markdownfs_record_event(
                audit_dir,
                "publish",
                workspace="ws:producer",
                payload={"ref": patch_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            write_file(
                evidence_path,
                f"# Test {approval_idx}\n\nPASS {approval_idx}\n".encode("utf-8"),
            )
            lineage[evidence_ref] = patch_ref
            markdownfs_record_event(
                audit_dir,
                "write",
                workspace="ws:tester",
                payload={
                    "path": str(evidence_path.relative_to(root)),
                    "derived_from": patch_ref,
                },
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            markdownfs_record_event(
                audit_dir,
                "publish",
                workspace="ws:tester",
                payload={"ref": evidence_ref},
                input_edges=1,
                output_edges=1,
                ref_events=1,
            )
            decision = lineage.get(evidence_ref) == patch_ref and evidence_path.exists()
            markdownfs_record_event(
                audit_dir,
                "policy_decision",
                workspace=None,
                payload={
                    "policy": "lineage_approval",
                    "target_ref": patch_ref,
                    "evidence_ref": evidence_ref,
                    "decision": "allow" if decision else "deny",
                },
            )
            if decision:
                approved_refs.append(patch_ref)
                markdownfs_record_event(
                    audit_dir,
                    "approve",
                    workspace=None,
                    payload={"target_ref": patch_ref, "evidence_ref": evidence_ref},
                    input_edges=2,
                    output_edges=1,
                    ref_events=1,
                )
        approve_ms = millis(start)

        start = time.perf_counter()
        if replay_dir.exists():
            shutil.rmtree(replay_dir)
        shutil.copytree(base_dir, replay_dir)
        replay_rows = [path for path in iter_files(replay_dir)]
        replay_ms = millis(start)

        start = time.perf_counter()
        counts = markdownfs_audit_counts(audit_dir)
        expected_agent_files = args.agents * args.files_per_agent
        expected_grep_results = args.agents * matching_file_count(
            args.files_per_agent,
            args.needle_every,
        )
        expected_replay_files = args.base_files + expected_agent_files
        failures = [row["failure"] for row in worker_results if row["failure"]]
        integrity_issues = []
        if counts["seq_count"] != counts["max_seq"] or counts["seq_count"] != counts["distinct_seq"]:
            integrity_issues.append("markdown audit sequence is not contiguous")
        if len(list(iter_files(base_dir))) != expected_replay_files:
            integrity_issues.append("base directory file count does not match expected replay set")
        integrity_ms = millis(start)

        accuracy = {
            "process_exitcodes_ok": process_exitcodes == [0] * args.agents,
            "worker_result_count_ok": len(worker_results) == args.agents,
            "worker_failures": failures,
            "read_recall": sum(row["read_ok"] for row in worker_results),
            "read_expected": expected_agent_files,
            "grep_recall": sum(row["grep_results"] for row in worker_results),
            "grep_expected": expected_grep_results,
            "merge_rows": len(merged_rows),
            "merge_expected": expected_agent_files,
            "approved_refs": len(approved_refs),
            "approvals_expected": args.approvals,
            "replay_rows": len(replay_rows),
            "replay_expected": expected_replay_files,
            "integrity_issues": integrity_issues,
            "event_sequence_contiguous": counts["seq_count"]
            == counts["max_seq"]
            == counts["distinct_seq"],
            "working_set_cache_passed": working_set_cache["passed"],
            "cache_contention_passed": cache_contention["passed"],
            "large_file_passed": large_file["passed"],
        }
        accuracy["passed"] = (
            accuracy["process_exitcodes_ok"]
            and accuracy["worker_result_count_ok"]
            and not accuracy["worker_failures"]
            and accuracy["read_recall"] == accuracy["read_expected"]
            and accuracy["grep_recall"] == accuracy["grep_expected"]
            and accuracy["merge_rows"] == accuracy["merge_expected"]
            and accuracy["approved_refs"] == accuracy["approvals_expected"]
            and accuracy["replay_rows"] == accuracy["replay_expected"]
            and not accuracy["integrity_issues"]
            and accuracy["event_sequence_contiguous"]
            and accuracy["working_set_cache_passed"]
            and accuracy["cache_contention_passed"]
            and accuracy["large_file_passed"]
        )

        return {
            "backend": "markdownfs",
            "parameters": {
                "agents": args.agents,
                "files_per_agent": args.files_per_agent,
                "base_files": args.base_files,
                "approvals": args.approvals,
                "payload_bytes": args.payload_bytes,
                "needle_every": args.needle_every,
                "cache_repeats": args.cache_repeats,
                "cache_workers": args.cache_workers,
                "cache_worker_repeats": args.cache_worker_repeats,
                "cache_shared_key": args.cache_shared_key,
                "large_file_bytes": args.large_file_bytes,
                "range_read_count": args.range_read_count,
                "range_size": args.range_size,
                "chunk_size": args.chunk_size,
                "large_range_stride": args.large_range_stride,
            },
            "timings_ms": {
                "setup_checkout": setup_ms,
                "concurrent_read_write_grep": concurrent_ms,
                "merge": merge_ms,
                "working_set_cache": working_set_cache["elapsed_ms"],
                "cache_contention": cache_contention.get("elapsed_ms", 0.0),
                "large_file": large_file.get("timings_ms", {}).get("write", 0.0)
                + large_file.get("timings_ms", {}).get("range_reads", 0.0)
                + large_file.get("timings_ms", {}).get("derive_chunks", 0.0)
                + large_file.get("timings_ms", {}).get("cache_build", 0.0)
                + large_file.get("timings_ms", {}).get("cache_read", 0.0),
                "approve": approve_ms,
                "replay": replay_ms,
                "integrity": integrity_ms,
                "total": millis(total_start),
                "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
            },
            "throughput": {
                "concurrent_writes_per_s": per_second(expected_agent_files, concurrent_ms),
                "merge_refs_per_s": per_second(len(merged_rows), merge_ms),
                "replay_files_per_s": per_second(len(replay_rows), replay_ms),
            },
            "working_set_cache": working_set_cache,
            "cache_contention": cache_contention,
            "large_file": large_file,
            "accuracy": accuracy,
            "audit": {
                "event_count": counts["event_count"],
                "edge_count": counts["edge_count"],
                "ref_event_count": counts["ref_event_count"],
                "seq_count": counts["seq_count"],
                "event_kinds": counts["event_kinds"],
                "semantic_limitations": [
                    "human-readable markdown audit is separate from file mutations",
                    "no immutable CAS node graph",
                    "lineage approval is simulated in Python metadata",
                    "replay is current-directory copy, not event-time reconstruction",
                    "no integrated policy or fragment visibility semantics",
                ],
            },
            "paths": {
                "workdir": str(root),
                "base_dir": str(base_dir),
                "audit_dir": str(audit_dir),
                "replay_dir": str(replay_dir),
            },
        }
    finally:
        if owned_tmpdir and not args.keep:
            shutil.rmtree(owned_tmpdir, ignore_errors=True)


def run_sqlite_memory(args):
    owned_tmpdir = None
    if args.workdir:
        root = Path(args.workdir)
        root.mkdir(parents=True, exist_ok=True)
    else:
        owned_tmpdir = tempfile.mkdtemp(prefix="sqlite-memory-agent-memory-bench-")
        root = Path(owned_tmpdir)

    db_path = str(root / "sqlite-memory.db")
    replay_dir = root / "replay"
    base_workspace = "__base__"

    try:
        total_start = time.perf_counter()
        conn = sqlite_memory_connect(db_path)

        start = time.perf_counter()
        for base_idx in range(args.base_files):
            path = f"shared/base-{base_idx:04d}.md"
            with conn:
                conn.execute(
                    """
                    INSERT INTO files (workspace, path, content, updated_ns)
                    VALUES (?1, ?2, ?3, ?4)
                    """,
                    (base_workspace, path, f"base file {base_idx}\n".encode("utf-8"), time.time_ns()),
                )
                sqlite_memory_record_event(
                    conn,
                    "write",
                    workspace="ws:importer",
                    payload={"path": path},
                    output_edges=1,
                    ref_events=1,
                )
                sqlite_memory_record_event(
                    conn,
                    "publish",
                    workspace="ws:importer",
                    payload={"ref": f"{BASE_REF}/{path}"},
                    input_edges=1,
                    output_edges=1,
                    ref_events=1,
                )
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            with conn:
                conn.execute("DELETE FROM files WHERE workspace = ?1", (workspace,))
                conn.execute(
                    """
                    INSERT INTO files (workspace, path, content, updated_ns)
                    SELECT ?1, path, content, ?2
                    FROM files
                    WHERE workspace = ?3
                    """,
                    (workspace, time.time_ns(), base_workspace),
                )
                sqlite_memory_record_event(
                    conn,
                    "checkout",
                    workspace=workspace,
                    payload={"base": BASE_REF},
                    input_edges=args.base_files,
                    output_edges=args.base_files,
                    ref_events=args.base_files,
                )
        setup_ms = millis(start)

        start = time.perf_counter()
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        processes = [
            ctx.Process(
                target=sqlite_memory_worker,
                args=(
                    db_path,
                    worker_idx,
                    args.files_per_agent,
                    args.payload_bytes,
                    args.needle_every,
                    queue,
                ),
            )
            for worker_idx in range(args.agents)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(args.worker_timeout_s)
        concurrent_ms = millis(start)

        worker_results = []
        while not queue.empty():
            worker_results.append(queue.get())
        worker_results.sort(key=lambda row: row["worker_idx"])
        process_exitcodes = [process.exitcode for process in processes]

        start = time.perf_counter()
        merged_rows = []
        for worker_idx in range(args.agents):
            workspace = f"ws:agent-{worker_idx}"
            rows = conn.execute(
                """
                SELECT path, content
                FROM files
                WHERE workspace = ?1 AND path LIKE ?2
                ORDER BY path
                """,
                (workspace, f"agents/{worker_idx}/%"),
            ).fetchall()
            with conn:
                for path, content in rows:
                    conn.execute(
                        """
                        INSERT INTO files (workspace, path, content, updated_ns)
                        VALUES (?1, ?2, ?3, ?4)
                        ON CONFLICT(workspace, path) DO UPDATE SET
                            content = excluded.content,
                            updated_ns = excluded.updated_ns
                        """,
                        (base_workspace, path, content, time.time_ns()),
                    )
                    merged_rows.append(path)
                sqlite_memory_record_event(
                    conn,
                    "merge_workspace",
                    workspace=workspace,
                    payload={"base": BASE_REF, "merged": len(rows)},
                    input_edges=len(rows),
                    output_edges=len(rows),
                    ref_events=len(rows),
                )
        merge_ms = millis(start)

        working_set_cache = run_sqlite_memory_working_set_cache(db_path, args)
        cache_contention = run_sqlite_memory_cache_contention(db_path, args)
        large_file = run_sqlite_memory_large_file_benchmark(db_path, args)

        start = time.perf_counter()
        approved_refs = []
        for approval_idx in range(args.approvals):
            patch_ref = f"artifact:patch-{approval_idx:04d}@v1"
            evidence_ref = f"artifact:test-{approval_idx:04d}@v1"
            with conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO files
                    (workspace, path, content, updated_ns)
                    VALUES ('artifacts', ?1, ?2, ?3)
                    """,
                    (patch_ref, f"patch {approval_idx}\n".encode("utf-8"), time.time_ns()),
                )
                sqlite_memory_record_event(
                    conn,
                    "write",
                    workspace="ws:producer",
                    payload={"path": patch_ref},
                    output_edges=1,
                    ref_events=1,
                )
                sqlite_memory_record_event(
                    conn,
                    "publish",
                    workspace="ws:producer",
                    payload={"ref": patch_ref},
                    input_edges=1,
                    output_edges=1,
                    ref_events=1,
                )
                conn.execute(
                    """
                    INSERT OR REPLACE INTO files
                    (workspace, path, content, updated_ns)
                    VALUES ('artifacts', ?1, ?2, ?3)
                    """,
                    (evidence_ref, f"PASS {approval_idx}\n".encode("utf-8"), time.time_ns()),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO lineage (evidence_ref, target_ref) VALUES (?1, ?2)",
                    (evidence_ref, patch_ref),
                )
                sqlite_memory_record_event(
                    conn,
                    "write",
                    workspace="ws:tester",
                    payload={"path": evidence_ref, "derived_from": patch_ref},
                    input_edges=1,
                    output_edges=1,
                    ref_events=1,
                )
                sqlite_memory_record_event(
                    conn,
                    "publish",
                    workspace="ws:tester",
                    payload={"ref": evidence_ref},
                    input_edges=1,
                    output_edges=1,
                    ref_events=1,
                )
                decision = conn.execute(
                    "SELECT 1 FROM lineage WHERE evidence_ref = ?1 AND target_ref = ?2",
                    (evidence_ref, patch_ref),
                ).fetchone()
                sqlite_memory_record_event(
                    conn,
                    "policy_decision",
                    payload={
                        "policy": "lineage_approval",
                        "target_ref": patch_ref,
                        "evidence_ref": evidence_ref,
                        "decision": "allow" if decision else "deny",
                    },
                )
                if decision:
                    approved_refs.append(patch_ref)
                    sqlite_memory_record_event(
                        conn,
                        "approve",
                        payload={"target_ref": patch_ref, "evidence_ref": evidence_ref},
                        input_edges=2,
                        output_edges=1,
                        ref_events=1,
                    )
        approve_ms = millis(start)

        start = time.perf_counter()
        if replay_dir.exists():
            shutil.rmtree(replay_dir)
        replay_rows = []
        for path, content in conn.execute(
            "SELECT path, content FROM files WHERE workspace = ?1 ORDER BY path",
            (base_workspace,),
        ):
            write_file(replay_dir / path, content)
            replay_rows.append(path)
        replay_ms = millis(start)

        start = time.perf_counter()
        counts = sqlite_memory_counts(db_path)
        expected_agent_files = args.agents * args.files_per_agent
        expected_grep_results = args.agents * matching_file_count(
            args.files_per_agent,
            args.needle_every,
        )
        expected_replay_files = args.base_files + expected_agent_files
        failures = [row["failure"] for row in worker_results if row["failure"]]
        base_file_count = conn.execute(
            "SELECT COUNT(*) FROM files WHERE workspace = ?1",
            (base_workspace,),
        ).fetchone()[0]
        integrity_issues = []
        if counts["seq_count"] != counts["max_seq"] or counts["seq_count"] != counts["distinct_seq"]:
            integrity_issues.append("audit sequence is not contiguous")
        if base_file_count != expected_replay_files:
            integrity_issues.append("base workspace file count does not match expected replay set")
        integrity_ms = millis(start)

        accuracy = {
            "process_exitcodes_ok": process_exitcodes == [0] * args.agents,
            "worker_result_count_ok": len(worker_results) == args.agents,
            "worker_failures": failures,
            "read_recall": sum(row["read_ok"] for row in worker_results),
            "read_expected": expected_agent_files,
            "grep_recall": sum(row["grep_results"] for row in worker_results),
            "grep_expected": expected_grep_results,
            "merge_rows": len(merged_rows),
            "merge_expected": expected_agent_files,
            "approved_refs": len(approved_refs),
            "approvals_expected": args.approvals,
            "replay_rows": len(replay_rows),
            "replay_expected": expected_replay_files,
            "integrity_issues": integrity_issues,
            "event_sequence_contiguous": counts["seq_count"]
            == counts["max_seq"]
            == counts["distinct_seq"],
            "working_set_cache_passed": working_set_cache["passed"],
            "cache_contention_passed": cache_contention["passed"],
            "large_file_passed": large_file["passed"],
        }
        accuracy["passed"] = (
            accuracy["process_exitcodes_ok"]
            and accuracy["worker_result_count_ok"]
            and not accuracy["worker_failures"]
            and accuracy["read_recall"] == accuracy["read_expected"]
            and accuracy["grep_recall"] == accuracy["grep_expected"]
            and accuracy["merge_rows"] == accuracy["merge_expected"]
            and accuracy["approved_refs"] == accuracy["approvals_expected"]
            and accuracy["replay_rows"] == accuracy["replay_expected"]
            and not accuracy["integrity_issues"]
            and accuracy["event_sequence_contiguous"]
            and accuracy["working_set_cache_passed"]
            and accuracy["cache_contention_passed"]
            and accuracy["large_file_passed"]
        )

        return {
            "backend": "sqlite-memory",
            "parameters": {
                "agents": args.agents,
                "files_per_agent": args.files_per_agent,
                "base_files": args.base_files,
                "approvals": args.approvals,
                "payload_bytes": args.payload_bytes,
                "needle_every": args.needle_every,
                "cache_repeats": args.cache_repeats,
                "cache_workers": args.cache_workers,
                "cache_worker_repeats": args.cache_worker_repeats,
                "cache_shared_key": args.cache_shared_key,
                "large_file_bytes": args.large_file_bytes,
                "range_read_count": args.range_read_count,
                "range_size": args.range_size,
                "chunk_size": args.chunk_size,
                "large_range_stride": args.large_range_stride,
            },
            "timings_ms": {
                "setup_checkout": setup_ms,
                "concurrent_read_write_grep": concurrent_ms,
                "merge": merge_ms,
                "working_set_cache": working_set_cache["elapsed_ms"],
                "cache_contention": cache_contention.get("elapsed_ms", 0.0),
                "large_file": large_file.get("timings_ms", {}).get("write", 0.0)
                + large_file.get("timings_ms", {}).get("range_reads", 0.0)
                + large_file.get("timings_ms", {}).get("derive_chunks", 0.0)
                + large_file.get("timings_ms", {}).get("cache_build", 0.0)
                + large_file.get("timings_ms", {}).get("cache_read", 0.0),
                "approve": approve_ms,
                "replay": replay_ms,
                "integrity": integrity_ms,
                "total": millis(total_start),
                "worker_elapsed": [row["elapsed_ms"] for row in worker_results],
            },
            "throughput": {
                "concurrent_writes_per_s": per_second(expected_agent_files, concurrent_ms),
                "merge_refs_per_s": per_second(len(merged_rows), merge_ms),
                "replay_files_per_s": per_second(len(replay_rows), replay_ms),
            },
            "working_set_cache": working_set_cache,
            "cache_contention": cache_contention,
            "large_file": large_file,
            "accuracy": accuracy,
            "audit": {
                "event_count": counts["event_count"],
                "edge_count": counts["edge_count"],
                "ref_event_count": counts["ref_event_count"],
                "seq_count": counts["seq_count"],
                "event_kinds": counts["event_kinds"],
                "semantic_limitations": [
                    "plain workspace/path/content rows, not immutable CAS nodes",
                    "audit events are not typed causal graph edges",
                    "lineage approval is simulated through one metadata table",
                    "replay is current-row materialization, not event-time reconstruction",
                    "no integrated policy or fragment visibility semantics",
                ],
            },
            "paths": {
                "workdir": str(root),
                "db_path": db_path,
                "replay_dir": str(replay_dir),
            },
        }
    finally:
        if owned_tmpdir and not args.keep:
            shutil.rmtree(owned_tmpdir, ignore_errors=True)


def backend_args(args, backend):
    child = SimpleNamespace(**vars(args))
    if args.workdir:
        child.workdir = str(Path(args.workdir) / backend)
    return child


def case_args(args, case_name, overrides):
    child = SimpleNamespace(**vars(args))
    for key, value in overrides.items():
        setattr(child, key, value)
    if args.workdir:
        child.workdir = str(Path(args.workdir) / case_name)
    return child


def run_single(args):
    if args.backend == "anfs":
        return run_anfs(args)
    if args.backend == "native-jsonl":
        return run_native_jsonl(args)
    if args.backend == "rag-jsonl":
        return run_rag_jsonl(args)
    if args.backend == "external-vector-jsonl":
        return run_rag_jsonl(args, external_vector=True)
    if args.backend == "markdownfs":
        return run_markdownfs(args)
    if args.backend == "sqlite-memory":
        return run_sqlite_memory(args)
    return {
        "results": [
            run_anfs(backend_args(args, "anfs")),
            run_native_jsonl(backend_args(args, "native-jsonl")),
            run_rag_jsonl(backend_args(args, "rag-jsonl")),
            run_rag_jsonl(backend_args(args, "external-vector-jsonl"), external_vector=True),
            run_markdownfs(backend_args(args, "markdownfs")),
            run_sqlite_memory(backend_args(args, "sqlite-memory")),
        ]
    }


def single_passed(result, backend):
    if backend == "all":
        return all(row["accuracy"]["passed"] for row in result["results"])
    return result["accuracy"]["passed"]


def matrix_cases(args):
    base = {
        "agents": args.agents,
        "files_per_agent": args.files_per_agent,
        "base_files": args.base_files,
        "approvals": args.approvals,
        "payload_bytes": args.payload_bytes,
        "needle_every": args.needle_every,
        "cache_repeats": args.cache_repeats,
        "cache_workers": args.cache_workers,
        "cache_worker_repeats": args.cache_worker_repeats,
        "cache_shared_key": args.cache_shared_key,
        "large_file_bytes": args.large_file_bytes,
        "range_read_count": args.range_read_count,
        "range_size": args.range_size,
        "chunk_size": args.chunk_size,
        "large_range_stride": args.large_range_stride,
    }
    if args.matrix == "quick":
        return [
            ("baseline", base),
            ("agents_x2", {**base, "agents": args.agents * 2}),
            ("files_x2", {**base, "files_per_agent": args.files_per_agent * 2}),
            ("payload_4k", {**base, "payload_bytes": max(args.payload_bytes, 4096)}),
            ("sparse_search", {**base, "needle_every": max(args.needle_every, 5)}),
            (
                "large_file_256k",
                {**base, "large_file_bytes": max(args.large_file_bytes, 256 * 1024)},
            ),
        ]

    cases = []
    agent_values = [args.agents, args.agents * 2]
    file_values = [args.files_per_agent, args.files_per_agent * 2]
    payload_values = sorted({args.payload_bytes, max(args.payload_bytes, 4096)})
    needle_values = sorted({args.needle_every, max(args.needle_every, 5)})
    large_file_values = sorted({args.large_file_bytes, max(args.large_file_bytes, 256 * 1024)})
    for agents in agent_values:
        for files_per_agent in file_values:
            for payload_bytes in payload_values:
                for needle_every in needle_values:
                    large_file_bytes = args.large_file_bytes
                    name = (
                        f"a{agents}_f{files_per_agent}_p{payload_bytes}"
                        f"_n{needle_every}"
                    )
                    cases.append(
                        (
                            name,
                            {
                                **base,
                                "agents": agents,
                                "files_per_agent": files_per_agent,
                                "payload_bytes": payload_bytes,
                                "needle_every": needle_every,
                                "large_file_bytes": large_file_bytes,
                            },
                        )
                    )
    for large_file_bytes in large_file_values:
        name = f"large_file_{large_file_bytes}"
        cases.append((name, {**base, "large_file_bytes": large_file_bytes}))
    return cases


def run_matrix(args):
    results = []
    for case_name, overrides in matrix_cases(args):
        child = case_args(args, case_name, overrides)
        result = run_single(child)
        results.append(
            {
                "case": case_name,
                "parameters": overrides,
                "result": result,
                "passed": single_passed(result, args.backend),
            }
        )
    return {
        "matrix": args.matrix,
        "backend": args.backend,
        "case_count": len(results),
        "passed": all(row["passed"] for row in results),
        "cases": results,
    }


def validate_args(args):
    if args.agents <= 0:
        raise SystemExit("--agents must be positive")
    if args.files_per_agent <= 0:
        raise SystemExit("--files-per-agent must be positive")
    if args.base_files < 0:
        raise SystemExit("--base-files must be non-negative")
    if args.approvals < 0:
        raise SystemExit("--approvals must be non-negative")
    if args.payload_bytes < 0:
        raise SystemExit("--payload-bytes must be non-negative")
    if args.needle_every <= 0:
        raise SystemExit("--needle-every must be positive")
    if args.cache_repeats < 0:
        raise SystemExit("--cache-repeats must be non-negative")
    if args.cache_workers < 0:
        raise SystemExit("--cache-workers must be non-negative")
    if args.cache_worker_repeats <= 0:
        raise SystemExit("--cache-worker-repeats must be positive")
    if args.large_file_bytes < 0:
        raise SystemExit("--large-file-bytes must be non-negative")
    if args.range_read_count <= 0:
        raise SystemExit("--range-read-count must be positive")
    if args.range_size <= 0:
        raise SystemExit("--range-size must be positive")
    if args.chunk_size <= 0:
        raise SystemExit("--chunk-size must be positive")
    if args.large_range_stride <= 0:
        raise SystemExit("--large-range-stride must be positive")


def main():
    parser = argparse.ArgumentParser(
        description="Benchmark ANFS as an agent-native memory filesystem."
    )
    parser.add_argument("--agents", type=int, default=4)
    parser.add_argument("--files-per-agent", type=int, default=50)
    parser.add_argument("--base-files", type=int, default=4)
    parser.add_argument("--approvals", type=int, default=8)
    parser.add_argument("--payload-bytes", type=int, default=0)
    parser.add_argument("--needle-every", type=int, default=1)
    parser.add_argument("--cache-repeats", type=int, default=0)
    parser.add_argument("--cache-workers", type=int, default=0)
    parser.add_argument("--cache-worker-repeats", type=int, default=2)
    parser.add_argument("--cache-shared-key", action="store_true")
    parser.add_argument("--large-file-bytes", type=int, default=0)
    parser.add_argument("--range-read-count", type=int, default=16)
    parser.add_argument("--range-size", type=int, default=4096)
    parser.add_argument("--chunk-size", type=int, default=65536)
    parser.add_argument("--large-range-stride", type=int, default=65537)
    parser.add_argument("--worker-timeout-s", type=int, default=60)
    parser.add_argument(
        "--backend",
        choices=[
            "anfs",
            "native-jsonl",
            "rag-jsonl",
            "external-vector-jsonl",
            "markdownfs",
            "sqlite-memory",
            "all",
        ],
        default="anfs",
    )
    parser.add_argument(
        "--matrix",
        choices=["quick", "full"],
        help="run a scale sweep over agent count, file count, payload size, and search cardinality",
    )
    parser.add_argument("--workdir")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument(
        "--output-json",
        help="write the benchmark result JSON to this path in addition to stdout",
    )
    args = parser.parse_args()
    validate_args(args)

    if args.matrix:
        result = run_matrix(args)
        passed = result["passed"]
    else:
        result = run_single(args)
        passed = single_passed(result, args.backend)
    result_json = json.dumps(result, indent=2, sort_keys=True)
    if args.output_json:
        output_path = Path(args.output_json)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(result_json + "\n", encoding="utf-8")
    print(result_json)
    if not passed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
