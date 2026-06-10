# Agent Filesystem Design

This document turns the "file systems for agents" direction into an ANFS
design target.

Source context:

- Amplify Partners, "File systems for agents", May 27, 2026:
  https://www.amplifypartners.com/blog-posts/file-systems-for-agents

## Thesis

Agents should interact with storage through a familiar file interface, because
models are good at paths, directories, reading files, writing files, and
searching text. But the implementation below that interface cannot be a normal
filesystem. Agent workloads require stronger coordination, queryability,
execution-aware policy, and replayable memory.

ANFS should therefore be:

```text
A POSIX-like agent memory filesystem backed by a transactional semantic kernel.
```

The filesystem interface is the compatibility layer. The durable substrate is
CAS blobs, immutable nodes, mutable refs, append-only events, lineage edges,
policy decisions, search indexes, and replayable snapshots.

## What We Are Designing

ANFS is not a kernel-level POSIX replacement in the MVP. It is a tool-level and
library-level filesystem for agents:

- path-shaped API: `read`, `write`, `ls`, `stat`, `mkdir`, `touch`, `rm`,
  `cp`, `mv`, `find`, `grep`;
- workspace isolation: each agent writes in its own namespace;
- shared memory namespace: approved or merged refs become reusable memory;
- transactional metadata: ref updates, events, edges, and audit rows commit
  together;
- content-addressed immutable data: bytes are deduped and replayable;
- search-as-context: search results are recorded as model-visible evidence;
- policy-aware transitions: approve, reject, archive, merge, consume, and GC are
  explicit operations;
- replay: any important event can materialize the file view that existed at that
  point.

## Requirements From Agent Workloads

### 1. Familiar File Interface

Agents should not need to reason about database tables for normal work. The
default surface should look like a filesystem:

```text
read("src/main.py")
write("notes/summary.md", bytes)
grep("refund policy", "contracts/")
find("memory/", "*.md")
```

Internally, these calls produce semantic events and typed edges.

### 2. Concurrency And Isolation

Many agents will write concurrently. The system must answer:

- What happens when two agents edit the same file?
- Can one agent safely experiment without affecting shared state?
- Can a set of file changes merge atomically?
- Can conflicts be detected before shared memory is mutated?

Design:

- every agent gets a workspace namespace, such as `ws:agent-7/...`;
- shared memory lives under resource/artifact namespaces, such as
  `resource:repo/main@v1/...`;
- workspace writes are copy-on-write ref updates;
- `merge_workspace(base, workspace)` applies non-conflicting changes atomically;
- conflict detection compares base snapshot, current base, and workspace state;
- event sequence provides a total order for audit and replay.

### 3. Query And Materialization

Agent retrieval is not a single RAG call. Agents iteratively select, filter,
compose, and materialize working sets.

Design:

- `find` and `grep` are first-class POSIX-like search tools;
- FTS and future embedding indexes attach to immutable nodes;
- search calls produce `search` events with result node edges;
- materialized views can reconstruct a namespace at a specific event;
- future query APIs should support structured filters over path, media type,
  state, agent, run, event kind, timestamps, and policy labels.

Target API direction:

```python
ws.find("contracts", "*.md")
ws.grep("termination", "contracts")
fs.query_refs(prefix="resource:contracts", state="approved", media_type="text/markdown")
fs.materialize_ref_view_at_event(event_id, output_dir, prefix="resource:repo/main@v1")
```

### 4. Dynamic Access Control

Traditional ACLs answer "can this user open this file?" Agents need stronger
questions:

- Can this agent combine document A with document B?
- Can this retrieved field be written into an output?
- Can intermediate state be persisted?
- Can this evidence approve that artifact?
- Can this memory be exported or replayed?

Design:

- reads/consumes are recorded with exact ref versions;
- policy decisions are append-only events;
- approval requires lineage evidence;
- search result nodes are linked into events, so later answers can be audited;
- future labels can attach to refs/nodes for sensitivity, retention, or use
  restrictions;
- future policy checks should run during search, merge, publish, approve,
  export, and replay.

