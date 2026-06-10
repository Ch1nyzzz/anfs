# ANFS

ANFS is an Agent-Native File System prototype.

The current implementation is a local-first Rust kernel exposed to Python with
PyO3. It stores immutable content in a CAS blob store, records immutable nodes
and causal events in SQLite, exposes mutable lifecycle refs, and enforces
lineage validation in Rust before approval transitions can commit.

The design target is a POSIX-like agent memory filesystem backed by a
transactional semantic kernel; see
[`docs/05_agent_filesystem_design.md`](docs/05_agent_filesystem_design.md).

## Current MVP

Implemented:

- Rust `anfs_core` native extension.
- SQLite schema for blobs, nodes, refs, events, event edges, ref audit, FTS.
- WAL mode and foreign keys.
- Immutable-table triggers for core append-only tables.
- Explicit append-only event ordering through `event_sequence`.
- DB-backed event sequence allocation with SQLite writer-lock waiting for
  concurrent engine handles.
- CAS blob storage with inline/file-backed split.
- Workspace `write`, `publish`, `consume`, `delete`.
- POSIX-like workspace facade through `read` / `cat`, `read_range`,
  `write_range`, `append`, `truncate`, `ls`, `stat`, `stat_posix`, `exists`,
  `is_file`, `is_dir`, `access`, `chmod`, `chown`, `utime`, `mkdir`, `touch`,
  `rm`, `cp`, `link`, `mv`, `find`, and `grep`.
- Workspace `find` and `grep` are uncapped by default, with optional explicit
  result limits for tool callers that need bounded context.
- Audited workspace structured query through `Workspace.query(...)`, which
  records query-as-context events with exact result node edges.
- Search-as-context label exclusion through
  `Workspace.search(..., policy_label_excludes=[...])`.
- Answer-as-artifact construction through `Workspace.answer(...)`, which writes
  an immutable answer node and records cited evidence refs as typed input edges.
- Answer evidence coverage inspection through
  `answer_evidence_coverage(answer_event_id)`.
- Deterministic answer/retrieval token-estimate accounting through
  `answer_token_accounting(answer_event_id)`.
- Deterministic answer quote-support inspection through
  `answer_quote_support(answer_event_id, min_quote_bytes=16)`.
- Recursive workspace directory `rm`, `cp`, and `mv` over ANFS refs, preserving
  immutable nodes and writing per-ref audit rows; directory `mv` batches event
  records while preserving exact per-ref edges.
- Workspace `search` records search-as-context events with result node edges.
- Full-text `search(...)` and `query(..., text=...)` treat caller input as
  literal text, so ordinary file contents such as identifiers or SSNs do not
  break SQLite FTS parsing.
- Engine `checkout`, `fork_workspace`, `open_workspace`, `approve`,
  `reject_ref`, `archive_ref`, `get_ref`, `read_node`.
- Selective node and path access through `read_node_range(...)` and
  `Workspace.read_range(...)`, derived chunk maps through `node_chunks(...)`,
  and persistent chunk indexes through
  `cache_node_chunks(...)` / `cached_node_chunks(...)`.
- Local embedding projection through `set_node_embedding(...)`,
  `node_embedding(...)`, and `vector_search(...)`.
- Chunk embedding projection through `set_node_chunk_embedding(...)`,
  `node_chunk_embedding(...)`, and `vector_search_chunks(...)` over cached
  `node_chunk_index` rows.
- JSON, Markdown frontmatter, and Markdown body heading/body block spans /
  field-policy labels through `json_field_spans(...)`,
  `markdown_field_spans(...)`, `markdown_field_values(...)`,
  `markdown_section_spans(...)`,
  `set_json_field_policy_label(...)`, `set_markdown_field_policy_label(...)`,
  and `set_markdown_section_policy_label(...)`.
- Optional expected-ref-version guards for approve, reject, and archive
  decisions.
- Review-separation policy denies producers approving or rejecting their own
  artifacts.
- First-class run lifecycle metadata through `start_run`, `finish_run`,
  `get_run`, and `runs`.
- Optional `run_id` context propagation for workspace, review, merge, and search
  events.
- Observability and replay queries through `events(...)`,
  `ref_history(ref_name)`, `event(event_id)`, and
  `ref_view_at_event(event_id, prefix=None)`.
- Replay materialization through
  `materialize_ref_view_at_event(event_id, output_dir, prefix=None,
  overwrite=False)`.
- Replay ref views and materialization can exclude active policy labels through
  `policy_label_excludes`.
- Existing-filesystem compatibility MVP through `materialize_workspace(...)`
  and `commit_worktree(...)`: materialize a workspace to a normal directory,
  edit with ordinary tools, then scan and commit file diffs back into ANFS refs.
- Cached materialized working sets through `cache_materialized_workspace(...)`
  and `cached_working_sets(...)`.
- Portable causal bundle export through
  `export_event_bundle(event_id, output_dir, include_blobs=True,
  policy_label_excludes=None, signing_key=None, signer_id=None)`.
- Portable run bundle export through
  `export_run_bundle(run_id, output_dir, include_blobs=True,
  policy_label_excludes=None, signing_key=None, signer_id=None)`.
- Portable full-history archive export through
  `export_history_archive(output_dir, include_blobs=True,
  policy_label_excludes=None, signing_key=None, signer_id=None)`.
- Replay checkpoints through
  `create_ref_view_checkpoint(event_id=None, prefix=None,
  include_inactive=False, agent_id="checkpoint_agent")`,
  `ref_view_checkpoints()`, and `verify_ref_view_checkpoint(checkpoint_id)`.
- Read-only archival readiness checks through
  `archival_readiness_plan(checkpoint_id, bundle_path, signature_key=None,
  require_signature=False)`.
- Portable causal bundle import through
  `import_event_bundle(bundle_path, bundle_objects_dir=None,
  policy_label_excludes=None, signature_key=None,
  require_signature=False)`.
- Multi-node artifact manifests through `publish_manifest()` and
  `manifest_children()` / `manifest_child_records()`.
- Whole-namespace immutable snapshots through `snapshot_namespace()`.
- Prefix-based checkout projection from published/approved base refs into a
  draft workspace namespace.
- Checkout base snapshot records in `workspace_base_refs`.
- Integrity verifier for foreign keys, blob storage, blob hashes, ref states,
  and ref lifecycle audit rows.
- Schema migration status and strict future-schema guards through
  `schema_status()` and `require_schema_current()`.
- Read-only operational compaction planning through `compaction_plan()`.
- Read-only GC reachability analysis through `gc_roots()` and
  `gc_candidates(older_than_ms=None, limit=None)`.
