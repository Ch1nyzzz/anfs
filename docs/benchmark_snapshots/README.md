# Benchmark Snapshots

This directory stores local benchmark evidence for the agent-native filesystem
requirements matrix. Timings are machine-local and should not be read as
portable performance claims; the important evidence is which invariants were
checked, which backends were compared, and whether the checks passed.

## 2026-06-09 Local Agent-Memory Snapshot

Raw JSON:

- `agent_memory_local_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend all \
  --agents 2 \
  --files-per-agent 20 \
  --base-files 2 \
  --approvals 4 \
  --cache-repeats 3 \
  --cache-workers 2 \
  --cache-worker-repeats 2 \
  --large-file-bytes 262144 \
  --output-json docs/benchmark_snapshots/agent_memory_local_snapshot.json
```

Scenario coverage:

- Concurrent multi-agent read/write/grep.
- Workspace merge into shared base namespace.
- Lineage approval.
- Replay materialization.
- Event sequence continuity.
- Integrity verification.
- Working-set cache miss/hit reuse.
- Concurrent cache materialization with per-worker cache keys.
- Large-file range reads, chunk derivation, chunk-cache build, and chunk-cache
  readback.

Summary:

| Backend | Passed | Total ms | Events | Cache hits | Contention hits | Large bytes | Cached chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ANFS | yes | 385.42 | 117 | 2 | 2 | 262144 | 4 |
| native-jsonl | yes | 162.54 | 114 | 2 | 2 | 262144 | 4 |
| rag-jsonl | yes | 167.85 | 114 | 2 | 2 | 262144 | 4 |
| external-vector-jsonl | yes | 172.79 | 114 | 2 | 2 | 262144 | 4 |
| markdownfs | yes | 174.49 | 114 | 2 | 2 | 262144 | 4 |
| sqlite-memory | yes | 126.77 | 114 | 2 | 2 | 262144 | 4 |

Interpretation:

ANFS passes the full invariant suite while carrying stronger semantics than the
baselines: immutable CAS nodes, typed causal event edges, ref audit rows,
event-time replay, integrated policy gates, managed working-set cache records,
and kernel-verified chunk-cache projections. The native, local RAG JSONL,
external vector JSONL sidecar, MarkdownFS, and SQLite baselines remain useful
comparisons, but the snapshot records their semantic limitations in the raw
JSON.

## 2026-06-09 Local Agent-Memory Quick Matrix Snapshot

Raw JSON:

- `agent_memory_quick_matrix_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend all \
  --matrix quick \
  --agents 2 \
  --files-per-agent 16 \
  --base-files 2 \
  --approvals 4 \
  --payload-bytes 256 \
  --needle-every 2 \
  --cache-repeats 2 \
  --cache-workers 2 \
  --cache-worker-repeats 2 \
  --large-file-bytes 131072 \
  --range-read-count 8 \
  --range-size 2048 \
  --output-json docs/benchmark_snapshots/agent_memory_quick_matrix_snapshot.json
```

Scenario coverage:

- Baseline multi-backend workflow.
- Agent count scaling from 2 to 4 agents.
- File count scaling from 16 to 32 files per agent.
- Payload scaling from 256 bytes to 4 KiB.
- Sparse search cardinality with `needle_every=5`.
- Large-file scaling from 128 KiB to 256 KiB.
- Working-set cache reuse and two-worker cache contention in every case.

Summary:

| Case | Passed | Agents | Files/agent | Payload bytes | Needle every | Large bytes | ANFS total ms | ANFS events | Cache hits | Contention hits | Cached chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | yes | 2 | 16 | 256 | 2 | 131072 | 295.35 | 101 | 1 | 2 | 2 |
| agents_x2 | yes | 4 | 16 | 256 | 2 | 131072 | 339.62 | 173 | 1 | 2 | 2 |
| files_x2 | yes | 2 | 32 | 256 | 2 | 131072 | 354.77 | 165 | 1 | 2 | 2 |
| payload_4k | yes | 2 | 16 | 4096 | 2 | 131072 | 301.33 | 101 | 1 | 2 | 2 |
| sparse_search | yes | 2 | 16 | 256 | 5 | 131072 | 269.30 | 101 | 1 | 2 | 2 |
| large_file_256k | yes | 2 | 16 | 256 | 2 | 262144 | 321.30 | 101 | 1 | 2 | 4 |