### 5. Small Files, Large Working Sets

Agents work with many small files today and larger working sets over time. The
system needs low-latency small operations and efficient scans.

Design:

- inline blobs under 64 KiB avoid filesystem round trips for small text;
- file-backed blobs handle larger content;
- refs are indexed by path-like names;
- search scans inline content without reopening each object;
- future work should add prefix-range scans, chunk indexes for large files, and
  cached materialized working sets.

## Architecture

```text
Agent tools / SDK
    read write ls stat find grep cp mv rm
        |
POSIX-like workspace facade
        |
Semantic kernel
    refs          mutable view
    nodes         immutable content records
    blobs         CAS bytes
    events        append-only operation log
    event_edges   lineage and context edges
    ref_events    per-ref lifecycle audit
    policies      append-only decisions
        |
Indexes and materializers
    FTS / future vectors / path indexes / replay
        |
Local-first storage
    SQLite WAL + object files
```

## Core Objects

### Blob

Content-addressed bytes. Immutable.

### Node

Immutable semantic object pointing at a blob, with kind, media type, metadata,
and creation time.

### Ref

Mutable name to node mapping:

```text
ws:coder/src/app.py -> node:abc, state=draft
resource:repo/main@v1/src/app.py -> node:def, state=published
artifact:patch@v1 -> node:xyz, state=approved
```

### Event

Append-only operation fact:

```text
write
read_ref
search
publish
merge_workspace
approve
delete_ref
checkout
snapshot_namespace
gc_collect
```

### Edge

Typed input/output relationship between events and nodes.

### Ref Event

Per-ref lifecycle audit row. This is what lets ANFS explain exactly when a ref
changed, from which node, to which node, and through which event.

## API Shape

### Workspace Tools

```python
ws.read("path")
ws.write("path", b"...")
ws.write_range("path", offset, b"...")
ws.append("path", b"...")
ws.truncate("path", length)
ws.cat("path")
ws.read_range("path", offset, length)
ws.ls("dir")
ws.stat("path")
ws.stat_posix("path")
ws.exists("path")
ws.is_file("path")
ws.is_dir("path")
ws.access("path", mode, uid=None, gid=None, groups=None)
ws.chmod("path", mode)
ws.chown("path", uid, gid)
ws.utime("path", atime_ms=None, mtime_ms=None)
ws.mkdir("dir")
ws.touch("empty.txt")
ws.rm("path", uid=None, gid=None, groups=None)
ws.cp("src", "dst")
ws.mv("src", "dst", uid=None, gid=None, groups=None)
ws.find("dir", "*.md")
ws.grep("needle", "dir")
```

### Memory Lifecycle

```python
ws.publish("src/app.py", "artifact:patch@v1")
fs.approve("artifact:patch@v1", ["artifact:test@v1"], "reviewer")
fs.merge_workspace("resource:repo/main@v1", "ws:coder", "merge_agent")
fs.materialize_ref_view_at_event(event_id, output_dir, prefix="resource:repo/main@v1")
```

### Future Query Surface

```python
fs.query(
    prefix="resource:contracts",
    text="termination",
    state=["published", "approved"],
    policy_label_excludes=["pii"],
    limit=50,
)
```

## Comparison Targets

ANFS should be compared against multiple baselines, because each answers a
different question.

### Native POSIX

Question: Are familiar file operations competitive?

Metrics:

- read/write/ls/find/grep latency;
- many-small-file behavior;
- recursive copy/delete/move behavior.

### Native POSIX Plus JSONL Audit

Question: If we bolt audit onto normal files, do we still need ANFS?

Metrics:

- concurrent write success;
- audit sequence continuity;
- merge/replay latency;
- audit completeness;
- semantic limitations.

### MarkdownFS

Question: Does human-readable file-as-database storage help agents more than
ANFS?

Metrics:

- ease of inspection;
- search quality;
- concurrent update behavior;
- conflict handling;
- audit/replay quality.