- Controlled physical GC for unreachable file-backed blobs through
  `collect_garbage(older_than_ms=None, limit=None)`.
- Policy-driven GC root pinning through `pin_ref()`, `unpin_gc_root()`, and
  `gc_pins()`.
- Retention-policy automation through append-only
  `set_retention_policy(...)` rules and `run_retention_policy(...)`.
- Durable node/ref policy labels through `set_policy_label(...)` and
  `policy_labels(...)`.
- Byte-range fragment policy labels through `set_fragment_policy_label(...)`
  and `fragment_policy_labels(...)`, with exact byte-identical copies inheriting
  active fragment labels through new audit events.
- Explicit partial-output fragment attribution through
  `propagate_fragment_policy_labels(...)`.
- Conservative exact-match automatic fragment attribution through
  `auto_propagate_fragment_policy_labels(...)`, which only propagates source
  labels when a labeled source byte span appears exactly once in the output.
- Conservative normalized-scalar automatic fragment attribution through
  `auto_propagate_fragment_policy_labels_by_normalized_scalar(...)`, which maps
  a uniquely matched JSON string, boolean, null, or finite number scalar to
  normalized Markdown/plain output.
- Conservative normalized JSON value attribution through
  `auto_propagate_fragment_policy_labels_by_normalized_json(...)`, which maps a
  uniquely matched JSON scalar, array, or object through deterministic minified
  JSON value projection.
- Conservative derived-output policy propagation: explicit
  `derived_from_nodes` writes and answer citation nodes propagate source node
  labels, and transformed source fragment labels become full-output fragment
  labels.
- Append-only visibility policy registry through `set_policy_rule(...)`,
  `clear_policy_rule(...)`, `policy_rules(...)`,
  `set_policy_expression_rule(...)`, `clear_policy_expression_rule(...)`, and
  `policy_expression_rules(...)`, including `label`, `all`, `any`, `not`, and
  threshold `at_least` expressions over active labels.
- Append-only purpose policy registry through `set_purpose_policy_rule(...)`,
  `clear_purpose_policy_rule(...)`, and `purpose_policy_rules(...)`, enforced
  when `read_ref(...)`, POSIX-style `read(...)`, `consume(...)`,
  `Workspace.search(...)`, or `Workspace.query(...)` carries an explicit
  purpose.
- Optional explicit-purpose capability authorization through
  `grant_agent_capability(...)`, `revoke_agent_capability(...)`,
  `agent_capabilities(...)`, `set_purpose_capability_rule(...)`,
  `clear_purpose_capability_rule(...)`, and `purpose_capability_rules(...)`.
  Default behavior remains compatible until a purpose requires a capability.
- Operation capability authorization through
  `set_operation_capability_rule(...)`, `clear_operation_capability_rule(...)`,
  and `operation_capability_rules(...)`, enforced for lifecycle/review
  operations such as `approve`, `reject_ref`, and `archive_ref`.
- Read-only workspace diff through `workspace_diff(base, workspace)`.
- Read-only conflict detection through `workspace_conflicts(base, workspace)`.
- Atomic conflict-checked ref merge through `merge_workspace(base, workspace,
  agent_id)`.
- Workspace/branch names are validated at public boundaries: they must use
  `ws:<ascii-name>` with letters, digits, `.`, `-`, or `_`, preventing branch
  ids from colliding with workspace-internal paths.
- Kernel-level typed errors exposed as Python exceptions.
- Recursive lineage check using SQLite `WITH RECURSIVE`.
- Manifest-aware approval: evidence must derive from the target manifest itself
  or cover every child node in the manifest.
- Append-only policy decision log for approval allow/deny decisions through
  `policy_decisions()`.
- Unified current-ref query through `query(...)` with prefix, full-text, state,
  agent, run, event kind, media type, timestamp, and policy-decision filters.
- Query-time label exclusion through `policy_label_excludes`.
- Typed `EventNotFoundError` for event lookup failures.
- Python killer demo proving `test_result@v1` cannot approve `patch@v2`.
- Agent-native filesystem conformance coverage for ordinary filesystem editing
  tools, worktree commit-back, query/search audit, chunk caches, field policy,
  merge, checked worktree contention, and integrity.

## Build

Create a virtual environment:

```bash
python3 -m venv .venv
```

Build and install the extension into the venv:

```bash
env PATH="$HOME/.cargo/bin:$PATH" uv tool run maturin develop
```

Run the demo/test script:

```bash
.venv/bin/python tests/test_demo.py
```

Expected output:

```text
ANFS killer demo passed.
```

Run the agent-memory benchmark:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py
```

Run ANFS against the native POSIX + JSONL audit baseline, the local RAG JSONL
retrieval baseline, the external vector JSONL sidecar baseline, the MarkdownFS
baseline, and the SQLite memory-store baseline:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all
```

Run a quick benchmark matrix over agent count, file count, payload size, and
search-result cardinality:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --matrix quick --backend all
```

Measure cached working-set miss/hit reuse in the same workflow:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all --cache-repeats 3
```

Measure concurrent cached working-set materialization across workers:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all --cache-workers 2 --cache-worker-repeats 2
```

Measure same-cache-key contention:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all --cache-workers 4 --cache-worker-repeats 2 --cache-shared-key
```

Measure large-file range reads and chunk-index build/read behavior:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all --large-file-bytes 1048576
```

Write benchmark output to a durable JSON snapshot:

```bash
.venv/bin/python benchmarks/agent_memory_benchmark.py --backend all --output-json docs/benchmark_snapshots/agent_memory_local_snapshot.json
```

Run the local LoCoMo-like task-memory benchmark:

```bash
.venv/bin/python benchmarks/task_memory_benchmark.py --backend all --memory-mode all
```

Run the same evaluator against a LoCoMo-style JSON/JSONL dataset:

```bash
.venv/bin/python benchmarks/task_memory_benchmark.py \
  --backend all \
  --memory-mode raw \
  --dataset-path path/to/locomo.json \
  --min-recall 0.5 \
  --min-token-f1 0.5