All six backends passed all six cases. The matrix is still local-machine
evidence, but it exercises the scale dimensions called out in the
agent-filesystem requirements matrix rather than a single fixed-size scenario.

## 2026-06-09 Local ANFS Full Matrix Snapshot

Raw JSON:

- `agent_memory_anfs_full_matrix_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend anfs \
  --matrix full \
  --agents 2 \
  --files-per-agent 16 \
  --base-files 2 \
  --approvals 4 \
  --payload-bytes 256 \
  --needle-every 2 \
  --cache-repeats 2 \
  --cache-workers 2 \
  --cache-worker-repeats 2 \
  --large-file-bytes 131072 \
  --range-read-count 8 \
  --range-size 2048 \
  --output-json docs/benchmark_snapshots/agent_memory_anfs_full_matrix_snapshot.json
```

Scenario coverage:

- Full matrix over 2/4 agents.
- Full matrix over 16/32 files per agent.
- Full matrix over 256-byte and 4 KiB payloads.
- Full matrix over dense and sparse search cardinality.
- Separate large-file cases for 128 KiB and 256 KiB.
- Every case verifies write/read recall, grep recall, merge rows, replay rows,
  approval coverage, event sequence continuity, integrity, working-set cache
  reuse, cache contention, and large-file range/chunk/cache behavior.

Summary:

| Case | Passed | Agents | Files/agent | Payload bytes | Needle every | Large bytes | Total ms | Events | Grep recall | Read recall | Merge rows | Cached chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| a2_f16_p256_n2 | yes | 2 | 16 | 256 | 2 | 131072 | 299.68 | 101 | 16 | 32 | 32 | 2 |
| a2_f16_p256_n5 | yes | 2 | 16 | 256 | 5 | 131072 | 271.29 | 101 | 8 | 32 | 32 | 2 |
| a2_f16_p4096_n2 | yes | 2 | 16 | 4096 | 2 | 131072 | 287.33 | 101 | 16 | 32 | 32 | 2 |
| a2_f16_p4096_n5 | yes | 2 | 16 | 4096 | 5 | 131072 | 283.48 | 101 | 8 | 32 | 32 | 2 |
| a2_f32_p256_n2 | yes | 2 | 32 | 256 | 2 | 131072 | 369.77 | 165 | 32 | 64 | 64 | 2 |
| a2_f32_p256_n5 | yes | 2 | 32 | 256 | 5 | 131072 | 375.14 | 165 | 14 | 64 | 64 | 2 |
| a2_f32_p4096_n2 | yes | 2 | 32 | 4096 | 2 | 131072 | 381.01 | 165 | 32 | 64 | 64 | 2 |
| a2_f32_p4096_n5 | yes | 2 | 32 | 4096 | 5 | 131072 | 381.14 | 165 | 14 | 64 | 64 | 2 |
| a4_f16_p256_n2 | yes | 4 | 16 | 256 | 2 | 131072 | 341.05 | 173 | 32 | 64 | 64 | 2 |
| a4_f16_p256_n5 | yes | 4 | 16 | 256 | 5 | 131072 | 342.34 | 173 | 16 | 64 | 64 | 2 |
| a4_f16_p4096_n2 | yes | 4 | 16 | 4096 | 2 | 131072 | 387.16 | 173 | 32 | 64 | 64 | 2 |
| a4_f16_p4096_n5 | yes | 4 | 16 | 4096 | 5 | 131072 | 385.72 | 173 | 16 | 64 | 64 | 2 |
| a4_f32_p256_n2 | yes | 4 | 32 | 256 | 2 | 131072 | 471.64 | 301 | 64 | 128 | 128 | 2 |
| a4_f32_p256_n5 | yes | 4 | 32 | 256 | 5 | 131072 | 468.73 | 301 | 28 | 128 | 128 | 2 |
| a4_f32_p4096_n2 | yes | 4 | 32 | 4096 | 2 | 131072 | 546.02 | 301 | 64 | 128 | 128 | 2 |
| a4_f32_p4096_n5 | yes | 4 | 32 | 4096 | 5 | 131072 | 555.82 | 301 | 28 | 128 | 128 | 2 |
| large_file_131072 | yes | 2 | 16 | 256 | 2 | 131072 | 270.91 | 101 | 16 | 32 | 32 | 2 |
| large_file_262144 | yes | 2 | 16 | 256 | 2 | 262144 | 320.27 | 101 | 16 | 32 | 32 | 4 |