Represented by `benchmarks/agent_memory_benchmark.py --backend markdownfs`.
This baseline stores content in ordinary Markdown files and audit entries as
Markdown documents with frontmatter, but it intentionally lacks immutable CAS
nodes, typed causal graph edges, event-time replay, and integrated policy
semantics.

### Local RAG JSONL

Question: Does a sidecar retrieval index over ordinary files remove the need
for an agent-native filesystem?

Metrics:

- retrieval recall;
- index update overhead;
- audit completeness;
- replay quality;
- semantic limitations.

Represented by `benchmarks/agent_memory_benchmark.py --backend rag-jsonl`.
This baseline stores content in ordinary files, appends retrieval documents to a
JSONL index, and searches that index with lexical token matching. It
intentionally lacks immutable CAS nodes, typed causal graph edges, event-time
replay, integrated policy semantics, and vector similarity.

### SQLite Memory Store

Question: Could this just be a database schema?

Metrics:

- query speed;
- schema friction;
- file-tool compatibility;
- replay;
- policy and lineage expressiveness.

Represented by `benchmarks/agent_memory_benchmark.py --backend sqlite-memory`.
This baseline stores workspace/path/content rows in a plain SQLite schema and
records lightweight audit rows, but it intentionally lacks immutable CAS nodes,
typed causal graph edges, event-time replay, and integrated policy semantics.

### LoCoMo / Memory Benchmarks

Question: Does ANFS improve task-level long-term memory?

Metrics:

- answer accuracy/F1;
- retrieval recall@k;
- context tokens used;
- latency and cost;
- evidence citation correctness;
- replayability of answers.

## Benchmark Plan

### System Benchmark

Already represented by `benchmarks/agent_memory_benchmark.py`:

- concurrent multi-agent `write/read/grep`;
- workspace merge;
- approval with lineage evidence;
- replay materialization;
- integrity verification;
- event-sequence continuity;
- audit counts.

### Task Benchmark

Represented locally by `benchmarks/task_memory_benchmark.py`, which provides a
deterministic LoCoMo-like adapter and JSON/JSONL dataset importer:

```text
LoCoMo sessions -> ANFS refs/events
questions -> audited Workspace.query refs/nodes
evidence -> cited answer nodes
answers -> exact-match, token-F1, recall@k, evidence coverage, integrity checks
```

Compare:

- native files + JSONL;
- ANFS raw-turn memory;
- ANFS extracted-memory;
- ANFS hybrid raw + extracted memory;
- LoCoMo-style JSON/JSONL import plus token-F1 evaluator;
- external vector JSONL sidecar baseline;
- future managed vector DB / ANN baseline;
- future model-backed answer generation and extraction variants.

## Current Gaps

- No kernel FUSE mount yet. The current surface is a Python/Rust tool API plus
  ordinary working tree materialization/commit sync with optional manifest base
  leases and `worktree_readiness(...)` preflight, not a mounted POSIX
  filesystem. That ordinary-directory adapter rejects symlinks, special entries,
  backslash-containing relative paths, and the root reserved
  `.anfs-worktree-manifest.json` user path before canonical worktree commits.
- POSIX-like hard links are limited to audited workspace refs that point at the
  same immutable node. Metadata overlays now inherit and synchronize across
  active same-node linked refs, and later writes split paths through new
  immutable nodes. Chmod overlays preserve setuid/setgid/sticky special mode bits
  in `stat_posix(...)`, and explicit-identity `rm`/`delete`/`mv` calls enforce
  sticky-directory deletion/replacement rules. New entries under setgid
  directories inherit parent gid metadata, and new subdirectories also inherit
  the setgid bit. They do not implement setuid or setgid execution semantics;
  ANFS path leases provide audited exclusive agent/run coordination over logical
  paths and directory subtrees, but open-handle lifetime semantics,
  descriptor-level POSIX `fcntl`/`flock`, mmap, ACLs, and FUSE syscall behavior
  remain future work.
- No lazy namespace move yet. Directory `mv` still eagerly rewrites subtree
  refs, even though it batches write/delete events. `cp` and `mv` implement the
  common destination-directory compatibility rule, so files and recursive
  directory trees copied/moved to an existing directory land under
  `dst/<source-basename>`.