```

This benchmark supports `raw`, `extracted`, and `hybrid` memory modes. Raw mode
publishes conversation-session memories as ANFS refs. Extracted and hybrid
modes write compact fact memories as derived nodes with lineage to raw session
nodes. Questions retrieve evidence with audited `Workspace.query(...)` events,
answers cite retrieved memory refs, and the benchmark checks retrieval recall,
exact match, token-F1, derived lineage coverage, answer evidence coverage, and
kernel integrity. `--dataset-path` accepts JSON objects with
`sessions`/`questions` or JSONL records with `kind="question"` question rows.
The `native-jsonl` comparison can answer the synthetic questions by scanning
files, but reports its missing immutable node graph, typed citation edges,
evidence coverage, lineage graph, and kernel integrity verification.

Current local benchmark snapshots are summarized in
`docs/benchmark_snapshots/README.md`.

The benchmark exercises concurrent multi-agent read/write/grep, workspace
merge, approval with lineage evidence, replay materialization, integrity
verification, and event-sequence continuity. It exits non-zero if recall,
audit, replay, approval, or integrity checks fail.

## Key Semantics

ANFS is not syscall-level audit. It records semantic causality:

- write;
- publish;
- consume;
- approve/reject;
- archive/delete ref;
- checkout;
- search-as-context.

Consume events record the consumed ref name, node id, lifecycle state, ref
version, and purpose so later debugging can identify the exact input version an
agent used even if the ref is later archived or updated.

`Workspace.read_ref(ref_name, purpose=None, tool_call_id=None)` is the controlled
read path for agents. It can read published and approved refs globally, plus
draft refs in the caller's own workspace, and records the exact ref version read.

Normal agent operations are append-only except for `refs`, the mutable view
table. Workspace delete is logical; physical object cleanup only happens through
the controlled GC executor.

`checkout(base="resource:repo/main@v1", workspace="ws:coder")` treats `base` as
a ref namespace prefix and projects refs such as
`resource:repo/main@v1/src/db.py` into `ws:coder/src/db.py`. If `base` names a
published or approved manifest ref, checkout uses that manifest's child paths
instead. Integrity checks verify that each checkout base snapshot row is backed
by the corresponding checkout event and typed `base_source:*` /
`workspace_view:*` edges.

`fork_workspace(source_workspace, target_workspace, agent_id, run_id=None,
tool_call_id=None)` copies the source workspace's current draft refs into a new
target workspace without copying bytes. It records a `fork_workspace` event with
`fork_source:*` input edges and `fork_workspace_view:*` output edges, preserving
copy-on-write behavior through immutable node ids. Existing target refs are
rejected rather than overwritten.

`snapshot_namespace(prefix, snapshot_ref, agent_id, kind="resource",
run_id=None, tool_call_id=None)` freezes the current active refs under a namespace into an
immutable `anfs.manifest.v1` node and publishes `snapshot_ref` to that manifest.
Deleted and archived refs are excluded, while child nodes are linked as event
inputs for lineage and GC reachability.

`verify_integrity()` is diagnostic only. It reports inconsistencies; it does not
repair canonical data or physically delete objects. It verifies blob hashes and
recorded blob sizes, checks that the database has the current kernel schema
migration record and no unsupported future schema versions, and ensures current
mutable refs and runs match their latest append-only audit events. It also
diagnoses ref audit rows whose old/new node ids no longer resolve or whose
audit chain is discontinuous, plus missing, orphaned, duplicated, and stale FTS
rows for searchable text and manifest nodes, validates manifest child metadata
against the referenced
immutable child nodes, checks manifest-producing event edges against manifest
bodies, and checks required lineage edge shapes for core
lifecycle, workspace, and snapshot events. Event-edge directions are also
checked against the lineage domain of `input` and `output`, and core event
kinds are checked for required edge roles. Access and search events are
checked so payloads and typed input edges stay aligned, and
manifest/lifecycle events are checked for their required lineage edges. Policy,
merge, and GC collection audit rows are also checked against their recorded
payload counts and required edge/audit relationships.

`repair_plan()` is the read-only companion to `verify_integrity()`. It returns
`(issue, classification, action, mutable_scope)` rows so operators can separate
rebuildable derived-index problems from blob restoration and manual canonical
audit repair. It does not mutate database rows or object bytes.

`compaction_plan()` is the read-only operational companion to GC and integrity
verification. It returns `(area, count, action, mutable_scope)` rows for
append-only event/ref history size, inactive refs, unreachable blob count and
bytes, inline blob count and bytes, active GC pins, active retention policies,
derived projection rows, and SQLite freelist pages. It does not run GC, VACUUM,
inline compaction, archival, or canonical repair.

`vacuum_database(dry_run=True)` is the narrow physical SQLite maintenance path.
It returns `(freelist_pages_before, freelist_pages_after, ran)`. With the
default dry run it only reports freelist pressure; with `dry_run=False` it runs
SQLite `VACUUM` and then reports the remaining freelist pages. It does not
archive history, mutate canonical rows, repair integrity issues, compact inline
blobs, or collect object bytes.

`compact_inline_blobs(dry_run=True, limit=None)` is the narrow inline storage
maintenance path. It returns `(candidate_count, candidate_bytes, compacted,
dry_run)`. With `dry_run=False`, it verifies each inline blob's recorded size
and SHA-256 hash, writes the bytes to the CAS object directory, and updates only
the blob storage representation from inline to file-backed. Node ids, ref
history, event history, and CAS `hash`/`size` identity remain unchanged.

`clean_orphan_objects(dry_run=True, limit=None)` removes noncanonical CAS object
files left by aborted writes/imports or interrupted storage maintenance. It
returns `(candidate_count, candidate_bytes, removed, dry_run)`. Object files are
eligible only when they are hash-shaped files under the object directory and are
not owned by file-backed `blobs` metadata; execution verifies size and SHA-256
before deleting. It never mutates SQLite rows or audit history.

`schema_status()` returns `(version, name, applied_at, status)` rows for applied
kernel migrations, including `current`, `applied`, `missing_current`,
`current_name_mismatch`, and `future_unsupported` states.
`schema_migration_plan()` returns `(version, name, status, action)` rows so
admins can inspect executable migration work before mutation.
`apply_schema_migrations(dry_run=True)` returns `(planned, applied, dry_run)`;
with `dry_run=False`, it applies only safe supported steps such as recording a
missing current migration row and refuses future versions or current-name
mismatches.
`require_schema_current()` raises `StorageCorruptionError` unless the current
kernel schema is present and no unsupported future version is recorded. Engine
initialization also rejects databases with future schema versions before running
normal schema initialization.

`rebuild_derived_indexes()` is the narrow admin repair path for rebuildable
state. It reconstructs `node_fts` from canonical node bytes and clears persisted
chunk indexes so later `cache_node_chunks(...)` calls regenerate them.

`gc_roots(include_workspaces=True)` reports live root refs.
`gc_candidates(include_workspaces=True, older_than_ms=None, limit=None)` uses
those roots plus lineage ancestry to identify currently unreachable blobs. When
`older_than_ms` is set, a blob is eligible only if every node that references it
was created at or before that millisecond timestamp. `limit` caps the ordered
candidate batch.
`collect_garbage(include_workspaces=True, dry_run=True, agent_id="gc",
older_than_ms=None, limit=None)` dry-runs by default. With `dry_run=False`, it
verifies and removes eligible file-backed object bytes, records an append-only
GC audit event, and leaves immutable blob metadata in place. Inline blobs are
reported but not physically reclaimed. GC audit rows are historical facts, not
permanent CAS tombstones; if the same content hash is written again, the current
object bytes are authoritative again.

`set_retention_policy(policy_name, include_workspaces=True, min_age_ms=None,
limit=None, enabled=True, agent_id="retention_agent", run_id=None)` records an
append-only retention policy rule. `retention_policies(active_only=True)`
derives current enabled rules from event sequence order. `run_retention_policy(
policy_name, dry_run=True, agent_id="retention_agent")` computes the current
age cutoff from `min_age_ms` and then delegates to the same bounded
`collect_garbage(...)` executor. Dry-run remains the default; disabled policies
cannot run.

`publish_manifest(["src/a.py", "src/b.py"], "artifact:patch@v1")` creates a
new immutable manifest node whose JSON body lists child paths, node ids, roles,
media types, content digests, and sizes. The artifact ref points to the manifest
node, and the publish event links each child node as an input to the manifest
output.

`policy_decisions(target_ref=None, policy=None, decision=None,
reason_code=None)` returns append-only policy decision events. Policy payloads
include stable `reason_code` values plus human-readable `reason` text. Failed
approval attempts are recorded before the typed error is returned, so denied
operations are still auditable.

`ref_history(ref_name)` returns the append-only mutation history for one ref,
ordered by ref audit insertion order. `event(event_id)` returns event metadata
and its typed input/output edges. Together they answer "how did this ref become
current?" without exposing raw mutable database access.

`events(after_seq=0, limit=100, kind=None, agent_id=None, workspace_id=None,
run_id=None, created_after_ms=None, created_before_ms=None,
payload_contains=None, tool_call_id=None)` scans the append-only event stream
in `event_sequence` order with stable pagination, filters, and input/output
edge counts.

`read_node_range(node_id, offset, length)` returns a byte slice from an
immutable node. Inline blobs are sliced in memory; file-backed blobs are read
with seek/read so large files do not have to be loaded fully for range access.
`Workspace.read_range(path, offset, length, purpose=None, tool_call_id=None)`
resolves a workspace path or explicit ref first, then applies the same
range-scoped read behavior through the POSIX-like facade.
`Workspace.write_range(path, offset, content, tool_call_id=None)` applies a
path-level `pwrite`-style replacement or append by creating a new immutable node
with lineage from the previous node; writes beyond EOF extend with NUL bytes
rather than creating hidden sparse-hole metadata.
`Workspace.append(path, content, tool_call_id=None)` is a convenience append
facade that creates a missing file or appends at EOF, recording an audited
`append` event and preserving previous-node lineage when a prior file exists.
`Workspace.truncate(path, length, tool_call_id=None)` is a path-level truncate
compatibility helper that shrinks or NUL-extends an existing regular file by
creating a new immutable node with lineage from the previous node.
`node_chunks(node_id, chunk_size=65536)` returns derived chunk rows
`(chunk_index, offset, size, sha256)` for selective retrieval and integrity
planning. Chunk rows are computed from canonical CAS bytes and are not
canonical state. `cache_node_chunks(node_id, chunk_size=65536)` persists the
same derived rows in `node_chunk_indexes` / `node_chunk_index`, while
`cached_node_chunks(node_id, chunk_size=65536)` reads the persisted index
without recomputing it. Integrity verification checks persisted chunk rows
against canonical node bytes.

`AnfsEngine.query(prefix=None, text=None, state=None, agent_id=None, run_id=None,
event_kind=None, media_type=None, created_after_ms=None,
created_before_ms=None, policy=None, decision=None, reason_code=None,
limit=100, policy_label_excludes=None)` returns current refs with their node
metadata, latest ref mutation event summary, and optional FTS snippet. Active
labels on either the ref or node can be excluded through
`policy_label_excludes`. It is a unified read-only surface for structured
filters over current ref views while lower-level APIs such as `events(...)`,
`search(...)`, and `ref_view_at_event(...)` remain available for event streams,
search-as-context logging, and replay.

`Workspace.query(..., tool_call_id=None, policy_label_excludes=None,
purpose=None)` accepts the same structured filters and return shape, but records
a `query` event with agent, run, workspace, tool call, filter payload, policy
label exclusions, explicit purpose, versioned result refs, and `query_result:*`
input edges. This is the controlled query-as-context path for agents that need
structured retrieval with later auditability. When `purpose` is non-empty,
active purpose deny rules filter result refs/nodes/fragments.

`node_events(node_id, direction=None, role=None, after_seq=0, limit=100)` asks
the inverse provenance question: which events touched this immutable node.
Optional direction and role filters narrow the result to specific edge types.

`lineage_nodes(node_id, direction="ancestors")` recursively expands immutable
node lineage. `ancestors` walks outputs back to inputs; `descendants` walks
inputs forward to outputs.

`lineage_graph(node_id, direction="ancestors")` returns the event sequence,
event id, event kind, edge roles, and logical paths that connect the reachable
lineage nodes. It is the debug-oriented view of the same recursive relation.

`start_run(run_id, agent_id=None, workspace_id=None, metadata_json=None)` and
`finish_run(run_id, state="succeeded", metadata_json=None)` maintain a current
run lifecycle view while also writing `run_start` / `run_finish` events and
append-only `run_events` audit rows. Integrity checks bind those audit rows to
matching lifecycle event kinds, run ids, payload states, and continuous
old/new run metadata.

Workspace operations accept optional `tool_call_id` values so run traces can be
tied back to model/tool boundaries. `Workspace.search(query, scope="published",
tool_call_id=None, policy_label_excludes=None, purpose=None)` returns matching
node snippets and records a `search` event. Active labels on result refs or
nodes can be excluded with `policy_label_excludes`; when `purpose` is non-empty,
active purpose deny rules also filter result refs/nodes/fragments. Search result
nodes are linked as input edges so later debugging can answer which retrieved
context an agent saw. Search payloads also record each result's ref name, node
id, state, ref version, label exclusions, and purpose. `scope="draft"` is
limited to the caller's workspace; published and approved scopes are global
lifecycle views.

`Workspace.answer(logical_path, content, citation_refs, tool_call_id=None,
policy_label_excludes=None, retrieval_event_ids=None)` writes an immutable
`answer` node into the workspace and records an `answer` event. Citation refs
are resolved to current readable refs, gated by active ref/node policy labels,
serialized with ref versions in the event payload, and linked as
`answer_citation:*` input edges. Optional retrieval event ids must reference
same-workspace `query` or `search` events that actually returned the cited
refs/nodes. The answer node is linked as the event's output `result`, so
generated answers can be published, exported, replayed, and audited like other
ANFS artifacts. `answer_evidence_coverage(answer_event_id)` returns one row per
answer citation as `(citation_ref, citation_node_id, covered,
covering_event_count)`, making retrieval coverage queryable without adding a
new scoring policy to the kernel. `answer_token_accounting(answer_event_id)`
returns deterministic byte-based estimates as `(schema, estimator,
answer_tokens, citation_tokens, total_tokens, citation_count,
retrieval_event_count)`, giving agent loops an auditable context-cost signal
without depending on vendor tokenizers.
`set_token_cost_profile(model, input_price_micros_per_million_tokens,
output_price_micros_per_million_tokens, prompt_overhead_tokens=0, ...)`
records an append-only adapter profile for caller-supplied model pricing and
prompt overhead. `answer_cost_estimate(answer_event_id, model)` combines the
deterministic token estimate with the active profile and returns integer
micro-unit cost estimates without hard-coding provider price tables.
`answer_quote_support(answer_event_id, min_quote_bytes=16)` recomputes the
longest exact byte overlap between the answer output and each cited node,
returning `(citation_ref, citation_node_id, supported, longest_exact_bytes)`.
This is a deterministic quote-support signal, not semantic entailment.

`pin_ref(ref_name, reason=None, agent_id="gc", run_id=None)` records an
append-only `gc_pin` event and pins the ref's current node as a GC root.
`unpin_gc_root(pin_id, agent_id="gc", run_id=None)` records an append-only
`gc_unpin` event. `gc_pins(active_only=True)` derives the current active pins
from event sequence order, so deleted or archived refs can remain physically
retained only while policy explicitly pins their node versions. Integrity
checks bind each pin audit row to the corresponding `gc_pin` / `gc_unpin` event
and typed root edge.

`set_policy_label(subject_type, subject_id, label, value, agent_id,
run_id=None, tool_call_id=None)` records an append-only `policy_label` event for
`subject_type="ref"` or `subject_type="node"`. Multiple values for the same
subject/label can be active at once. `value=None` clears all active values for
that subject/label without deleting history. `policy_labels(subject_type=None,
subject_id=None, label=None, active_only=True)` derives the active or historical
label view from event sequence order. Integrity checks bind label audit rows to
matching policy-label events and typed subject edges.

`set_fragment_policy_label(node_id, offset, length, label, value, agent_id,
run_id=None, tool_call_id=None)` records an append-only policy label for a byte
range inside an immutable node. `fragment_policy_labels(node_id=None,
label=None, active_only=True)` derives active or historical fragment labels.
Active `visibility` / `deny` policy rules for `subject_type="fragment"` block
overlapping `read_node_range(...)` calls and conservatively block whole-node
projections such as full node reads, citations, search/query visibility, replay,
bundle export/import, and worktree materialization when a node contains a
denied fragment.

`propagate_fragment_policy_labels(...)` maps source fragment labels to an
explicit output byte range. `auto_propagate_fragment_policy_labels(...)`
performs a conservative exact-byte scan and writes propagated output fragment
labels only when the labeled source bytes appear exactly once in the output.
`auto_propagate_fragment_policy_labels_by_normalized_scalar(...)` handles the
common cross-format case where a labeled JSON scalar appears once in normalized
output text, such as `"123-45-6789"` becoming `123-45-6789` or `7.0` becoming
`7`, plus booleans, nulls, and finite non-integer numbers such as `0.75`.
`auto_propagate_fragment_policy_labels_by_normalized_json(...)` extends the
same unique-match rule to JSON arrays and objects by matching their minified
JSON value representation in generated output, without claiming semantic
entailment or paraphrase attribution.

`json_field_spans(node_id)` maps JSON paths such as `$.customer.ssn` and
`$.items[0].price` to byte-range spans in a JSON node.
`set_json_field_policy_label(node_id, json_path, label, value, agent_id,
run_id=None, tool_call_id=None)` resolves that semantic JSON path to a fragment
range and records the corresponding fragment policy label.

`markdown_field_spans(node_id)` maps Markdown frontmatter paths such as
`frontmatter.owner_email` and `frontmatter.owner.email` to byte-range spans for
top-level and indented nested scalar `key: value` entries, conservative object
spans, block sequence item spans, inline scalar sequence item spans such as
`frontmatter.tags[1]`, inline sequence object item fields such as
`frontmatter.contacts[0].email`, inline object scalar fields such as
`frontmatter.contact.email`, nested inline object fields such as
`frontmatter.contact.manager.email`, nested inline array item paths such as
`frontmatter.contacts[0].meta.flags[1]`, quoted-key bracket paths such as
`frontmatter["owner.email"]` or `frontmatter.contact["email.addr"]`,
tag/anchor-decorated payloads such as `!pii &primary ada@example.com`, alias
token spans such as `frontmatter.alias_owner` with kind `alias`, conservative
top-level inline-object alias expansion paths such as
`frontmatter.alias_template.email`, conservative recursive alias/merge chain
expansion paths such as `frontmatter.alias_template_deep.email` and
`frontmatter.merged_template_chain.email`, conservative merge-array expansion
paths such as `frontmatter.merged_array_template.phone`, scalar alias target
paths such as `frontmatter.alias_owner.__target` and
`frontmatter.scalar_alias_deep.__target`, block-sequence alias/anchor/merge
bookkeeping such as `frontmatter.block_aliases[0].__target` and
`frontmatter.block_merge_items[1].email`, YAML null scalar kind spans such as
`frontmatter.optional_owner`, explicit YAML core tag kind overrides such as
`!!str 7`, `!!timestamp 2026-06-09`, `!!binary SGVsbG8=`, or
`!<tag:yaml.org,2002:str> 42`, explicit container tags such as
`!!set {read: null}` and `!!omap [{first: one}]`, conservative unquoted ISO
timestamp scalars such as `2026-06-09`, quoted scalar payload spans that
exclude the quote delimiters while remaining kind `string` unless an explicit
core tag overrides the kind, and `|` / `>` block scalar spans in an opening
`---` frontmatter block.
`markdown_field_values(node_id)` returns `(field_path, value, kind)` rows over
the same conservative frontmatter paths. It preserves the raw byte spans for
policy/audit use while decoding single-quoted doubled apostrophes and common
YAML double-quoted escapes such as `\n`, `\"`, `\\`, `\xNN`, `\uNNNN`, and
`\UNNNNNNNN` for semantic value consumers.
`set_markdown_field_policy_label(node_id, field_path, label, value, agent_id,
run_id=None, tool_call_id=None)` resolves that frontmatter field path to a
fragment range and records the corresponding fragment policy label.