Interpretation:

This snapshot is ANFS-only because it is meant as a release-style kernel proof
artifact rather than a baseline comparison. It verifies the full local matrix
without weakening the invariant checks used by the multi-backend snapshot.

## 2026-06-09 Local Multi-Backend Full Matrix Snapshot

Raw JSON:

- `agent_memory_full_matrix_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend all \
  --matrix full \
  --agents 2 \
  --files-per-agent 16 \
  --base-files 2 \
  --approvals 4 \
  --payload-bytes 256 \
  --needle-every 2 \
  --cache-repeats 2 \
  --cache-workers 2 \
  --cache-worker-repeats 2 \
  --large-file-bytes 131072 \
  --range-read-count 8 \
  --range-size 2048 \
  --output-json docs/benchmark_snapshots/agent_memory_full_matrix_snapshot.json
```

Scenario coverage:

- Same 18 full-matrix cases as the ANFS-only full matrix.
- Six backends: ANFS, native JSONL, local RAG JSONL, external vector JSONL,
  MarkdownFS, and SQLite memory-store.
- Baseline semantic limitations are recorded in the raw JSON for each backend.

Summary:

| Backend | Passed cases | Min total ms | Max total ms |
| --- | ---: | ---: | ---: |
| ANFS | 18 / 18 | 268.45 | 548.92 |
| native-jsonl | 18 / 18 | 154.48 | 231.51 |
| rag-jsonl | 18 / 18 | 159.65 | 278.14 |
| external-vector-jsonl | 18 / 18 | 160.92 | 284.83 |
| markdownfs | 18 / 18 | 159.66 | 244.63 |
| sqlite-memory | 18 / 18 | 124.78 | 168.44 |

Interpretation:

This is the broader baseline companion to the ANFS-only full matrix. Every
backend passes its own correctness checks across the full local matrix; the raw
JSON keeps the distinction between baseline pass/fail and ANFS-specific kernel
semantics such as immutable CAS nodes, typed causal edges, integrated policy
gates, event-time replay, and kernel integrity verification.

## 2026-06-09 Local ANFS 1 MiB Large-File Snapshot

Raw JSON:

- `agent_memory_large_file_1m_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend anfs \
  --agents 2 \
  --files-per-agent 8 \
  --base-files 2 \
  --approvals 2 \
  --cache-repeats 1 \
  --cache-workers 1 \
  --cache-worker-repeats 1 \
  --large-file-bytes 1048576 \
  --range-read-count 32 \
  --range-size 8192 \
  --chunk-size 65536 \
  --output-json docs/benchmark_snapshots/agent_memory_large_file_1m_snapshot.json
```

Scenario coverage:

- 1 MiB file-backed node write.
- Thirty-two 8 KiB range reads at deterministic offsets.
- 64 KiB chunk derivation over the full node.
- Persistent chunk-cache build and cached chunk-row readback.
- Normal ANFS write/read/search, merge, approval, replay, working-set cache, and
  integrity checks around the large-file path.

Summary:

| Backend | Passed | Total ms | Integrity issues | Large bytes | Range reads | Range OK | Chunk size | Derived chunks | Cached chunks |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ANFS | yes | 970.46 | 0 | 1048576 | 32 | 32 | 65536 | 16 | 16 |

Interpretation:

This is an ANFS-only proof snapshot for the large-file requirement. It is not a
portable performance claim; it verifies that file-backed blobs, range reads,
derived chunk maps, persistent chunk caches, replay, and kernel integrity remain
correct at 1 MiB, beyond the existing 128 KiB / 256 KiB local matrix cases.

## 2026-06-09 Local ANFS Large-Collection Snapshot

Raw JSON:

- `agent_memory_large_collection_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend anfs \
  --agents 4 \
  --files-per-agent 64 \
  --base-files 4 \
  --approvals 8 \
  --payload-bytes 1024 \
  --needle-every 4 \
  --cache-repeats 1 \
  --cache-workers 2 \
  --cache-worker-repeats 1 \
  --large-file-bytes 262144 \
  --range-read-count 8 \
  --range-size 4096 \
  --chunk-size 65536 \
  --output-json docs/benchmark_snapshots/agent_memory_large_collection_snapshot.json
```

Scenario coverage:

- Four concurrent agent workspaces.
- Sixty-four files per agent, for 256 agent-written refs.
- Sparse search with every fourth file containing the query needle.
- Merge of 256 workspace refs into the shared base namespace.
- Replay of 260 files, working-set cache materialization of 68 files, two-worker
  cache contention, large-file range/chunk/cache checks, event sequence
  continuity, and kernel integrity verification.

Summary:

| Backend | Passed | Total ms | Events | Edges | Reads OK | Grep OK | Merge rows | Replay rows | Integrity issues |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ANFS | yes | 867.69 | 585 | 1485 | 256 / 256 | 64 / 64 | 256 | 260 | 0 |

Interpretation:

This is an ANFS-only local collection-scale proof. It increases the checked
agent-written file count beyond the existing local full matrix while preserving
the same correctness gates: typed events and edges, ref audit rows, replay,
cache materialization, and clean integrity.

## 2026-06-09 Local Same-Key Cache Contention Snapshot

Raw JSON:

- `agent_memory_same_key_contention_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py \
  --backend all \
  --agents 2 \
  --files-per-agent 20 \
  --base-files 2 \
  --approvals 4 \
  --payload-bytes 256 \
  --needle-every 2 \
  --cache-repeats 2 \
  --cache-workers 4 \
  --cache-worker-repeats 3 \
  --cache-shared-key \
  --large-file-bytes 131072 \
  --range-read-count 8 \
  --range-size 2048 \
  --output-json docs/benchmark_snapshots/agent_memory_same_key_contention_snapshot.json
```

Scenario coverage:

- Four concurrent cache workers target the same cache key.
- Each worker attempts three materializations for the same workspace.
- Every backend also runs the normal write/read/search, merge, approval,
  replay, and large-file checks.
- ANFS verifies that the managed cache registry converges to one shared
  materialized working set for the contended key.

Summary:

| Backend | Passed | Total ms | Workers | Attempts | Misses | Hits | File counts | ANFS cache rows |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| ANFS | yes | 346.01 | 4 | 12 | 1 | 11 | `[22, 22, 22, 22]` | 1 |
| native-jsonl | yes | 171.05 | 4 | 12 | 1 | 11 | `[22, 22, 22, 22]` | n/a |
| rag-jsonl | yes | 173.59 | 4 | 12 | 1 | 11 | `[22, 22, 22, 22]` | n/a |
| external-vector-jsonl | yes | 175.63 | 4 | 12 | 1 | 11 | `[22, 22, 22, 22]` | n/a |
| markdownfs | yes | 173.46 | 4 | 12 | 1 | 11 | `[22, 22, 22, 22]` | n/a |
| sqlite-memory | yes | 140.83 | 4 | 12 | 4 | 8 | `[22, 22, 22, 22]` | n/a |