- Local embedding projection exists through caller-provided node embeddings,
  cached chunk embeddings, `vector_search(...)`, and
  `vector_search_chunks(...)`. Search is still FTS/text-first plus POSIX
  `grep`/`find`; local RAG JSONL and external vector JSONL sidecar baselines
  are bundled, while managed vector database service and approximate
  nearest-neighbor sweeps remain future work.
- Unified current-ref query exists through `fs.query(...)` for prefix, text,
  state, agent, run, event kind, media type, timestamp, and policy-decision
  filters. Workspace agents can use `Workspace.query(...)` to record structured
  query-as-context events with exact result node edges. Deeper query-time policy
  composition across richer YAML/body semantic field parsing beyond heading
  sections and conservative body blocks remains future work. Basic boolean and
  threshold visibility expressions over active labels are supported through
  `policy_expression_rule` events.
- Durable ref/node policy labels, byte-range fragment policy labels, JSON
  field-to-fragment policy extraction, conservative Markdown frontmatter field
  extraction for scalar fields, nested fields, object/list spans, inline scalar
  sequence items, inline sequence object item fields, inline object scalar
  fields, bounded nested inline object fields, bounded nested inline array item
	  paths, quoted-key bracket paths, tag/anchor-decorated payload spans, alias
	  token spans, conservative alias expansion paths, conservative recursive
	  alias/merge chain expansion paths, conservative merge-array expansion paths,
	  scalar alias target paths including chained scalar alias targets,
	  block-sequence alias/anchor/merge bookkeeping, YAML null scalar kind spans,
	  explicit YAML core tag kind overrides including timestamp/binary tags,
  explicit set/omap container tag kinds, and conservative unquoted ISO
  timestamp scalar inference, quoted scalar payload spans that exclude quote
  delimiters while preserving string kind unless an explicit core tag overrides
  it, decoded quoted scalar semantic values through `markdown_field_values(...)`,
  and block scalars,
  Markdown body ATX/Setext heading section extraction, and
  conservative Markdown
  paragraph/paragraph-line/inline-strong/inline-strong-text/
  inline-emphasis/inline-emphasis-text/inline-link/inline-link-label/
  inline-link-destination/inline-link-title/reference-link/reference-link-label/
  reference-link-reference/reference-link-resolved-destination/reference-link-resolved-title/inline-image/inline-image-alt/
  inline-image-destination/inline-image-title/reference-image/reference-image-alt/
  reference-image-reference/reference-image-resolved-destination/reference-image-resolved-title/inline-code/autolink/autolink-target/list/list-item/task-checkbox/blockquote/
  blockquote-line/table/table-align/table-row/table-cell/code/html/
  link-reference/link-reference-label/link-reference-destination/
  link-reference-title/thematic-break body block extraction exist.
  Inline strong, inline emphasis, inline link, reference-link, inline image,
  reference-image, and autolink extraction suppress spans that overlap
  conservative inline-code ranges, and current conservative inline parsers
  ignore delimiter bytes escaped by an odd number of immediately preceding
  backslashes. Inline-code extraction supports same-line matching backtick runs
  of one or more backticks. Direct inline link/image destination extraction
  supports balanced unescaped parentheses inside destinations and
  angle-bracketed destination payload spans; link-reference-definition
  destination extraction also supports angle-bracketed payload spans. Direct
  inline/reference title extraction supports trailing quoted or parenthesized
  forms. Reference label matching normalizes backslash-escaped ASCII
  punctuation while preserving raw label byte spans for policy and audit.
  Duplicate link-reference definitions resolve to the first normalized
  definition, while later duplicate definitions still expose raw component
  spans for audit. Link-reference definitions support immediate next-line
  title-only continuations for quoted or parenthesized titles.
  Exact byte-identical copies of labeled node bytes inherit active
  fragment labels through new audit events. Explicit derived writes and answer
  nodes inherit source policy labels conservatively: source node labels become
  output node labels, and transformed source fragment labels become full-output
  fragment labels. Explicit partial-output attribution can propagate source
  fragment labels to caller-provided output byte ranges, exact-byte automatic
  attribution can propagate unique literal matches, and normalized JSON scalar
  attribution can propagate unique string, boolean, null, and finite number
  scalar matches. Normalized JSON value attribution can propagate unique
  minified JSON array/object value matches.
  Full YAML parsing, deeper Markdown AST extraction, and complex cross-format
  field mapping remain future work. Current policy checks cover
  ref/node/fragment/event/lifecycle level.