`markdown_section_spans(node_id)` maps Markdown body heading paths such as
`body.private-notes` to byte-range spans from the heading line through the
section body, stopping before the next same-or-higher-level heading. ATX
headings (`## Private Notes`) and conservative Setext headings (`Private Notes`
followed by `-------------`) are supported. Fenced code blocks and opening
frontmatter are skipped for heading detection, and indented code blocks suppress
heading-like lines as code. The same API also exposes
conservative non-heading body block paths such as
`body.private-notes.paragraph.1`, `body.private-notes.paragraph.1.line.2`,
`body.private-notes.paragraph.1.line.2.strong.1`,
`body.private-notes.paragraph.1.line.2.strong.1.text`,
`body.private-notes.paragraph.1.line.2.emphasis.1`,
`body.private-notes.paragraph.1.line.2.emphasis.1.text`,
`body.private-notes.paragraph.1.line.2.link.1`,
`body.private-notes.paragraph.1.line.2.link.1.label`,
`body.private-notes.paragraph.1.line.2.link.1.destination`,
`body.private-notes.paragraph.1.line.2.link.1.title`,
`body.private-notes.paragraph.1.line.2.reference-link.1`,
`body.private-notes.paragraph.1.line.2.reference-link.1.reference`,
`body.private-notes.paragraph.1.line.2.reference-link.1.resolved-destination`,
`body.private-notes.paragraph.1.line.2.reference-link.1.resolved-title`,
`body.private-notes.paragraph.1.line.2.image.1`,
`body.private-notes.paragraph.1.line.2.image.1.alt`,
`body.private-notes.paragraph.1.line.2.image.1.destination`,
`body.private-notes.paragraph.1.line.2.image.1.title`,
`body.private-notes.paragraph.1.line.2.reference-image.1`,
`body.private-notes.paragraph.1.line.2.reference-image.1.reference`,
`body.private-notes.paragraph.1.line.2.reference-image.1.resolved-destination`,
`body.private-notes.paragraph.1.line.2.reference-image.1.resolved-title`,
`body.private-notes.paragraph.1.line.2.code.1`,
`body.private-notes.paragraph.1.line.2.autolink.1`,
`body.private-notes.paragraph.1.line.2.autolink.1.target`,
`body.private-notes.list.1`,
`body.private-notes.list.1.item.2`,
`body.private-notes.list.1.item.2.checkbox`,
`body.private-notes.blockquote.1`, `body.private-notes.blockquote.1.line.2`,
`body.private-notes.table.1`,
`body.private-notes.table.1.align.2`,
`body.private-notes.table.1.row.2`, `body.private-notes.table.1.row.2.cell.2`,
`body.private-notes.code.1`,
`body.private-notes.html.1`, `body.private-notes.link-reference.1`,
`body.private-notes.link-reference.1.destination`,
`body.private-notes.thematic-break.1`, and
`body.paragraph.1` for no-heading documents. `set_markdown_section_policy_label(
node_id, section_path, label, value, agent_id, run_id=None, tool_call_id=None)`
resolves that heading or block path to a fragment range and records the
corresponding fragment policy label. Conservative inline parsers suppress spans
for delimiter bytes escaped with an odd number of immediately preceding
backslashes. Inline-code spans support same-line matching backtick runs such as
`` `code` `` and `` ``code with ` inner tick`` ``. Direct inline link/image and
link-reference-definition destinations support balanced unescaped parentheses
inside the destination, and angle-bracketed destinations expose the target
bytes without the surrounding `<` / `>` delimiters. Direct inline/reference
titles support trailing quoted or parenthesized forms. Reference label matching
normalizes backslash-escaped ASCII punctuation while preserving raw label byte
spans for policy and audit. Duplicate link-reference definitions resolve to
the first normalized definition while later duplicate definitions remain
visible as raw component spans. Link-reference definitions support immediate
next-line title-only continuations for quoted or parenthesized titles.

`set_policy_rule(label, value=None, effect="deny", scope="visibility",
subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None)`
records an append-only registry rule. Current registry support is intentionally
narrow: active `visibility` / `deny` rules interpret active ref/node/fragment policy
label values and are enforced by the shared visibility resolver across query,
search, answer, replay, bundle import/export, and worktree projections.
`clear_policy_rule(...)` clears the active rule without deleting history, and
`policy_rules(scope=None, subject_type=None, label=None, active_only=True)`
returns active or historical registry rows.
`set_policy_expression_rule(expression_json, effect="deny",
scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None,
tool_call_id=None)` records an append-only expression rule over active labels.
Expression JSON supports `label` leaves plus `all`, `any`, and `not` operators.
`clear_policy_expression_rule(...)` clears an expression rule without deleting
history, and `policy_expression_rules(...)` returns active or historical
expression rows.

`set_purpose_policy_rule(purpose, label, value=None, effect="deny",
subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None)`
records an append-only deny rule for a declared access purpose such as
`export`. Active purpose rules interpret ref/node/fragment policy labels and
are enforced by `read_ref(...)`, POSIX-style `read(...)`, `consume(...)`,
`Workspace.search(...)`, and `Workspace.query(...)` only when callers pass a
non-empty purpose. `purpose=None` keeps the compatibility path unchanged while
ordinary visibility rules still apply.

`grant_agent_capability(target_agent_id, capability, ...)` and
`revoke_agent_capability(...)` maintain an append-only active capability view
for agents. `set_purpose_capability_rule(purpose, capability,
effect="require", ...)` declares that a non-empty purpose can only be used by
agents with the named capability. Workspace `read_ref(...)`, POSIX-style
`read(...)`, `consume(...)`, `Workspace.search(...)`, and
`Workspace.query(...)` enforce those rules before applying purpose label
filters. If no purpose capability rule is active, existing file-style reads and
queries are unchanged.

`set_operation_capability_rule(operation, capability, effect="require", ...)`
declares that a kernel operation such as `approve`, `reject_ref`, or
`archive_ref` requires an active agent capability. `clear_operation_capability_rule(...)`
preserves history while removing the active requirement, and
`operation_capability_rules(...)` exposes active or historical requirements.

`verify_integrity()` also cross-checks ref audit rows against their mutation
events. A ref publish, approval, rejection, deletion, archive, snapshot,
checkout, or workspace write must have a matching typed event edge for the
audited ref/node, so `ref_events` remains replayable evidence rather than a
detached log.

`ref_view_at_event(event_id, prefix=None, include_inactive=False,
policy_label_excludes=None)` reconstructs the latest ref state at or before a
specific event. Active labels on current refs or nodes can be excluded. This is
the first time-travel view primitive: a caller can ask what a workspace or
resource namespace looked like after a given operation, without mutating current
refs.

`materialize_ref_view_at_event(event_id, output_dir, prefix=None,
overwrite=False, atomic=True, policy_label_excludes=None)` writes that
reconstructed ref view to a normal directory for inspection or replay. It reads
immutable node bytes from CAS, writes `.anfs-replay-manifest.json`, refuses to
replace existing targets by default, honors active label exclusions, and does
not mutate ANFS metadata. When `atomic=True`, new directories and explicit
overwrites are staged in a sibling temporary directory before being published.
Pass `overwrite=True` only when replacing the directory contents is intentional.

`materialize_workspace(workspace, output_dir, overwrite=False, atomic=True,
policy_label_excludes=None)` writes the active non-directory refs in a workspace
to a normal directory with `.anfs-worktree-manifest.json`. This is the
ordinary-filesystem compatibility path for tools that expect files on disk.
The workspace POSIX-like facade also includes `touch(path)`, which creates a
missing empty regular file through normal `write` semantics and leaves existing
regular files unchanged; explicit time changes use audited `utime(...)`
metadata overlays.
`read_range(path, offset, length, purpose=None, tool_call_id=None)` provides a
path-level `pread`-style compatibility helper over immutable node range reads.
It records an audited `read_range` event and applies visibility and purpose
fragment policies only to the requested byte range.
`write_range(path, offset, content, tool_call_id=None)` provides a path-level
`pwrite`-style compatibility helper for in-place-looking edits. It records an
audited `write_range` event, creates a new immutable CAS node, and links the
previous node as lineage rather than adding mutable block state. Writes beyond
EOF zero-fill the gap in the new node instead of adding sparse-hole state.
`append(path, content, tool_call_id=None)` provides ordinary append
compatibility while still creating a new immutable CAS node and an audited
`append` event.
`truncate(path, length, tool_call_id=None)` provides ordinary file truncation
compatibility for existing regular files. It records an audited `truncate`
event, preserves previous-node lineage, shrinks files, and uses NUL bytes for
extension instead of adding sparse-hole or mutable block state.
`exists(path)`, `is_file(path)`, and `is_dir(path)` are metadata convenience
helpers over the same `stat` semantics; missing or deleted paths return `False`
instead of raising `RefNotFoundError`.
`stat_posix(path)` returns a derived adapter view with the `stat(...)` fields
plus synthetic mode, link count, uid/gid, inode, and atime/mtime/ctime values.
Mode uses a small workspace path metadata overlay when `chmod(...)` has been
called, uid/gid use the overlay when `chown(...)` has been called, and
atime/mtime use the same overlay when `utime(...)` has been called; the data
authority remains existing refs and immutable nodes, not a second file-content
state. File link count and inode are derived from active workspace refs that
point at the same immutable node; file metadata overlays are inherited by new
`link(...)` paths and synchronized across active linked refs that still point at
that same node.
`access(path, mode, uid=None, gid=None, groups=None)` accepts an
`os.access`-style mask (`0=F_OK`, `4=R_OK`, `2=W_OK`, `1=X_OK`) and returns a
derived adapter predicate from current path existence, readability policy,
workspace write ability, synthetic mode bits, and optional effective
uid/gid/group ids. Effective uid `0` gets root-style read/write mode-bit
bypass while execute still requires at least one execute bit; fragment policy
can still deny reads. It is not an OS sandbox, ACL, or process impersonation
model.
`chmod(path, mode, tool_call_id=None)` records an audited path-level POSIX mode
override for workspace logical paths. It affects `stat_posix(...)` and
`access(...)`, synchronizes active linked file paths that still point at the
same immutable node, preserves POSIX special mode bits such as setuid, setgid,
and sticky in `stat_posix(...)` while `access(...)` continues to evaluate only
owner/group/other rwx bits, and rejects explicit non-workspace refs. New regular
files and touched files created under a setgid directory inherit the parent gid;
new subdirectories inherit both the parent gid and the setgid bit. This remains
metadata inheritance, not ACL, descriptor, setgid execution, or kernel-enforced
sandbox semantics.
`chown(path, uid, gid, tool_call_id=None)` records audited path-level uid/gid
overrides for workspace logical paths. `-1` preserves the existing uid or gid.
It affects `stat_posix(...)`, synchronizes active linked file paths that still
point at the same immutable node, rejects explicit non-workspace refs, and does
not make `access(...)` impersonate OS users or groups.
`utime(path, atime_ms=None, mtime_ms=None, tool_call_id=None)` records audited
path-level atime/mtime overrides for workspace logical paths. When both
timestamps are omitted it sets both to the current kernel time. It synchronizes
active linked file paths that still point at the same immutable node, rejects
explicit non-workspace refs, and does not create a new content node.
`acquire_lock(path, ttl_ms=None, tool_call_id=None)` records an audited
exclusive ANFS path lease for the current workspace agent/run and returns a
`lock:*` token. Active leases block other agents/runs from mutating the same
path, descendants of a locked directory, or a parent directory that would cover a
locked descendant. `release_lock(path, lock_id, tool_call_id=None)` releases the
lease for the owning agent/run, and `lock_info(path="")` lists active,
non-expired workspace leases. This is a kernel coordination primitive for agent
tools, not POSIX descriptor-level `fcntl`/`flock`, open-handle, mmap, or FUSE
semantics.
`delete(path, tool_call_id=None, uid=None, gid=None, groups=None)`,
`rm(path, tool_call_id=None, uid=None, gid=None, groups=None)`, and
`mv(src_path, dst_path, tool_call_id=None, uid=None, gid=None, groups=None)` keep
their existing agent-tool behavior when no effective identity is supplied. When
`uid`/`gid` are supplied, they enforce POSIX-like sticky-directory mutation rules:
effective uid `0`, the sticky parent directory owner, or the entry owner may
delete or replace an entry in a sticky directory. `cp(src_path, dst_path, ...)`
and `mv(src_path, dst_path, ...)` treat an existing destination directory as a
container and place the source basename under it for both regular files and
recursive directory operations.
`link(src_path, dst_path, tool_call_id=None)` records an audited hard-link-like
workspace ref from `dst_path` to the same immutable file node as `src_path`.
Later writes to either path still create a new immutable node for that path, so
this is a content-node/ref compatibility link rather than full mutable POSIX
inode sharing.

`worktree_readiness(workspace, input_dir, policy_label_excludes=None)` checks a
materialized ordinary directory without mutating ANFS. It reports whether the
directory exists, whether `.anfs-worktree-manifest.json` still matches current
workspace refs, whether the tree contains only supported regular files and
directories with safe relative paths, the local added/modified/deleted/unchanged
counts, and whether a checked commit would pass its base lease precondition.

`cache_materialized_workspace(workspace, cache_root, cache_key=None,
overwrite=False, atomic=True, policy_label_excludes=None)` materializes the
active workspace view into a managed cache directory under `cache_root`. It
stores a manifest hash and file metadata in SQLite, returns
`(cache_key, output_dir, file_count, reused)`, and reuses an existing cache when
the current workspace view still matches. `cached_working_sets(workspace=None)`
lists managed cached working sets.

`commit_worktree(workspace, input_dir, agent_id, run_id=None,
tool_call_id=None, delete_missing=True, require_manifest_match=False,
policy_label_excludes=None)` scans a normal directory, compares it with current
workspace refs, writes added/modified files back through workspace `write`
events, and marks missing files as deleted when `delete_missing=True`. Modified
files derive from their previous node, so lineage remains visible. Active
policy labels on existing refs or nodes can block materialization or commit.
Symlinks and other special filesystem entries are rejected rather than followed,
so a worktree commit cannot import bytes outside the materialized directory by
link traversal. Relative worktree paths containing `\` are also rejected to
avoid POSIX-vs-Windows path ambiguity. The root
`.anfs-worktree-manifest.json` path is reserved for adapter metadata: it is
ignored during local file scanning and cannot be materialized or committed as a
workspace user file.
When `require_manifest_match=True`, the `.anfs-worktree-manifest.json` written
by `materialize_workspace(...)` must still match the current workspace node ids
before any mutation is applied; stale materialized directories fail with a ref
conflict and should be refreshed before retrying. A grouped `worktree_commit`
event records the whole directory diff and links old/new nodes with typed
`worktree_*` edges. The current workspace view, optional manifest lease check,
per-file mutations, ref audit rows, and grouped commit event are written in one
SQLite transaction, so ANFS canonical state does not partially advance when a
late file mutation fails.

`export_event_bundle(event_id, output_dir, include_blobs=True,
policy_label_excludes=None, signing_key=None, signer_id=None)` exports the
event-time causal subgraph around an event: events, edges, nodes, ref audit
rows, blob metadata, a bundle checksum, an optional HMAC-SHA256 bundle
signature, and optionally CAS object bytes. With `policy_label_excludes`, export
preflights the closed bundle and rejects active labels on any included ref or
node before writing bundle files.
Each exported event includes its source `event_sequence.seq`, and import uses
that value to preserve relative event order in the destination database.
Bundles exported with `include_blobs=False` can be imported only into a
compatible database that already has the referenced CAS blobs by hash and size.

`export_run_bundle(run_id, output_dir, include_blobs=True,
policy_label_excludes=None, signing_key=None, signer_id=None)` exports every
event in a run plus the causal upstream evidence those events depend on. It uses
the same policy-label preflight and bundle schema as event export, so
`import_event_bundle(...)` can restore the run history in another ANFS database.

`export_history_archive(output_dir, include_blobs=True,
policy_label_excludes=None, signing_key=None, signer_id=None)` exports the full
append-only event/ref history through the latest event sequence into the same
portable bundle schema. It includes unrelated branches, ref audit rows,
event edges, referenced nodes, blob metadata, optional CAS object bytes, and the
same checksum/signature fields. The export is a history archival proof and does
not delete or compact canonical rows.

`create_ref_view_checkpoint(event_id=None, prefix=None, include_inactive=False,
agent_id="checkpoint_agent")` records an immutable checkpoint for the ref view
at a target event. The checkpoint stores target event/sequence, normalized
prefix, inactive-ref mode, ref count, and a deterministic checksum over the
replay view. `verify_ref_view_checkpoint(checkpoint_id)` recomputes that proof.
This is a precondition primitive for future physical history compaction; it
does not delete or rewrite event/ref history.

`archival_readiness_plan(checkpoint_id, bundle_path, signature_key=None,
require_signature=False)` is the read-only bridge between archive export and
future physical compaction. It verifies that a history archive bundle covers the
current full event/ref audit history and that a replay checkpoint is still
valid, then reports whether physical archival preconditions are satisfied. It
does not delete, rewrite, or compact canonical rows.

`import_event_bundle(bundle_path, bundle_objects_dir=None,
policy_label_excludes=None, signature_key=None, require_signature=False)`
imports a bundle into another ANFS database. It verifies the bundle checksum
when present and verifies an optional HMAC-SHA256 signature when a
`signature_key` is supplied. With `require_signature=True`, unsigned bundles are
rejected before import and `signature_key` is required. Import then restores
immutable evidence tables and ref audit history without creating live refs. With
`policy_label_excludes`, import preflights the bundle against active destination
ref/node labels and rejects before writing canonical rows or objects. Import
also validates the bundle's internal reference closure before writing: the root
event, edge events/nodes, ref-audit events/nodes, and node blobs must all be
present in the bundle. Derived local indexes such as `node_fts` are rebuilt from
immutable blob bytes during import rather than serialized as canonical bundle
state. Existing event edges and ref audit rows with the same primary keys must
match exactly, so imported history can be inspected or materialized without
silently overwriting the destination namespace.

`workspace_diff(base, workspace)` compares a base ref namespace with a workspace
namespace and returns per-path `added`, `modified`, `deleted`, or `unchanged`
status. It is read-only.

`workspace_conflicts(base, workspace)` compares checkout-base, current-base, and
workspace state. A path is a conflict only when the workspace changed it and the
current base changed away from the checkout snapshot too.

`merge_workspace(base, workspace, agent_id, run_id=None, tool_call_id=None)`
refuses to run if conflicts exist.
Otherwise it applies workspace changes to the base namespace atomically:
added/modified paths update base refs, deleted paths archive base refs, and every
ref mutation is audited and tied to a matching per-ref merge edge. Merge
allow/deny decisions are recorded as `policy_decision` events under the
`workspace_merge` policy. When `base` is a snapshot manifest ref, merge writes
to the original source namespace recorded in the snapshot metadata.