Interpretation:

For ANFS, same-key contention is serialized through the managed working-set
cache path: only one miss builds the shared cache, all other attempts reuse it,
all workers observe the expected file count, and the cache registry lists one
row for the contended key. This is stronger evidence than per-worker cache keys
because it exercises a real shared working-set conflict.

## 2026-06-09 Local Shared-Ref Contention Snapshot

Raw JSON:

- `ref_contention_local_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/ref_contention_benchmark.py \
  --workers 16 \
  --rounds 5 \
  --timeout-s 60 \
  --output-json docs/benchmark_snapshots/ref_contention_local_snapshot.json
```

Scenario coverage:

- Sixteen independent processes race to approve the same published ref in each
  round.
- All workers use the same stale `expected_version`.
- Exactly one approval should commit per round; all other workers should return
  `RefConflictError`.
- The benchmark verifies final ref state/version, ref audit history, contiguous
  event sequence allocation, and `verify_integrity()`.

Summary:

| Rounds | Workers/round | Passed rounds | Total approvals | Total conflicts | Total errors | Event count | Sequence contiguous | Integrity issues |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |
| 5 | 16 | 5 | 5 | 75 | 0 | 30 | yes | 0 |

Interpretation:

This snapshot exercises the shared-state arbitration path directly: ANFS
serializes lifecycle writers through immediate transactions, the winner updates
the ref from `published` to `approved`, and losing writers observe a stale
version conflict rather than silently overwriting state or surfacing SQLite lock
errors.

## 2026-06-09 Local Task-Memory Snapshot

Raw JSON:

- `task_memory_local_snapshot.json`

Command:

```bash
.venv/bin/python benchmarks/task_memory_benchmark.py \
  --backend all \
  --memory-mode all \
  --sessions 24 \
  --questions 12 \
  --top-k 3 \
  --output-json docs/benchmark_snapshots/task_memory_local_snapshot.json
```

Scenario coverage:

- LoCoMo-like conversation sessions stored as memory refs.
- Raw, extracted, and hybrid memory modes.
- Audited retrieval through `Workspace.query(...)`.
- Answer nodes that cite retrieved refs.
- Extracted memory facts with ANFS lineage back to raw session nodes.
- Retrieval recall@k, exact-match, and token-F1 scoring.
- ANFS answer evidence coverage and integrity verification.

The checked-in snapshot uses deterministic synthetic sessions. The same
benchmark can import a LoCoMo-style JSON/JSONL dataset with
`--dataset-path path/to/locomo.json`; official dataset snapshots are not stored
in this repository.

Summary:

| Backend | Mode | Passed | Total ms | Recall@k | Exact match | Token-F1 | Covered answers | Lineage links | Context bytes |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ANFS | raw | yes | 45.33 | 1.00 | 1.00 | 1.00 | 12 | 0 | 1916 |
| native-jsonl | raw | yes | 4.93 | 1.00 | 1.00 | 1.00 | 0 | 0 | 1916 |
| ANFS | extracted | yes | 61.43 | 1.00 | 1.00 | 1.00 | 12 | 24 | 1656 |
| native-jsonl | extracted | yes | 5.16 | 1.00 | 1.00 | 1.00 | 0 | 0 | 1656 |
| ANFS | hybrid | yes | 64.24 | 1.00 | 1.00 | 1.00 | 12 | 24 | 1656 |
| native-jsonl | hybrid | yes | 7.13 | 1.00 | 1.00 | 1.00 | 0 | 0 | 1656 |

Interpretation:

Both backends answer the deterministic synthetic questions, but only ANFS
proves that the retrieved context is represented as query event edges, answers
cite versioned memory refs, extracted facts carry derived lineage back to raw
sessions, evidence coverage is queryable, and the resulting event/ref/evidence
graph passes kernel integrity verification. Native JSONL remains a speed and
compatibility comparison without those semantics.