- Structured current-ref query, search-as-context, answer construction, replay
  materialization, bundle export/import, and working tree sync can exclude
  active ref/node, JSON-field, Markdown-frontmatter-field,
  Markdown-heading/body-block, and denied fragment policy labels, exact-byte
  automatic attribution, and normalized JSON scalar attribution for strings,
  booleans, nulls, and finite numbers plus normalized JSON array/object value
  attribution, but policies do not yet compose through
  full YAML semantic field parsing, deeper Markdown AST extraction, or broad
  semantic/cross-format attribution extraction. Basic `label` / `all` / `any` / `not` / `at_least`
  policy expressions over active labels are supported; lifecycle/review
  operation capability rules cover `approve`, `reject_ref`, and `archive_ref`;
  broader downstream execution sandbox controls remain future work.
- A local LoCoMo-like task-memory benchmark adapter exists through
  `benchmarks/task_memory_benchmark.py`: sessions become published memory refs,
  extracted fact memories can become derived refs with lineage to raw session
  nodes, questions use audited query events, answers cite retrieved refs, and
  metrics include recall@k, exact match, token-F1, derived lineage coverage,
  evidence coverage, context bytes, and integrity verification. JSON/JSONL
  LoCoMo-style dataset import exists. Official dataset snapshots, model-based
  answer generation, model-backed memory extraction, and task-memory vector DB
  comparisons remain future work.
- MarkdownFS baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend markdownfs`.
- Local RAG JSONL retrieval baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend rag-jsonl`.
- External vector JSONL sidecar baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend external-vector-jsonl`; it
  uses deterministic hashed embeddings and cosine search outside the kernel,
  not a managed vector database service.
- SQLite memory-store baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend sqlite-memory`.
- Large files are stored as file-backed blobs and support selective range reads,
  path-level `read_range(...)`, path-level `write_range(...)` compatibility
  edits backed by new immutable nodes including zero-fill extension beyond EOF,
  path-level `append(...)`, path-level `truncate(...)` shrink/NUL-extension
  compatibility, derived chunk maps, persistent chunk indexes, and
  caller-provided chunk embedding projections over cached chunk rows. The
  agent-memory benchmark can measure range reads, chunk derivation, chunk-cache
  build, and chunk-cache reads through `--large-file-bytes`. A local 256 KiB
  large-file snapshot, local
  quick/full matrix sweeps, an ANFS-only 1 MiB range/chunk/cache snapshot, and a
  4-agent / 256-file ANFS-only large-collection snapshot are published under
  `docs/benchmark_snapshots`; release-machine and heavier-parameter sweep
  snapshots remain future work.
- Cached materialized working sets exist, and the agent-memory benchmark can
  measure repeated cache miss/hit reuse through `--cache-repeats` and
  cache contention through `--cache-workers`, including same-key contention
  with `--cache-shared-key`. A local cache reuse/contention snapshot, a local
  quick matrix sweep, and a four-worker same-key contention snapshot are
  published under `docs/benchmark_snapshots`; larger hit-rate and contention
  sweeps remain future work.
- Shared-ref contention is covered by a multi-process stale-version regression
  and `benchmarks/ref_contention_benchmark.py`: racing reviewers with the same
  `expected_version` produce exactly one committed lifecycle update, conflict
  losers, contiguous event sequences, and clean integrity. Lifecycle decisions
  acquire immediate write transactions before reading `ref_version`, so
  contended losers surface `RefConflictError` rather than SQLite lock errors.
- No distributed coordinator yet; current MVP is local-first SQLite.
- No cross-machine shared namespace protocol yet.
- Explicit workspace fork exists through `fork_workspace(...)`, built on refs,
  immutable nodes, and typed fork edges. Workspace/branch ids are validated as
  `ws:<ascii-name>` before public lifecycle operations record events or refs.
  Richer branch lifecycle and remote branch coordination remain future work.
- Purpose-specific read/consume/search/query enforcement exists when callers
  declare a purpose. Purpose capability rules can also require the calling
  workspace agent to hold an append-only capability before using a protected
  purpose. This is explicit-purpose authorization, not a full downstream
  execution sandbox.
- No model-backed first-class memory extraction service. The local task-memory
  benchmark can synthesize extracted memory refs as derived nodes with lineage
  to raw turns/artifacts, but ANFS does not yet extract structured memories from
  arbitrary real conversations.
- Answer-citation API exists for cited refs/nodes and optional prior
  search/query event ids. `answer_evidence_coverage(...)` exposes retrieval
  coverage for cited refs/nodes, and `answer_quote_support(...)` exposes
  deterministic exact-quote support. Semantic entailment and model-based
  citation quality scoring remain future work.
- Deterministic token-estimate accounting exists for answer construction and
  cited retrieval context through `answer_token_accounting(...)`. Append-only
  token cost profiles let callers supply model pricing and prompt overhead, and
  `answer_cost_estimate(...)` returns auditable integer cost estimates from the
  active profile. Exact vendor tokenizer parity and provider price freshness
  remain adapter-level concerns.
- Benchmark scale sweep support exists for agent count, file count, payload
  size, and search result cardinality through
  `benchmarks/agent_memory_benchmark.py --matrix quick|full`. A local
  multi-backend benchmark snapshot, a local quick matrix snapshot, an ANFS full
  local matrix snapshot, and a multi-backend full local matrix snapshot are
  published under `docs/benchmark_snapshots`. A local shared-ref contention
  snapshot exists through `benchmarks/ref_contention_benchmark.py`, and checked
  worktree commit contention is covered by a spawned-process regression; larger
  release-machine snapshots and longer contention soak jobs remain future work.
- Read-only `compaction_plan()` reports event/ref history size, inactive refs,
  GC pressure, inline blob pressure, orphan object files, active pins, active
  retention policies, derived projection rows, and SQLite freelist pages.
  `vacuum_database(dry_run=True)` adds narrow SQLite freelist maintenance with
  dry-run by default. `run_retention_policy(...)` automates existing bounded
  local GC from append-only retention rules. `compact_inline_blobs(dry_run=True)`
  verifies inline blob size/hash and moves bytes to file-backed CAS object
  storage without changing node/ref/event identity. `clean_orphan_objects(...)`
  removes verified noncanonical CAS object files left by aborted writes or
  interrupted storage maintenance without mutating SQLite rows.
  `export_history_archive(...)` exports the complete append-only event/ref
  history through the latest event sequence into a checksummed/signable bundle
  without mutating canonical rows. `create_ref_view_checkpoint(...)` records an
  immutable replay proof for a target event boundary and
  `verify_ref_view_checkpoint(...)` recomputes the ref-view checksum.
  `archival_readiness_plan(...)` verifies that a history archive bundle covers
  current event/ref history and that the checkpoint is still valid. Physical
  event/ref archival compaction remains future admin-mode work.
- Strict schema guards reject unsupported future schema versions before normal
  initialization, and `schema_status()` / `require_schema_current()` expose
  migration state for admin checks. `schema_migration_plan()` and
  `apply_schema_migrations(dry_run=True)` provide explicit safe migration
  planning/execution for the supported schema. Future versioned upgrade steps
  remain future work.

## Design Principle

Keep the interface boring and familiar:

```text
paths, files, directories, grep, find
```

Make the substrate agent-native:

```text
transactions, events, lineage, policy, replay, search, memory
```

That is the point of ANFS.
