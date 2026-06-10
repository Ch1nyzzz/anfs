# ANFS Progress Tracker

## Current Phase

Phase 1: Rust/PyO3 MVP kernel implementation.

## Milestones

### M0: Documentation Baseline

Status: complete

Acceptance criteria:

- Project background documented.
- MVP PRD documented.
- Gap analysis against existing filesystems documented.
- Missing design decisions listed.

### M1: Rust/PyO3 Project Scaffold

Status: complete

Acceptance criteria:

- `Cargo.toml` configured for PyO3 extension module.
- `pyproject.toml` configured for maturin.
- `src/lib.rs` exposes `anfs_core`.
- Python can import `anfs_core` after `maturin develop`.

### M2: SQLite Kernel Schema

Status: complete

Acceptance criteria:

- WAL mode enabled.
- Foreign keys enabled.
- Core tables created.
- Schema migration table exists.
- Current kernel schema version is recorded.
- Integrity verifier diagnoses missing or unsupported schema versions.
- Immutable-table protection strategy implemented or explicitly deferred.

### M3: CAS Blob Store

Status: complete

Acceptance criteria:

- SHA-256 hashing.
- Blob deduplication.
- Inline storage under 64 KiB.
- File-backed storage at or above 64 KiB.
- Integrity verification for file-backed blobs.

### M4: Node/Event/Ref Operations

Status: complete

Acceptance criteria:

- `write` creates blob, node, event, edge, draft ref, and ref audit row.
- `publish` creates event, edge, published ref, and ref audit row.
- Ref lifecycle transitions are validated.
- Ref conflicts are detected.

### M5: Lineage Validation

Status: complete

Acceptance criteria:

- `approve` checks evidence lineage in Rust.
- Valid evidence approves target.
- Invalid evidence raises `LineageMismatchError`.
- Approve creates event and ref audit row.

### M6: Killer Demo

Status: complete

Acceptance criteria:

- Python demo creates patch v1.
- Python demo creates test result derived from patch v1.
- Python demo creates patch v2.
- Python demo attempts invalid approval.
- Rust core rejects invalid approval.

## Open Decisions

1. Should immutable-table enforcement stay as SQLite triggers, or move toward a
   stricter migration-only admin mode?
2. Should local embedding projection stay scan-based, or should larger
   benchmark results justify an approximate nearest-neighbor projection?
3. Should blob hashes move from hex `TEXT` to binary `BLOB(32)` before larger
   scale tests?

## Open Implementation Gaps

These are known gaps against the agent-filesystem design target in
`docs/05_agent_filesystem_design.md`.

Highest priority:

- Task benchmark adapter: a local LoCoMo-like synthetic adapter exists through
  M148, and JSON/JSONL dataset import with token-F1 evaluation exists through
  M153, proving raw, extracted, and hybrid retrieval/answer/evidence-coverage
  workflows. Official real LoCoMo snapshot runs, model-based answer generation,
  model-backed memory extraction, and task-memory vector DB comparisons remain
  future work.
- Query-time policy: byte-range fragment labels and JSON field-to-fragment
  labels exist, exact byte-identical copies inherit active fragment labels,
  Markdown frontmatter scalar fields, nested fields, conservative object/list
  spans, inline scalar sequence items, inline sequence object item fields,
  inline object scalar fields, bounded nested inline object fields, quoted-key
  bracket paths, bounded nested inline array item paths, tag/anchor-decorated
  payload spans, alias token spans, conservative top-level inline-object alias
  expansion paths, conservative recursive alias/merge chain expansion paths,
  conservative merge-array expansion paths, scalar alias target paths including
  chained scalar alias targets, block-sequence alias/anchor/merge bookkeeping,
  YAML null scalar kind spans, explicit YAML core tag kind overrides including
  timestamp/binary and set/omap container tags, conservative unquoted ISO
  timestamp scalar inference, quoted scalar payload spans excluding quote
  delimiters, decoded quoted scalar semantic values through
  `markdown_field_values(...)`, and block scalars resolve to fragment labels,
  and Markdown body ATX / Setext heading sections plus
  conservative paragraph/paragraph-line/inline-strong/inline-strong-text/
  inline-emphasis/inline-emphasis-text/inline-link/inline-link-destination/
  inline-link-title/inline-link-label/reference-link/reference-link-label/
  reference-link-reference/reference-link-resolved-destination/
  reference-link-resolved-title/inline-image/
  inline-image-alt/inline-image-destination/inline-image-title/reference-image/
  reference-image-alt/reference-image-reference/
  reference-image-resolved-destination/reference-image-resolved-title/
  inline-code/autolink/autolink-target/
  list/list-item/task-checkbox/blockquote/blockquote-line/table/table-align/
  table-row/table-cell/code/html/link-reference/link-reference-label/
  link-reference-destination/link-reference-title/thematic-break body blocks
  resolve to fragment labels. Current conservative inline parsers suppress
  spans for delimiter bytes escaped by an odd number of immediately preceding
  backslashes, inline-code spans support same-line matching backtick runs, and
  direct inline link/image destination spans preserve balanced unescaped
  parentheses and angle-bracket payload bytes. Link-reference definition
  destinations also expose angle-bracket payload bytes. Direct
  inline/reference title spans support trailing quoted and parenthesized forms.
  Reference label matching normalizes backslash-escaped ASCII punctuation while
  raw label spans remain available for policy and audit. Duplicate
  link-reference definitions resolve to the first normalized definition while
  later duplicate component spans remain visible. Link-reference definitions
  support immediate next-line title-only continuations.
  Policies do not yet compose through full YAML or non-exact cross-format body
  parsing.
  Basic boolean and threshold visibility expressions over active labels are
  implemented through M140 and M173. Current structured query, search, answer
  construction, replay materialization, bundle export/import, working tree sync,
  and conservative derived-output propagation support active label exclusions.
- Fragment policy labels: durable node/ref labels and byte-range fragment
  labels exist; JSON, conservative Markdown frontmatter, and Markdown body ATX /
  Setext heading section plus conservative body block, paragraph-line,
  inline-strong, inline-strong-text, inline-emphasis, inline-emphasis-text,
  inline-link, inline-link-label, inline-link-destination, inline-link-title, reference-link,
  reference-link-label, reference-link-reference,
  reference-link-resolved-destination, reference-link-resolved-title,
  inline-image, inline-image-alt,
  inline-image-destination, inline-image-title, reference-image, reference-image-alt,
  reference-image-reference, reference-image-resolved-destination,
  reference-image-resolved-title, inline-code,
  autolink, autolink-target, list-item, task-checkbox, blockquote-line,
  table-align, table-row, table-cell, html-block, link-reference,
  link-reference-label, link-reference-destination, link-reference-title, and thematic-break
  extraction exist, with conservative backslash-escaped delimiter suppression
  for current inline parsers and same-line matching inline-code backtick runs;
  direct inline link/image destination spans preserve balanced unescaped
  parentheses and angle-bracket payload bytes; link-reference definition
  destination spans expose angle-bracket payload bytes; direct inline/reference
  title spans support trailing quoted and parenthesized forms; reference label
  matching normalizes backslash-escaped ASCII punctuation while preserving raw
  label spans; duplicate link-reference definitions resolve to the first
  normalized definition while later duplicate component spans remain visible;
  link-reference definitions support immediate next-line title-only
  continuations;
  full YAML and non-exact cross-format semantic field
  extraction remain future work.
- Vector/embedding projection: local caller-provided node embeddings,
  chunk-row embeddings, `vector_search(...)`, and `vector_search_chunks(...)`
  are implemented through M129 and M139; a local RAG JSONL retrieval baseline
  exists through M147; an external vector JSONL sidecar baseline exists through
  M154; managed vector DB services, ANN indexes, and larger embedding sweeps
  remain future work.

Performance and filesystem fidelity:

- No FUSE/kernel mount; current interface is Python/Rust tool API plus
  materialized working tree sync. Checked worktree commits can now require the
  materialized manifest to match the current workspace node ids before mutation,
  cover changed/missing/new current path stale-base cases, and multi-process
  checked commit contention proves only one stale materialized base advances.
  Checked worktree commits now retry SQLite busy lock errors as a whole
  transaction, so a losing concurrent checked commit re-reads current refs and
  returns the intended stale-manifest conflict instead of surfacing a transient
  database lock error.
- POSIX-like path access now includes audited `read_range(...)`, so callers can
  perform path-level partial reads without first resolving a node id while still
  honoring range-scoped visibility and purpose fragment policies.
- POSIX-like path writes now include audited `write_range(...)`, so callers can
  perform pwrite-style replacement, append, or zero-fill extension edits while
  ANFS still records a new immutable node and source lineage instead of mutable
  block or sparse-hole state.
- POSIX-like append now includes audited `append(...)`, so callers can express
  append-at-EOF file behavior without leaving the immutable CAS/ref/event model.
- POSIX-like truncation now includes audited `truncate(...)`, so callers can
  shrink or NUL-extend existing regular files while ANFS still records a new
  immutable node and source lineage instead of sparse-hole or mutable block
  state.
- POSIX-like metadata checks now include `exists(...)`, `is_file(...)`, and
  `is_dir(...)` over the existing stat semantics, reducing adapter friction
  without adding metadata state.
- POSIX-like stat metadata now includes derived `stat_posix(...)` adapter
  fields for mode, nlink, uid/gid, inode, and atime/mtime/ctime. M214 adds
  audited path-level chmod mode overlays, M215 adds audited path-level utime
  atime/mtime overlays, and M216 adds audited path-level chown uid/gid overlays
  while keeping file contents in the existing immutable node/ref model. M218
  adds hard-link-like workspace refs to the same immutable node and derives file
  nlink/inode from active refs that share that node. M219 adds metadata overlay
  inheritance and synchronization across active linked refs while they still
  point at the same immutable node. M223 proves chmod special mode bit
  preservation for setuid/setgid/sticky bits without adding OS execution
  semantics. M224 adds explicit-identity sticky-directory delete/replacement
  checks for `delete`/`rm`/`mv`. M225 adds setgid directory metadata inheritance
  for newly created files and subdirectories. M226 adds audited exclusive ANFS
  path leases that block other agents/runs from mutating locked paths or covered
  directory subtrees.
- POSIX-like access checks now include derived `access(path, mode)` for
  `F_OK`/`R_OK`/`W_OK`/`X_OK`, including read-policy interaction and chmod mode
  overlays. M220 adds optional effective uid/gid/group owner/group/other
  mode-bit selection, and M221 adds root-style uid `0` read/write mode-bit
  bypass with execute-bit requirements, without becoming an OS sandbox or
  process impersonation model.
- No lazy namespace move; directory `mv` eagerly rewrites subtree refs.
- Large-file selective range reads, derived chunk maps, and persistent chunk
  indexes exist; local benchmark snapshot coverage exists under
  `docs/benchmark_snapshots`, including an agent-memory quick matrix that
  varies agents, files, payload, search cardinality, and large-file size, plus
  an ANFS full local matrix over the same dimensions and an ANFS-only 1 MiB
  large-file range/chunk/cache snapshot. A 4-agent / 256-file ANFS-only
  large-collection snapshot exists through M177. Release-machine and
  heavier-parameter sweeps remain pending.
- Benchmark matrix support exists for agent count, file count, payload size,
  and search result cardinality; a local quick matrix snapshot exists through
  M160, a four-worker same-key cache contention snapshot exists through M161,
  an ANFS full local matrix exists through M162, and a multi-backend full local
  matrix exists through M163. Shared-ref stale-version contention coverage
  exists through M165. Release-machine sweeps and heavier contention stress
  remain pending.

Benchmark baselines:

- MarkdownFS baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend markdownfs`.
- SQLite memory-store baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend sqlite-memory`.
- Local RAG JSONL retrieval baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend rag-jsonl`.
- External vector JSONL sidecar baseline exists through
  `benchmarks/agent_memory_benchmark.py --backend external-vector-jsonl`; it is
  not a managed vector database service.
- Native POSIX + JSONL baseline exists, but it intentionally lacks CAS,
  lineage graph, event-time replay, and integrated policy semantics.

Memory-layer features:

- No model-backed first-class memory extraction service. The local task-memory
  benchmark can synthesize extracted memory refs as derived nodes with lineage
  to raw conversation-session nodes.
- Answer-citation API links cited refs/nodes to optional prior search/query
  event ids, and `answer_evidence_coverage(...)` exposes retrieval coverage.
  `answer_quote_support(...)` exposes deterministic exact-quote support.
  Semantic entailment remains future work.
- Deterministic token-estimate accounting exists for answer construction and
  cited retrieval context. Append-only token cost profiles exist through M157
  for caller-supplied model pricing and prompt overhead. Exact vendor tokenizer
  parity and provider price freshness remain adapter-level concerns.

Operations:

- No distributed coordinator or cross-machine shared namespace protocol.
- Explicit workspace fork exists through M144. Workspace/branch naming policy
  exists through M171. Richer branch lifecycle and remote branch coordination
  remain future work.
- Read-only `compaction_plan()` reports event/ref history size, inactive refs,
  GC pressure, inline blob pressure, active pins, active retention policies,
  derived projection rows, and SQLite freelist pages. Narrow SQLite `VACUUM`
  orchestration exists through M150. Retention-policy automation exists through
  M152. Inline blob compaction exists through M156. Full event/ref history
  archive export exists through M164 as a signed/checksummed preflight proof.
  Immutable ref-view replay checkpoints exist through M166 as boundary proofs.
  Read-only archive/checkpoint readiness planning exists through M167.
  Physical event/ref history archival compaction remains future admin-mode work.
- Strict schema guards reject unsupported future schema versions before normal
  initialization, and `schema_status()` / `require_schema_current()` expose
  migration state for admin checks. `schema_migration_plan()` and
  `apply_schema_migrations(dry_run=True)` expose explicit safe migration
  planning/execution for the supported schema; future versioned upgrade steps
  remain future work.

## Current Implementation Notes

- `checkout(base, workspace)` projects published/approved base refs into a
  workspace namespace and records checkout base snapshots.
- `fork_workspace(source_workspace, target_workspace, ...)` copies current
  source workspace draft refs into a target workspace with typed fork edges.
- `snapshot_namespace(prefix, snapshot_ref)` freezes active namespace refs into
  immutable manifest snapshot refs.
- `refs` includes `ref_version` and ref updates use optimistic checks.
- Immutable core tables are protected by SQLite triggers.
- Workspace delete is logical and marks the ref as `deleted`; controlled GC can
  physically remove unreachable file-backed object bytes.
- `ref_history(ref_name)` exposes ref audit rows in insertion order.
- `event(event_id)` exposes event metadata and typed input/output edges.
- `query(...)` exposes a unified read-only current-ref query with prefix, text,
  state, agent, run, event kind, media type, timestamp, and policy-decision
  filters.
- `Workspace.query(...)` uses the same structured filters while recording
  query-as-context events with exact result node edges.
- `set_policy_label(...)` and `policy_labels(...)` expose durable node/ref
  policy labels.
- `set_policy_rule(...)`, `clear_policy_rule(...)`, and `policy_rules(...)`
  expose an append-only visibility policy registry over label values.
- `set_fragment_policy_label(...)` and `fragment_policy_labels(...)` expose
  append-only byte-range fragment policy labels.
- Structured query supports active ref/node label exclusion through
  `policy_label_excludes`.
- Search-as-context supports active ref/node label exclusion through
  `policy_label_excludes`.
- Answer construction records cited refs as typed input edges and supports
  active ref/node label exclusion through `policy_label_excludes`.
- Time-travel ref views and replay materialization support active ref/node label
  exclusion through `policy_label_excludes`.
- Event and run bundle export preflight active ref/node label exclusions through
  `policy_label_excludes`.
- Bundle import can preflight active destination ref/node label exclusions
  through `policy_label_excludes`.
- Policy label gates are centralized in `VisibilityPolicy`.
- Workspace working trees can be materialized to ordinary directories and
  committed back into workspace refs through `commit_worktree(...)`.
- The Python killer demo is implemented in `tests/test_demo.py` and can run
  without pytest via `.venv/bin/python tests/test_demo.py`.

## Next Milestones

### M7: Real Workspace Checkout

Status: complete

Acceptance criteria:

- `checkout(base, workspace)` copies or projects base refs into workspace refs.
- Checkout records source refs/nodes in event edges.
- Workspace changes remain copy-on-write.

Implemented:

- Prefix-based projection from published/approved refs under `base`.
- Manifest snapshot refs can also be checked out directly as base refs.
- Target workspace refs are created as `draft`.
- Checkout refuses to overwrite existing workspace refs.
- Checkout records input/output event edges and ref audit rows.

Remaining:

- Whole-workspace snapshot refs are implemented through M35 and direct snapshot
  checkout is implemented through M39.

### M8: GC Planning And Integrity Verifier

Status: complete

Acceptance criteria:

- Define GC roots.
- Add verifier for blob hash mismatch, missing object files, dangling refs, and
  invalid lifecycle history.
- Do not physically delete data until verifier exists.

Implemented:

- `verify_integrity()` reports SQLite foreign key violations.
- Verifies inline/file blob hash consistency.
- Detects missing or unreadable file-backed blobs.
- Checks current ref states and ref event lifecycle transitions.
- `gc_roots(include_workspaces=True)` reports live root refs.
- `gc_candidates(include_workspaces=True)` reports currently unreachable blobs.
- Reachability follows live refs and walks backward through lineage event edges
  to preserve ancestors/evidence.
- Workspace drafts are treated as live roots by default, and can be excluded for
  cleanup planning.
- `collect_garbage(include_workspaces=True, dry_run=True, agent_id="gc")`
  provides controlled physical cleanup for file-backed unreachable blobs.
- GC collection writes `events.kind = 'gc_collect'` and append-only
  `gc_blob_events` audit rows.
- `verify_integrity()` understands GC-collected file blobs.
- Policy-driven root pinning is implemented with append-only
  `gc_pin_events`.
- `pin_ref(...)`, `unpin_gc_root(...)`, and `gc_pins(...)` expose pin lifecycle
  without adding mutable GC state.
- GC roots and candidates include active pins derived from event sequence order.
- `verify_integrity()` validates GC pin state transitions.
- `gc_candidates(..., older_than_ms=None)` and
  `collect_garbage(..., older_than_ms=None)` support retention cutoffs.
- Retention eligibility uses the newest node timestamp for each unreachable
  blob, so deduped content reused by a recent node is not collected early.

Remaining:

- Inline blob compaction is deferred because inline bytes live in immutable
  SQLite rows.

### M9: Multi-Node Artifact Manifest

Status: complete

Acceptance criteria:

- Manifest node format defined.
- Publish can produce a bundle artifact.
- Lineage validation works across manifest children.

Implemented:

- `Workspace.publish_manifest(paths, artifact_ref_name, kind="artifact")`
  creates an immutable manifest node.
- Manifest content uses JSON schema `anfs.manifest.v1`.
- `AnfsEngine.manifest_children(node_id)` exposes child path/node pairs.
- `AnfsEngine.manifest_child_records(node_id)` exposes child role, media type,
  digest, and size.
- Manifest publish event records child nodes as inputs and manifest node as
  output.
- Approval succeeds only when evidence derives from the manifest node directly
  or collectively covers every child node.
- GC reachability preserves child blobs through the published manifest root.

Remaining:

- Rich per-child metadata is implemented through M29.
- Canonical JSON rules beyond deterministic sorted path order.
- Bundle-level diff/merge APIs.

### M10: Policy Decision Log

Status: complete

Acceptance criteria:

- Approval allow/deny decisions are recorded as append-only events.
- Denied approval attempts remain auditable even though the ref mutation is not
  committed.
- Policy decision events link to target and evidence nodes.
- Python API can query all decisions, decisions for one target ref, or decisions
  matching policy fields.

Implemented:

- `events.kind = 'policy_decision'` stores policy decisions.
- Payload JSON records policy, decision, reason code, reason, target ref/node,
  and evidence refs.
- `event_edges` links target and evidence nodes.
- `AnfsEngine.policy_decisions(target_ref=None, policy=None, decision=None,
  reason_code=None)` exposes the log.
- Lineage approval and workspace merge decisions include stable
  `reason_code` values.

Remaining:

- Policy registry value rules are implemented through M121.
- Export/replay formatting for policy decisions.

### M11: Workspace Diff

Status: complete

Acceptance criteria:

- Compare base namespace and workspace namespace by logical path.
- Report added, modified, deleted, and unchanged refs.
- Preserve node ids in the diff output.
- Do not mutate storage.

Implemented:

- `AnfsEngine.workspace_diff(base, workspace)` returns sorted diff rows.
- Base refs are read from published/approved refs under the base prefix.
- Workspace refs include draft/deleted state.
- Tests cover all four statuses.

Remaining:

- Human-readable patch rendering.

### M12: Checkout Base Snapshot And Conflict Detection

Status: complete

Acceptance criteria:

- Checkout persists the base node for every projected path.
- Conflict detection compares checkout base, current base, and workspace.
- Conflicts are reported only when both sides changed from checkout base.
- Conflict detection is read-only.

Implemented:

- `workspace_base_refs` append-only table records checkout base snapshots.
- `AnfsEngine.workspace_conflicts(base, workspace)` returns conflict rows.
- Tests cover base-only changes, workspace-only changes, and true conflicts.

Remaining:

- Conflict policy decision events are implemented through M26 merge policy
  auditing.
- Whole-workspace snapshot refs are implemented through M35.

### M13: Atomic Workspace Merge

Status: complete

Acceptance criteria:

- Merge refuses to run if conflict detection finds conflicts.
- Merge uses checkout base snapshot to determine workspace-authored changes.
- Merge applies added, modified, and deleted paths atomically.
- Merge writes event and ref audit rows.

Implemented:

- `AnfsEngine.merge_workspace(base, workspace, agent_id)` returns merged rows.
- Added paths create base refs.
- Modified paths update base refs.
- Deleted paths archive base refs.
- Conflict rejection leaves base refs untouched.
- Tests cover successful merge and conflict rejection.

Remaining:

- Merge policy decision log is implemented through M26.
- Merge into whole-workspace manifest/snapshot refs.
- Merge result artifact generation.

### M14: Observability Query Surface

Status: complete

Acceptance criteria:

- Python can inspect a ref's mutation history without raw SQL access.
- Python can inspect an event and its typed input/output edges.
- Missing event lookup raises a typed `EventNotFoundError`.
- Queries do not mutate storage.

Implemented:

- `AnfsEngine.ref_history(ref_name)` joins `ref_events` with `events`.
- History is ordered by `ref_events` insertion order, avoiding timestamp ties.
- `AnfsEngine.event(event_id)` returns event metadata and all event edges.
- Tests verify publish/approve history, event edge roles, and missing-event
  errors.

Remaining:

- Higher-level replay API that materializes a workspace view as files.
- Export format for portable trace bundles.
- Event pagination/filtering for large runs.

### M15: Time-Travel Ref View

Status: complete

Acceptance criteria:

- Python can reconstruct a ref namespace view at or before a specific event.
- The view is derived from append-only `ref_events`, not current `refs`.
- Deleted and archived refs are excluded by default.
- Callers can opt into inactive refs for forensic/debug use.
- Missing event lookup raises `EventNotFoundError`.
- Query does not mutate storage.

Implemented:

- `AnfsEngine.ref_view_at_event(event_id, prefix=None, include_inactive=False,
  policy_label_excludes=None)` returns
  `(ref_name, node_id, ref_kind, state, source_event_id, created_at)`.
- Event ordering uses SQLite event insertion order rather than wall-clock time.
- Prefix filtering supports workspace/resource namespace reconstruction.
- Tests verify namespace state after publish events and after archive.

Remaining:

- Add run-id-scoped bundle export once normal APIs populate `run_id`.

### M16: Replay Directory Materialization

Status: complete

Acceptance criteria:

- Python can materialize a reconstructed event-time ref view to a normal
  directory.
- Materialization reads immutable node bytes from CAS.
- Materialization does not mutate ANFS metadata.
- Prefix filtering maps namespace refs to relative file paths.
- Unsafe relative paths are rejected.
- Deleted and archived refs remain excluded by default.

Implemented:

- `AnfsEngine.materialize_ref_view_at_event(event_id, output_dir, prefix=None,
  include_inactive=False)` writes files for the reconstructed view.
- The API returns `(ref_name, relative_path, node_id, size)` for written files.
- Path materialization rejects empty, `.`, `..`, and absolute path segments.
- Materialization writes `.anfs-replay-manifest.json` into the output
  directory.
- Tests verify files are written with expected bytes and archived refs are
  excluded from active materialization.

Remaining:

- Explicit replay manifests are implemented through M30.
- Existing target overwrite policy is implemented through M31.

### M17: Portable Event Bundle Export

Status: complete

Acceptance criteria:

- Python can export a portable causal bundle rooted at one event.
- The bundle includes causal events up to the root event, not future consumers.
- The bundle includes event edges, nodes, blob metadata, and ref audit rows.
- The bundle can optionally copy CAS object bytes into the export directory.
- Missing event lookup raises `EventNotFoundError`.
- Export does not mutate ANFS metadata.

Implemented:

- `AnfsEngine.export_event_bundle(event_id, output_dir, include_blobs=True,
  policy_label_excludes=None)` writes `bundle.json`.
- Export follows lineage backward from the root event's nodes using recursive
  SQLite queries.
- CAS object bytes are copied under `objects/ab/cd/hash` when requested.
- The API returns `(bundle_path, event_count, node_count, blob_count)`.
- Tests verify bundle schema, root event id, event kinds, nodes, edges, ref
  audit rows, and object hash integrity.

Remaining:

- Bundle checksums are implemented through M27; HMAC-SHA256 bundle signatures
  are implemented through M143.
- Run-id export filters are implemented through M23.

### M18: Portable Event Bundle Import

Status: complete

Acceptance criteria:

- Python can import an exported event bundle into a fresh ANFS database.
- Import restores blobs, nodes, events, event edges, and ref audit rows.
- Import verifies copied object bytes against bundle hashes and sizes.
- Import is idempotent for the same bundle.
- Import does not create live `refs` automatically.
- Imported history remains replayable through `event`, `ref_view_at_event`, and
  `materialize_ref_view_at_event`.

Implemented:

- `AnfsEngine.import_event_bundle(bundle_path, bundle_objects_dir=None,
  policy_label_excludes=None)` imports bundle content.
- Bundle schema is validated as `anfs.event_bundle.v1`.
- Existing imported nodes/events are checked for exact metadata compatibility.
- CAS object bytes are imported into the destination object's storage layout.
- Tests verify export/import roundtrip, node bytes, event lookup, reconstructed
  artifact view, replay materialization, and idempotent import.

Remaining:

- Bundle checksum verification is implemented through M27; HMAC-SHA256
  signature verification is implemented through M143.
- Optional policy for promoting imported `ref_events` into live destination refs.
- Richer conflict diagnostics for bundle import into non-empty databases are
  implemented through M68.

### M19: Controlled Physical GC

Status: complete

Acceptance criteria:

- Python can dry-run GC without deleting object bytes.
- Python can execute GC for unreachable file-backed blobs.
- GC excludes live roots and lineage ancestors.
- GC verifies object hash before removal.
- GC records append-only audit events.
- `verify_integrity()` does not flag intentionally collected file blobs.
- Inline blobs are not physically collected.

Implemented:

- Added append-only `gc_blob_events` table with immutability triggers.
- Added `AnfsEngine.collect_garbage(include_workspaces=True, dry_run=True,
  agent_id="gc", older_than_ms=None, limit=None)`.
- File-backed unreachable blobs are removed only when `dry_run=False`.
- `gc_candidates()` excludes already collected blobs.
- `gc_candidates(..., older_than_ms=...)` and
  `collect_garbage(..., older_than_ms=...)` enforce retention windows.
- `gc_candidates(..., limit=...)` and `collect_garbage(..., limit=...)`
  enforce deterministic bounded batches.
- `read_node()` raises `StorageCorruptionError` for physically collected blobs.
- Tests verify dry-run, physical removal, live blob preservation, audit events,
  retention windows, batch limits, integrity behavior, and typed read failure
  after collection.

Remaining:

- External scheduling controls for large stores.
- Inline blob compaction or database vacuum strategy.

### M20: Explicit Event Sequence

Status: complete

Acceptance criteria:

- Every event has an explicit monotonic sequence number.
- Sequence rows are append-only and immutable.
- New events insert sequence rows in the same transaction.
- Existing events can be backfilled without mutating immutable `events`.
- Time-travel replay and bundle export use sequence order, not SQLite `rowid`.
- Bundle import preserves a complete sequence table in the destination DB.

Implemented:

- Added append-only `event_sequence(event_id, seq)` table with immutability
  triggers.
- `init_db()` backfills missing sequence rows ordered by legacy event row order.
- `insert_event()` writes `event_sequence` rows for all new kernel events.
- Bundle import inserts sequence rows for imported events.
- `ref_view_at_event()` and event bundle export now use `event_sequence.seq`.
- Tests verify monotonic sequence rows and import sequence completeness.

Remaining:

- Superseded by M25 concurrent sequence allocation hardening.

### M21: Sequence-Aware Event Listing

Status: complete

Acceptance criteria:

- Python can list events in explicit sequence order.
- Event listing supports stable pagination using `after_seq`.
- Event listing supports kind filtering.
- Invalid pagination limits are rejected with a typed policy error.
- Imported bundles remain listable by event sequence.

Implemented:

- Added `AnfsEngine.events(after_seq=0, limit=100, kind=None, agent_id=None,
  workspace_id=None, run_id=None, created_after_ms=None,
  created_before_ms=None, payload_contains=None)`.
- The API returns `(seq, event_id, kind, agent_id, workspace_id, created_at,
  input_count, output_count)`.
- Pagination and filtering use `event_sequence.seq`.
- Tests cover first/second page, kind/agent/workspace/payload/time filtering,
  edge counts, invalid limits, invalid time ranges, and imported bundle event
  listing.

Remaining:

- Add indexed payload search if event volumes require more than LIKE filtering.

### M22: Run Context Propagation

Status: complete

Acceptance criteria:

- Workspace handles can carry an optional `run_id`.
- Workspace-generated events persist that `run_id`.
- Review/approval events can carry an optional `run_id`.
- Policy decision events created during approval carry the same `run_id`.
- Event listing can filter by `run_id` using real stored data.
- Existing API calls without `run_id` remain compatible.

Implemented:

- `open_workspace(..., run_id=None)` and `checkout(..., run_id=None)` create
  run-scoped workspace handles.
- Workspace `write`, `publish`, `publish_manifest`, `consume`, and `delete`
  events inherit the workspace `run_id`.
- Workspace `write`, `publish`, `publish_manifest`, `consume`, `delete`, and
  `search` can persist optional `tool_call_id`.
- `approve`, `archive_ref`, and `merge_workspace` accept optional `run_id`.
- Approval policy decision events inherit the approval `run_id`.
- Tests verify coder/tester/reviewer run ids and workspace tool-call ids are
  persisted and filterable.

Remaining:

- First-class run metadata objects are implemented through M28.

### M23: Run-Scoped Bundle Export

Status: complete

Acceptance criteria:

- Python can export a portable bundle by `run_id`.
- The run bundle includes every event in that run.
- The run bundle includes upstream causal events, nodes, ref audit rows, and
  blobs needed by those run events.
- The run bundle does not include future events from other runs merely because
  they later consume the same nodes.
- The run bundle uses the existing event bundle schema and import path.
- Missing run ids raise a typed event lookup failure.

Implemented:

- Added `AnfsEngine.export_run_bundle(run_id, output_dir, include_blobs=True,
  policy_label_excludes=None)`.
- The exported bundle root is the last event in the run by `event_sequence`.
- Export follows the run's event set plus backward lineage dependencies up to
  that root sequence.
- Import remains `import_event_bundle(bundle_path, bundle_objects_dir=None,
  policy_label_excludes=None)`.
- Tests verify a run bundle preserves unpublished run-local draft output and
  excludes a later tester run.

Remaining:

- First-class run metadata objects are implemented through M28.

### M24: Search-As-Context Event Logging

Status: complete

Acceptance criteria:

- `Workspace.search(query, scope)` keeps the existing Python return shape.
- Search records a `search` event with agent, workspace, optional run id, query,
  scope, and result count.
- Search result nodes are linked to the event as typed input edges.
- Search does not mutate blobs, nodes, or refs.
- Search events are visible through `events(kind="search")` and `event(...)`.

Implemented:

- `Workspace.search()` now writes an append-only `search` event after resolving
  the FTS result set.
- Search payload stores `query`, `scope`, and `result_count`.
- Search result edges use roles `search_result:<index>` and logical paths set
  to the matched ref names.
- Tests verify run/workspace propagation, payload contents, edge roles, and
  event listing edge counts.
- Optional `tool_call_id` propagation is implemented through M42.

Remaining:

- Represent embedding/vector indexes as derived `index` nodes once semantic
  retrieval moves beyond FTS5.

### M25: Concurrent Event Sequence Allocation

Status: complete

Acceptance criteria:

- Event sequence allocation is owned by SQLite, not computed with
  application-side `MAX(seq)+1`.
- Multiple `AnfsEngine` handles can append events to the same database without
  duplicate sequence numbers.
- SQLite writer-lock contention waits briefly instead of failing immediately.
- Event sequence remains contiguous for successfully committed events.
- Existing databases with preexisting `event_sequence` rows can seed the new
  allocator without mutating immutable event rows.

Implemented:

- Added internal `event_sequence_allocations(seq INTEGER PRIMARY KEY
  AUTOINCREMENT)` table.
- `insert_event_sequence_if_missing()` now allocates seq values through the
  SQLite autoincrement table.
- `init_db()` seeds the allocator from the current max sequence for older DBs.
- Connection initialization sets `PRAGMA busy_timeout=5000`.
- `verify_integrity()` checks event/sequence count parity and contiguous seqs.
- Added a multi-process test where four independent engine handles concurrently
  write events to the same database and produce a contiguous sequence.
- Added a separate shared-ref stale-version contention regression through
  M165.

Remaining:

- Add longer-running soak/stress jobs outside the lightweight demo script if
  this kernel is used under sustained swarm workloads.

### M26: Workspace Merge Policy Decision Log

Status: complete

Acceptance criteria:

- Successful merge attempts record an allow `policy_decision`.
- Rejected merge attempts record a deny `policy_decision` before returning
  `RefConflictError`.
- Denied merge attempts do not create `merge_workspace` events or mutate base
  refs.
- Merge policy payloads identify base, workspace, decision, reason, conflict
  count, and change count.
- Merge policy events link relevant conflict/change nodes through typed edges.

Implemented:

- Added `workspace_merge` policy decision events for merge allow and deny paths.
- Conflict denials commit only the policy decision event before returning the
  typed ref conflict error.
- Successful merges record the allow decision in the same transaction as the
  merge event and ref mutations.
- Tests verify allow payloads, deny payloads, conflict edges, and that denied
  merges leave base refs untouched.

Remaining:

- Add a configurable policy registry if merge policy grows beyond conflict
  detection.

### M27: Event Bundle Checksum Verification

Status: complete

Acceptance criteria:

- Newly exported bundles include a bundle-level metadata checksum.
- Import verifies the checksum before writing imported metadata or objects.
- Tampered checked bundle metadata raises `StorageCorruptionError`.
- Existing bundles without a checksum remain importable for compatibility.
- Object byte hash verification remains independent from bundle metadata
  checksum verification.

Implemented:

- Added optional `bundle_checksum` to `anfs.event_bundle.v1` JSON output.
- Export computes the checksum over the canonical bundle JSON with the checksum
  field omitted.
- Import recomputes and verifies the checksum when present.
- Tests verify checksum shape on export and rejection after tampering with event
  metadata.

Remaining:

- HMAC-SHA256 bundle signatures are implemented through M143. Public-key
  signing and external PKI remain outside the compact kernel.

### M143: HMAC-Signed Event Bundles

Status: complete

Acceptance criteria:

- Event bundle export can attach an optional authenticated signature without
  changing default unsigned bundle compatibility.
- Run bundle export uses the same optional signing path.
- The signature covers the same canonical metadata payload used for
  `bundle_checksum`.
- Import can verify a present signature when a `signature_key` is supplied.
- Import can require a signature and reject unsigned bundles before writing
  canonical rows or object bytes.
- Import requires `signature_key` when `require_signature=True`.
- Wrong signature keys raise `StorageCorruptionError`.

Implemented:

- Added optional `bundle_signature` to `anfs.event_bundle.v1`.
- Added `signing_key=None` and `signer_id=None` to `export_event_bundle(...)`
  and `export_run_bundle(...)`.
- Added `signature_key=None` and `require_signature=False` to
  `import_event_bundle(...)`.
- Implemented local HMAC-SHA256 signing over canonical bundle metadata with the
  checksum and signature fields omitted.
- Added regression coverage for signed bundle shape, required-signature import,
  wrong-key rejection, missing verification-key rejection, and
  required-signature rejection for unsigned bundles.

Remaining:

- Public-key signatures, certificate chains, and external trust policy remain
  outside the kernel MVP.

### M144: Explicit Workspace Fork

Status: complete

Acceptance criteria:

- Callers can fork one workspace into another without copying blob bytes.
- The fork preserves copy-on-write behavior through immutable node ids.
- Existing target refs are rejected rather than overwritten.
- Fork events carry source and target workspace metadata.
- Fork events record typed source input edges and target workspace output edges.
- Fork-created target refs have ref audit rows.
- Integrity verification diagnoses malformed fork events and fork ref-audit
  rows without matching typed fork edges.

Implemented:

- Added `fork_workspace(source_workspace, target_workspace, agent_id,
  run_id=None, tool_call_id=None) -> Workspace`.
- Added `src/fork.rs` with workspace draft-ref projection over existing
  immutable nodes.
- Added `fork_workspace` events with `fork_source:*` and
  `fork_workspace_view:*` edges.
- Extended `verify_integrity()` edge-shape and ref-event linkage checks for
  `fork_workspace`.
- Added regression coverage for copy-on-write fork behavior, event payload and
  typed edges, target-ref conflict rejection, self-fork rejection, and malformed
  fork event diagnostics.

Remaining:

- Branch naming policy is implemented through M171. Richer branch lifecycle and
  distributed/remote branch coordination remain future work.

### M145: Read-Only Operational Compaction Plan

Status: complete

Acceptance criteria:

- Operators can inspect event/ref history pressure without mutating canonical
  state.
- Operators can see inactive refs, unreachable blob counts/bytes, active GC
  pins, derived projection row counts, and SQLite freelist pages.
- The planning API must not run GC, VACUUM, archival, or canonical repair.
- Regression coverage proves the plan does not change integrity state or GC
  candidates.

Implemented:

- Added `compaction_plan() -> list[(area, count, action, mutable_scope)]`.
- The plan reports canonical event count, ref audit history count, inactive ref
  count, unreachable blob count and bytes, active GC pin count, active
  retention policy count, inline blob count and bytes, rebuildable derived
  projection rows, and SQLite freelist pages.
- Recommendations point operators to existing narrow paths such as
  `collect_garbage(..., dry_run=True)` and `rebuild_derived_indexes()` while
  keeping physical compaction out of the read-only planner.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Full event/ref history archive export exists through M164. Physical event/ref
  archival compaction remains future admin-mode work. SQLite freelist VACUUM
  orchestration is implemented through M150, retention-policy automation is
  implemented through M152, and inline blob compaction is implemented through
  M156.

### M150: SQLite Vacuum Admin Workflow

Status: complete

Acceptance criteria:

- Admins can inspect SQLite freelist pressure without mutation.
- Admins can explicitly run SQLite `VACUUM` through a narrow API.
- Dry-run is enabled by default.
- VACUUM does not mutate canonical rows, archive event/ref history, repair
  integrity issues, compact inline blobs, or collect object bytes.
- Regression coverage proves dry-run leaves freelist pages unchanged and
  non-dry-run reduces freelist pressure while preserving readable nodes and
  integrity.

Implemented:

- Added `vacuum_database(dry_run=True) -> (freelist_pages_before,
  freelist_pages_after, ran)`.
- Added regression coverage that creates SQLite freelist pressure, verifies
  dry-run behavior, runs `VACUUM`, checks freelist reduction, and confirms
  `verify_integrity()` remains clean.

Remaining:

- Full event/ref history archive export exists through M164. Physical event/ref
  archival compaction remains future admin-mode work. Inline blob compaction is
  implemented through M156.

### M152: Retention Policy Automation

Status: complete

Acceptance criteria:

- Admins can persist append-only retention policies without mutating existing
  GC metadata.
- Active policies are derived from event sequence order.
- A retention policy runner computes the current GC cutoff from `min_age_ms`.
- Dry-run remains the default.
- Physical deletion delegates to the existing bounded `collect_garbage(...)`
  executor and records the source retention policy in the GC event payload.
- Disabled policies cannot run.

Implemented:

- Added `retention_policy_events` with immutable triggers.
- Added `set_retention_policy(...)`, `retention_policies(...)`, and
  `run_retention_policy(...)`.
- Added retention policy integrity checks for wrong event kind, invalid effect,
  invalid workspace flag, invalid age/limit, and missing audit rows.
- `compaction_plan()` now reports active retention policy count.
- Added regression coverage for policy creation, active/history listing,
  dry-run behavior, bounded physical collection, GC event policy attribution,
  disabling, and clean integrity verification.

Remaining:

- Retention policies automate existing local GC only; they do not archive
  event/ref history. Inline blob compaction is implemented through M156 as a
  separate explicit admin workflow.
- Distributed retention coordination remains future work.

### M151: Checked Worktree Commit Base Lease

Status: complete

Acceptance criteria:

- A materialized ordinary directory can be committed with an explicit base check
  against `.anfs-worktree-manifest.json`.
- Checked commit rejects stale worktrees when the current workspace changed,
  deleted a path, or added a path after materialization.
- The base check and all worktree mutations happen in one SQLite transaction.
- Existing `commit_worktree(...)` callers remain backward compatible when they
  do not opt in.

Implemented:

- Added `require_manifest_match=False` to `commit_worktree(...)`.
- Checked commit validates manifest schema, workspace, paths, refs, and node
  ids against the current visible workspace entries before applying writes.
- Worktree commit events record whether a manifest match was required.
- Added regression coverage for stale checked commit rejection, refresh, checked
  success, lineage preservation, and clean integrity verification.

Remaining:

- This is a worktree lease, not a FUSE mount or distributed coordinator.
- Cross-machine namespace coordination and remote branch leases remain future
  work.

### M168: Worktree Readiness Preflight

Status: complete

Acceptance criteria:

- Callers can inspect a materialized ordinary directory before attempting a
  checked commit.
- The preflight reports whether the input directory exists.
- The preflight reports whether `.anfs-worktree-manifest.json` still matches
  current workspace refs.
- The preflight reports added, modified, deleted, and unchanged local file
  counts.
- The preflight reports whether a checked commit would pass its base lease
  precondition.
- The API is read-only and does not write commit events or mutate refs.

Implemented:

- Added `AnfsEngine.worktree_readiness(workspace, input_dir,
  policy_label_excludes=None)`.
- Added a worktree change-count helper reusing the same active workspace view
  and manifest lease check used by `commit_worktree(...,
  require_manifest_match=True)`.
- Added regression coverage in `tests/test_demo.py` for missing input
  directory, clean materialized worktree, local edit, and stale external
  workspace update.
- Updated `README.md`, `docs/01_prd.md`, `docs/03_progress_tracker.md`,
  `docs/05_agent_filesystem_design.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- This is still an ordinary-directory adapter, not FUSE. Kernel mount behavior
  remains future work.

### M169: Redundancy And Complexity Audit

Status: complete

Acceptance criteria:

- The project has an explicit answer to whether the agent-native filesystem
  design is becoming too redundant or complex.
- The answer distinguishes canonical kernel state from projections, adapters,
  external services, and benchmarks.
- Future FUSE, managed vector search, model-backed extraction, richer policy
  algebra, remote coordination, and physical history compaction work has an
  entry gate before it can add canonical state.
- The audit maps complexity back to the Amplify filesystem-for-agents
  requirements instead of treating breadth as self-justifying.

Implemented:

- Added `docs/09_complexity_audit.md`.
- Classified POSIX-like APIs, ordinary worktree sync, and future FUSE as upper
  compatibility surfaces over one semantic kernel.
- Classified FTS, query, embeddings, chunk embeddings, vector baselines, and
  managed vector/ANN services as retrieval projections or external services,
  not canonical state.
- Classified bundles, history archive export, replay checkpoints, readiness
  plans, and future physical compaction as a safety chain with explicit proof
  boundaries.
- Added complexity failure signals and a simplification backlog for large
  modules and provider/model/FUSE-adjacent work.
- Updated `docs/README.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- The audit is documentation, not an automated lint rule.
- Internal Rust module size pressure remains in `engine.rs`, `integrity.rs`,
  `policy_labels.rs`, `workspace.rs`, `bundle.rs`, and `worktree.rs`; split
  only when reviewability improves without changing the Python API.

### M170: Worktree Symlink And Special-Entry Safety

Status: complete

Acceptance criteria:

- The ordinary-directory compatibility adapter must not follow symlinks during
  worktree commit.
- Symlinks and other special filesystem entries must be rejected before any
  canonical worktree mutation is committed.
- `worktree_readiness(...)` must expose unsupported worktree entries as a
  preflight result instead of forcing callers to discover them only through
  commit failure.
- Existing regular-file worktree materialization and commit behavior remains
  backward compatible.

Implemented:

- `scan_worktree_files(...)` now emits explicit policy-denied messages for
  symlink entries and special entries.
- `worktree_readiness(...)` reports a `supported_entries` row. It returns
  `false` and marks local-change counting / checked-commit readiness as failed
  when unsupported entries are present.
- `commit_worktree(...)` continues to reject unsupported entries before opening
  the SQLite mutation transaction, so no `worktree_commit` event is recorded.
- Added regression coverage proving a symlink to a file outside the
  materialized directory is rejected and not followed.
- Updated `README.md`, `docs/01_prd.md`, `docs/06_kernel_contract.md`,
  `docs/07_agent_fs_requirements_matrix.md`, and
  `docs/08_agent_native_fs_conformance.md`.

Remaining:

- This hardens the ordinary-directory adapter; it is still not a mounted FUSE
  implementation.
- Native filesystem metadata such as permissions, owners, device files, and
  symlink targets are intentionally not canonical ANFS state.

### M187: Worktree Reserved Manifest Path Safety

Status: complete

Acceptance criteria:

- The ordinary-directory compatibility adapter must treat the root
  `.anfs-worktree-manifest.json` path as reserved adapter metadata rather than a
  workspace user file.
- Materialization must reject a canonical workspace user ref with that reserved
  root path instead of overwriting user bytes with adapter metadata.
- Commit-back must reject a current workspace containing that reserved path
  before recording a grouped `worktree_commit` event or mutating canonical refs.
- Local files at the root manifest path remain adapter metadata during scans and
  are not imported as user files.

Implemented:

- Extended `validate_relative_worktree_path(...)` so worktree materialization and
  current-workspace commit scans reject `.anfs-worktree-manifest.json` as a
  root logical user path.
- Preserved `scan_worktree_files(...)` behavior that ignores the local root
  manifest file as adapter metadata.
- Added regression coverage proving materialization rejects a canonical reserved
  path, commit-back rejects a current reserved path without recording
  `worktree_commit`, local manifest metadata is ignored rather than imported,
  and integrity remains clean.
- Wired the regression into the full demo runner and updated README, PRD,
  kernel contract, design, requirements matrix, conformance, and progress docs.

Remaining:

- This is an ordinary-directory adapter namespace-safety proof, not a mounted
  FUSE implementation.

### M181: Worktree Backslash Path Safety Proof

Status: complete

Acceptance criteria:

- The ordinary-directory compatibility adapter must reject worktree paths that
  contain `\`, because that byte is a filename character on POSIX but a path
  separator on Windows-like tooling.
- `worktree_readiness(...)` must surface the ambiguous path as an unsupported
  entry and mark checked commit readiness as failed.
- `commit_worktree(...)` must reject the same path before recording a grouped
  `worktree_commit` event or mutating canonical refs.
- Existing regular-file worktree materialization and commit behavior remains
  unchanged.

Implemented:

- Existing `validate_relative_worktree_path(...)` rejects backslash-containing
  logical paths for both materialization-derived and scanned worktree paths.
- Added regression coverage that creates a normal POSIX file named
  `bad\name.txt` in a materialized worktree, verifies readiness reports the
  unsafe path, verifies checked commit rejects it, and verifies no
  `worktree_commit` event is recorded.
- Wired the new regression into the full demo runner; also wired the existing
  symlink rejection regression into the full demo runner.
- Updated README, PRD, kernel contract, progress, conformance, and requirements
  matrix docs.

Remaining:

- This is a path-safety proof for the ordinary-directory adapter, not a mounted
  cross-platform POSIX/FUSE implementation.

### M171: Workspace Branch Naming Policy

Status: complete

Acceptance criteria:

- Workspace/branch ids must be validated before public lifecycle operations
  record events or mutate refs.
- Valid workspace names remain backward compatible with existing `ws:coder`
  style names.
- Workspace names must not contain `/`, so branch ids cannot be confused with
  workspace-internal logical paths.
- Invalid names are rejected through typed policy errors.

Implemented:

- Added `validate_workspace_name(...)` in `src/naming.rs`.
- Enforced `ws:<ascii-name>` with letters, digits, `.`, `-`, and `_` at
  `open_workspace`, `checkout`, `fork_workspace`, workspace diff/conflict/merge,
  and worktree materialization/readiness/commit boundaries.
- Added regression coverage proving invalid workspace names are rejected before
  checkout/fork events or worktree mutations are recorded, while valid
  hyphen/underscore workspace names remain accepted.
- Updated `README.md`, `docs/01_prd.md`, `docs/03_progress_tracker.md`,
  `docs/05_agent_filesystem_design.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- This is a local branch naming policy, not a richer branch lifecycle manager
  or remote branch coordinator.

### M172: Orphan CAS Object Cleanup

Status: complete

Acceptance criteria:

- Aborted writes/imports or interrupted storage maintenance can leave
  noncanonical object files under `objects_dir`.
- Operators can see orphan object file pressure through `compaction_plan()`.
- Operators can dry-run orphan object cleanup without mutating SQLite rows or
  audit history.
- Physical cleanup verifies object size/hash and deletes only hash-shaped files
  not owned by file-backed `blobs` metadata.
- Worktree multi-file transaction rollback remains canonical-state atomic even
  when a failed file write materialized a file-backed CAS object.

Implemented:

- Added `orphan_object_files(...)` scanning in `src/blob.rs`.
- Added `AnfsEngine.clean_orphan_objects(dry_run=True, limit=None) ->
  (candidate_count, candidate_bytes, removed, dry_run)`.
- Added `orphan_object_files` and `orphan_object_bytes` rows to
  `compaction_plan()`.
- Extended the worktree late-failure rollback test to use a file-backed failed
  write, prove canonical rows roll back, detect the orphan object, dry-run
  cleanup, remove it explicitly, and then successfully commit the same content.
- Updated `README.md`, `docs/01_prd.md`, `docs/03_progress_tracker.md`,
  `docs/05_agent_filesystem_design.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- This is local object-store maintenance, not canonical repair and not
  distributed object coordination.

### M174: Checked Worktree Commit Contention Proof

Status: complete

Acceptance criteria:

- Concurrent checked commits from separate materialized directories with the
  same base manifest must not both advance the same workspace.
- The losing checked commit must return `RefConflictError` during the manifest
  lease check, before recording `worktree_commit` or mutating refs.
- The winning checked commit must remain a normal grouped worktree commit with
  per-path status metadata and lineage from old nodes.
- The final namespace must match exactly one complete commit, never a partial
  mix of both concurrent worktrees.

Implemented:

- Added a multi-process regression with two stale-base checked worktree commits
  over the same workspace.
- The test materializes two ordinary directories, edits different files in each,
  runs both `commit_worktree(..., require_manifest_match=True)` calls through
  spawned processes, and asserts one commit plus one conflict.
- The test verifies only one `worktree_commit` event exists, the rejected
  worktree's edited file is not imported, retained paths still point at their
  base nodes, modified paths preserve lineage, and `verify_integrity()` stays
  clean.
- Updated README, PRD, design, conformance, progress, and requirements matrix
  docs.

Remaining:

- This is a local executable contention proof, not a release-machine swarm soak
  or distributed filesystem coordination result.

### M175: Checked Worktree Stale Path Coverage

Status: complete

Acceptance criteria:

- Checked worktree commits must reject a materialized base when a current path's
  node changed after materialization.
- Checked worktree commits must reject a materialized base when a manifest path
  disappeared from the current workspace after materialization.
- Checked worktree commits must reject a materialized base when a new current
  workspace path appeared after materialization.
- Rejected checked commits must not record `worktree_commit` events or recreate /
  delete refs from stale ordinary directories.

Implemented:

- Existing coverage proves changed-node stale bases are rejected before mutation.
- Added regression coverage for disappeared manifest paths where the current
  workspace ref was deleted after materialization.
- Added regression coverage for newly appeared current paths where an empty
  materialized base cannot overwrite a newer workspace path.
- Both new cases assert `worktree_readiness(...)` reports a failed
  `manifest_lease`, `commit_worktree(..., require_manifest_match=True)` raises
  `RefConflictError`, no grouped `worktree_commit` event is recorded, and
  `verify_integrity()` remains clean.

Remaining:

- This proves local stale-base path-shape handling, not distributed lease
  coordination or long-running swarm contention.

### M146: Strict Schema Status And Future-Version Guard

Status: complete

Acceptance criteria:

- Kernel initialization rejects databases that record unsupported future schema
  versions before running normal initialization.
- Callers can inspect migration status without mutating schema metadata.
- Callers can explicitly require the current supported schema and fail fast if
  the current row is missing, mismatched, or a future version is present.
- Integrity verification still diagnoses future schema rows when they appear
  after an engine is already open.

Implemented:

- Added `schema_status() -> list[(version, name, applied_at, status)]`.
- Added `require_schema_current()`.
- Added pre-initialization future-schema rejection in `AnfsEngine(...)`.
- Added `schema_migration_plan()` and `apply_schema_migrations(...)` through
  M159.
- Added regression coverage for normal `current` status, explicit guard
  failure after future migration insertion, verifier diagnostics, constructor
  rejection on reopen, and future-only database rejection without initializing
  kernel tables.

Remaining:

- Future non-v1 schema upgrades should add explicit versioned migration steps.

### M28: First-Class Run Metadata

Status: complete

Acceptance criteria:

- Python can explicitly start a run with optional agent, workspace, and metadata.
- Python can finish a run into a terminal state.
- Current run state is queryable without replaying all events.
- Run lifecycle transitions write normal `events` plus append-only `run_events`
  audit rows.
- Workspace events can still use `run_id` as before.
- Missing run lookups raise a typed `RunNotFoundError`.
- `verify_integrity()` validates run states and run lifecycle transitions.

Implemented:

- Added `runs` lifecycle view table and append-only `run_events` audit table.
- Added `AnfsEngine.start_run`, `finish_run`, `get_run`, and `runs`.
- Added `RunNotFoundError`.
- Added `run_start` and `run_finish` events with `run_id` populated.
- Tests verify start/list/filter/finish behavior, event ordering, audit rows,
  typed errors, and integrity checks.

Remaining:

- Add richer run ownership/authorization policy if run management becomes a
  multi-role workflow.

### M29: Rich Manifest Child Metadata

Status: complete

Acceptance criteria:

- Manifest child records include role, media type, content digest, and size.
- Existing `manifest_children(node_id)` remains backward compatible.
- Python can query rich child metadata without parsing manifest JSON manually.
- Manifest JSON itself stores the rich child metadata.
- Approval and GC manifest behavior remain unchanged.

Implemented:

- Extended `anfs.manifest.v1` children with optional `role`, `media_type`,
  `digest`, and `size`.
- `publish_manifest()` populates child metadata from immutable node/blob rows.
- Added `AnfsEngine.manifest_child_records(node_id)`.
- Tests verify old child tuple API, rich record API, and manifest JSON contents.

Remaining:

- Define stricter canonical JSON rules if manifest bytes need cross-language
  reproducibility beyond Rust serde field ordering.

### M30: Replay Directory Manifest

Status: complete

Acceptance criteria:

- Materialized replay directories include a machine-readable manifest file.
- The replay manifest records schema, source event id, prefix, inactive-ref
  policy, and materialization timestamp.
- Each materialized file entry maps relative path back to ref name, node id, and
  size.
- The Python return value of `materialize_ref_view_at_event` remains unchanged.
- Archived/deleted refs excluded from active materialization are also excluded
  from the replay manifest.

Implemented:

- `materialize_ref_view_at_event(...)` writes `.anfs-replay-manifest.json`.
- Added `anfs.replay_manifest.v1` JSON shape in the Rust core.
- Tests verify manifest contents for full and active replay directories.

Remaining:

- Atomic staging is implemented through M36.

### M31: Replay Materialization Overwrite Policy

Status: complete

Acceptance criteria:

- Existing materialized target files are rejected by default.
- Existing `.anfs-replay-manifest.json` is rejected by default.
- `overwrite=True` explicitly allows replacement.
- Ref/path resolution and Python return value remain unchanged.
- Policy denial happens before writing new files for detected target conflicts.

Implemented:

- Added optional `overwrite=False` parameter to
  `materialize_ref_view_at_event(...)`.
- Added preflight `ensure_materialization_targets_writable(...)`.
- Tests verify refusal preserves existing bytes and explicit overwrite restores
  replay bytes.

Remaining:

- Atomic staging is implemented through M36.

### M32: Policy-Driven GC Root Pinning

Status: complete

Acceptance criteria:

- Callers can pin a ref's current node as a GC root.
- Pinning and unpinning are append-only events, not mutable hidden state.
- GC roots and candidates include active pins derived from event sequence order.
- Deleted or archived refs can remain physically retained while pinned.
- Unpinned nodes become eligible for GC once no other root reaches them.
- Integrity verification validates GC pin lifecycle transitions.

Implemented:

- Added immutable `gc_pin_events` table with `gc_pin` / `gc_unpin` events.
- Added `pin_ref(...)`, `unpin_gc_root(...)`, and `gc_pins(...)`.
- Added active-pin reachability to `gc_roots(...)` and `gc_candidates(...)`.
- Added verifier checks for invalid pin actions and invalid pin/unpin ordering.
- Tests cover retaining a deleted file-backed ref while pinned, releasing it
  after unpin, physical GC, and refusal to unpin inactive pins.

Remaining:

- Retention windows are implemented through M33.

### M33: Retention Window For Physical GC

Status: complete

Acceptance criteria:

- GC candidates can be filtered by a caller-supplied millisecond cutoff.
- Physical GC applies the same cutoff as dry-run planning.
- A blob is eligible only when it is unreachable and every node referencing that
  blob was created at or before the cutoff.
- Default behavior remains unchanged when no cutoff is supplied.
- GC audit payload records the cutoff used for a physical collection run.

Implemented:

- Added optional `older_than_ms=None` to `gc_candidates(...)`.
- Added optional `older_than_ms=None` to `collect_garbage(...)`.
- Added SQL retention gating based on `MAX(nodes.created_at)` per blob.
- Added tests for recent unreachable blob filtering, no-op physical GC under a
  strict cutoff, and successful collection under a future cutoff.

Remaining:

- Batch limits are implemented through M34; external scheduling controls can
  build on that policy gate.

### M34: Bounded GC Batches

Status: complete

Acceptance criteria:

- GC candidates can be limited to a deterministic ordered batch.
- Physical GC applies the same candidate limit as dry-run planning.
- Default behavior remains unchanged when no limit is supplied.
- Invalid limits are rejected before planning or collection.
- GC audit payload records the limit used for a physical collection run.

Implemented:

- Added optional `limit=None` to `gc_candidates(...)`.
- Added optional `limit=None` to `collect_garbage(...)`.
- Added limit validation with a supported range of `1..=10000`.
- Added SQL `LIMIT` over hash-ordered candidate rows.
- Tests cover two-batch collection of three file-backed blobs and invalid limit
  rejection.

Remaining:

- External scheduling can repeatedly call bounded batches.

### M35: Namespace Snapshot Manifest Refs

Status: complete

Acceptance criteria:

- Python can freeze the current active refs under a namespace into one immutable
  manifest node.
- Deleted and archived refs are excluded from the snapshot.
- Snapshot children carry path, node id, role, media type, digest, and size.
- Snapshot creation writes a normal event with child input edges and manifest
  output edge.
- Snapshot refs are published immutable refs and participate in GC reachability.
- Empty namespace snapshots are rejected with a typed policy error.

Implemented:

- Added `AnfsEngine.snapshot_namespace(prefix, snapshot_ref, agent_id,
  kind="resource", run_id=None)`.
- Snapshot manifests use existing `anfs.manifest.v1` format with child role
  `snapshot_child`.
- Added `snapshot_namespace` events with run id propagation and typed edges.
- Added tests for active-view snapshot contents, event edges, GC reachability,
  and empty namespace rejection.

Remaining:

- Checkout accepts snapshot manifest refs directly through M39.
- Merge accepts snapshot manifest refs directly through M45.

### M36: Atomic Replay Materialization Staging

Status: complete

Acceptance criteria:

- Replay materialization can stage files in a sibling temporary directory before
  publishing the target directory.
- New replay directories use atomic staging by default.
- `overwrite=True` replaces the target directory through staging rather than
  leaving stale files behind.
- Existing target conflicts are still rejected before writing when
  `overwrite=False`.
- The Python return value remains unchanged.

Implemented:

- Added optional `atomic=True` to `materialize_ref_view_at_event(...)`.
- Added staging-directory publish path for new replay directories and explicit
  overwrites.
- Added shared file writer for staged and direct materialization paths.
- Tests verify explicit overwrite restores replay bytes and removes stale files.

Remaining:

- Existing non-overwrite directories still use preflighted direct writes for
  backward compatibility.

### M37: Schema Migration Version Record

Status: complete

Acceptance criteria:

- Database initialization records the current supported kernel schema version.
- Reopening a database preserves the migration record.
- `verify_integrity()` reports missing current schema records.
- `verify_integrity()` reports future schema versions unsupported by this
  engine.
- Normal clean databases continue to verify with no issues.

Implemented:

- Added `CURRENT_SCHEMA_VERSION` and `CURRENT_SCHEMA_NAME`.
- `init_db()` records `anfs_kernel_schema_v1` in `schema_migrations`.
- Added migration table inspection to `verify_integrity()`.
- Tests verify the migration row on new databases and future-version detection.

Remaining:

- Future structural migrations should add explicit versioned migration steps
  rather than relying only on `CREATE TABLE IF NOT EXISTS`.

### M38: Event Listing Payload And Time Filters

Status: complete

Acceptance criteria:

- Event listing can filter by event creation time lower bound.
- Event listing can filter by event creation time upper bound.
- Event listing can filter by payload substring for debugging.
- Invalid time ranges are rejected with a typed policy error.
- Existing pagination, kind, agent, workspace, and run filters remain
  compatible.

Implemented:

- Added optional `created_after_ms`, `created_before_ms`, and
  `payload_contains` parameters to `events(...)`.
- Added SQL filters over `events.created_at` and `events.payload_json`.
- Added LIKE escaping for payload substring filters.
- Tests verify payload filtering, time range filtering, and invalid range
  rejection.

Remaining:

- Add indexed payload search or JSON-field filters if event payload volumes
  require it.

### M39: Checkout From Snapshot Manifest Refs

Status: complete

Acceptance criteria:

- `checkout(base=...)` accepts a published or approved manifest ref as the base.
- Manifest child paths become workspace logical paths.
- Deleted refs excluded from the original snapshot remain excluded on checkout.
- Checkout base snapshots record the snapshot ref and child node ids.
- Prefix-based checkout remains backward compatible.

Implemented:

- Added manifest-ref detection in checkout base resolution.
- Reused existing `anfs.manifest.v1` children for checkout entries.
- Existing checkout events, `workspace_base_refs`, and ref audit rows are still
  used for snapshot checkout.
- Tests verify checkout from `snapshot_namespace(...)` output and exclusion of
  deleted snapshot children.

Remaining:

- Merge accepts snapshot manifest refs directly through M45.

### M40: Policy Decision Reason Codes

Status: complete

Acceptance criteria:

- Lineage approval policy decisions include stable reason codes.
- Workspace merge policy decisions include stable reason codes.
- Existing human-readable `reason` fields remain present for compatibility.
- Policy decision filtering by target ref remains unchanged.
- Tests verify allow and deny reason codes.

Implemented:

- Added `reason_code` to lineage approval policy payloads.
- Added `reason_code` to workspace merge policy payloads.
- Current lineage codes are `lineage_evidence_missing` and
  `lineage_evidence_covers_target`.
- Current merge codes are `workspace_conflicts` and
  `no_workspace_conflicts`.
- Tests assert reason codes for approval allow/deny and merge allow/deny.

Remaining:

- Policy registry value rules are implemented through M121.

### M41: Policy Decision Field Filters

Status: complete

Acceptance criteria:

- Policy decision listing remains backward compatible for `target_ref`.
- Callers can filter policy decisions by policy name.
- Callers can filter policy decisions by allow/deny decision.
- Callers can filter policy decisions by stable reason code.
- Policy decision rows are returned in event sequence order.

Implemented:

- Extended `policy_decisions(...)` with optional `policy`, `decision`, and
  `reason_code` filters.
- Replaced split target-only SQL with one JSON-field-filtered query.
- Ordered results by `event_sequence.seq`.
- Tests verify policy, decision, reason-code, and combined filters.

Remaining:

- Policy registry value rules are implemented through M121.

### M42: Workspace Tool-Call Trace Propagation

Status: complete

Acceptance criteria:

- Workspace write events can record `tool_call_id`.
- Workspace publish events can record `tool_call_id`.
- Workspace manifest publish events can record `tool_call_id`.
- Workspace consume events can record `tool_call_id`.
- Workspace delete events can record `tool_call_id`.
- Workspace search events can record `tool_call_id`.
- Existing calls without `tool_call_id` remain backward compatible.

Implemented:

- Added optional `tool_call_id=None` to `publish(...)`.
- Added optional `tool_call_id=None` to `publish_manifest(...)`.
- Added optional `tool_call_id=None` to `consume(...)`.
- Added optional `tool_call_id=None` to `delete(...)`.
- Added optional `tool_call_id=None` to `search(...)`.
- Tests verify tool-call ids persisted on workspace write, publish, manifest,
  consume, delete, and search events.

Remaining:

- Engine-level event-producing operations are implemented through M43.

### M43: Engine Tool-Call Trace Propagation

Status: complete

Acceptance criteria:

- Checkout events can record `tool_call_id`.
- Approval events and their `policy_decision` events can record the same
  `tool_call_id`.
- Archive events can record `tool_call_id`.
- Namespace snapshot events can record `tool_call_id`.
- Workspace merge events and their allow/deny `policy_decision` events can
  record the same `tool_call_id`.
- Denied merge attempts still record the policy tool-call id even though no
  `merge_workspace` event is created.
- Existing calls without `tool_call_id` remain backward compatible.

Implemented:

- Added optional `tool_call_id=None` to `checkout(...)`.
- Added optional `tool_call_id=None` to `approve(...)`.
- Added optional `tool_call_id=None` to `archive_ref(...)`.
- Added optional `tool_call_id=None` to `snapshot_namespace(...)`.
- Added optional `tool_call_id=None` to `merge_workspace(...)`.
- Propagated engine tool-call ids into approval and merge policy-decision
  events.
- Tests verify checkout, snapshot, approve, archive, merge allow, and merge
  deny trace propagation.

Remaining:

- Export/import already preserves event `tool_call_id` through the bundle
  schema.

### M44: Explicit Reject Lifecycle Operation

Status: complete

Acceptance criteria:

- Published refs can transition to `rejected` through a Rust-owned API.
- Rejection records a `reject` event with optional reason payload.
- Rejection records target input/output edges for lineage and replay.
- Rejection updates `refs` and `ref_events` atomically.
- Invalid rejection transitions raise `InvalidStateTransitionError`.
- `run_id` and `tool_call_id` propagate to reject events.
- Rejected refs can still transition to `archived` through the existing
  lifecycle rule.

Implemented:

- Added `AnfsEngine.reject_ref(target_ref, agent_id, reason=None, run_id=None,
  tool_call_id=None)`.
- Reused the existing lifecycle validator for `published -> rejected` and
  `rejected -> archived`.
- Added tests for event payload, edges, ref history, tool-call propagation,
  archive-after-reject, and rejection of `approved -> rejected`.

Remaining:

- Additional general role configuration beyond explicit-purpose capabilities
  remains future work.

### M45: Merge From Snapshot Manifest Refs

Status: complete

Acceptance criteria:

- `workspace_diff(base, workspace)` can use a published or approved manifest ref
  as the base view.
- `workspace_conflicts(base, workspace)` can compare a workspace checked out
  from a snapshot manifest against the current source namespace.
- `merge_workspace(base, workspace, ...)` can accept a snapshot manifest ref as
  `base`.
- Snapshot merges update the original namespace recorded in the snapshot
  manifest metadata, not children under the snapshot ref name.
- Conflict rejection still leaves target refs untouched and creates only a deny
  policy event.
- Prefix-based diff, conflict detection, and merge remain backward compatible.

Implemented:

- Added base-view loading for manifest refs using existing
  `anfs.manifest.v1` children.
- Added snapshot manifest source-prefix resolution from node `metadata_json`.
- Updated merge apply logic to write to the resolved source namespace when
  `base` is a snapshot manifest ref.
- Added tests for non-conflicting snapshot merge and current-base conflict
  detection from a snapshot checkout.

Remaining:

- Generic non-snapshot manifests still do not define a merge target namespace;
  only namespace snapshots carry the required source prefix.

### M46: Explicit Ref Version Preconditions

Status: complete

Acceptance criteria:

- Approval decisions can include an expected target `ref_version`.
- Rejection decisions can include an expected target `ref_version`.
- Archive decisions can include an expected target `ref_version`.
- Version mismatches raise `RefConflictError`.
- Stale approval preconditions fail before lineage checks or policy-decision
  events are written.
- Existing calls without `expected_version` remain backward compatible.

Implemented:

- Added optional `expected_version=None` to `approve(...)`.
- Added optional `expected_version=None` to `reject_ref(...)`.
- Added optional `expected_version=None` to `archive_ref(...)`.
- Added a Rust helper that compares the fetched `RefRecord.ref_version` before
  lifecycle validation or event insertion.
- Tests verify successful guarded reject/archive, stale archive rejection, and
  stale approval rejection without policy-event side effects.

Remaining:

- Write/publish operations already use internal optimistic checks; future API
  variants can expose expected versions for workspace draft updates if needed.

### M47: Review Separation Policy Gate

Status: complete

Acceptance criteria:

- An agent that produced or published a target node cannot approve that target.
- An agent that produced or published a target node cannot reject that target.
- Self-review denials write an append-only `policy_decision` event.
- Self-review denials use stable policy metadata:
  `policy="review_separation"`, `decision="deny"`, and
  `reason_code="self_review_denied"`.
- Denied self-review leaves the target ref state unchanged.
- Successful review by a different agent remains backward compatible.

Implemented:

- Added a Rust producer check over output edges from creation/publication
  events for the target node.
- Added review-separation denial checks to `approve(...)` before lineage
  validation.
- Added review-separation denial checks to `reject_ref(...)` before writing the
  reject event.
- Tests verify self-approval denial, self-rejection denial, policy payloads,
  run/tool-call propagation, unchanged refs, and successful review by another
  agent.

Remaining:

- General role policy configuration beyond explicit-purpose capabilities remains
  future work.

### M48: Workspace-Scoped Draft Search

Status: complete

Acceptance criteria:

- `Workspace.search(..., scope="draft")` only returns draft refs from the
  caller workspace namespace.
- Draft search results record normal search events and result edges.
- Published search remains a global lifecycle view.
- Existing `published` and `approved` search behavior remains backward
  compatible.

Implemented:

- Added a workspace ref-name filter to draft-scope search in the Rust kernel.
- Kept global state-only filtering for published and approved scopes.
- Tests verify that two workspaces with matching draft text only see their own
  draft nodes, and that published search remains visible across workspaces.

Remaining:

- Broader read permission policy can build on this scoped-search boundary.

### M49: Versioned Consume Event Payloads

Status: complete

Acceptance criteria:

- Consume events record the consumed ref name.
- Consume events record the consumed node id.
- Consume events record the lifecycle state observed at consume time.
- Consume events record the `ref_version` observed at consume time.
- Consume events retain the optional purpose field.
- Existing `consume(...) -> node_id` return behavior and consumed input edge are
  unchanged.

Implemented:

- Replaced the old raw-purpose consume payload with structured JSON containing
  `ref_name`, `node_id`, `state`, `ref_version`, and `purpose`.
- Kept the `consumed` input edge with the consumed ref as `logical_path`.
- Tests verify that consuming a ref records the pre-archive state/version even
  when the ref is archived later.

Remaining:

- Broader read APIs can reuse this versioned-consumption payload shape.

### M50: Controlled Ref Read API

Status: complete

Acceptance criteria:

- Workspace agents can read bytes through a ref-oriented API.
- Published and approved refs are readable globally.
- Draft refs are readable only from the caller workspace namespace.
- Cross-workspace draft reads raise `PolicyDeniedError`.
- Successful reads record `read_ref` events with ref name, node id, lifecycle
  state, `ref_version`, and optional purpose.
- Successful reads link the read node through an input event edge.
- Existing diagnostic `read_node(node_id)` remains backward compatible.

Implemented:

- Added `Workspace.read_ref(ref_name, purpose=None, tool_call_id=None) -> bytes`.
- Added Rust policy checks for published/approved global reads and
  caller-workspace draft reads.
- Added `read_ref` events with versioned payloads and `read_ref` input edges.
- Tests verify own-draft read, denied cross-workspace draft read, published
  cross-workspace read, payload contents, tool-call propagation, and edges.

Remaining:

- A configurable read policy can later extend this beyond lifecycle/workspace
  rules.

### M51: Versioned Search Result Payloads

Status: complete

Acceptance criteria:

- Search events retain the existing query, scope, and result count metadata.
- Search events record each returned result's ref name.
- Search events record each returned result's node id.
- Search events record each returned result's lifecycle state at search time.
- Search events record each returned result's `ref_version` at search time.
- Existing Python search return shape remains unchanged.
- Existing search result input edges remain unchanged.

Implemented:

- Extended the Rust FTS query to load `ref_name`, `state`, and `ref_version`
  for every matched result.
- Added a versioned `results` array to search event payloads.
- Kept return values as `(node_id, snippet)` and kept `search_result:<index>`
  input edges.
- Tests verify published and draft search payloads include versioned result
  metadata.

Remaining:

- Future semantic/vector index nodes can reuse the same result payload shape.

### M52: Event Listing Tool-Call Filter

Status: complete

Acceptance criteria:

- `events(...)` can filter directly by `tool_call_id`.
- The new filter composes with existing `kind`, run, workspace, time, and
  payload filters.
- Existing event list row shape remains unchanged.
- Existing calls without `tool_call_id` remain backward compatible.

Implemented:

- Added optional `tool_call_id=None` to `AnfsEngine.events(...)`.
- Added a SQL predicate against `events.tool_call_id` in the append-only event
  listing query.
- Kept `tool_call_id` as a filter-only field so the stable list row remains
  `(seq, event_id, kind, agent_id, workspace_id, created_at, input_count,
  output_count)`.
- Tests verify direct `tool_call_id` filtering, `kind + tool_call_id`
  filtering, and empty results for unknown tool calls.

Remaining:

- Future dedicated trace views can expose richer tool-call summaries without
  changing the low-level event stream API.

### M53: Node Provenance Event Lookup

Status: complete

Acceptance criteria:

- The engine can list events that touched a specific immutable node.
- Results use the same stable row shape as `events(...)`.
- Callers can filter by edge `direction`.
- Callers can filter by edge `role`.
- Results support stable pagination with `after_seq` and bounded `limit`.
- Missing nodes raise `NodeNotFoundError`.
- Invalid directions raise `PolicyDeniedError`.

Implemented:

- Added `AnfsEngine.node_events(node_id, direction=None, role=None,
  after_seq=0, limit=100)`.
- Added a Rust SQL query that uses `EXISTS` against `event_edges`, avoiding
  duplicate event rows when a node appears under multiple roles in one event.
- Kept input/output edge counts as totals for each returned event, matching
  `events(...)`.
- Tests verify write, publish, and derived-write provenance for a node, plus
  direction, role, pagination, and error handling.

Remaining:

- A higher-level lineage neighborhood API can later walk ancestors/descendants
  across multiple events.

### M54: Recursive Lineage Node Query

Status: complete

Acceptance criteria:

- The engine exposes the same recursive lineage relation used by approval
  validation as a read-only API.
- `ancestors` walks from an output node back to event input nodes.
- `descendants` walks from an input node forward to event output nodes.
- The returned set includes the starting node.
- Missing nodes raise `NodeNotFoundError`.
- Invalid directions raise `PolicyDeniedError`.

Implemented:

- Added `AnfsEngine.lineage_nodes(node_id, direction="ancestors")`.
- Generalized the internal recursive SQLite query so approval and public
  lineage inspection share the same traversal semantics.
- Added deterministic ordering for returned node ids.
- Tests verify ancestor and descendant expansion across a derived write, plus
  invalid direction and missing-node failures.

Remaining:

- Implemented in M55.

### M55: Lineage Graph Edge View

Status: complete

Acceptance criteria:

- The engine can expose the event/edge path connecting recursive lineage nodes.
- The returned rows include stable event sequence.
- The returned rows include event id and event kind.
- The returned rows include from/to node ids.
- The returned rows include from/to edge roles.
- The returned rows include from/to logical paths.
- `ancestors` and `descendants` use the same direction semantics as
  `lineage_nodes(...)`.
- Missing nodes raise `NodeNotFoundError`.
- Invalid directions raise `PolicyDeniedError`.

Implemented:

- Added `AnfsEngine.lineage_graph(node_id, direction="ancestors")`.
- Implemented recursive SQLite graph queries over immutable `event_edges` and
  `event_sequence`.
- Kept the graph as a derived view, with no new storage tables.
- Tests verify ancestor graph edges, descendant graph edges, publish self-edges,
  derived write edges, invalid directions, and missing-node handling.

Remaining:

- A visualization layer can render these rows as a DAG without changing the
  kernel API.

### M56: Ref Audit Chain Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` checks that current mutable `refs` rows are justified by
  append-only `ref_events`.
- The verifier compares each current ref's node id with the latest audit row's
  `new_node_id`.
- The verifier compares each current ref's lifecycle state with the latest
  audit row's `new_state`.
- Missing or incomplete latest audit rows are reported.
- Normal API-created refs still verify cleanly.
- Direct database tampering of current refs is diagnosed.

Implemented:

- Added a verifier query that ranks `ref_events` by `event_sequence.seq` per
  `ref_name` and joins the latest audit state against current `refs`.
- Added diagnostic messages for current/audit divergence and missing complete
  audit rows.
- Added a test that creates a published ref through the API, confirms integrity
  is clean, then mutates `refs.state` directly through SQLite and verifies the
  divergence is reported.

Remaining:

- A future repair tool could materialize a safe report or recovery plan from
  the audit chain, but `verify_integrity()` remains diagnostic-only.
- Historical ref audit node-reference checks are implemented through M70.

### M57: Run Audit Chain Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` checks that current mutable `runs` rows are justified by
  append-only `run_events`.
- The verifier compares each current run's lifecycle state with the latest
  audit row's `new_state`.
- The verifier compares each current run's metadata with the latest audit row's
  `new_metadata_json`.
- Missing latest audit rows are reported.
- Normal API-created run lifecycles still verify cleanly.
- Direct database tampering of current run rows is diagnosed.

Implemented:

- Added a verifier query that ranks `run_events` by `event_sequence.seq` per
  `run_id` and joins the latest audit state against current `runs`.
- Added diagnostic messages for current/audit divergence and missing audit
  rows.
- Added a test that starts and finishes a run through the API, confirms
  integrity is clean, then mutates `runs.state` directly through SQLite and
  verifies the divergence is reported.

Remaining:

- A future repair tool could reuse the audit chain to recommend run-view
  recovery, while keeping `verify_integrity()` diagnostic-only.

### M58: Blob Size Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` checks recorded `blobs.size` for inline blobs.
- `verify_integrity()` checks recorded `blobs.size` for file-backed blobs whose
  object bytes are still present.
- GC-collected file blobs remain exempt from physical file reads.
- Existing hash verification remains unchanged.
- Normal API-created blobs still verify cleanly.
- Direct database tampering of blob size metadata is diagnosed.

Implemented:

- Extended the verifier blob query to load `size`.
- Compared inline content length against recorded size.
- Compared file object byte length against recorded size before hash checking.
- Added diagnostics for inline and file-backed size mismatches.
- Added a test that writes an inline blob through the API, confirms integrity
  is clean, mutates `blobs.size` directly through SQLite, and verifies the size
  mismatch is reported.

Remaining:

- Inline blob compaction reuses these verifier checks through M156.

### M59: Search Index Coverage Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses missing `node_fts` rows for text nodes.
- `verify_integrity()` diagnoses missing `node_fts` rows for JSON nodes.
- `verify_integrity()` diagnoses missing `node_fts` rows for ANFS manifest
  nodes.
- Binary nodes are not required to have FTS rows.
- Existing search behavior and return shape remain unchanged.
- Normal API-created searchable nodes still verify cleanly.
- Direct deletion of a searchable node's FTS row is diagnosed.

Implemented:

- Added a verifier query over `nodes` left-joined to `node_fts`.
- Scoped the check to media types that should be searchable:
  `text/*`, `application/json`, and the ANFS manifest media type.
- Added diagnostics naming the missing node id and media type.
- Added a test that writes a text node through the API, confirms integrity is
  clean, deletes its `node_fts` row directly through SQLite, and verifies the
  missing index row is reported.

Remaining:

- Future semantic/vector index nodes can add their own coverage checks without
  changing this FTS-specific diagnostic.

### M60: Search Index Content Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses orphaned `node_fts` rows whose `node_id` does
  not exist in `nodes`.
- `verify_integrity()` diagnoses duplicate `node_fts` rows for the same node.
- `verify_integrity()` diagnoses stale FTS body text for searchable nodes.
- Body comparison derives expected text from immutable blob bytes.
- GC-collected file-backed blobs remain exempt from body comparison.
- Existing search behavior and return shape remain unchanged.

Implemented:

- Added orphan FTS row detection with a `node_fts` to `nodes` left join.
- Added duplicate FTS row detection grouped by `node_id`.
- Added body verification for searchable media types by comparing FTS `body`
  against inline or file-backed blob bytes.
- Added a test that corrupts a searchable node's FTS state with one stale body,
  one duplicate row, and one orphan row, then verifies all diagnostics.

Remaining:

- Future semantic/vector indexes should model their own index-body/version
  checks as derived index nodes rather than overloading FTS diagnostics.

### M61: Manifest Child Metadata Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` validates all nodes with ANFS manifest media type.
- Manifest JSON schema must be `anfs.manifest.v1`.
- Manifest child paths must not be empty.
- Manifest child paths must not be duplicated within one manifest.
- Manifest child node ids must resolve to existing immutable nodes.
- Manifest child digest must match the referenced child node's blob hash when
  present.
- Manifest child media type must match the referenced child node's media type.
- Manifest child size must match the referenced child node's blob size.

Implemented:

- Added manifest-node scanning to `verify_integrity()`.
- Parsed manifest JSON from immutable blob bytes and reported invalid JSON or
  unsupported schema.
- Validated child path uniqueness and referenced node existence.
- Reused `node_manifest_metadata(...)` to compare child digest, media type, and
  size against authoritative node/blob rows.
- Added a test that inserts a malformed manifest node referencing a real child
  node with bad digest, media type, and size, then verifies all diagnostics.

Remaining:

- Canonical cross-language JSON serialization rules remain future work if
  manifest bytes need deterministic generation outside the Rust kernel.

### M62: Event Edge Direction Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses event edges whose `direction` is not `input`
  or `output`.
- The diagnostic identifies event id, node id, role, and invalid direction.
- Existing lineage, bundle, replay, and event listing behavior remain
  unchanged.
- Normal API-created events still verify cleanly.
- Direct insertion of an invalid event edge is diagnosed.

Implemented:

- Added an `event_edges.direction` domain check to `verify_integrity()`.
- Reported invalid edges as `event_edge <event>/<node>/<role> has invalid
  direction <direction>`.
- Added a test that writes a normal event, confirms integrity is clean, inserts
  a malformed `event_edges` row directly through SQLite, and verifies the
  diagnostic.

Remaining:

- Per-event edge-shape validation can later verify required roles for each
  event kind.

### M63: Core Event Edge Shape Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses malformed `write` events missing their single
  output `result` edge.
- `verify_integrity()` diagnoses malformed `publish` events missing their input
  `workspace_node` edge.
- `verify_integrity()` diagnoses malformed `publish` events missing their output
  `published_ref` edge.
- `verify_integrity()` diagnoses malformed `approve` events missing their input
  `target` edge.
- `verify_integrity()` diagnoses malformed `approve` events missing evidence
  input edges.
- `verify_integrity()` diagnoses malformed `approve` events missing their
  output `approved_target` edge.
- Normal API-created events still verify cleanly.

Implemented:

- Added an aggregate edge-shape verifier for `write`, `publish`, and `approve`
  events.
- Checked required edge roles and cardinality without changing event creation,
  lineage traversal, replay, or bundle export behavior.
- Added a test that creates a valid node, then inserts a malformed `publish`
  event directly through SQLite with no `published_ref` output, and verifies the
  shape diagnostic.

Remaining:

- Additional event kinds such as `consume`, `read_ref`, `search`,
  `publish_manifest`, and `merge_workspace` can get role-specific shape checks
  in later milestones.

### M64: Access And Search Event Edge Shape Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses malformed `consume` events without exactly one
  input `consumed` edge.
- `verify_integrity()` diagnoses malformed `read_ref` events without exactly
  one input `read_ref` edge.
- `verify_integrity()` diagnoses malformed `search` events with missing or
  invalid `result_count` payloads.
- `verify_integrity()` diagnoses malformed `search` events whose
  `result_count` does not match the number of `search_result:*` input edges.
- Normal API-created consume, read, and search events still verify cleanly.

Implemented:

- Added an aggregate shape verifier for `consume`, `read_ref`, and `search`
  events.
- Parsed search event payload JSON in Rust and compared `result_count` against
  typed result input edges.
- Added a test that inserts malformed consume, read, and search events directly
  through SQLite and verifies all diagnostics.

Remaining:

- `publish_manifest`, `delete_ref`, `reject`, `checkout`, and
  `merge_workspace` can get role-specific checks in later milestones.

### M65: Manifest And Lifecycle Event Edge Shape Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses malformed `publish_manifest` events without at
  least one input `manifest_child:*` edge.
- `verify_integrity()` diagnoses malformed `publish_manifest` events without
  exactly one output `manifest` edge.
- `verify_integrity()` diagnoses malformed `delete_ref` events without exactly
  one input `deleted_ref` edge.
- `verify_integrity()` diagnoses malformed `reject` events without exactly one
  input `target` edge.
- `verify_integrity()` diagnoses malformed `reject` events without exactly one
  output `rejected_target` edge.
- Normal API-created manifest publish, delete, and reject events still verify
  cleanly.

Implemented:

- Added an aggregate shape verifier for `publish_manifest`, `delete_ref`, and
  `reject` events.
- Kept checks focused on required lineage roles and cardinality.
- Added a test that inserts malformed manifest publish, delete, and reject
  events directly through SQLite and verifies diagnostics.

Remaining:

- `merge_workspace`, `policy_decision`, and `gc_collect` can get
  role-specific checks in later milestones.

### M66: Workspace And Snapshot Event Edge Shape Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses base `checkout` events without at least one
  input `base_source:*` edge.
- `verify_integrity()` diagnoses base `checkout` events without at least one
  output `workspace_view:*` edge.
- `verify_integrity()` diagnoses `archive_ref` events without exactly one input
  `archived_ref` edge.
- `verify_integrity()` diagnoses `snapshot_namespace` events without at least
  one input `snapshot_child:*` edge.
- `verify_integrity()` diagnoses `snapshot_namespace` events without exactly
  one output `snapshot_manifest` edge.
- Normal API-created checkout, archive, and snapshot events still verify
  cleanly.

Implemented:

- Added an aggregate shape verifier for `checkout`, `archive_ref`, and
  `snapshot_namespace` events.
- Scoped checkout edge requirements to checkout events that actually carry a
  base payload, so workspace-open events remain valid.
- Added a test that inserts malformed workspace and snapshot events directly
  through SQLite and verifies diagnostics.

Remaining:

- No known workspace/snapshot edge-shape verifier gap remains.

### M67: Policy, Merge, And GC Audit Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses non-merge `policy_decision` events without
  exactly one input `policy_target` edge.
- `verify_integrity()` diagnoses non-merge `policy_decision` events whose
  `evidence_refs` payload count does not match `policy_evidence:*` edges.
- `verify_integrity()` diagnoses `workspace_merge` policy decisions whose
  positive `conflict_count` lacks `merge_conflict:*` input edges.
- `verify_integrity()` diagnoses `workspace_merge` policy decisions whose
  positive `change_count` exceeds `merge_change:*` input edge count.
- `verify_integrity()` diagnoses `merge_workspace` events whose ref audit rows
  do not match `merge_output:*` plus `merge_deleted:*` edge counts.
- `verify_integrity()` diagnoses `merge_workspace` events whose
  `merge_input:*` and `merge_output:*` edge counts diverge.
- `verify_integrity()` diagnoses `gc_collect` events with missing or invalid
  `candidate_count` payloads.
- `verify_integrity()` diagnoses `gc_collect` events whose physical
  `gc_blob_events` rows exceed recorded `candidate_count`.
- `verify_integrity()` diagnoses `gc_blob_events` rows that reference a
  non-`gc_collect` event.
- Normal API-created policy, merge, and GC events still verify cleanly.

Implemented:

- Added policy decision payload/edge consistency checks while keeping
  `workspace_merge` policy semantics separate from lineage approval policy
  semantics.
- Added merge event checks that use `ref_events` as the authoritative mutation
  count, so no-op merges remain valid.
- Added GC audit checks that compare `gc_collect` payload counts with physical
  `gc_blob_events` rows without requiring GC DAG node edges.
- Added a malformed-event test covering policy, merge, and GC diagnostics.

Remaining:

- Future workflow event kinds should add shape checks when their edge roles are
  promoted to stable kernel contracts.

### M68: Bundle Import Edge And Ref-Audit Conflict Hardening

Status: complete

Acceptance criteria:

- Import remains idempotent for the exact same bundle.
- Import into a database with an existing event edge sharing the same primary
  key must verify that `logical_path` matches exactly.
- Import into a database with an existing ref audit row sharing the same primary
  key must verify that old/new node and lifecycle fields match exactly.
- Conflicting imported event edges raise `RefConflictError`.
- Conflicting imported ref audit rows raise `RefConflictError`.
- Existing checksum verification and object-byte verification remain unchanged.

Implemented:

- Replaced silent `INSERT OR IGNORE` acceptance for imported `event_edges` with
  insert-or-verify semantics.
- Replaced silent `INSERT OR IGNORE` acceptance for imported `ref_events` with
  insert-or-verify semantics.
- Added a regression test that imports a valid bundle, then rejects compatible
  old-style bundles with conflicting edge and ref-audit content.

Remaining:

- Optional policy for promoting imported `ref_events` into live destination refs
  remains future work.

### M69: Recreated CAS Blob After Physical GC

Status: complete

Acceptance criteria:

- Writing content whose hash was previously physically collected must
  re-materialize the file-backed object bytes.
- `read_node()` must read the current object file when it exists, even if older
  `gc_blob_events` rows exist for the same hash.
- `verify_integrity()` must verify existing object bytes instead of skipping a
  file-backed blob solely because historical GC audit rows exist.
- If a re-materialized same-hash blob becomes unreachable again, `gc_candidates`
  must return it and `collect_garbage(..., dry_run=False)` must collect it
  again.
- Existing blob metadata insertion must reject conflicting CAS metadata instead
  of silently ignoring it.

Implemented:

- Added insert-or-verify semantics for existing `blobs` rows.
- Changed file-backed blob reads so GC audit rows only explain missing object
  files; they no longer block reads when the object file exists.
- Changed integrity verification to validate existing file-backed object bytes
  even when historical GC rows exist.
- Changed GC candidate filtering so historical GC rows exclude only still-missing
  file-backed objects.
- Added a regression test that collects a file blob, writes the same bytes
  again, reads and verifies the recreated node, then collects it a second time.

Remaining:

- A future storage compaction design may add explicit blob materialization
  generation ids if object stores need stronger physical lifecycle accounting.

### M70: Ref Audit Node Reference Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `ref_events` rows whose `old_node_id` is
  present but does not resolve to an immutable node.
- `verify_integrity()` diagnoses `ref_events` rows whose `new_node_id` is
  missing.
- `verify_integrity()` diagnoses `ref_events` rows whose `new_node_id` does not
  resolve to an immutable node.
- Normal API-created ref audit rows still verify cleanly.

Implemented:

- Added a logical FK verifier for `ref_events.old_node_id` and
  `ref_events.new_node_id`, covering references that SQLite cannot enforce
  directly because `old_node_id` is nullable.
- Added diagnostics naming the bad ref, event id, field, and missing node id.
- Added a regression test that inserts a malformed append-only ref audit row
  with dangling old/new node ids and verifies both diagnostics.

Remaining:

- A future repair tool can use the audit chain to propose fixes, but the kernel
  verifier remains diagnostic-only.

### M71: GC Pin Event Linkage Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `gc_pin_events` rows whose `action='pin'`
  references an event whose kind is not `gc_pin`.
- `verify_integrity()` diagnoses `gc_pin_events` rows whose `action='unpin'`
  references an event whose kind is not `gc_unpin`.
- `verify_integrity()` diagnoses pin/unpin audit rows without exactly one typed
  input root edge matching the audited `ref_name` and `node_id`.
- `verify_integrity()` diagnoses `gc_pin` and `gc_unpin` events without exactly
  one `gc_pin_events` audit row.
- Normal API-created GC pin/unpin events still verify cleanly.

Implemented:

- Added event-kind linkage checks between `gc_pin_events.action` and referenced
  event kind.
- Added typed root-edge checks for `gc_pin_root` and `gc_unpin_root`.
- Added event-side audit cardinality checks for `gc_pin` / `gc_unpin` events.
- Added a regression test that inserts malformed GC pin audit/event rows and
  verifies all diagnostics.

Remaining:

- Future policy retention primitives should add equivalent audit/event/edge
  linkage checks when introduced.

### M72: Run Event Linkage Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `run_events` rows whose transition implies
  `run_start` but references a different event kind.
- `verify_integrity()` diagnoses `run_events` rows whose transition implies
  `run_finish` but references a different event kind.
- `verify_integrity()` diagnoses run audit rows whose referenced event has a
  mismatched `events.run_id`.
- `verify_integrity()` diagnoses run audit rows whose referenced event payload
  has a mismatched `run_id` or lifecycle `state`.
- `verify_integrity()` diagnoses `run_start` and `run_finish` events without
  exactly one `run_events` audit row.
- Normal API-created run start/finish events still verify cleanly.

Implemented:

- Extended the run audit verifier to join `run_events` against `events`.
- Derived expected event kind from the audited state transition.
- Checked referenced event `run_id` and JSON payload fields against the audit
  row.
- Added event-side audit cardinality checks for `run_start` and `run_finish`.
- Added a regression test with malformed run audit/event linkage.

Remaining:

- Future richer run-state machines should extend the transition-to-event-kind
  table in this verifier.

### M73: Ref Audit Chain Continuity Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses the first `ref_events` row for a ref when it
  has non-empty old node/state.
- `verify_integrity()` diagnoses later `ref_events` rows whose old node/state
  does not match the previous audit row's new node/state.
- Normal API-created ref audit chains still verify cleanly.

Implemented:

- Added a window-function verifier over `ref_events` ordered by
  `event_sequence.seq` per ref.
- Checked first-row creation shape and pairwise old/new continuity.
- Added a regression test that inserts malformed first and second audit rows
  for the same ref and verifies both diagnostics.

Remaining:

- A future repair tool can use the discontinuity diagnostics to suggest a
  replay-safe recovery plan.

### M74: Run Audit Chain Continuity Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses the first `run_events` row for a run when it
  has non-empty old state/metadata.
- `verify_integrity()` diagnoses later `run_events` rows whose old
  state/metadata does not match the previous audit row's new state/metadata.
- Normal API-created run audit chains still verify cleanly.

Implemented:

- Added a window-function verifier over `run_events` ordered by
  `event_sequence.seq` per run.
- Checked first-row creation shape and pairwise old/new state/metadata
  continuity.
- Added a regression test that inserts malformed first and second run audit
  rows and verifies both diagnostics.

Remaining:

- Future richer run lifecycle repair tooling can use the same diagnostics to
  suggest recovery plans.

### M75: Checkout Base Snapshot Integrity Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `workspace_base_refs` rows whose
  `checkout_event_id` does not reference a `checkout` event.
- `verify_integrity()` diagnoses checkout base snapshot rows without exactly
  one matching input `base_source:*` edge for the recorded source ref and node.
- `verify_integrity()` diagnoses checkout base snapshot rows without exactly
  one matching output `workspace_view:*` edge for the projected workspace ref
  and node.
- Normal API-created checkout base snapshots still verify cleanly.

Implemented:

- Added a verifier query joining `workspace_base_refs` to checkout events and
  typed checkout edges.
- Checked source edge and workspace projection edge cardinality per snapshot
  row.
- Added a regression test that inserts malformed checkout base snapshot rows
  and verifies event-kind, source-edge, and workspace-edge diagnostics.

Remaining:

- Future checkout variants should preserve the same snapshot/event/edge
  consistency contract.

### M76: Manifest Event Child Edge Consistency Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `publish_manifest` events whose
  `manifest_child:*` input edge count does not match the output manifest body's
  child count.
- `verify_integrity()` diagnoses `publish_manifest` events missing a matching
  input `manifest_child:*` edge for any output manifest child path/node pair.
- `verify_integrity()` diagnoses `snapshot_namespace` events whose
  `snapshot_child:*` input edge count does not match the output manifest body's
  child count.
- `verify_integrity()` diagnoses `snapshot_namespace` events missing a matching
  input `snapshot_child:*` edge for any output manifest child path/node pair.
- Normal API-created manifest publish and namespace snapshot events still verify
  cleanly.

Implemented:

- Added a manifest-producing event verifier for `publish_manifest` and
  `snapshot_namespace`.
- Parsed each output manifest node and compared child path/node pairs against
  typed input edges for the producing event.
- Added a regression test that inserts malformed manifest-producing events with
  missing child inputs and verifies diagnostics for both event kinds.

Remaining:

- Future manifest-producing event kinds should define their child edge role
  prefix and reuse the same verifier pattern.

### M77: Ref Audit Event Edge Linkage Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses ref audit rows for `publish` events whose
  audited ref/node is not backed by a matching output `published_ref` edge.
- `verify_integrity()` diagnoses ref audit rows for lifecycle and workspace
  mutation events whose audited ref/node is not backed by the event's matching
  typed edge.
- Workspace-local write/delete audit rows are checked against their event
  workspace id plus logical path payload, because those event edges use logical
  paths instead of full ref names.
- Normal API-created ref mutations still verify cleanly.

Implemented:

- Added a ref audit linkage verifier for `write`, `publish`,
  `publish_manifest`, `approve`, `reject`, `delete_ref`, `archive_ref`,
  `snapshot_namespace`, and base `checkout` events.
- Added a small typed-edge counting helper used by the verifier.
- Added a regression test that inserts a malformed publish ref audit row with
  no matching `published_ref` output edge.

Remaining:

- `merge_workspace` has a separate count-level verifier today. A future pass can
  tighten it to per-ref edge matching for each changed/deleted base path.

### M78: Merge Ref Audit Per-Ref Edge Linkage Check

Status: complete

Acceptance criteria:

- `verify_integrity()` diagnoses `merge_workspace` ref audit rows whose
  `new_state='published'` does not have exactly one matching output
  `merge_output:*` edge for the audited ref/node.
- `verify_integrity()` diagnoses `merge_workspace` ref audit rows whose
  `new_state='archived'` does not have exactly one matching input
  `merge_deleted:*` edge for the audited ref/node.
- The existing count-level merge checks remain in place.
- Normal API-created merge events still verify cleanly.

Implemented:

- Added per-ref merge linkage checks after the existing merge event shape
  verifier.
- Added a regression test where merge edge counts still balance, but the
  `merge_output:*` edge points at the wrong base ref.

Remaining:

- Future merge statuses should define explicit edge roles before being added to
  the state machine.

### M79: Bundle Internal Reference Closure Validation

Status: complete

Acceptance criteria:

- `import_event_bundle(...)` rejects bundles whose `root_event_id` is absent
  from the bundled `events`.
- `import_event_bundle(...)` rejects event edges that reference an event or node
  missing from the bundle.
- `import_event_bundle(...)` rejects ref audit rows that reference an event or
  old/new node missing from the bundle.
- `import_event_bundle(...)` rejects nodes whose blob hash is missing from the
  bundled blob metadata.
- Rejected structurally invalid bundles do not partially write events into the
  destination database.
- Valid exported bundles remain importable and idempotent.

Implemented:

- Added `validate_event_bundle_references(...)` after checksum verification and
  before import transactions write metadata.
- The validator checks duplicate event/node/blob/edge/ref-audit primary keys
  and validates the bundle's event/node/blob reference closure.
- Added a regression test for old-style unchecked bundles missing the root
  event and for bundles with an edge pointing to a missing node.

Remaining:

- HMAC-SHA256 bundle authentication is implemented through M143. Public-key
  provenance and external trust policy remain future work.

### M80: Bundle Import Rebuilds Derived Search Index

Status: complete

Acceptance criteria:

- `import_event_bundle(...)` rebuilds `node_fts` rows for imported nodes whose
  media type is searchable.
- Search index rows are derived from imported immutable blob bytes, not stored
  as canonical bundle payload.
- Re-importing the same bundle remains idempotent and does not create duplicate
  FTS rows.
- A valid imported bundle verifies cleanly with `verify_integrity()`.

Implemented:

- Added `ensure_imported_node_fts(...)` during node import after node/blob
  metadata has been restored.
- Added `is_searchable_media_type(...)` to keep import behavior aligned with the
  verifier's text/json/manifest coverage rules.
- Tightened the event bundle import roundtrip test to assert FTS row count and
  a clean integrity report after idempotent import.

Remaining:

- Future semantic/vector indexes should use the same derived-index pattern
  rather than being serialized as canonical bundle payload.

### M81: Metadata-Only Bundle Import Reuses Existing CAS Blobs

Status: complete

Acceptance criteria:

- Bundles exported with `include_blobs=False` can import into a compatible
  database that already has the referenced CAS blobs.
- Existing destination blobs are validated by hash through the CAS reader and by
  the size recorded in bundle metadata.
- Fresh databases still require object bytes for metadata-only bundles.
- Imported metadata-only bundles remain integrity-clean when compatible blobs
  are present.

Implemented:

- Added `existing_bundle_blob_matches(...)` and used it before object-byte reads
  for bundle blobs with no `object_path`.
- Kept explicit object-path imports on the existing object-byte verification
  path.
- Added a regression test that exports without blobs, seeds the destination with
  the same content hash, imports the bundle, reads the imported source node, and
  verifies clean integrity.

Remaining:

- Cross-store blob prefetch or remote object lookup can build on the same
  hash/size contract later.

### M82: Bundle Event Source Sequence Preservation

Status: complete

Acceptance criteria:

- Exported bundle events include their source `event_sequence.seq` as
  `source_seq`.
- `import_event_bundle(...)` validates positive, non-duplicated `source_seq`
  values when present.
- Bundles cannot mix events with and without `source_seq`.
- For source-sequenced bundles, the root event must have the maximum
  `source_seq`.
- Import assigns destination event sequences in source relative order, even if
  the JSON event array is reordered.
- Legacy bundles without `source_seq` remain array-order compatible.

Implemented:

- Added optional `source_seq` to `BundleEvent`.
- Updated event and run bundle export queries to serialize source sequence
  numbers.
- Added bundle validation for duplicate/missing/mixed/invalid source sequence
  metadata.
- Added source-sequence sorting before importing events into `event_sequence`.
- Added tests for exported sequence order and reversed event-array import.

Remaining:

- If bundle schemas are versioned beyond `anfs.event_bundle.v1`, sequence
  preservation should become mandatory rather than optional.

### M83: Bundle Source Sequence Tamper Regression Coverage

Status: complete

Acceptance criteria:

- Import rejects bundles with duplicate `source_seq` values before writing any
  events.
- Import rejects bundles that mix events with and without `source_seq` before
  writing any events.
- Import rejects source-sequenced bundles whose root event is not the event with
  maximum `source_seq` before writing any events.
- Valid source-sequenced bundles remain importable.

Implemented:

- Added a regression test that derives tampered checksum-free bundles from a
  valid export and verifies all three invalid source sequence cases are rejected
  with no partial destination events.

Remaining:

- None for current `source_seq` validation coverage.

### M84: Cross-Platform Replay Path Segment Hardening

Status: complete

Acceptance criteria:

- Replay materialization rejects ref-derived path segments containing
  backslashes.
- Existing rejection of empty, `.`, and `..` segments remains unchanged.
- Rejected unsafe replay paths do not create the output directory.
- Normal replay materialization remains unchanged.

Implemented:

- Hardened `materialized_relative_path(...)` to reject any logical path segment
  containing `\\`.
- Added a regression test that publishes a ref with a backslash-containing child
  path and verifies materialization fails with `PolicyDeniedError` without
  writing replay output.

Remaining:

- Future platform-specific path rules should be encoded here rather than in
  callers.

### M85: Replay Materialization Duplicate Path Rejection

Status: complete

Acceptance criteria:

- Replay materialization rejects views where two refs would write the same
  relative output path.
- Rejection happens before creating the output directory.
- Existing overwrite and staging behavior remains unchanged for non-conflicting
  views.

Implemented:

- Added duplicate relative path tracking inside
  `materialize_ref_view_at_event(...)`.
- Added a regression test for the collision between a materialized exact prefix
  ref (`__ref__`) and a child ref named `__ref__`.

Remaining:

- Future path mapping modes should preserve the same one-ref-to-one-output-path
  invariant.

### M86: Direct Replay Overwrite Removes Stale Files

Status: complete

Acceptance criteria:

- `materialize_ref_view_at_event(..., overwrite=True, atomic=False)` removes
  stale files from previous materializations.
- Direct overwrite matches atomic overwrite's replacement semantics.
- Default `overwrite=False` conflict refusal remains unchanged.

Implemented:

- Direct materialization now removes the existing output directory first when
  `overwrite=True`.
- Extended the replay materialization test to verify stale files are removed in
  direct non-atomic overwrite mode.

Remaining:

- If partial direct overwrite failure recovery is needed, callers should keep
  the default `atomic=True` mode.

### M87: Rust Module Refactor Foundation

Status: complete

Acceptance criteria:

- Low-risk shared code is split out of `src/lib.rs` without changing Python API
  behavior.
- Typed Python exception mapping remains intact after extraction.
- Shared serializable structs and row aliases have one canonical module.
- A concrete follow-on module plan exists before moving larger subsystems.

Implemented:

- Added `src/errors.rs` for `AnfsError`, `AnfsResult`, and PyO3 exception
  mapping.
- Added `src/types.rs` for shared row aliases and manifest/bundle/replay data
  structs.
- Added `docs/04_refactor_plan.md` with target module boundaries and extraction
  order.

Remaining:

- Move large subsystems in the documented order, starting with schema and CAS
  blob helpers.

### M88: Rust Physical Subsystem Split

Status: complete

Acceptance criteria:

- `src/lib.rs` no longer contains every subsystem implementation inline.
- Major implementation areas are split into focused Rust source files.
- Python API and runtime behavior remain unchanged.
- The split is documented as a transitional physical split before stricter
  module-boundary hardening.

Implemented:

- Extracted physical implementation files:
  `schema.rs`, `storage.rs`, `workspace_ops.rs`, `events.rs`, `runs.rs`,
  `observability.rs`, `replay.rs`, `bundle.rs`, `refs_lineage_manifest.rs`,
  `integrity.rs`, and `gc_snapshot.rs`.
- Kept root-level `include!` wiring so existing internal function visibility and
  PyO3 behavior remain stable.
- Reduced `src/lib.rs` from 8281 lines to roughly 1832 lines.
- Updated `docs/04_refactor_plan.md` with the completed physical split and the
  next true-module conversion plan.

Remaining:

- Convert include files into true Rust modules with explicit `pub(crate)`
  boundaries.
- Split `refs_lineage_manifest.rs` further into dedicated `refs`, `lineage`,
  and `manifest` modules.

### M89: Schema True Module Conversion

Status: complete

Acceptance criteria:

- `schema.rs` is no longer root-included as plain source text.
- Schema initialization and migration inspection are exposed through explicit
  `pub(crate)` functions.
- Existing database initialization behavior remains unchanged.

Implemented:

- Converted `src/schema.rs` into a real Rust module.
- Added explicit imports for SQLite, shared errors, schema constants, time, and
  event-sequence backfill helpers.
- Updated `src/lib.rs` to `mod schema;` and import `init_db` /
  `schema_migrations` explicitly.

Remaining:

- Continue converting physical include files into true modules, with
  `storage.rs` next.

### M90: Storage Helper True Module Conversion

Status: complete

Acceptance criteria:

- `storage.rs` is no longer root-included as plain source text.
- CAS/blob helpers, id/time helpers, ref-name helpers, and checkout base
  projection helpers are available through explicit crate-internal exports.
- Existing behavior remains unchanged.

Implemented:

- Converted `src/storage.rs` into a true Rust module.
- Added explicit imports for SQLite, CAS hashing, filesystem paths, shared
  errors/types, and current cross-subsystem helpers.
- Updated `src/lib.rs` to `mod storage; use storage::*;`.

Remaining:

- Split the mixed `storage.rs` boundary further into `blob.rs`,
  `naming.rs`/common helpers, and checkout projection helpers after additional
  subsystems are true modules.

### M91: Events True Module Conversion

Status: complete

Acceptance criteria:

- `events.rs` is no longer root-included as plain source text.
- Event insertion, event edge insertion, event sequence allocation/backfill, and
  policy decision event helpers are exposed through explicit crate-internal
  functions.
- Schema initialization imports event-sequence helpers from the event module.
- Existing behavior remains unchanged.

Implemented:

- Converted `src/events.rs` into a true Rust module.
- Added explicit SQLite and JSON imports plus crate helper imports.
- Updated `src/lib.rs` to `mod events; use events::*;`.
- Updated `src/schema.rs` to import `backfill_event_sequence` and
  `seed_event_sequence_allocator` from `crate::events`.

Remaining:

- Event listing APIs still live in `observability.rs`; they can move into a
  richer event-query module later.

### M92: Runs True Module Conversion

Status: complete

Acceptance criteria:

- `runs.rs` is no longer root-included as plain source text.
- Run lifecycle creation, finish, lookup/listing, and run audit helpers are
  exposed through explicit crate-internal functions.
- Existing run lifecycle behavior remains unchanged.

Implemented:

- Converted `src/runs.rs` into a true Rust module.
- Added explicit SQLite, JSON, event, time/id, error, and row-type imports.
- Updated `src/lib.rs` to `mod runs; use runs::*;`.

Remaining:

- `runs.rs` currently depends on the shared `collect_rows` helper that still
  lives in the root include path; move that helper into a common query utility
  once the remaining include files become true modules.

### M93: Observability True Module Conversion

Status: complete

Acceptance criteria:

- `observability.rs` is no longer root-included as plain source text.
- Ref history, event detail, event stream, and node-event query helpers are
  exposed through explicit crate-internal functions.
- Existing observability/query behavior remains unchanged.

Implemented:

- Converted `src/observability.rs` into a true Rust module.
- Added explicit SQLite and shared row/error imports.
- Updated `src/lib.rs` to `mod observability; use observability::*;`.
- Removed the dead root-level `escape_like` helper from `replay.rs` after the
  query filter logic moved fully into `observability.rs`.

Remaining:

- `observability.rs` and `runs.rs` still depend on `collect_rows`, which remains
  in root-included `refs_lineage_manifest.rs`; move it into a common query
  utility module next.

### M94: Shared Query Helper Module

Status: complete

Acceptance criteria:

- Shared query row collection logic no longer lives in a domain subsystem file.
- Converted true modules depend on the helper through an explicit module path.
- Remaining root-included subsystems continue to compile unchanged.

Implemented:

- Added `src/query.rs` with `collect_rows`.
- Updated `src/runs.rs` and `src/observability.rs` to import
  `crate::query::collect_rows`.
- Removed `collect_rows` from `src/refs_lineage_manifest.rs`.
- Kept a crate-root import for remaining include files that still call
  `collect_rows`.

Remaining:

- Convert the remaining root-included subsystems into true modules, then remove
  compatibility imports from `src/lib.rs`.

### M95: GC And Snapshot True Module Conversion

Status: complete

Acceptance criteria:

- `gc_snapshot.rs` is no longer root-included as plain source text.
- GC roots, candidates, collection, pins, and namespace snapshot helpers are
  exposed through explicit crate-internal functions.
- Existing GC and snapshot behavior remains unchanged.

Implemented:

- Converted `src/gc_snapshot.rs` into a true Rust module.
- Added explicit SQLite, JSON, filesystem, path, storage, ref, manifest, event,
  and shared type imports.
- Updated `src/lib.rs` to `mod gc_snapshot; use gc_snapshot::*;`.
- Kept `is_blob_gc_collected` as a crate-internal helper because bundle and
  integrity checks need to distinguish missing files from blobs intentionally
  collected by GC.

Remaining:

- Convert `workspace_ops.rs`, `replay.rs`, `bundle.rs`,
  `refs_lineage_manifest.rs`, and `integrity.rs` from root includes into true
  modules.

### M96: Workspace Ops True Module Conversion

Status: complete

Acceptance criteria:

- `workspace_ops.rs` is no longer root-included as plain source text.
- Workspace diff, conflict detection, checkout-base comparison, merge-target
  prefix resolution, and node existence checks are exposed through explicit
  crate-internal functions.
- Existing workspace merge behavior remains unchanged.

Implemented:

- Converted `src/workspace_ops.rs` into a true Rust module.
- Added explicit SQLite, path, collection, storage/ref, manifest, naming, error,
  and result imports.
- Updated `src/lib.rs` to `mod workspace_ops; use workspace_ops::*;`.
- Kept `ensure_node_exists` as a crate-internal helper for write, lineage, and
  observability paths.

Remaining:

- Convert `replay.rs`, `bundle.rs`, `refs_lineage_manifest.rs`, and
  `integrity.rs` from root includes into true modules.

### M97: Replay True Module Conversion

Status: complete

Acceptance criteria:

- `replay.rs` is no longer root-included as plain source text.
- Ref view reconstruction and filesystem materialization helpers are exposed
  through explicit crate-internal functions.
- Existing replay/materialization behavior remains unchanged.

Implemented:

- Converted `src/replay.rs` into a true Rust module.
- Added explicit SQLite, filesystem, path, UUID, type, and storage helper
  imports.
- Updated `src/lib.rs` to `mod replay; use replay::*;`.
- Removed the now-unused `Uuid` import from the PyO3 facade.

Remaining:

- `replay.rs` still relies on `read_node_bytes`, which lives in the remaining
  root-included refs/manifest subsystem. Move that helper when
  `refs_lineage_manifest.rs` becomes explicit.
- Convert `bundle.rs`, `refs_lineage_manifest.rs`, and `integrity.rs` from root
  includes into true modules.

### M98: Refs Lineage Manifest True Module Conversion

Status: complete

Acceptance criteria:

- `refs_lineage_manifest.rs` is no longer root-included as plain source text.
- Ref mutation/audit helpers, lineage traversal, lineage approval coverage,
  manifest child reads, and node/blob byte reads are exposed through explicit
  crate-internal functions.
- Existing ref lifecycle, lineage, and manifest behavior remains unchanged.

Implemented:

- Converted `src/refs_lineage_manifest.rs` into a true Rust module.
- Added explicit SQLite, filesystem, path, set, GC, query, storage, state, and
  shared type imports.
- Updated `src/lib.rs` to `mod refs_lineage_manifest; use
  refs_lineage_manifest::*;`.
- Removed the obsolete root-level `Transaction` import from `src/lib.rs`.

Remaining:

- Split this large true module into `refs.rs`, `lineage.rs`, and `manifest.rs`
  after the include-based transition is finished.
- Convert `bundle.rs` and `integrity.rs` from root includes into true modules.

### M99: Bundle True Module Conversion

Status: complete

Acceptance criteria:

- `bundle.rs` is no longer root-included as plain source text.
- Event bundle export, run bundle export, and bundle import are exposed through
  explicit crate-internal functions.
- Existing bundle validation/import/export behavior remains unchanged.

Implemented:

- Converted `src/bundle.rs` into a true Rust module.
- Added explicit SQLite, filesystem, path, set, event sequence, storage,
  query, refs/manifest, error, and bundle type imports.
- Updated `src/lib.rs` to `mod bundle; use bundle::*;`.
- Removed obsolete root-level `OptionalExtension` and `collect_rows` imports
  from the PyO3 facade.

Remaining:

- Convert `integrity.rs` from root include into a true module.

### M100: Integrity True Module Conversion And Include Removal

Status: complete

Acceptance criteria:

- `integrity.rs` is no longer root-included as plain source text.
- `src/lib.rs` contains no `include!` wiring.
- Integrity verification remains available through the existing PyO3 facade.
- Existing behavior remains unchanged.

Implemented:

- Converted `src/integrity.rs` into a true Rust module.
- Added explicit SQLite, filesystem, path, collection, schema, GC, storage,
  manifest, ref-state, run-state, and shared error/type imports.
- Updated `src/lib.rs` to `mod integrity; use integrity::*;`.
- Removed the final root-level include.

Remaining:

- Second-stage structural cleanup can now proceed with ordinary Rust module
  boundaries: split `refs_lineage_manifest.rs`, split mixed storage/naming
  helpers, and eventually move the PyO3 facade implementations into
  `engine.rs`.

### M101: Refs Lineage Manifest Split

Status: complete

Acceptance criteria:

- The mixed `refs_lineage_manifest.rs` module is removed.
- Ref lifecycle logic, lineage traversal, and manifest/blob read logic are in
  separate Rust modules.
- Existing behavior remains unchanged.

Implemented:

- Added `src/refs.rs` for ref state mutations, ref version checks, ref audit
  insertion, state validation, and producer-agent checks.
- Added `src/lineage.rs` for lineage node traversal, lineage graph rows, and
  evidence coverage checks.
- Added `src/manifest.rs` for blob/node byte reads, manifest child reads, and
  manifest child metadata lookup.
- Updated `src/lib.rs` module declarations/imports and removed
  `src/refs_lineage_manifest.rs`.

Remaining:

- Split mixed storage helpers into blob/CAS, ref naming, and connection/id/time
  helpers.

### M102: Storage Helper Split

Status: complete

Acceptance criteria:

- The mixed `storage.rs` module is removed.
- CAS/blob logic, common connection/time/id helpers, naming helpers, checkout
  projection helpers, manifest node creation, and ref helper logic live in
  domain-specific modules.
- Existing behavior remains unchanged.

Implemented:

- Added `src/blob.rs` for `BlobStorage`, CAS hashing, object path resolution,
  blob materialization, blob insertion, and existing blob metadata validation.
- Added `src/common.rs` for connection locking, content media inference,
  millisecond timestamps, and generated node/event/pin IDs.
- Added `src/naming.rs` for workspace/ref path naming and ref-kind inference.
- Added `src/checkout.rs` for checkout base projection and
  `workspace_base_refs` capture.
- Moved `RefWriteMode`, `fetch_ref`, and `insert_new_ref` into `src/refs.rs`.
- Moved manifest node creation into `src/manifest.rs`.
- Removed `src/storage.rs` and updated `src/lib.rs` module wiring.

Remaining:

- `src/lib.rs` still hosts the PyO3 facade and method implementations. The
  next structural step is to move engine/workspace impl blocks into explicit
  modules while preserving the Python API.

### M103: Engine And Workspace Facade Split

Status: complete

Acceptance criteria:

- `src/lib.rs` no longer hosts the `AnfsEngine` and `Workspace` method bodies.
- PyO3 class registration remains in `src/lib.rs`.
- Existing Python API behavior remains unchanged.

Implemented:

- Added `src/engine.rs` containing the `AnfsEngine` `#[pymethods]` block and
  internal `impl AnfsEngine`.
- Added `src/workspace.rs` containing the `Workspace` `#[pymethods]` block and
  internal `impl Workspace`.
- Reduced `src/lib.rs` to module declarations/imports, constants, shared
  `Inner`/`AnfsEngine`/`Workspace` type definitions, and `#[pymodule]`
  registration.

Remaining:

- `engine.rs` and `workspace.rs` use `use super::*` as a transition import.
  Replace with explicit imports after the current MVP API stabilizes.
- `bundle.rs` and `integrity.rs` are explicit modules but still large; split
  only when their internal sections become hard to review.

### M104: POSIX-Like Workspace Tool Surface

Status: complete

Acceptance criteria:

- Models can use familiar path-shaped workspace tools instead of only ANFS ref
  names for common file operations.
- POSIX-like tools remain a facade over ANFS semantic refs, immutable nodes,
  append-only events, and ref audit rows.
- `read` / `cat` record `read_ref` events.
- `find` and `grep` record `search` events with result node edges.
- Mutating operations preserve integrity-verifier-compatible event shapes.
- Directory `rm`, `cp`, and `mv` operate recursively over workspace refs.
- POSIX-like operations remain fast enough to be useful under audit; performance
  comparisons must account for lineage, ref audit rows, and model-visible search
  events that native filesystems do not record.

Implemented:

- Added `Workspace.read()` and `Workspace.cat()` path facades over
  `read_ref`.
- Added `Workspace.ls()` and `Workspace.stat()` over workspace ref namespace
  projections, including virtual and marker-backed directories.
- Added `Workspace.mkdir()` using lightweight directory marker refs.
- Added `Workspace.touch()` for ordinary-file compatibility: it creates missing
  empty files through `write` and treats existing regular files as no-ops;
  explicit timestamp edits are handled by M215 `utime(...)`.
- Added `Workspace.rm()` as a POSIX alias for logical delete, with recursive
  directory deletion.
- Added `Workspace.cp()` and `Workspace.mv()` for files and recursive
  workspace directory trees.
- Added destination-directory compatibility for `cp(...)` and `mv(...)` through
  M230, so file and recursive directory sources land under
  `dst/<source-basename>` when the destination is an active directory.
- Added `Workspace.find()` and `Workspace.grep()` as audited model-visible
  search tools, uncapped by default with optional explicit result limits.
- Optimized POSIX `find` by avoiding unnecessary node joins for directory
  classification.
- Optimized POSIX `grep` by reading inline blob content from the scan query
  instead of reconnecting per candidate.
- POSIX search events now keep exact result nodes in event edges and store a
  compact payload summary to avoid duplicating large result sets in JSON.
- Directory `mv` batches write/delete events for subtree moves while preserving
  per-ref audit rows and per-ref event edges.
- SQLite runs in WAL mode with `synchronous=NORMAL` for higher local write
  throughput.
- Added tests for path read/list/stat/search behavior, touch create/no-op/
  directory rejection behavior, file copy/move/delete, recursive directory
  copy/move/delete, and `verify_integrity()`.

Current local release benchmark snapshot for 500 small files:

- ANFS `grep`, recursive `cp`, and recursive `rm` are faster than the Python
  native-filesystem comparison used in development.
- ANFS `find` is close to native in this benchmark while still writing a search
  event.
- `write`, audited `read`, and `ls` remain slower than native metadata-only
  operations because ANFS writes events, refs, edges, and audit rows.
- Recursive `mv` is much faster after batching, but still cannot match native
  `rename(2)` without a lazy namespace-move representation because ANFS eagerly
  rewrites every affected workspace ref for auditability.

Remaining:

- This is intentionally POSIX-like rather than full POSIX/FUSE fidelity.
  Immutable-node links and effective-id mode-bit access are implemented through
  M218-M220, but ACLs, OS process impersonation, descriptor-level POSIX locks, mmap, special
  files, and exact kernel syscall behavior remain out of scope unless a later
  design explicitly needs them.
- If directory `mv` must match native rename latency, add a lazy namespace move
  primitive instead of eagerly rewriting all subtree refs.

### M105: Agent-Memory Workflow Benchmark

Status: complete

Acceptance criteria:

- Benchmark covers multi-agent concurrent read/write/search.
- Benchmark covers workspace merge into a shared base namespace.
- Benchmark covers approval with lineage evidence.
- Benchmark covers replay materialization.
- Benchmark reports correctness and audit quality, not only speed.
- Benchmark fails non-zero when recall, merge count, approval count, replay
  count, integrity, or event sequence continuity fails.

Implemented:

- Added `benchmarks/agent_memory_benchmark.py`.
- Benchmark supports `--backend anfs`, `--backend native-jsonl`,
  `--backend rag-jsonl`, `--backend markdownfs`, `--backend sqlite-memory`,
  and `--backend all`.
- Benchmark supports `--matrix quick` and `--matrix full` scale sweeps over
  agent count, files per agent, payload size, and search-result cardinality.
- Benchmark supports `--payload-bytes` and `--needle-every` to vary file size
  and grep/search hit density without changing the workflow shape.
- Benchmark supports `--cache-repeats` to measure managed working-set cache
  miss/hit reuse in the same workflow.
- Benchmark supports `--cache-workers` and `--cache-worker-repeats` to measure
  concurrent managed working-set materialization across workers.
- Benchmark supports `--cache-shared-key` to stress same-cache-key contention.
- Benchmark supports `--large-file-bytes`, `--range-read-count`,
  `--range-size`, and `--chunk-size` to measure large-file range reads,
  derived chunk maps, chunk-cache build, and chunk-cache reads.
- Benchmark supports `--output-json` to publish reproducible local result
  snapshots.
- Default scenario uses four agents, 50 files per agent, four base files, and
  eight approval decisions.
- Measures setup/checkout, concurrent read/write/grep, merge, approve, replay,
  integrity, total time, and throughput.
- Reports audit counts for events, event edges, ref audit rows, event sequence,
  and event kind distribution.
- Validates read recall, grep recall, merged ref count, approved refs, replayed
  files, empty `verify_integrity()`, process exit codes, and contiguous event
  sequence.
- Increased SQLite busy timeout to 30 seconds at both PRAGMA and connection API
  levels after the benchmark exposed a real multi-process `database is locked`
  failure at the default scale.
- Added retry around schema initialization, migration recording, and event
  sequence seeding/backfill after `--backend all` exposed a second startup-time
  `database is locked` failure.
- Added native POSIX + JSONL audit baseline. This baseline uses real
  directories/files plus a file-locked JSONL event sequence, but explicitly
  reports semantic limitations: audit is separate from mutations, lineage is
  simulated in Python metadata, replay is a current-directory copy, and there is
  no immutable CAS node graph.
- Added MarkdownFS baseline. This baseline stores content as Markdown files and
  audit events as Markdown documents with frontmatter, but explicitly reports
  semantic limitations: audit is separate from mutations, lineage is simulated
  in Python metadata, replay is a current-directory copy, and there is no
  immutable CAS node graph or integrated policy semantics.
- Added local RAG JSONL baseline. This baseline stores ordinary files plus an
  append-only JSONL retrieval index and uses lexical token matching for the
  benchmark search step, while explicitly reporting that it lacks vector
  similarity, immutable CAS nodes, typed causal graph edges, event-time replay,
  and integrated policy semantics.
- Added SQLite memory-store baseline. This baseline stores workspace/path/
  content rows plus lightweight audit rows in SQLite, but explicitly reports
  semantic limitations: no immutable CAS node graph, no typed causal graph
  edges, lineage approval is simulated through metadata, replay is current-row
  materialization, and policy/fragment visibility semantics are absent.
- Added `docs/benchmark_snapshots/agent_memory_local_snapshot.json` and
  `docs/benchmark_snapshots/README.md` as a local published snapshot covering
  all five backends, cache reuse, cache contention, and large-file chunk/range
  checks.

Current local release benchmark snapshot:

- Parameters: 4 agents, 50 files per agent, 4 base files, 8 approvals.
- ANFS accuracy: passed.
- ANFS concurrent read/write/grep: about 115 ms for 200 files.
- ANFS merge: about 5 ms for 200 refs.
- ANFS approval: about 12 ms for 8 lineage approvals.
- ANFS replay: about 20 ms for 204 files.
- ANFS integrity: about 16 ms.
- Audit generated: 472 events, 1340 event edges, 464 ref audit rows.
- Native POSIX + JSONL baseline accuracy: passed.
- Native POSIX + JSONL concurrent read/write/grep: about 135 ms for 200 files.
- Native POSIX + JSONL merge: about 48 ms for 200 refs.
- Native POSIX + JSONL approval: about 9 ms for 8 simulated lineage approvals.
- Native POSIX + JSONL replay: about 40 ms for 204 files.
- Native POSIX + JSONL integrity: about 2 ms for JSONL/file-count checks.

Remaining:

- Add managed vector DB service and ANN baselines beyond the external vector
  JSONL sidecar baseline.
- Publish larger scale sweep snapshots and add stronger contention-level
  controls beyond varying agent count.
- Publish larger cache hit-rate/cache-contention and large-file sweeps across
  working-set size, file size, range count, range size, and chunk size.

### M106: Unified Current-Ref Query API

Status: complete

Acceptance criteria:

- Python can query current refs through one `AnfsEngine.query(...)` surface.
- Query supports prefix, FTS text, lifecycle state, agent id, run id, event kind,
  media type, node timestamp, and policy-decision filters.
- Query returns ref identity, node identity, ref state/version, node kind/media
  metadata, node creation time, latest ref mutation event summary, and optional
  text snippet.
- Invalid limits and invalid time ranges are rejected with typed policy errors.
- Engine-level query is read-only and does not record context events.

Implemented:

- Added `AnfsEngine.query(prefix=None, text=None, state=None, agent_id=None,
  run_id=None, event_kind=None, media_type=None, created_after_ms=None,
  created_before_ms=None, policy=None, decision=None, reason_code=None,
  limit=100, policy_label_excludes=None)`.
- Added `QueryRefRow` return rows:
  `(ref_name, node_id, ref_kind, state, ref_version, node_kind, media_type,
  node_created_at, last_event_seq, last_event_kind, snippet)`.
- Query uses `refs` / `nodes` for the current view, `node_fts` for text
  filtering/snippets, event/ref audit tables for agent/run/event filters, and
  policy decision payloads for policy filters.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Query-time policy enforcement through query/retrieval/export/replay is now
  implemented through the shared visibility policy.
- Field-level policy labels are implemented through M123 and M126; local
  node-level embedding projection is implemented through M129.

### M107: Audited Structured Query-As-Context

Status: complete

Acceptance criteria:

- Workspace agents can run the same structured current-ref query as
  `AnfsEngine.query(...)`.
- Workspace structured query records the exact result set as a model-visible
  context event.
- Query context events carry agent id, run id, workspace id, tool-call id,
  filters, result count, and versioned result refs.
- Result nodes are linked through typed input edges so later lineage/debugging
  can prove which current-ref query results the agent saw.
- Existing engine-level `query(...)` remains read-only for diagnostic callers.

Implemented:

- Added `Workspace.query(..., tool_call_id=None)` with the same filters and
  row shape as `AnfsEngine.query(...)`.
- Added `events.kind = 'query'` context events for workspace structured queries.
- Query payloads record filters and per-result ref/node/state/version metadata.
- Query result edges use roles `query_result:<index>` with matched ref names as
  `logical_path`.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Query-time label exclusion is implemented through M109; broader enforcement
  remains future work.
- Byte-range fragment labels are implemented through M122; semantic field
  extraction remains future work.
- Future answer-citation APIs can consume `query` event ids as evidence.

### M108: Durable Node/Ref Policy Labels

Status: complete

Acceptance criteria:

- Callers can attach durable policy labels to refs and nodes.
- Label writes are append-only and represented by normal events.
- Clearing a label removes it from the active label view without deleting
  historical label events.
- Active and historical label views are queryable from Python.
- Label events include agent, run, and tool-call context.
- Integrity verification diagnoses malformed label audit/event/edge linkage.

Implemented:

- Added append-only `policy_label_events`.
- Added `AnfsEngine.set_policy_label(subject_type, subject_id, label, value,
  agent_id, run_id=None, tool_call_id=None)`.
- Added `AnfsEngine.policy_labels(subject_type=None, subject_id=None,
  label=None, active_only=True)`.
- `subject_type` currently supports `ref` and `node`.
- `value=None` records a clear operation; active labels are derived from latest
  event sequence per `(subject_type, subject_id, label)`.
- Label events use `events.kind = 'policy_label'` and link the target node with
  a `policy_label_subject` input edge.
- Added integrity checks and regression coverage in `tests/test_demo.py`.

Remaining:

- Broader query-time enforcement beyond structured query label exclusion
  remains future work.
- Byte-range fragment labels are implemented through M122; semantic field
  extraction remains future work.

### M109: Query-Time Policy Label Exclusion

Status: complete

Acceptance criteria:

- Structured current-ref query can exclude refs or nodes carrying active labels.
- Label exclusion applies to both `AnfsEngine.query(...)` and
  `Workspace.query(...)`.
- Cleared labels no longer exclude results from active query views.
- Audited workspace query events record the labels used for exclusion.
- Invalid exclusion labels are rejected with typed policy errors.

Implemented:

- Added `policy_label_excludes=None` to `AnfsEngine.query(...)`.
- Added `policy_label_excludes=None` to `Workspace.query(...)`.
- Exclusion checks active labels on both the current ref and its current node.
- Workspace query payloads record `policy_label_excludes` for later audit.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Search-time label exclusion is implemented through M110; replay
  materialization label exclusion is implemented through M111; bundle export
  label exclusion is implemented through M112. Answer construction does not yet
  enforce policy labels.
- Policy registry value interpretation is implemented through M121.

### M110: Search-Time Policy Label Exclusion

Status: complete

Acceptance criteria:

- `Workspace.search(...)` can exclude results whose active ref or node labels
  match caller-provided labels.
- Search-as-context events record the labels used for exclusion.
- Search result edges include only visible, non-excluded results.
- Cleared labels no longer exclude results from active search views.
- Invalid exclusion labels are rejected with typed policy errors.

Implemented:

- Added `policy_label_excludes=None` to `Workspace.search(...)`.
- Search filtering reuses the same active ref/node label exclusion helper as
  structured query.
- Search payloads record `policy_label_excludes`.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Replay materialization label exclusion is implemented through M111; bundle
  export label exclusion is implemented through M112. Answer construction does
  not yet enforce policy labels.
- Policy registry value interpretation is implemented through M121.

### M111: Replay Policy Label Exclusion

Status: complete

Acceptance criteria:

- `ref_view_at_event(...)` can exclude refs or nodes carrying active labels.
- `materialize_ref_view_at_event(...)` honors the same active label exclusions.
- Excluded refs are absent from materialized files and replay manifests.
- Cleared labels no longer exclude refs from replay views.
- Invalid exclusion labels are rejected with typed policy errors.

Implemented:

- Added `policy_label_excludes=None` to `ref_view_at_event(...)`.
- Added `policy_label_excludes=None` to `materialize_ref_view_at_event(...)`.
- Replay filtering reuses the same active ref/node label exclusion helper as
  structured query and search.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Bundle export label exclusion is implemented through M112. Bundle import
  destination-side label exclusion is implemented through M118.
- Answer construction, citation APIs, and retrieval-event links are implemented
  through M113 and M117.
- Policy registry value interpretation is implemented through M121.

### M112: Bundle Export Policy Label Gate

Status: complete

Acceptance criteria:

- `export_event_bundle(...)` can reject bundles containing refs or nodes with
  excluded active labels.
- `export_run_bundle(...)` applies the same active label gate.
- Rejected exports do not write `bundle.json` or object files.
- Cleared labels no longer block bundle export.
- Invalid exclusion labels are rejected with typed policy errors.

Implemented:

- Added `policy_label_excludes=None` to `export_event_bundle(...)`.
- Added `policy_label_excludes=None` to `export_run_bundle(...)`.
- Bundle export preflights the closed event/run bundle and rejects rather than
  filtering, preserving bundle causal closure and importability.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Policy registry value interpretation is implemented through M121.
- Byte-range fragment labels are implemented through M122; semantic field
  extraction remains future work.

### M113: Answer Construction Citation Events

Status: complete

Acceptance criteria:

- Workspace agents can write generated answers as immutable answer nodes.
- Answer events record cited refs, node ids, ref states, and ref versions.
- Citation refs are linked as `answer_citation:*` input edges.
- Answer output nodes are linked through one `result` output edge.
- Active policy label exclusions reject blocked citation refs or nodes.
- Cross-workspace drafts cannot be cited by another workspace.
- Integrity verification detects answer citation/output edge mismatches.

Implemented:

- Added `Workspace.answer(path, content, citation_refs, tool_call_id=None,
  policy_label_excludes=None, retrieval_event_ids=None) -> node_id`.
- Added `answer` events with citation payloads and typed citation/result edges.
- Added `answer` node metadata with `anfs.answer_node.v1` citation metadata.
- Reused active ref/node policy label gates for answer citations.
- Added optional retrieval event id payload links to same-workspace query/search
  events.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Retrieval coverage inspection is implemented through M133; semantic citation
  quality scoring remains future work.
- Policy registry value interpretation is implemented through M121.

### M117: Answer Retrieval Event Links

Status: complete

Acceptance criteria:

- `Workspace.answer(...)` can reference prior retrieval event ids.
- Referenced retrieval events must exist and be `query` or `search` events.
- Referenced retrieval events must belong to the answer workspace.
- Every cited ref/node must be covered by at least one referenced retrieval
  event edge.
- Integrity verification diagnoses missing, wrong-kind, wrong-workspace, or
  non-covering retrieval event ids.

Implemented:

- Added optional `retrieval_event_ids=None` to `Workspace.answer(...)`.
- Answer payloads and answer-node metadata record retrieval event ids.
- Answer creation rejects retrieval ids that do not cover cited refs/nodes.
- `verify_integrity()` validates retrieval event existence, kind, workspace,
  and citation coverage.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Retrieval coverage inspection is implemented through M133; semantic citation
  quality scoring remains future work.
- Token-estimate accounting is implemented through M138. Caller-supplied
  pricing and prompt-overhead profiles are implemented through M157; exact
  vendor tokenizer parity remains adapter-level future work.

### M118: Bundle Import Policy Label Gate

Status: complete

Acceptance criteria:

- `import_event_bundle(...)` can reject bundles whose node ids are blocked by
  active destination node labels.
- `import_event_bundle(...)` can reject bundles whose ref-audit ref names are
  blocked by active destination ref labels.
- Rejected imports do not write canonical rows or object bytes.
- Invalid exclusion labels are rejected with typed policy errors.
- Existing unrestricted import behavior remains idempotent.

Implemented:

- Added `policy_label_excludes=None` to `import_event_bundle(...)`.
- Import validates bundle checksum and internal closure, then preflights active
  destination ref/node labels before opening the write transaction.
- Destination-side policy uses the shared `VisibilityPolicy` resolver.
- Added regression coverage in `tests/test_demo.py` for ref-label rejection,
  node-label rejection, invalid labels, and unchanged table counts after
  rejection.

Remaining:

- Policy registry value interpretation is implemented through M121.
- Byte-range fragment labels are implemented through M122; semantic field
  extraction remains future work.

### M114: Unified Visibility Policy Resolver

Status: complete

Acceptance criteria:

- Query, search, answer, replay, bundle export/import, and worktree sync use one
  visibility policy module for active ref/node policy label gates.
- Invalid exclusion labels are rejected consistently.
- Filtering surfaces can skip excluded refs/nodes.
- Rejecting surfaces can produce typed `PolicyDeniedError` before writing
  projected files or output artifacts.

Implemented:

- Added `src/visibility.rs` with `VisibilityPolicy`.
- Moved active policy-label lookup out of `query.rs`.
- Rewired query, search, answer, replay, bundle export/import, and worktree sync to
  use the shared resolver.
- Existing policy-label regression coverage remains passing.

Remaining:

- Policy registry value interpretation is implemented through M121.
- Byte-range fragment labels are implemented through M122; semantic field
  extraction remains future work.

### M121: Visibility Policy Registry

Status: complete

Acceptance criteria:

- Callers can record append-only visibility policy rules.
- Callers can clear active rules without deleting history.
- Active and historical rule views are queryable from Python.
- Active `visibility` / `deny` rules interpret active ref/node policy label
  values.
- Registry-driven deny rules are enforced through the same `VisibilityPolicy`
  resolver used by query, search, answer, replay, bundle import/export, and
  worktree projections.
- Integrity verification detects malformed policy-rule rows and orphan
  `policy_rule` events.

Implemented:

- Added append-only `policy_rule_events`.
- Added `set_policy_rule(...)`, `clear_policy_rule(...)`, and
  `policy_rules(...)`.
- Added registry-driven value matching with wildcard value support through
  `value=None`.
- Preserved explicit `policy_label_excludes` behavior alongside registry
  rules.
- Reworked visibility unrestricted detection so active registry rules trigger
  filtering even when callers pass no explicit exclusions.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Byte-range fragment labels are implemented through M122.
- JSON, Markdown frontmatter, and Markdown body heading/body-block semantic
  field extraction are implemented through M123, M126, and M134.
- Exact byte-identical fragment-label propagation is implemented through M125;
  conservative derived-output propagation is implemented through M127.

### M122: Byte-Range Fragment Policy Labels

Status: complete

Acceptance criteria:

- Callers can attach append-only policy labels to byte ranges inside immutable
  nodes.
- Callers can clear active fragment labels without deleting history.
- Active and historical fragment labels are queryable from Python.
- Invalid ranges are rejected before writing label events.
- Active `visibility` / `deny` rules for `subject_type="fragment"` block
  overlapping range reads.
- Whole-node projections are conservatively blocked when the node contains a
  denied fragment.
- Integrity verification detects malformed fragment-label rows and orphan
  `fragment_policy_label` events.

Implemented:

- Added append-only `fragment_policy_label_events`.
- Added `set_fragment_policy_label(...)` and `fragment_policy_labels(...)`.
- Range reads and chunk maps reject ranges overlapping denied fragments.
- Full node reads, `read_ref`, query/search visibility, answer citations,
  replay, bundle import/export, and worktree materialization inherit fragment
  blocking through the shared `VisibilityPolicy`.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- JSON field extraction is implemented through M123; Markdown frontmatter field
  extraction is implemented through M126; richer non-JSON semantic field
  extraction remains future work.
- Exact byte-identical fragment-label propagation is implemented through M125;
  conservative derived-output propagation is implemented through M127.

### M123: JSON Field Policy Extraction

Status: complete

Acceptance criteria:

- Callers can derive JSON field paths and byte ranges from an immutable node.
- JSON path rows include path, offset, length, and JSON value kind.
- Callers can attach policy labels to JSON field paths.
- Field policy labels resolve to byte-range fragment policy labels.
- Missing JSON paths and invalid JSON are rejected with typed policy errors.
- Existing fragment visibility enforcement applies to field-derived labels.

Implemented:

- Added `json_field_spans(node_id)`.
- Added `set_json_field_policy_label(node_id, json_path, label, value, ...)`.
- Implemented JSON field span extraction for object fields and array elements.
- Field labels reuse `fragment_policy_label_events`, so downstream enforcement
  and integrity checks stay unified.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Markdown frontmatter field extraction is implemented through M126; richer
  non-JSON semantic field extraction remains future work.
- Exact byte-identical fragment-label propagation is implemented through M125;
  conservative derived-output propagation is implemented through M127.

### M124: Agent-Native FS Conformance Proof

Status: complete

Acceptance criteria:

- Existing filesystem tools can edit a materialized ANFS workspace and commit
  the directory diff back into kernel state.
- The same scenario proves copy-on-write checkout, worktree commit events,
  query/search audit, large-node chunk caching, JSON field policy, merge, and
  integrity verification.
- Full-text search treats caller input as literal text rather than raw SQLite
  FTS syntax.
- The proof documents remaining gaps instead of claiming full POSIX or
  distributed filesystem parity.

Implemented:

- Added
  `test_agent_native_fs_conformance_preserves_kernel_invariants_with_existing_tools`
  in `tests/test_demo.py`.
- Added safe literal FTS query construction for `Workspace.search(...)` and
  `query(..., text=...)`.
- Added `docs/08_agent_native_fs_conformance.md`.

Remaining:

- FUSE/mount compatibility remains future work.
- Conservative transformed downstream output policy propagation is implemented
  through M127; richer policy algebra remains future work.
- A local multi-backend benchmark snapshot exists under
  `docs/benchmark_snapshots`; larger release-scale benchmark snapshots remain
  future work.

### M125: Exact-Copy Fragment Policy Propagation

Status: complete

Acceptance criteria:

- When a new immutable artifact node has the same blob hash as an existing node,
  active byte-range fragment policy labels are propagated to the new node.
- Propagated labels are explicit `fragment_policy_label` events with subject
  edges to the new node, so existing integrity verification remains sufficient.
- Multiple same-blob sources deduplicate equivalent active labels by
  offset/length/label/value.
- Worktree commit-back inherits this behavior through the shared workspace
  write path, preserving compatibility with ordinary copy tools.

Implemented:

- Added `propagate_fragment_policy_labels_for_blob(...)` in the write
  transaction after node insertion.
- Added regression coverage for direct exact-copy writes.
- Updated agent-native conformance so `shutil.copyfile(...)` inherits the JSON
  field fragment label without explicitly labeling the copied node.

Remaining:

- Conservative transformed downstream output propagation is implemented through
  M127; summaries and explicit derived outputs now inherit source policy labels.
- Cross-format extraction and richer non-JSON semantic field propagation remain
  future work.

### M126: Markdown Frontmatter Field Policy Extraction

Status: complete

Acceptance criteria:

- Callers can derive top-level and indented nested Markdown frontmatter field
  paths and byte ranges from an immutable node.
- Callers can derive conservative frontmatter object, scalar list item, inline
  scalar sequence item, inline object scalar field, and block scalar spans.
  Later milestones add quoted-key bracket paths and inline sequence object item
  fields.
- Field rows include path, offset, length, and inferred scalar kind.
- Callers can attach policy labels to Markdown frontmatter field paths.
- Field policy labels resolve to byte-range fragment policy labels.
- Missing fields and Markdown files without opening/closing frontmatter fences
  are rejected with typed policy errors.
- Existing fragment visibility enforcement applies to Markdown-derived labels.

Implemented:

- Added `markdown_field_spans(node_id)`.
- Added `set_markdown_field_policy_label(node_id, field_path, label, value, ...)`.
- Implemented conservative parsing for opening `---` frontmatter blocks with
  top-level scalar `key: value` entries, indented nested scalar fields,
  parent object spans, scalar sequence item paths such as
  `frontmatter.recipients[0]`, sequence object item fields such as
  `frontmatter.recipients[1].name`, inline scalar sequence item paths such as
  `frontmatter.tags[1]`, inline object scalar field paths such as
  `frontmatter.contact.email`, and `|` / `>` block scalar spans. Later
  milestones add quoted-key bracket paths and inline sequence object item fields.
- Markdown field labels reuse `fragment_policy_label_events`, so downstream
  enforcement and integrity checks stay unified.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Full YAML alias expansion, merge keys, complete tag typing, complex YAML typing,
  non-heading Markdown body semantic spans, cross-format extraction, and richer
  downstream semantic mapping remain future work.

### M127: Conservative Derived-Output Policy Propagation

Status: complete

Acceptance criteria:

- A write with explicit `derived_from_nodes` propagates active source node
  labels to the new output node.
- A transformed write with explicit `derived_from_nodes` propagates active
  source fragment labels to a full-output fragment label on the new node.
- Exact byte-identical derived writes keep precise same-blob fragment ranges
  instead of widening them to the whole output.
- `Workspace.answer(...)` treats citation nodes as derived inputs and propagates
  citation labels to the answer node.
- Existing node and fragment visibility rules block propagated derived outputs.

Implemented:

- Added `propagate_derived_policy_labels(...)` in the policy layer.
- Workspace writes call derived propagation after node insertion.
- Answer nodes call the same propagation logic over citation node ids.
- Added regression coverage for transformed writes and answer outputs.

Remaining:

- Multi-value active labels are implemented through M131; richer boolean policy
  expressions remain future work.
- Explicit partial-output attribution is implemented through M130; exact-byte
  automatic attribution is implemented through M132; normalized JSON scalar
  attribution for strings, booleans, nulls, and finite numbers is implemented
  through M185/M186/M228, and normalized JSON array/object value attribution is
  implemented through M229. Broader cross-format semantic mapping remains
  future work.

### M128: Derived Index Repair Admin API

Status: complete

Acceptance criteria:

- Callers can repair rebuildable derived state without mutating canonical
  blobs, nodes, refs, events, policy rows, or audit rows.
- Repair reconstructs `node_fts` rows from canonical node bytes.
- Repair clears persisted chunk indexes so later `cache_node_chunks(...)` calls
  regenerate them from canonical node bytes.
- Corrupted FTS rows, orphan FTS rows, missing FTS rows, and corrupted chunk
  cache rows can be repaired back to a clean `verify_integrity()` result.

Implemented:

- Added `rebuild_derived_indexes() -> (fts_rows_rebuilt,
  chunk_indexes_cleared)`.
- Added `repair_plan() -> list[(issue, classification, action,
  mutable_scope)]`.
- `repair_plan()` classifies verifier issues as `derived_index`,
  `blob_storage`, `canonical_policy`, `canonical_audit`, or `inspect` and
  recommends the narrowest non-mutating next action.
- Added regression coverage that corrupts FTS and chunk-cache tables, repairs
  them, verifies integrity, confirms search works again, and confirms chunk
  cache regeneration works.
- Added regression coverage that proves `repair_plan()` does not mutate derived
  state and classifies both derived-index and canonical-audit issues.

Remaining:

- `repair_plan()` is read-only; automatic canonical repair, physical
  compaction/archival execution, and large-scale repair workflows remain future
  work.

### M129: Local Embedding Projection

Status: complete

Acceptance criteria:

- Callers can attach a finite, positive-norm embedding vector to an immutable
  node for a named model.
- Callers can read back the stored node embedding.
- Callers can search current refs by cosine similarity for a selected lifecycle
  state.
- Vector search applies the shared visibility policy and excludes refs/nodes
  blocked by active labels or caller-provided label exclusions.
- Embedding rows are derived projection state, not canonical content.
- Integrity verification diagnoses malformed embedding projection rows.

Implemented:

- Added `node_embeddings` projection table.
- Added `set_node_embedding(node_id, model, vector)`.
- Added `node_embedding(node_id, model)`.
- Added `vector_search(model, query_vector, state="published", limit=10,
  policy_label_excludes=None)`.
- Added regression coverage for cosine ordering, dimension mismatch skipping,
  visibility filtering, invalid-vector rejection, and embedding integrity
  diagnostics.

Remaining:

- Local RAG JSONL baseline is implemented through M147. External vector JSONL
  sidecar baseline is implemented through M154; approximate nearest-neighbor
  indexing, managed vector DB service baselines, and benchmark sweeps over large
  embedding collections remain future work.

### M130: Explicit Partial-Output Fragment Attribution

Status: complete

Acceptance criteria:

- Callers can propagate active fragment labels from a source node byte range to
  a precise output node byte range.
- The API rejects invalid source and output ranges before writing events.
- Propagated fragment-label events record the source node id and source range in
  payload metadata.
- Existing fragment visibility rules block the attributed output span while
  allowing unrelated output ranges to remain readable.

Implemented:

- Added `propagate_fragment_policy_labels(source_node_id, source_offset,
  source_length, output_node_id, output_offset, output_length, agent_id, ...)`.
- Added regression coverage for JSON-field source labels mapped to a precise
  Markdown output range.

Remaining:

- Exact-byte automatic attribution is implemented through M132, normalized JSON
  string scalar attribution is implemented through M185, integer-valued JSON
  number attribution is implemented through M186, and bool/null/finite-number
  normalized scalar attribution is implemented through M228. Normalized JSON
  array/object value attribution is implemented through M229. Broader non-exact
  semantic attribution remains future work.

### M131: Multi-Value Ref/Node Policy Labels

Status: complete

Acceptance criteria:

- Ref/node policy labels can have multiple active values for the same
  `(subject_type, subject_id, label)`.
- `policy_labels(active_only=True)` returns all active values.
- `value=None` continues to clear the active label, now clearing all active
  values for that subject/label.
- Visibility deny rules match any active value.
- Explicit `policy_label_excludes` still blocks by label regardless of value.
- Derived-output policy propagation carries all active source node label values.

Implemented:

- Updated active policy-label SQL to group latest set events by
  `(subject_type, subject_id, label, value)` and suppress values cleared by a
  later `value=NULL` event.
- Updated visibility rule matching and explicit label-exclusion checks to use
  the same active multi-value semantics.
- Updated derived-output source label collection to propagate every active
  value.
- Added regression coverage for multiple active values, value-specific deny
  rules, rule clearing, and clear-all label semantics.

Remaining:

- Basic `label` / `all` / `any` / `not` visibility expressions are implemented
  through M140, threshold `at_least` expressions are implemented through M173,
  and lifecycle/review operation capability rules are implemented through M184.

### M173: Threshold Policy Expression Algebra

Status: complete

Acceptance criteria:

- Visibility expression rules can represent threshold combinations without
  enumerating equivalent `any` combinations.
- Threshold rules remain append-only `policy_expression_rule` facts evaluated by
  the existing shared visibility resolver.
- Invalid threshold counts and child lists are rejected before appending an
  event.
- Threshold expressions apply to ref, node, and fragment subjects through the
  same policy path as label and boolean expressions.

Implemented:

- Added `{"at_least":{"count":N,"of":[...]}}` expression support over active
  policy labels.
- Added validation for integer counts, non-empty child lists, in-range
  thresholds, and recursively valid child expressions.
- Added short-circuit threshold evaluation.
- Added regression coverage for invalid thresholds and query visibility where a
  one-label node stays visible while two-label and three-label nodes are denied
  by a threshold rule.

Remaining:

- This extends visibility algebra; full YAML extraction, richer body semantic
  extraction, and downstream execution sandbox controls remain future work.

### M140: Visibility Policy Expression Rules

Status: complete

Acceptance criteria:

- Callers can record append-only visibility expression rules.
- Callers can clear expression rules without deleting history.
- Active and historical expression rule views are queryable from Python.
- Expression JSON supports `label` leaves plus `all`, `any`, and `not`
  operators over active label values.
- Expression rules apply through the shared `VisibilityPolicy` resolver for
  ref, node, and fragment subjects.
- Integrity verification diagnoses malformed expression-rule rows, invalid
  expressions, and orphan `policy_expression_rule` events.

Implemented:

- Added append-only `policy_expression_rule_events`.
- Added `set_policy_expression_rule(...)`,
  `clear_policy_expression_rule(...)`, and `policy_expression_rules(...)`.
- Added expression evaluation over active ref/node labels and active overlapping
  fragment labels.
- Added regression coverage for composed node labels, expression clearing,
  invalid expressions, fragment-range expression blocking, and integrity
  diagnostics for malformed expression-rule audit rows.

Remaining:

- Threshold `at_least` expression algebra is implemented in M173. Operation
  capability rules are implemented in M184.
- Purpose-specific read/consume/search/query enforcement is implemented in
  M142, and opt-in purpose capability authorization is implemented in M149.
  A full downstream execution sandbox remains future work.

### M142: Purpose Policy Rules For Read, Consume, Search, And Query

Status: complete

Acceptance criteria:

- Callers can register append-only purpose deny rules without deleting history.
- Active purpose rules can be cleared while preserving the historical audit.
- Rules match active ref, node, and fragment policy labels.
- `read_ref(...)`, POSIX-style `read(...)`, and `consume(...)` enforce rules
  only when a non-empty purpose is supplied.
- `Workspace.search(...)` and `Workspace.query(...)` enforce rules as
  query-time result filters when a non-empty purpose is supplied.
- `purpose=None` remains compatible with ordinary file reads while visibility
  rules still apply.
- Integrity verification diagnoses malformed purpose-rule audit rows and
  orphan `purpose_policy_rule` events.

Implemented:

- Added append-only `purpose_policy_rule_events`.
- Added `set_purpose_policy_rule(...)`, `clear_purpose_policy_rule(...)`, and
  `purpose_policy_rules(...)`.
- Wired explicit-purpose enforcement into workspace `read_ref`, POSIX-style
  `read`, `consume`, `search`, and `query`.
- Added regression coverage for ref-, node-, and fragment-label purpose gates,
  query-time search/query filtering, rule clearing/history, compatibility reads
  with `purpose=None`, and integrity diagnostics for malformed purpose-rule
  audit rows.

Remaining:

- Purpose policy currently protects declared read/consume/search/query
  purposes; M149 adds opt-in agent capability authorization for those purposes.
  This is still not a full downstream execution sandbox.

### M149: Purpose Capability Authorization

Status: complete

Acceptance criteria:

- Admins can grant and revoke named capabilities for agents without deleting
  history.
- Admins can require a named capability before a workspace agent may use a
  non-empty purpose.
- `purpose=None` remains compatible with ordinary file-style reads and queries.
- Workspace `read_ref`, POSIX-style `read`, `consume`, `search`, and `query`
  reject protected purposes when the caller lacks the required capability.
- Granting the capability allows the protected purpose; revoking the capability
  blocks it again.
- Clearing the purpose capability rule restores compatibility for that purpose.
- Integrity verification diagnoses malformed capability audit rows and orphan
  `agent_capability` / `purpose_capability_rule` events.

Implemented:

- Added append-only `agent_capability_events` and
  `purpose_capability_rule_events`.
- Added `grant_agent_capability(...)`, `revoke_agent_capability(...)`,
  `agent_capabilities(...)`, `set_purpose_capability_rule(...)`,
  `clear_purpose_capability_rule(...)`, and
  `purpose_capability_rules(...)`.
- Wired explicit-purpose capability checks into workspace `read_ref`,
  POSIX-style `read`, `consume`, `search`, and `query`.
- Added regression coverage for compatibility reads, denied protected purposes,
  grant/revoke behavior, rule clearing/history, and integrity diagnostics.

Remaining:

- This is an explicit-purpose authorization gate, not a complete downstream
  execution sandbox or OS-level role system.

### M184: Operation Capability Rules For Lifecycle Review

Status: complete

Acceptance criteria:

- Admins can require a named capability for a kernel operation without deleting
  historical rules.
- Active operation requirements must use the same append-only capability grant
  view as purpose capability requirements.
- `approve(...)`, `reject_ref(...)`, and `archive_ref(...)` must reject callers
  missing the required capability before lifecycle events or ref mutations are
  recorded.
- Granting the capability allows the protected operation; revoking the
  capability blocks it again.
- Clearing an operation capability rule preserves history while removing the
  active requirement.
- Integrity verification diagnoses malformed operation capability audit rows
  and orphan `operation_capability_rule` events.

Implemented:

- Added append-only `operation_capability_rule_events` with immutable triggers.
- Added `set_operation_capability_rule(...)`,
  `clear_operation_capability_rule(...)`, and
  `operation_capability_rules(...)`.
- Reused active `agent_capability_events` as the capability/role grant source
  for both purpose and operation requirements.
- Wired operation capability checks into `approve`, `reject_ref`, and
  `archive_ref`.
- Added regression coverage for denied approve, grant-allowed approve,
  revoke-denied reject, grant-allowed reject, denied archive, grant-allowed
  archive, clear/history behavior, and integrity diagnostics.

Remaining:

- This is kernel operation authorization for lifecycle/review operations, not a
  complete downstream execution sandbox, OS-level role system, or policy gate
  for every possible adapter command.

### M132: Exact-Match Automatic Fragment Attribution

Status: complete

Acceptance criteria:

- Callers can ask the kernel to propagate active source fragment labels to an
  output node without manually supplying the output byte range.
- Propagation happens only when a labeled source byte span appears exactly once
  in the output bytes.
- Ambiguous repeated output matches do not propagate labels.
- Propagated fragment-label events record the source node id and source range in
  payload metadata.
- Existing fragment visibility rules block the automatically attributed output
  span while allowing unrelated output ranges to remain readable.

Implemented:

- Added `auto_propagate_fragment_policy_labels(source_node_id, output_node_id,
  agent_id, run_id=None, tool_call_id=None)`.
- Added conservative exact-byte matching over active source fragment labels.
- Added de-duplication for output `(offset, length, label, value)` events.
- Added regression coverage for unique exact matches, repeated ambiguous
  matches, propagated audit payload metadata, and visibility enforcement.

Remaining:

- Normalized JSON scalar source-to-output attribution for strings,
  integer-valued numbers, booleans, nulls, and finite numbers is implemented
  through M185/M186/M228. Broader semantic, non-exact, and cross-format
  attribution remains future work.

### M185: Normalized JSON String Scalar Attribution

Status: complete

Acceptance criteria:

- The kernel can propagate active fragment labels from a labeled JSON string
  scalar to an output where the same scalar appears once without JSON quotes.
- Existing exact-match attribution remains exact-only and does not silently
  change behavior.
- Ambiguous repeated normalized output matches do not propagate labels.
- Propagated fragment-label events retain source node id and source range
  metadata.
- Existing fragment visibility rules block the attributed output span while
  allowing unrelated ranges to remain readable.

Implemented:

- Added
  `auto_propagate_fragment_policy_labels_by_normalized_scalar(source_node_id,
  output_node_id, agent_id, run_id=None, tool_call_id=None)`.
- Added conservative JSON string scalar normalization using JSON string
  decoding, then unique unquoted byte matching in the output node.
- Added regression coverage proving exact attribution does not propagate
  `"123-45-6789"` to `123-45-6789`, normalized scalar attribution does,
  repeated normalized output matches are skipped, audit payload source metadata
  is recorded, and fragment visibility enforcement blocks the mapped output
  span.

Remaining:

- This covers one common cross-format scalar case. Integer-valued JSON number
  normalization is implemented through M186, bool/null/finite-number
  normalization is implemented through M228, and JSON array/object value
  attribution is implemented through M229. Semantic entailment, paraphrase
  detection, and broader cross-format attribution extraction remain future
  work.

### M186: Normalized Integer-Valued JSON Number Attribution

Status: complete

Acceptance criteria:

- The normalized scalar attribution API can propagate active fragment labels
  from a labeled JSON number such as `7.0` to an output where the normalized
  integer text `7` appears exactly once.
- Existing exact-match attribution remains exact-only and does not silently
  propagate `7.0` to `7`.
- Ambiguous repeated normalized number output matches do not propagate labels.
- Propagated fragment-label events retain source node id and source range
  metadata.
- Existing fragment visibility rules block the attributed normalized output
  span while allowing unrelated ranges to remain readable.

Implemented:

- Extended
  `auto_propagate_fragment_policy_labels_by_normalized_scalar(source_node_id,
  output_node_id, agent_id, run_id=None, tool_call_id=None)` from JSON string
  decoding to a conservative JSON scalar helper.
- Added integer-valued JSON number normalization for signed integers, unsigned
  integers, and finite float-form JSON numbers with no fractional component.
- Preserved the unique-output-match requirement so repeated normalized output
  numbers remain ambiguous and are skipped.
- Added regression coverage proving exact attribution skips `7.0` to `7`,
  normalized scalar attribution propagates it, audit payload source metadata is
  recorded, duplicate normalized outputs are skipped, and fragment visibility
  enforcement blocks the mapped output byte.

Remaining:

- M228 adds boolean, null, and finite non-integer number normalization. M229
  adds JSON array/object value attribution. Locale/format-aware number
  rendering, semantic entailment, and broader cross-format attribution
  extraction remain future work.

### M133: Answer Evidence Coverage Inspection

Status: complete

Acceptance criteria:

- Callers can inspect an existing answer event and get one coverage row per
  cited ref/node.
- Coverage is derived from the answer payload's `retrieval_event_ids` and the
  existing `query_result:*` / `search_result:*` event edges.
- Answers with cited refs but no linked retrieval events report uncovered
  citations instead of pretending evidence sufficiency.
- The API is read-only and does not add a separate scoring policy to the
  kernel.

Implemented:

- Added `answer_evidence_coverage(answer_event_id) -> list[(citation_ref,
  citation_node_id, covered, covering_event_count)]`.
- Added regression coverage for a cited answer covered by a prior query event
  and a cited answer without linked retrieval context.

Remaining:

- Exact-quote support is implemented through M141; semantic entailment remains
  outside the compact kernel.

### M141: Answer Exact Quote Support

Status: complete

Acceptance criteria:

- Callers can inspect an existing answer event and get one quote-support row per
  cited ref/node.
- Quote support is derived from the answer event's output `result` edge and
  `answer_citation:*` input edges, not from trusted payload metadata.
- The API reports the longest exact byte overlap between the generated answer
  and each cited node.
- The API lets callers choose a minimum quote byte threshold.
- The API is read-only and does not add semantic entailment scoring or model
  dependencies to the kernel.

Implemented:

- Added `answer_quote_support(answer_event_id, min_quote_bytes=16) ->
  list[(citation_ref, citation_node_id, supported, longest_exact_bytes)]`.
- Added a deterministic longest exact byte-overlap scan over answer output
  bytes and cited-node bytes.
- Added regression coverage for directly quoted evidence, non-quoted cited
  evidence, and invalid thresholds.

Remaining:

- Semantic entailment, normalized quote matching, and model-based citation
  quality scoring remain future work.

### M138: Answer Token Accounting Estimate

Status: complete

Acceptance criteria:

- Answer event payloads record deterministic token estimates for generated
  answer bytes and cited evidence bytes.
- Answer node metadata carries the same accounting so materialized answer nodes
  preserve the context-cost signal.
- The estimator is stable and model-independent; it must not depend on vendor
  tokenizers, prompt templates, or pricing tables.
- Callers can inspect the accounting from an answer event id.
- `verify_integrity()` diagnoses missing, malformed, or inconsistent answer
  token-accounting payloads.

Implemented:

- Added `token_accounting` payload/metadata with schema
  `anfs.token_accounting.estimate.v1` and estimator `ceil_bytes_div_4`.
- Added `answer_token_accounting(answer_event_id) -> (schema, estimator,
  answer_tokens, citation_tokens, total_tokens, citation_count,
  retrieval_event_count)`.
- `verify_integrity()` recomputes expected answer/citation estimates from the
  answer result node and cited input nodes, then checks the recorded schema,
  estimator, token counts, citation count, and retrieval-event count.
- Added regression coverage for answers with and without linked retrieval
  events.
- Added regression coverage for corrupted answer token-accounting payloads.

Remaining:

- Caller-supplied model pricing and prompt-overhead profiles are implemented
  through M157. Exact vendor tokenizer parity and provider price-table freshness
  remain adapter-level concerns.

### M134: Markdown Body Heading Section Policy Extraction

Status: complete

Acceptance criteria:

- Callers can derive byte ranges for Markdown body sections headed by ATX
  headings (`#` through `######`).
- Opening frontmatter is skipped before body heading extraction.
- Fenced and indented code blocks are skipped so heading-like text inside code
  does not create policy spans.
- Section spans include nested lower-level headings and stop before the next
  same-or-higher-level heading.
- Conservative non-heading body block spans are exposed for paragraphs,
  paragraph lines, inline links, inline link destinations, inline link titles, inline link labels,
  reference links, reference link labels, reference markers, resolved reference
  destinations, resolved reference titles, inline strong spans, inline strong
  text, inline emphasis spans, inline emphasis text, inline images, inline image alt text,
  inline image destinations, inline image titles,
  reference images, reference image alt text, reference image markers, resolved
  reference image destinations, resolved reference image titles, inline code spans, autolinks, autolink targets,
  lists, list items, task checkboxes, blockquotes, blockquote lines, tables, table rows, table cells,
  fenced/indented code blocks, common HTML blocks, link reference definitions,
  and thematic breaks, scoped under the nearest heading when one exists.
- No-heading Markdown bodies can still expose `body.paragraph.N` /
  `body.list.N` paths.
- Callers can attach policy labels to Markdown section paths and reuse existing
  fragment visibility enforcement.

Implemented:

- Added `markdown_section_spans(node_id) -> list[(section_path, offset, length,
  kind)]`.
- Added `set_markdown_section_policy_label(node_id, section_path, label, value,
  agent_id, run_id=None, tool_call_id=None)`.
- Added regression coverage for frontmatter skipping, fenced/indented-code heading
  suppression, nested section inclusion, visibility enforcement, missing
  section rejection, conservative paragraph/list/table/code block extraction,
  and no-heading paragraph/list policy enforcement. Later milestones added
  paragraph-line, inline-strong, inline-strong-text, inline-emphasis,
  inline-emphasis-text, inline-link, inline-link-label, inline-link-destination, inline-link-title,
  reference-link, reference-link-label, reference-link-reference,
  reference-link-resolved-destination, reference-link-resolved-title,
  inline-image, inline-image-alt,
  inline-image-destination, inline-image-title, reference-image, reference-image-alt,
  reference-image-reference, reference-image-resolved-destination,
  reference-image-resolved-title, inline-code,
  autolink, autolink-target, list-item, task-checkbox, blockquote-line,
  table-align, table-row, table-cell, html-block, thematic-break, indented-code,
  link-reference, link-reference-label, link-reference-destination, and
  link-reference-title spans while keeping parent block paths stable.

Remaining:

- Conservative Markdown frontmatter object/list/block-scalar spans are
  implemented through M135. Full YAML parsing, deeper Markdown AST semantics,
  and cross-format extraction remain future work.

### M178: Conservative Setext Markdown Body Headings

Status: complete

Acceptance criteria:

- Markdown body section extraction supports common Setext headings in addition
  to ATX headings.
- Setext headings must remain conservative: only a paragraph-like title line
  immediately followed by an indented-at-most-three `===` or `---` underline is
  treated as a heading.
- Setext title and underline lines must not also be exposed as ordinary body
  paragraph/list/table/table-row/code blocks.
- Section policy labels over Setext heading paths must reuse the existing
  fragment label enforcement path.

Implemented:

- Added conservative Setext heading detection to `markdown_section_spans(...)`.
- Added body-block suppression for Setext title/underline pairs.
- Added regression coverage for Setext h1/h2 spans, section boundaries, scoped
  paragraph spans under Setext headings, fragment policy labeling, range
  blocking, and clean integrity verification.
- Updated README, PRD, design, kernel contract, progress, conformance, and
  requirements matrix docs.

Remaining:

- This is still not a full Markdown AST parser; deeper Markdown constructs,
  full YAML parsing, and non-exact/cross-format semantic extraction remain
  future work.

### M179: Conservative Markdown List Item Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes item-level spans inside conservative list
  blocks.
- Item paths must remain scoped under their parent list block path, so existing
  `body.<scope>.list.N` paths remain stable.
- List item labels must reuse existing fragment policy enforcement and allow
  unrelated list items in the same block to remain readable.
- The parser must remain conservative and must not claim full nested Markdown
  list AST semantics.

Implemented:

- Added `body.<scope>.list.N.item.M` span emission for marker lines inside
  conservative list blocks.
- Kept existing `list` block spans unchanged for callers that want whole-list
  policy.
- Added regression coverage for item span paths, item byte ranges, single-item
  fragment policy labels, allowed sibling item reads, blocked labeled item reads,
  and clean integrity verification.
- Updated README, PRD, design, kernel contract, progress, conformance, and
  requirements matrix docs.

Remaining:

- This is item-level extraction for conservative list blocks, not a complete
  Markdown nested-list AST parser.

### M180: Conservative Markdown Table Row Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes row-level spans inside conservative table
  blocks.
- Row paths must remain scoped under their parent table block path, so existing
  `body.<scope>.table.N` paths remain stable.
- Common Markdown separator rows such as `| --- | --- |` must not be exposed as
  policy-bearing content rows.
- Table row labels must reuse existing fragment policy enforcement and allow
  unrelated rows in the same table to remain readable.
- The parser must remain conservative and must not claim full Markdown table
  AST semantics.

Implemented:

- Added `body.<scope>.table.N.row.M` span emission for non-separator lines
  inside conservative table blocks.
- Kept existing `table` block spans unchanged for callers that want whole-table
  policy.
- Added regression coverage for row span paths, row byte ranges, separator-row
  skipping, single-row fragment policy labels, allowed sibling row reads,
  blocked labeled row reads, and clean integrity verification.
- Updated README, PRD, design, kernel contract, progress, conformance, and
  requirements matrix docs.

Remaining:

- This is row-level extraction for conservative pipe table blocks, not a
  complete Markdown table AST parser.

### M231: Conservative Markdown Thematic Break Body Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes standalone thematic break lines as
  conservative body block spans.
- Thematic break paths must be scoped under the nearest heading when one exists,
  using paths such as `body.private-notes.thematic-break.1`, and must use
  `body.thematic-break.N` in no-heading documents.
- ATX/Setext heading extraction must remain stable: Setext heading underline
  lines such as `---` must not also be exposed as thematic-break spans.
- Thematic-break labels must reuse existing fragment policy enforcement and
  allow surrounding paragraph/list/table/code blocks to keep their existing
  paths.
- The parser must remain conservative and must not claim full Markdown AST
  semantics.

Implemented:

- Added conservative thematic break detection for indented-at-most-three lines
  containing at least three `-`, `_`, or `*` markers with optional spaces.
- Added `thematic-break` body block span emission before generic paragraph/list
  grouping so thematic breaks act as block boundaries.
- Kept Setext heading suppression ahead of thematic-break detection.
- Added regression coverage for scoped `---` spans, no-heading `* * *` spans,
  and Setext underline non-emission.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a conservative thematic-break detector, not a complete Markdown block
  parser. Full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M232: Conservative Markdown HTML Body Block Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes common block-level HTML runs as conservative
  `html` body block spans.
- HTML block paths must be scoped under the nearest heading when one exists,
  using paths such as `body.private-notes.html.1`.
- HTML blocks must act as boundaries for surrounding paragraph/list/table/code
  block grouping.
- Heading-like text inside a captured HTML block must not create Markdown
  heading spans.
- HTML block labels must reuse existing fragment policy enforcement and allow
  surrounding body blocks to remain readable.
- The parser must remain conservative and must not claim full Markdown or HTML
  AST semantics.

Implemented:

- Added conservative HTML block-start detection for common Markdown block tags,
  HTML comments, declarations, and processing instructions with
  case-insensitive ASCII tag matching.
- Added `html` body block span emission that captures contiguous nonblank lines
  from a recognized HTML block start.
- Added body-block boundary handling so HTML blocks split adjacent paragraph
  blocks rather than being folded into them.
- Added regression coverage for scoped HTML block spans, uppercase tag matching,
  heading suppression inside HTML, fragment policy enforcement on only the HTML
  block, and readable surrounding paragraphs.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a conservative HTML block detector, not a complete Markdown/HTML AST
  parser. Full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M233: Conservative Markdown Indented Code Body Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes indented code runs as conservative `code`
  body block spans alongside fenced code blocks.
- Indented code paths must be scoped under the nearest heading when one exists,
  reusing the existing `body.<scope>.code.N` path family.
- Heading-like text inside indented code, such as `    # Not A Heading`, must
  not create Markdown heading spans.
- Indented code blocks must act as boundaries for surrounding paragraph/list/
  table/html/link-reference/thematic-break grouping.
- Indented-code labels must reuse existing fragment policy enforcement and
  allow surrounding paragraphs to remain readable.
- The parser must remain conservative and must not claim full Markdown AST
  semantics.

Implemented:

- Added indented code line detection for nonblank lines indented at least four
  ASCII whitespace bytes.
- Added `code` body block span emission for contiguous indented code lines.
- Restricted ATX heading detection to lines indented at most three spaces, so
  indented `# ...` lines no longer become heading paths.
- Added body-block boundary handling so indented code splits adjacent paragraph
  blocks rather than being folded into them.
- Added regression coverage for scoped indented code spans, heading suppression
  inside indented code, fragment policy enforcement on only the code block, and
  readable surrounding paragraphs.
- Updated README, kernel contract, and progress docs.

Remaining:

- This is a conservative indented-code detector, not a complete Markdown block
  parser. Blank-line continuation inside indented code, nested-list code blocks,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M234: Conservative Markdown Link Reference Body Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes single-line link reference definitions as
  conservative `link-reference` body block spans.
- Link reference paths must be scoped under the nearest heading when one exists,
  using paths such as `body.references.link-reference.1`.
- Link reference definitions must act as boundaries for surrounding paragraph/
  list/table/code/html/thematic-break grouping.
- A link reference definition followed by `---` must not be misclassified as a
  Setext heading.
- Link-reference labels must reuse existing fragment policy enforcement and
  allow surrounding paragraphs and other link references to remain readable.
- The parser must remain conservative and must not claim full Markdown AST
  semantics.

Implemented:

- Added conservative link reference line detection for indented-at-most-three
  single-line `[label]: destination` definitions with nonempty labels and
  nonempty destinations.
- Added `link-reference` body block span emission before generic paragraph/list
  grouping.
- Added body-block boundary handling so link references split adjacent
  paragraphs rather than being folded into them.
- Added Setext title suppression so `[label]: ...` followed by `---` remains a
  link reference plus thematic break rather than a heading.
- Added regression coverage for scoped link reference spans, Setext false
  positive suppression, fragment policy enforcement on only one link reference,
  and readable surrounding paragraphs plus sibling references.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a conservative single-line link reference detector, not a complete
  Markdown reference parser. Multiline link reference continuations, escaped
  bracket labels, full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M235: Conservative Markdown Blockquote Line Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes line-level spans inside conservative
  blockquote blocks.
- Blockquote line paths must stay under the parent blockquote path, using paths
  such as `body.quotes.blockquote.1.line.2`, so existing
  `body.<scope>.blockquote.N` paths remain stable.
- Blockquote-line labels must reuse existing fragment policy enforcement and
  allow sibling quote lines plus surrounding paragraphs to remain readable.
- The parser must remain conservative and must not claim full nested Markdown
  blockquote AST semantics.

Implemented:

- Added `body.<scope>.blockquote.N.line.M` span emission for `>` marker lines
  inside conservative blockquote blocks.
- Kept existing blockquote block spans unchanged for callers that want
  whole-quote policy.
- Added regression coverage for blockquote block and line span paths, exact
  line byte ranges, single-line fragment policy labels, readable sibling quote
  lines, readable surrounding paragraphs, and clean integrity verification.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is line-level extraction for conservative blockquote blocks, not a
  complete nested Markdown blockquote AST parser. Nested quote/list/code
  semantics, full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M236: Conservative Markdown Task Checkbox Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes checkbox marker spans inside conservative
  task-list items.
- Task checkbox paths must stay under the parent list-item path, using paths
  such as `body.checklist.list.1.item.2.checkbox`, so existing
  `body.<scope>.list.N` and `body.<scope>.list.N.item.M` paths remain stable.
- Unchecked `[ ]`, checked `[x]`, and checked `[X]` markers must be recognized
  for unordered and ordered list items.
- Task-checkbox labels must reuse existing fragment policy enforcement and
  allow sibling items plus the labeled item's non-checkbox text to remain
  readable.
- The parser must remain conservative and must not claim full Markdown task
  list AST semantics.

Implemented:

- Added task checkbox detection after conservative list markers and before item
  body text.
- Added `body.<scope>.list.N.item.M.checkbox` span emission for exact checkbox
  marker bytes.
- Preserved existing list-item numbering while adding checkbox child paths.
- Added regression coverage for unchecked, lowercase checked, uppercase checked,
  ordered-list checkbox markers, exact checkbox byte ranges, single-checkbox
  fragment policy labels, readable sibling checkboxes, and readable item text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is marker-level extraction for conservative task-list items, not a
  complete Markdown task-list AST parser. Nested task lists, loose-list
  continuation semantics, full Markdown AST semantics, full YAML parsing, and
  broad cross-format extraction remain future work.

### M237: Conservative Markdown Paragraph Line Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes line-level spans inside conservative
  paragraph blocks.
- Paragraph line paths must stay under the parent paragraph path, using paths
  such as `body.private-notes.paragraph.1.line.2`, so existing
  `body.<scope>.paragraph.N` paths remain stable.
- Paragraph-line labels must reuse existing fragment policy enforcement and
  allow sibling paragraph lines plus surrounding blocks to remain readable.
- The parser must remain conservative and must not claim full Markdown inline
  or paragraph-flow AST semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M` span emission for nonblank lines
  inside conservative paragraph blocks.
- Kept existing paragraph block spans unchanged for callers that want
  whole-paragraph policy.
- Added regression coverage for paragraph block and line span paths, exact line
  byte ranges, single-line fragment policy labels, readable sibling paragraph
  lines, readable surrounding list content, and clean integrity verification.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is line-level extraction for conservative paragraph blocks, not a
  complete Markdown inline parser. Soft-break semantics, nested inline
  precedence, full escaped-character semantics, full Markdown AST semantics,
  full YAML parsing, and broad cross-format extraction remain future work.

### M238: Conservative Markdown Inline Link Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative inline link spans inside
  paragraph lines.
- Inline link paths must stay under the parent paragraph-line path, using paths
  such as `body.inline-links.paragraph.1.line.2.link.1`, so existing paragraph
  and paragraph-line paths remain stable.
- Image markers such as `![alt](url)` must not be exposed as ordinary inline
  links.
- Inline-link labels must reuse existing fragment policy enforcement and allow
  same-line non-link text plus sibling links to remain readable.
- The parser must remain conservative and must not claim full Markdown inline
  AST semantics.

Implemented:

- Added conservative single-line `[label](destination)` detection for nonempty
  labels and nonempty destinations inside paragraph lines.
- Added `body.<scope>.paragraph.N.line.M.link.K` span emission for exact link
  bytes with kind `inline-link`.
- Preserved existing paragraph and paragraph-line spans unchanged for callers
  that want broader policy.
- Added regression coverage for public and private paragraph inline links,
  image-marker non-emission, exact link byte ranges, single-link fragment policy
  labels, readable sibling links, and readable same-line non-link text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is conservative inline-link extraction only, not a complete Markdown
  inline parser. Nested brackets, escaped-character normalization,
  collapsed/shortcut reference-style inline resolution, autolinks, image
  reference resolution, nested emphasis/link precedence, full Markdown AST
  semantics, full YAML parsing, and broad cross-format extraction remain future
  work.

### M239: Conservative Markdown Inline Code Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative inline code spans inside
  paragraph lines.
- Inline code paths must stay under the parent paragraph-line path, using paths
  such as `body.inline-code.paragraph.1.line.2.code.1`, so existing paragraph
  and paragraph-line paths remain stable.
- Multi-backtick runs must be matched by delimiter run length so inner shorter
  backtick runs can remain part of the code payload.
- Inline-code labels must reuse existing fragment policy enforcement and allow
  same-line non-code text plus sibling inline code spans to remain readable.
- The parser must remain conservative and must not claim full Markdown inline
  AST semantics.

Implemented:

- Added conservative same-line single-backtick code detection for nonempty code
  payloads inside paragraph lines; M257 later expanded this to matching
  same-line backtick runs of one or more backticks.
- Added `body.<scope>.paragraph.N.line.M.code.K` span emission for exact inline
  code bytes with kind `inline-code`.
- Preserved existing paragraph, paragraph-line, and block-code spans unchanged
  for callers that want broader policy.
- Added regression coverage for public and private paragraph inline code, exact
  inline code byte ranges, single-code-span fragment policy labels, readable
  sibling inline code, and readable same-line non-code text. M257 later added
  double/triple-backtick coverage.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is conservative same-line inline-code extraction only, not a complete
  Markdown inline parser. Escaped-character normalization, whitespace
  normalization, full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M240: Conservative Markdown Autolink Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative autolink spans inside paragraph
  lines.
- Autolink paths must stay under the parent paragraph-line path, using paths
  such as `body.autolinks.paragraph.1.line.2.autolink.1`, so existing paragraph
  and paragraph-line paths remain stable.
- Angle-bracket text that is not a supported URL/mailto target must not be
  exposed as an autolink.
- Autolink labels must reuse existing fragment policy enforcement and allow
  same-line non-autolink text plus sibling autolinks to remain readable.
- The parser must remain conservative and must not claim full Markdown autolink
  or inline AST semantics.

Implemented:

- Added conservative `<http://...>`, `<https://...>`, and `<mailto:...>`
  autolink detection for nonempty, whitespace-free targets inside paragraph
  lines.
- Added `body.<scope>.paragraph.N.line.M.autolink.K` span emission for exact
  autolink bytes with kind `autolink`.
- Preserved existing paragraph, paragraph-line, inline-link, and inline-code
  spans unchanged for callers that want broader policy.
- Added regression coverage for public HTTPS autolinks, private mailto
  autolinks, non-URL angle text non-emission, exact autolink byte ranges,
  single-autolink fragment policy labels, readable sibling autolinks, and
  readable same-line non-autolink text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is conservative URL/mailto autolink extraction only, not a complete
  Markdown inline parser. Bare email autolinks, full URI grammar, escaped
  target normalization, full Markdown AST semantics, full YAML parsing, and
  broad cross-format extraction remain future work. M241 adds conservative
  overlap suppression against inline-code ranges.

### M241: Conservative Markdown Inline Code Overlap Suppression

Status: complete

Acceptance criteria:

- Markdown inline-link, reference-link, inline-image, reference-image, and
  autolink extraction must not emit spans whose byte ranges overlap
  conservative inline-code spans in the same paragraph line.
- Public links/reference-links/images/reference-images/autolinks outside inline
  code on the same line must still be emitted with stable `link.N` /
  `reference-link.N` / `image.N` / `reference-image.N` / `autolink.N`
  numbering.
- Inline-code labels must reuse existing fragment policy enforcement and allow
  surrounding non-code text plus outside links to remain readable.
- Existing paragraph, paragraph-line, inline-link, reference-link,
  inline-image, reference-image, inline-code, and autolink paths must remain
  stable for non-overlapping inputs.

Implemented:

- Added a shared conservative inline-code range pass for each paragraph line.
- Updated inline-link, reference-link, inline-image, reference-image, and
  autolink span extraction to skip candidate spans that overlap those
  inline-code ranges.
- Kept inline-code span emission on the existing
  `body.<scope>.paragraph.N.line.M.code.K` path family.
- Added regression coverage for code-contained `[hidden](...)`,
  `[hidden][ref]`, `![hidden](...)`, `![hidden][ref]`, and `<https://...>`
  text, outside public link/reference-link/image/reference-image/autolink
  emission on the same lines, exact byte ranges, inline-code fragment policy
  enforcement, readable same-line non-code text, and readable outside
  links/reference-links/images/reference-images.
- Updated design, kernel contract, and progress docs.

Remaining:

- This is overlap suppression against the conservative same-line inline-code
  detector only. Escaped-character normalization, complete inline precedence,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M242: Conservative Markdown Inline Link Destination Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes destination-level spans inside conservative
  inline links.
- Destination paths must stay under the parent inline-link path, using paths
  such as `body.inline-links.paragraph.1.line.2.link.1.destination`, so
  existing inline-link paths remain stable.
- Destination spans must point at the bytes inside parentheses, excluding the
  surrounding parentheses and trimming only leading/trailing ASCII whitespace.
- Destination labels must reuse existing fragment policy enforcement and allow
  the link label plus same-line non-destination text to remain readable.
- The parser must remain conservative and must not claim full Markdown inline
  link grammar semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M.link.K.destination` span emission for
  conservative inline-link destinations.
- Kept existing `inline-link` spans unchanged for callers that want whole-link
  policy.
- Reused inline-code overlap suppression so code-contained links still do not
  emit link or destination spans.
- Added regression coverage for destination span paths, exact destination byte
  ranges, single-destination fragment policy labels, readable link label text,
  and readable same-line non-destination text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is destination-level extraction for conservative inline links, not a
  complete Markdown link parser. Escaped-character normalization,
  full title grammar, escaped title normalization, full inline precedence, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M243: Conservative Markdown Autolink Target Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes target-level spans inside conservative
  autolinks.
- Target paths must stay under the parent autolink path, using paths such as
  `body.autolinks.paragraph.1.line.2.autolink.1.target`, so existing autolink
  paths remain stable.
- Target spans must point at the bytes inside angle brackets, excluding the
  surrounding `<` / `>`.
- Target labels must reuse existing fragment policy enforcement and allow the
  angle-bracket wrapper plus same-line non-target text to remain readable.
- The parser must remain conservative and must not claim full Markdown autolink
  grammar semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M.autolink.K.target` span emission for
  conservative autolink targets.
- Kept existing `autolink` spans unchanged for callers that want whole-autolink
  policy.
- Reused inline-code overlap suppression so code-contained autolinks still do
  not emit autolink or target spans.
- Added regression coverage for target span paths, exact target byte ranges,
  single-target fragment policy labels, readable angle-bracket wrapper text,
  and readable same-line non-target text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is target-level extraction for conservative URL/mailto autolinks, not a
  complete Markdown autolink parser. Bare email autolinks, full URI grammar,
  escaped target normalization, full Markdown AST semantics, full YAML parsing,
  and broad cross-format extraction remain future work.

### M244: Conservative Markdown Inline Link Label Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes label-level spans inside conservative inline
  links.
- Label paths must stay under the parent inline-link path, using paths such as
  `body.inline-links.paragraph.1.line.2.link.1.label`, so existing inline-link
  and destination paths remain stable.
- Label spans must point at the bytes inside square brackets, excluding the
  surrounding `[` / `]`.
- Label labels must reuse existing fragment policy enforcement and allow the
  destination plus same-line non-label text to remain readable.
- The parser must remain conservative and must not claim full Markdown inline
  link grammar semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M.link.K.label` span emission for
  conservative inline-link labels.
- Kept existing `inline-link` and `inline-link-destination` spans unchanged for
  callers that want whole-link or destination policy.
- Reused inline-code overlap suppression so code-contained links still do not
  emit link, label, or destination spans.
- Added regression coverage for label span paths, exact label byte ranges,
  single-label fragment policy labels, readable destination text, and readable
  same-line non-label text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is label-level extraction for conservative inline links, not a complete
  Markdown inline parser. Nested brackets, escaped-character normalization,
  full inline precedence, full Markdown AST semantics, full YAML parsing, and
  broad cross-format extraction remain future work.

### M248: Conservative Markdown Inline Image Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative inline image spans inside
  paragraph lines for same-line `![alt](destination)` images.
- Image paths must stay under the parent paragraph-line path, using paths such
  as `body.inline-images.paragraph.1.line.2.image.1`, so existing paragraph,
  paragraph-line, and inline-link paths remain stable.
- Image alt and destination paths must stay under the parent image path, using
  `.alt` and `.destination` children.
- Destination spans must point at the bytes inside parentheses, excluding the
  surrounding parentheses and trimming only leading/trailing ASCII whitespace.
- Image destination labels must reuse existing fragment policy enforcement and
  allow the alt text plus same-line non-destination text to remain readable.
- The parser must remain conservative and must not claim full Markdown image
  grammar semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M.image.K` span emission for
  conservative inline images.
- Added `body.<scope>.paragraph.N.line.M.image.K.alt` and
  `body.<scope>.paragraph.N.line.M.image.K.destination` child spans for
  nonempty alt text and nonempty destinations.
- Kept existing `inline-link` paths unchanged; image markers still are not
  emitted as ordinary links.
- Reused inline-code overlap suppression so code-contained images do not emit
  image, alt, or destination spans.
- Added regression coverage for image byte ranges, alt byte ranges,
  destination byte ranges, destination fragment policy enforcement, readable alt
  and surrounding text, and code-contained image suppression.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is same-line inline image extraction for conservative `![alt](target)`
  forms, not a complete Markdown image parser. Nested brackets, escaped
  delimiter normalization, full title grammar, escaped title normalization,
  full inline precedence, full Markdown AST semantics, full YAML parsing, and
  broad cross-format extraction remain future work.

### M249: Conservative Markdown Reference-Style Link Resolution Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative inline reference-style link
  spans for same-file `[label][reference]` links when a matching single-line
  `[reference]: destination` definition exists.
- Reference-link paths must stay under the parent paragraph-line path, using
  paths such as `body.reference-links.paragraph.1.line.2.reference-link.1`, so
  existing inline-link, image, and autolink paths remain stable.
- Reference-link child paths must expose the visible label, the reference
  marker, and the resolved destination bytes from the matching definition.
- Definition label matching must use conservative case-insensitive ASCII
  normalization with ASCII whitespace collapsing.
- Reference-link resolved-destination labels must reuse existing fragment
  policy enforcement and allow the inline label/reference text to remain
  readable.
- The parser must remain conservative and must not claim full Markdown
  reference-link grammar semantics.

Implemented:

- Added a conservative same-file link-reference definition scan that skips
  fenced and indented code.
- Added `body.<scope>.paragraph.N.line.M.reference-link.K` span emission for
  resolved `[label][reference]` links.
- Added `.label`, `.reference`, and `.resolved-destination` child spans for
  inline label bytes, inline reference marker bytes, and definition destination
  bytes.
- Kept existing `inline-link`, `inline-image`, `autolink`, and
  `link-reference` block spans unchanged.
- Reused inline-code overlap suppression so code-contained reference links do
  not emit reference-link child spans.
- Added regression coverage for normalized reference matching, inline byte
  ranges, resolved destination byte ranges, destination fragment policy
  enforcement, readable inline label/reference text, readable outside reference
  links, and code-contained reference-link suppression.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is explicit same-line `[label][reference]` resolution against
  conservative single-line definitions, not a complete Markdown reference link
  parser. Full multiline definitions beyond immediate title continuations,
  full label grammar, full title grammar, duplicate-definition diagnostics,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M250: Conservative Markdown Reference-Style Image Resolution Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative reference-style image spans for
  same-file `![alt][reference]` images when a matching single-line
  `[reference]: destination` definition exists.
- Reference-image paths must stay under the parent paragraph-line path, using
  paths such as `body.reference-images.paragraph.1.line.2.reference-image.1`,
  so existing inline-image, reference-link, inline-link, and autolink paths
  remain stable.
- Reference-image child paths must expose the alt text, the reference marker,
  and the resolved destination bytes from the matching definition.
- Definition label matching must reuse conservative case-insensitive ASCII
  normalization with ASCII whitespace collapsing.
- Reference-image resolved-destination labels must reuse existing fragment
  policy enforcement and allow the inline alt/reference text to remain
  readable.
- The parser must remain conservative and must not claim full Markdown
  reference-image grammar semantics.

Implemented:

- Added `body.<scope>.paragraph.N.line.M.reference-image.K` span emission for
  resolved `![alt][reference]` images.
- Added `.alt`, `.reference`, and `.resolved-destination` child spans for
  inline alt bytes, inline reference marker bytes, and definition destination
  bytes.
- Reused the same conservative link-reference definition scan as M249.
- Kept existing `inline-image`, `reference-link`, `inline-link`, `autolink`,
  and `link-reference` block spans unchanged.
- Reused inline-code overlap suppression so code-contained reference images do
  not emit reference-image child spans.
- Added regression coverage for normalized reference matching, inline byte
  ranges, resolved destination byte ranges, destination fragment policy
  enforcement, readable inline alt/reference text, readable outside reference
  images, and code-contained reference-image suppression.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is explicit same-line `![alt][reference]` resolution against
  conservative single-line definitions, not a complete Markdown reference image
  parser. Full multiline definitions beyond immediate title continuations,
  full label grammar, full title grammar, duplicate-definition diagnostics,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M251: Conservative Markdown Inline Link and Image Title Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative title spans for direct inline
  links and images that use trailing quoted titles inside the destination
  parentheses.
- Destination spans for titled direct links/images must point only at the target
  URL/path bytes, excluding the title and surrounding whitespace.
- Title paths must stay under the parent direct link/image path, using `.title`
  children such as `body.links.paragraph.1.line.1.link.1.title`.
- Title spans must point at the bytes inside the quote delimiters, excluding the
  surrounding quotes.
- Title labels must reuse existing fragment policy enforcement and allow the
  destination plus same-line non-title text to remain readable.
- The parser must remain conservative and must not claim full Markdown title
  grammar semantics.

Implemented:

- Added a shared conservative destination/title splitter for direct
  `[label](target "title")`, `[label](target 'title')`,
  `![alt](target "title")`, and `![alt](target 'title')` forms.
- Added `body.<scope>.paragraph.N.line.M.link.K.title` spans with kind
  `inline-link-title`.
- Added `body.<scope>.paragraph.N.line.M.image.K.title` spans with kind
  `inline-image-title`.
- Preserved existing no-title direct link and image destination byte ranges.
- Added regression coverage for link and image title byte ranges, URL-only
  destination byte ranges, title fragment policy enforcement, and readable
  destination/surrounding text.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is trailing quote-title extraction for direct inline links/images, not a
  complete Markdown title parser; M259 later added conservative
  parenthesized-title support. Escaped title normalization, full title grammar,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M252: Conservative Markdown Link Reference Definition Component Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes component-level spans for conservative
  single-line link reference definitions.
- Link-reference component paths must stay under the parent link-reference
  block path, using `.label`, `.destination`, and optional `.title` children.
- Destination spans for titled reference definitions must point only at the
  target URL/path bytes, excluding the title and surrounding whitespace.
- Reference-link and reference-image resolved-destination spans must point only
  at the target URL/path bytes when the definition has a title.
- Reference-link and reference-image resolved-title spans must point at the
  title bytes inside quote delimiters when a matching definition has a title.
- Component labels and resolved-title labels must reuse existing fragment policy
  enforcement.

Implemented:

- Extended the conservative link-reference definition parser to split label,
  destination, and trailing quoted title components.
- Added `body.<scope>.link-reference.N.label`,
  `body.<scope>.link-reference.N.destination`, and
  `body.<scope>.link-reference.N.title` spans with kinds
  `link-reference-label`, `link-reference-destination`, and
  `link-reference-title`.
- Updated reference-link and reference-image resolved-destination spans to use
  URL-only destination ranges for titled definitions.
- Added `.resolved-title` child spans for reference-link and reference-image
  resolved titles.
- Added regression coverage for definition component byte ranges, URL-only
  resolved destinations, resolved-title byte ranges, and policy enforcement
  that keeps inline reference text readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is component extraction for conservative single-line definitions, not a
  complete Markdown reference definition parser. Full multiline definitions
  beyond immediate title continuations, full title grammar, escaped title
  normalization, full label grammar, duplicate-definition diagnostics, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M253: Conservative Markdown Collapsed Reference Link and Image Spans

Status: complete

Acceptance criteria:

- Markdown body extraction resolves collapsed reference links such as
  `[label][]` by using the visible label as the reference label.
- Markdown body extraction resolves collapsed reference images such as
  `![alt][]` by using the alt text as the reference label.
- Collapsed references must reuse the existing reference-link/reference-image
  path families so explicit `[label][reference]` and `![alt][reference]` paths
  remain stable.
- Collapsed `.reference` child spans must exist with zero length at the empty
  marker position, preserving the child path shape while representing the
  collapsed marker faithfully.
- Existing resolved-destination and resolved-title propagation from matching
  link-reference definitions must work for collapsed references.
- Inline-code overlap suppression must continue to block code-contained
  collapsed references.

Implemented:

- Updated conservative reference-link parsing to accept `[label][]` and resolve
  it against a matching `[label]: destination` definition.
- Updated conservative reference-image parsing to accept `![alt][]` and resolve
  it against a matching `[alt]: destination` definition.
- Kept `reference-link`, `reference-link-label`, `reference-link-reference`,
  `reference-link-resolved-destination`, `reference-link-resolved-title`,
  `reference-image`, `reference-image-alt`, `reference-image-reference`,
  `reference-image-resolved-destination`, and `reference-image-resolved-title`
  path families unchanged.
- Added regression coverage for collapsed link/image byte ranges, zero-length
  reference marker spans, resolved destination byte ranges, and readable inline
  collapsed reference text.
- Updated progress docs.

Remaining:

- This is collapsed-reference support for conservative single-line
  links/images, not a complete Markdown reference parser. Multiline
  definitions, full label grammar, full title grammar, duplicate-definition
  edge cases, full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M255: Conservative Markdown Inline Emphasis and Strong Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative single-line inline strong spans
  for `**text**` and `__text__` delimiters inside paragraph lines.
- Markdown body extraction exposes conservative single-line inline emphasis
  spans for `*text*` and `_text_` delimiters inside paragraph lines.
- Inline strong and emphasis spans must expose both the delimited span and a
  `.text` child span so policy labels can target either the marker-inclusive
  phrase or only the emphasized payload.
- Inline strong and emphasis extraction must suppress spans that overlap
  conservative inline-code ranges.
- Inline emphasis extraction must not duplicate conservative inline strong
  ranges.

Implemented:

- Added `inline-strong` / `inline-strong-text` spans under paragraph line paths
  such as `body.section.paragraph.1.line.1.strong.1` and
  `body.section.paragraph.1.line.1.strong.1.text`.
- Added `inline-emphasis` / `inline-emphasis-text` spans under paragraph line
  paths such as `body.section.paragraph.1.line.1.emphasis.1` and
  `body.section.paragraph.1.line.1.emphasis.1.text`.
- Reused the existing conservative inline-code exclusion model so emphasis-like
  delimiters inside backtick code spans are not exposed as semantic spans.
- Added regression coverage for star and underscore delimiters, strong and
  emphasis byte ranges, inline-code suppression, and `.text` fragment policy
  enforcement.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative same-line emphasis/strong extraction, not a complete
  CommonMark delimiter parser. Nested inline precedence, emphasis inside
  complex inline constructs, full escaped-character normalization, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M256: Conservative Markdown Escaped Inline Delimiter Suppression

Status: complete

Acceptance criteria:

- Conservative Markdown inline delimiter detection must not treat delimiter
  bytes escaped by an odd number of immediately preceding backslashes as syntax
  delimiters.
- Escaped `*` / `_` markers must not create inline emphasis or strong spans,
  while later unescaped emphasis and strong spans on the same line still work.
- Escaped `[` / `]` / `)` delimiters must not prematurely create or close
  inline link, inline image, reference-link, reference-image, or
  link-reference-definition spans.
- Escaped backticks must not create inline-code spans, and escaped `<` / `>`
  delimiters must not create autolink spans.
- Existing inline-code overlap suppression and existing child span path families
  must remain stable.

Implemented:

- Added shared Markdown byte helpers for odd-backslash escape detection and
  forward/reverse unescaped delimiter lookup.
- Updated conservative inline strong/emphasis, inline link, inline image,
  reference-link, reference-image, inline-code, autolink, inline link/image
  title detection, and link-reference-definition label detection to use
  escape-aware delimiter lookup.
- Added regression coverage proving escaped emphasis/strong, link, image,
  code, autolink, and reference-link candidates do not emit spans while
  unescaped syntax later on the same line still does.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is delimiter suppression for current conservative inline parsers, not a
  complete CommonMark escape implementation. Escaped-character semantic
  normalization, escaped reference-label matching, nested inline precedence,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M257: Conservative Markdown Multi-Backtick Inline Code Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes inline-code spans for same-line matching
  backtick runs of one or more backticks.
- Multi-backtick code spans must match opening and closing delimiter run length
  so shorter backtick runs can remain part of the code payload.
- Existing `body.<scope>.paragraph.N.line.M.code.K` paths and `inline-code`
  kind values must remain stable.
- Inline link, reference-link, inline image, reference-image, autolink,
  emphasis, and strong extraction must continue to suppress spans overlapping
  multi-backtick inline-code ranges.
- Existing single-backtick policy enforcement must continue to work.

Implemented:

- Replaced the single-backtick-only detector with a same-line backtick-run
  matcher that records exact spans from the opening run through the matching
  closing run.
- Added double-backtick and triple-backtick regression coverage, including
  inner shorter backtick runs that stay inside the code payload.
- Added overlap regression coverage proving links inside multi-backtick code
  do not emit link spans while public links later on the same line still do.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative same-line backtick-run extraction, not a complete
  CommonMark code-span parser. Whitespace normalization, newline-spanning code
  spans, full escaped-character normalization, nested inline precedence, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M258: Conservative Markdown Nested Parentheses in Inline Destinations

Status: complete

Acceptance criteria:

- Direct inline link destination parsing must preserve balanced unescaped
  parentheses inside the destination instead of closing at the first `)`.
- Direct inline image destination parsing must preserve balanced unescaped
  parentheses inside the destination instead of closing at the first `)`.
- Escaped parentheses inside direct inline destinations must not prematurely
  close the destination span.
- Existing inline-link, inline-link-destination, inline-image, and
  inline-image-destination path families must remain stable.
- Existing title extraction for trailing quoted titles must still work when the
  destination contains balanced parentheses.

Implemented:

- Added a conservative inline destination close scanner that tracks unescaped
  `(` / `)` depth and ignores escaped parentheses.
- Updated direct inline link and image parsing to use the balanced destination
  close scanner.
- Added regression coverage for nested-parenthesis link destinations,
  nested-parenthesis titled link destinations, nested-parenthesis image
  destinations, escaped-parenthesis destinations, and exact destination byte
  ranges.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is balanced-parenthesis support for same-line direct inline link/image
  destinations, not a complete Markdown destination parser. Full title grammar,
  full escaped-character normalization, nested inline precedence, full Markdown
  AST semantics, full YAML parsing, and broad cross-format extraction remain
  future work.

### M259: Conservative Markdown Parenthesized Title Spans

Status: complete

Acceptance criteria:

- Direct inline link title extraction must support trailing parenthesized title
  payloads such as `[label](destination (Title))`.
- Direct inline image title extraction must support trailing parenthesized title
  payloads such as `![alt](destination (Title))`.
- Link-reference definition component extraction must support trailing
  parenthesized title payloads such as `[ref]: destination (Title)`.
- Reference-link and reference-image resolved-title spans must point at the
  parenthesized definition title payload when a matching definition uses
  parenthesized title syntax.
- Destination spans must continue to exclude title bytes and surrounding
  whitespace.

Implemented:

- Extended the shared Markdown destination/title splitter to recognize a final
  unescaped `)` with a matching `(` preceded by whitespace as a conservative
  parenthesized title delimiter.
- Reused the same splitter for direct inline links, direct inline images, and
  link-reference definitions, keeping existing title path families stable.
- Added regression coverage for parenthesized direct inline link titles,
  parenthesized direct inline image titles, parenthesized link-reference
  definition titles, resolved reference titles, and URL-only destination spans.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative trailing parenthesized-title extraction, not a complete
  Markdown title parser. Escaped title normalization, full title grammar,
  newline-spanning definitions, nested inline precedence, full Markdown AST
  semantics, full YAML parsing, and broad cross-format extraction remain future
  work.

### M260: Conservative Markdown Angle-Bracketed Inline Destination Spans

Status: complete

Acceptance criteria:

- Direct inline link destination spans must support angle-bracketed
  destinations such as `[label](<https://example.test/path with spaces>)`.
- Direct inline image destination spans must support angle-bracketed
  destinations such as `![alt](<https://example.test/image name.png>)`.
- Destination child spans must point at the payload bytes inside `<` / `>`,
  excluding the surrounding angle-bracket delimiters.
- Existing trailing quoted-title extraction must still work after an
  angle-bracketed direct inline destination.
- Existing direct inline link/image path families must remain stable.

Implemented:

- Updated the shared destination/title splitter to narrow direct inline
  destination payload spans when the destination body is wrapped in unescaped
  `<` / `>` delimiters.
- Added regression coverage for angle-bracketed direct inline link
  destinations, angle-bracketed titled direct inline link destinations, and
  angle-bracketed direct inline image destinations.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative angle-bracket payload extraction for same-line direct
  inline link/image destinations, not a complete Markdown destination parser.
  Escaped angle-bracket normalization, full title grammar, nested inline
  precedence, full Markdown AST semantics, full YAML parsing, and broad
  cross-format extraction remain future work.

### M261: Conservative Markdown Link Reference Angle Destination Spans

Status: complete

Acceptance criteria:

- Link-reference definition destination spans must support angle-bracketed
  destinations such as `[ref]: <https://example.test/path with spaces>`.
- Link-reference definition `.destination` child spans must point at the
  payload bytes inside `<` / `>`, excluding the surrounding angle-bracket
  delimiters.
- Reference-link and reference-image `.resolved-destination` child spans must
  point at the same angle-bracket payload bytes when a matching definition uses
  angle destination syntax.
- Existing title extraction must continue to work after an angle-bracketed
  link-reference definition destination.
- Existing link-reference path families must remain stable.

Implemented:

- Verified the shared destination/title splitter used by link-reference
  definitions already narrows angle-bracketed payload spans after M260.
- Added regression coverage for reference-link definitions with angle
  destinations, resolved reference-link destinations, reference-image
  definitions with angle destinations, and resolved reference-image
  destinations.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative angle-bracket payload extraction for single-line
  link-reference definitions, not a complete Markdown reference definition
  parser. Escaped angle-bracket normalization, full multiline definitions
  beyond immediate title continuations, full title grammar,
  duplicate-definition diagnostics, full Markdown AST semantics, full YAML
  parsing, and broad cross-format extraction remain future work.

### M262: Conservative Markdown Escaped Reference Label Normalization

Status: complete

Acceptance criteria:

- Reference-link label matching must normalize backslash-escaped ASCII
  punctuation so inline markers such as `[label][escaped\] ref]` can match
  definitions such as `[escaped\] ref]: destination`.
- Reference-image label matching must apply the same escaped punctuation
  normalization.
- Link-reference definition duplicate checks must use the same normalized label
  representation.
- Raw `reference-link-reference`, `reference-image-reference`, and
  `link-reference-label` byte spans must remain unchanged so policy and audit
  can still target the source bytes exactly.
- Existing ASCII case-folding and whitespace-collapsing label normalization must
  remain stable.

Implemented:

- Added conservative reference-label unescaping for backslash-escaped ASCII
  punctuation before UTF-8 decoding, ASCII lowercasing, and whitespace
  collapsing.
- Added regression coverage for escaped reference-link markers, escaped
  link-reference definition labels, resolved link destinations, escaped
  reference-image markers, escaped image definition labels, and resolved image
  destinations.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is conservative escaped ASCII punctuation normalization for reference
  labels, not a complete CommonMark label parser. Full Unicode case folding,
  entity normalization, full multiline definitions beyond immediate title
  continuations, full title grammar, duplicate-definition diagnostics, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M263: Conservative Markdown Duplicate Reference Definition Precedence

Status: complete

Acceptance criteria:

- Reference-link resolution must use the first matching normalized
  link-reference definition when later definitions reuse the same normalized
  label.
- Reference-image resolution must use the same first-definition-wins behavior.
- Later duplicate definitions must still expose `link-reference`,
  `link-reference-label`, `link-reference-destination`, and optional
  `link-reference-title` component spans for audit and policy labeling.
- Duplicate matching must use the same normalized label representation as
  ordinary reference resolution, including ASCII case folding, whitespace
  collapsing, and conservative escaped ASCII punctuation normalization.
- Existing reference-link/reference-image path families must remain stable.

Implemented:

- Added regression coverage proving duplicate reference links resolve to the
  first normalized definition destination while a later duplicate definition
  still exposes its raw destination span.
- Added regression coverage proving duplicate reference images resolve to the
  first normalized definition destination while a later duplicate definition
  still exposes its raw destination span.
- Documented the existing first-definition-wins resolution behavior as a stable
  conservative contract.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is deterministic duplicate precedence for conservative single-line
  link-reference definitions, not a complete Markdown reference definition
  system. Duplicate-definition diagnostics, full multiline definitions beyond
  immediate title continuations, full label grammar, full title grammar, full
  Markdown AST semantics, full YAML parsing, and broad cross-format extraction
  remain future work.

### M264: Conservative Markdown Link Reference Title Continuation Spans

Status: complete

Acceptance criteria:

- Link-reference definitions must support an immediate next-line title-only
  continuation after a destination-only first line, such as `[ref]: target`
  followed by `"Title"`.
- Continuation titles must support quoted and parenthesized title payloads.
- Link-reference block spans must cover both the definition line and the
  immediate title continuation line.
- `link-reference-title`, `reference-link-resolved-title`, and
  `reference-image-resolved-title` spans must point at the continuation title
  payload bytes.
- Existing single-line link-reference parsing and duplicate precedence must
  remain stable.

Implemented:

- Added a conservative continuation-title detector for the immediate next line
  after a title-less link-reference definition.
- Updated the link-reference definition scan to propagate continuation title
  spans into resolved reference-link/reference-image metadata.
- Updated link-reference body block extraction to include the title
  continuation line and expose a `link-reference-title` child span over the
  continuation payload.
- Added regression coverage for multiline reference-link and reference-image
  definitions with quoted continuation titles, resolved-title spans, definition
  component spans, and block byte ranges.
- Updated README, PRD, design, kernel contract, conformance, requirements
  matrix, and progress docs.

Remaining:

- This is immediate next-line title-only continuation support, not a complete
  multiline Markdown reference definition parser. Multiline destinations,
  arbitrary continuation blocks, escaped title normalization, full title
  grammar, full label grammar, duplicate-definition diagnostics, full Markdown
  AST semantics, full YAML parsing, and broad cross-format extraction remain
  future work.

### M254: Conservative Markdown Shortcut Reference Link and Image Spans

Status: complete

Acceptance criteria:

- Markdown body extraction resolves shortcut reference links such as `[label]`
  by using the visible label as the reference label when a matching definition
  exists.
- Markdown body extraction resolves shortcut reference images such as `![alt]`
  by using the alt text as the reference label when a matching definition
  exists.
- Shortcut references must reuse the existing reference-link/reference-image
  path families so explicit and collapsed reference paths remain stable.
- Shortcut `.reference` child spans must exist with zero length immediately
  after the closing `]`, preserving the child path shape while representing the
  absent marker faithfully.
- Existing resolved-destination and resolved-title propagation from matching
  link-reference definitions must work for shortcut references.
- Direct links/images followed by `(` must not be reclassified as shortcut
  references.

Implemented:

- Updated conservative reference-link parsing to accept `[label]` and resolve
  it against a matching `[label]: destination` definition when the label is not
  followed by `(` or `[`.
- Updated conservative reference-image parsing to accept `![alt]` and resolve
  it against a matching `[alt]: destination` definition when the alt text is not
  followed by `(` or `[`.
- Kept `reference-link`, `reference-link-label`, `reference-link-reference`,
  `reference-link-resolved-destination`, `reference-link-resolved-title`,
  `reference-image`, `reference-image-alt`, `reference-image-reference`,
  `reference-image-resolved-destination`, and `reference-image-resolved-title`
  path families unchanged.
- Added regression coverage for shortcut link/image byte ranges, zero-length
  reference marker spans, resolved destination byte ranges, and readable inline
  shortcut reference text.
- Updated progress docs.

Remaining:

- This is shortcut-reference support for conservative single-line links/images,
  not a complete Markdown reference parser. Full multiline definitions beyond
  immediate title continuations, full label grammar, full title grammar,
  duplicate-definition diagnostics, full Markdown AST semantics, full YAML
  parsing, and broad cross-format extraction remain future work.

### M245: Conservative Markdown Table Cell Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative cell-level spans inside
  pipe-style table rows.
- Cell paths must stay under their parent table row paths, using paths such as
  `body.accounts.table.1.row.3.cell.2`, so existing table and row paths remain
  stable.
- Cell spans must point at trimmed nonempty cell payload bytes, excluding pipe
  separators and leading/trailing ASCII whitespace.
- Cell labels must reuse existing fragment policy enforcement and allow
  neighboring cells and rows to remain readable.
- The parser must remain conservative and must not claim full Markdown table
  grammar semantics.

Implemented:

- Added `body.<scope>.table.N.row.M.cell.K` span emission for conservative
  table cells.
- Kept existing `table` and `table-row` spans unchanged for callers that want
  whole-table or row-level policy.
- Added regression coverage for header/data cell paths, exact cell byte ranges,
  single-cell fragment policy labels, readable neighboring cell text, and
  readable unrelated rows.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is cell-level extraction for conservative pipe tables, not a complete
  Markdown table parser. Multiline cells, block content inside table cells,
  full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M246: Escaped-Pipe-Aware Markdown Table Cell Splitting

Status: complete

Acceptance criteria:

- Conservative Markdown table row and cell detection must treat `\|` as table
  cell payload text rather than as a cell separator.
- Existing table, table-row, and table-cell paths must remain stable for
  ordinary pipe tables.
- Cell fragment labels over rows containing escaped pipes must still enforce
  policy at the intended cell byte range.
- Separator-row detection must use the same unescaped-pipe boundary rule as
  table cell span extraction.

Implemented:

- Added a shared unescaped-pipe predicate for conservative table line
  detection, separator-row detection, and table cell splitting.
- Updated table cell span extraction so escaped pipes stay inside the current
  cell payload.
- Added regression coverage for escaped-pipe payload bytes, absence of a
  spurious extra cell, and single-cell policy enforcement on the neighboring
  cell.

Remaining:

- This still is not a complete Markdown table parser. Multiline cells, block
  content inside table cells, full Markdown AST semantics, full YAML parsing,
  and broad cross-format extraction remain future work.

### M247: Conservative Markdown Table Alignment Spans

Status: complete

Acceptance criteria:

- Markdown body extraction exposes conservative table alignment metadata spans
  from pipe-table separator rows.
- Alignment paths must stay under their parent table path, using paths such as
  `body.accounts.table.1.align.2`, so existing table, row, and cell paths remain
  stable.
- Alignment spans must point at trimmed separator payload bytes such as `---`,
  `:---`, `---:`, or `:---:`, excluding pipe separators and surrounding
  whitespace.
- Alignment labels must reuse existing fragment policy enforcement and allow
  data rows to remain readable.
- Separator-row detection and alignment span extraction must use the same
  unescaped-pipe boundary rule.

Implemented:

- Added `body.<scope>.table.N.align.K` span emission for conservative table
  alignment cells.
- Kept existing `table`, `table-row`, and `table-cell` spans unchanged.
- Added regression coverage for left/right/center alignment byte ranges,
  alignment fragment policy enforcement, and readable data rows.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is alignment-metadata extraction for conservative pipe tables, not a
  complete Markdown table parser. Multiline cells, block content inside table
  cells, full Markdown AST semantics, full YAML parsing, and broad cross-format
  extraction remain future work.

### M135: Nested Scalar Markdown Frontmatter Extraction

Status: complete

Acceptance criteria:

- Callers can derive byte ranges for top-level Markdown frontmatter scalar
  fields.
- Callers can derive byte ranges for indented nested scalar frontmatter fields
  using dotted paths such as `frontmatter.owner.email`.
- Callers can derive conservative object spans, scalar sequence item spans,
  inline scalar sequence item spans, inline object scalar field spans, and block
  scalar spans.
- Nested frontmatter field labels resolve to byte-range fragment policy labels.
- Existing fragment visibility rules block the labeled nested scalar value
  range.
- Full YAML alias expansion, merge keys, complete tag typing, and complex YAML
  typing remain outside the compact parser.

Implemented:

- Extended conservative opening `---` frontmatter parsing with an indentation
  stack for nested scalar `key: value` fields.
- Added conservative object spans, scalar sequence item paths, sequence object
  item field paths, inline scalar sequence item paths, and `|` / `>` block
  scalar spans. Later milestones added inline object scalar field paths,
  quoted-key bracket paths, inline sequence object item fields, bounded nested
  inline object fields, bounded nested inline array item paths, and tag/anchor
  decoration payload spans while preserving existing frontmatter paths.
- Preserved existing top-level field paths such as `frontmatter.owner_email`.
- Added regression coverage for nested scalar span extraction, object/list/block
  scalar spans, nested field policy labels, and fragment visibility enforcement.

Remaining:

- Full YAML alias expansion, merge keys, complete tag typing, and complex YAML
  typing remain future work.

### M182: Conservative Markdown Frontmatter Inline Sequence Items

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction exposes item-level spans for common inline
  scalar sequences such as `tags: [public, secret]`.
- Inline sequence parent paths such as `frontmatter.tags` remain available and
  are classified as `array`.
- Item paths such as `frontmatter.tags[1]` resolve to exact byte ranges for the
  item value, so policy labels can block one item without blocking siblings.
- Existing block sequence, nested scalar, object, and block-scalar frontmatter
  paths remain stable.
- The parser must remain conservative and must not claim full YAML semantics.

Implemented:

- Added `push_markdown_frontmatter_value_spans(...)` and inline sequence item
  extraction under the existing frontmatter field-span API.
- Added `frontmatter.tags[0]` / `frontmatter.tags[1]` / typed scalar item
  coverage for inline sequence values.
- Added regression coverage proving a `tag-secret` label on
  `frontmatter.tags[1]` blocks only that item while `frontmatter.tags[0]`
  remains readable.
- Updated README, PRD, kernel contract, progress, conformance, design, and
  requirements matrix docs.

Remaining:

- This is common inline scalar sequence extraction only. Bounded nested inline
  array item paths are implemented through M191, and tag/anchor-decorated
  payload spans are implemented through M192, and alias token spans are
  implemented through M193. Full YAML alias expansion, merge keys, complete tag
  typing, recursive semantics beyond bounded inline object/array fields, and
  complex YAML typing remain future work.

### M183: Conservative Markdown Frontmatter Inline Object Fields

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction exposes parent spans for common inline objects
  such as `contact: {email: ada@example.com, team: privacy}`.
- Direct scalar fields inside the inline object resolve to dotted paths such as
  `frontmatter.contact.email`.
- Inline object field labels must reuse existing fragment policy enforcement
  and allow unrelated sibling fields in the same object to remain readable.
- Existing scalar, block sequence, inline sequence, nested object, and
  block-scalar frontmatter paths remain stable.
- The parser must remain conservative and must not claim full YAML semantics.

Implemented:

- Added conservative inline object detection for `{...}` frontmatter values.
- Added direct scalar field extraction for bare-key inline mappings, including
  value kind inference. Later milestones added quoted-key bracket paths while
  preserving existing bare-key paths.
- Added regression coverage for `frontmatter.contact`,
  `frontmatter.contact.email`, `frontmatter.contact.team`,
  `frontmatter.contact.priority`, and `frontmatter.contact.active`.
- Added policy coverage proving a deny label on `frontmatter.contact.email`
  blocks only that field while `frontmatter.contact.team` remains readable.
- Updated README, PRD, kernel contract, progress, conformance, design, and
  requirements matrix docs.

Remaining:

- This is direct scalar field extraction for simple inline objects only. Inline
  sequence object item fields are implemented through M189, and bounded nested
  inline object fields are implemented through M190. Bounded nested inline array
  item paths are implemented through M191, and tag/anchor-decorated payload
  spans are implemented through M192, and alias token spans are implemented
  through M193. Full YAML alias expansion, merge keys, complete tag typing, and
  complex YAML typing remain future work.

### M188: Conservative Markdown Frontmatter Quoted Keys

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction supports quoted YAML keys in top-level,
  indented nested, block sequence object item, and inline object field contexts.
- Quoted keys that would be ambiguous under dotted paths, such as
  `"owner.email"` or `"team:name"`, must resolve to bracket paths such as
  `frontmatter["owner.email"]` and `frontmatter.quoted_parent["team:name"]`.
- Quoted-key field labels must reuse existing fragment policy enforcement and
  allow unrelated sibling fields in the same object to remain readable.
- Existing bare-key paths such as `frontmatter.owner.email` and
  `frontmatter.contact.email` remain stable.
- The parser remains conservative and does not claim full YAML semantics.

Implemented:

- Changed frontmatter key splitting to find key/value separators outside quotes,
  so quoted key text containing `:` is not split incorrectly.
- Added conservative quoted-key decoding for double-quoted JSON-style keys and
  single-quoted YAML-style keys with doubled single-quote escaping.
- Added bracket path formatting for non-bare keys using JSON string escaping,
  while keeping simple bare keys on dotted paths.
- Extended inline object field extraction to use the same quoted-key parser and
  bracket path formatter.
- Added regression coverage for top-level quoted keys, nested quoted keys,
  inline object quoted keys, bracket-path policy labels, sibling field reads,
  blocked quoted-key field reads, and clean integrity verification.

Remaining:

- Full YAML alias expansion, merge keys, complete tag typing, and complex YAML
  typing remain future work.

### M189: Conservative Markdown Frontmatter Inline Sequence Object Fields

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction exposes item-level object spans for common
  inline sequence object items such as
  `contacts: [{email: first@example.com, team: legal}]`.
- Direct scalar fields inside an inline sequence object item resolve to paths
  such as `frontmatter.contacts[0].email`.
- Quoted keys inside inline sequence object items resolve to bracket paths such
  as `frontmatter.contacts[1]["email.addr"]`.
- Inline sequence splitting must ignore commas inside quoted text and nested
  `{...}` / `[...]` delimiters, so object fields are not split as separate
  sequence items.
- Inline sequence object field labels reuse existing fragment policy enforcement
  and allow unrelated sibling fields in the same object item to remain readable.
- Existing inline scalar sequence item paths such as `frontmatter.tags[1]` remain
  stable.
- The parser remains conservative and does not claim full YAML semantics.

Implemented:

- Extended `push_markdown_inline_sequence_item_spans(...)` to classify direct
  inline mapping items as `object` spans and emit their direct scalar field
  spans under item paths.
- Reused the inline mapping field extractor, including M188 quoted-key bracket
  paths, for object items inside inline sequences.
- Added delimiter-depth-aware comma splitting for inline sequences and inline
  mappings so commas inside nested `{...}` / `[...]` values are not treated as
  top-level separators.
- Added regression coverage for `frontmatter.contacts`, object item spans,
  `frontmatter.contacts[0].email`, `frontmatter.contacts[1]["email.addr"]`,
  sibling reads, blocked field reads, and clean integrity verification.

Remaining:

- Bounded nested inline object fields are implemented through M190, and bounded
  nested inline array item paths are implemented through M191, and tag/anchor
  decoration payload spans are implemented through M192, and alias token spans
  are implemented through M193. Full YAML alias expansion, merge keys, complete
  tag typing, and complex YAML typing remain future work.

### M190: Bounded Nested Inline Object Frontmatter Fields

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction exposes nested object spans inside inline
  objects, such as `frontmatter.contact.manager`.
- Direct scalar fields inside nested inline objects resolve to dotted paths such
  as `frontmatter.contact.manager.email`.
- The same nested object extraction works inside inline sequence object items,
  such as `frontmatter.contacts[0].meta.risk`.
- Nested inline object field labels reuse existing fragment policy enforcement
  and allow unrelated sibling fields to remain readable.
- Existing inline object, inline sequence object, and quoted-key paths remain
  stable.
- Recursion is bounded and the parser remains conservative rather than claiming
  full YAML semantics.

Implemented:

- Extended the inline mapping extractor to emit object spans for nested `{...}`
  field values, then recursively extract direct scalar fields under the nested
  path.
- Reused the existing quoted-key bracket path formatter for nested inline object
  fields.
- Added a bounded recursion depth to prevent the conservative parser from
  becoming an unbounded YAML interpreter.
- Added regression coverage for `frontmatter.contact.manager`,
  `frontmatter.contact.manager.email`, `frontmatter.contacts[0].meta`,
  `frontmatter.contacts[0].meta.risk`, sibling reads, blocked nested field
  reads, and clean integrity verification.

Remaining:

- Bounded nested inline array item paths are implemented through M191, and
  tag/anchor decoration payload spans are implemented through M192, and alias
  token spans are implemented through M193. Full YAML alias expansion, merge
  keys, recursive alias expansion, complete tag typing, and complex YAML typing
  remain future work.

### M191: Bounded Nested Inline Array Frontmatter Items

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction exposes nested inline array spans inside inline
  objects, such as `frontmatter.contact.groups`.
- Direct scalar items inside nested inline arrays resolve to item paths such as
  `frontmatter.contact.groups[1]`.
- The same nested inline array extraction works inside inline sequence object
  items, such as `frontmatter.contacts[0].meta.flags[1]`.
- Nested inline array item labels reuse existing fragment policy enforcement and
  allow unrelated sibling items to remain readable.
- Existing inline scalar sequence, inline object, inline sequence object, nested
  inline object, and quoted-key paths remain stable.
- Recursion is bounded and the parser remains conservative rather than claiming
  full YAML semantics.

Implemented:

- Extended nested inline mapping field extraction to classify `[...]` field
  values as `array` spans and emit direct item spans under the nested field path.
- Reused the bounded inline sequence item extractor for nested arrays so scalar
  items, object items, and quoted object keys continue to share one parser.
- Added a shared bounded inline nesting limit for sequence and mapping traversal.
- Added regression coverage for `frontmatter.contact.groups`,
  `frontmatter.contact.groups[1]`, `frontmatter.contacts[0].meta.flags`,
  `frontmatter.contacts[0].meta.flags[1]`, sibling reads, blocked nested array
  item reads, and clean integrity verification.

Remaining:

- Tag/anchor decoration payload spans are implemented through M192, and alias
  token spans are implemented through M193. Full YAML alias expansion, merge
  keys, complete tag typing, and complex YAML typing remain future work.

### M192: Conservative YAML Tag And Anchor Decoration Payload Spans

Status: complete

Acceptance criteria:

- Markdown frontmatter field extraction ignores leading YAML tag and anchor
  decorations when computing payload spans, so `!pii &primary value` maps the
  field span to `value`.
- Decoration stripping works for top-level scalar values, inline object values,
  inline array values, and direct inline sequence items.
- Decorated inline object and array values still expose their object/array parent
  spans and child field/item spans.
- Decorated item labels reuse existing fragment policy enforcement and allow
  unrelated sibling items to remain readable.
- Existing undecorated frontmatter paths and spans remain stable.
- This decoration-stripping milestone does not perform alias expansion,
  merge-key evaluation, or complete YAML tag typing.

Implemented:

- Added `markdown_frontmatter_value_payload(...)` to strip leading `!tag`,
  `!!tag`, `!<tag>`, and `&anchor` decoration tokens from the payload span while
  preserving offsets for the remaining payload bytes.
- Applied payload stripping at top-level frontmatter values, inline sequence
  items, and inline mapping fields before object/array/scalar classification.
- Added regression coverage for `frontmatter.decorated_owner`,
  `frontmatter.decorated_contact`, `frontmatter.decorated_contact.email`,
  `frontmatter.decorated_contact.flags[1]`, sibling reads, blocked decorated
  item reads, and clean integrity verification.

Remaining:

- Alias token spans are implemented through M193, and conservative alias
  expansion paths are implemented through M195. Conservative merge-key,
  merge-array, and bounded recursive alias/merge chain expansion paths are
  implemented through M196, M198, and M222. Complete tag typing and complex YAML
  typing remain future work.

### M193: Conservative YAML Alias Token Frontmatter Spans

Status: complete

Acceptance criteria:

- YAML alias references such as `*primary` are exposed as field or item spans
  with kind `alias`.
- Alias token spans work for top-level scalar values, inline object fields, and
  inline sequence items.
- Alias token labels reuse fragment policy enforcement and block only the alias
  token, not siblings or the anchor target.
- This alias-token milestone does not perform alias expansion, merge-key
  evaluation, or complete YAML tag typing.

Implemented:

- Added conservative alias kind inference for valid `*alias_name` scalar
  payloads after existing tag/anchor decoration stripping.
- Reused the existing frontmatter field-span API so top-level aliases, inline
  object alias fields, and inline sequence alias items can be labeled through
  `set_markdown_field_policy_label(...)`.
- Added regression coverage for `frontmatter.alias_owner`,
  `frontmatter.alias_contact.email`, `frontmatter.alias_contact.backups[0]`,
  sibling reads, blocked alias-token reads, and clean integrity verification.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Conservative alias expansion paths are implemented through M195, and
	  conservative merge-key expansion paths are implemented through M196, and
	  conservative merge-array expansion paths are implemented through M198.
	  Bounded recursive alias/merge chain expansion is implemented through M222.
	  Nested merge arrays, complete YAML merge precedence, complete tag typing, and
	  complex YAML typing remain future work.

### M195: Conservative YAML Inline Object Alias Expansion Paths

Status: complete

Acceptance criteria:

- A top-level Markdown frontmatter inline object with a leading YAML anchor,
  such as `template: &contact {email: a@example.com}`, can be referenced by a
  top-level alias such as `copy: *contact`.
- The alias path exposes child semantic paths such as
  `frontmatter.copy.email` based on the anchored object's already parsed child
  paths.
- Expanded alias child paths use the alias token byte range and kind `alias`, so
  policy labels block the reference token rather than inventing bytes that do
  not exist at the alias site.
- Existing alias token spans, decorated payload spans, quoted-key paths, and
  inline object/array paths remain stable.

Implemented:

- Recorded top-level frontmatter anchor definitions and top-level alias
  references during conservative frontmatter parsing.
- Added post-processing that mirrors anchored child-path suffixes under the
  alias path while keeping offsets on the alias token.
- Added regression coverage for `frontmatter.alias_template.email`,
  `frontmatter.alias_template.flags`, and
  `frontmatter.alias_template.flags[0]`, plus policy enforcement proving a
  label on the expanded alias path blocks only the alias token while the anchor
  source remains readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Conservative merge-key expansion paths are implemented through M196, and
	  scalar alias target paths are implemented through M197, and conservative
	  merge-array expansion paths are implemented through M198. Bounded recursive
	  alias/merge chain expansion is implemented through M222. Complete YAML merge
	  precedence, complete tag typing, and complex YAML typing remain future work.

### M196: Conservative YAML Merge-Key Frontmatter Expansion Paths

Status: complete

Acceptance criteria:

- A top-level Markdown frontmatter inline object can use a simple merge key such
  as `merged: {<<: *contact_template, team: ops}` where the alias points to a
  top-level anchored inline object.
- The merged path exposes child semantic paths such as
  `frontmatter.merged.email` based on the anchored object's already parsed child
  paths, unless an explicit field already exists at that path.
- Merged child paths use the merge alias token byte range and kind `alias`, so
  policy labels block the merge reference token rather than inventing expanded
  bytes.
- Existing explicit fields in the merged object, alias expansion paths, quoted
  keys, decorated payload spans, and inline object/array paths remain stable.

Implemented:

- Added conservative detection for inline mapping merge fields whose key is
  `<<` and whose value is a single alias token.
- Added post-processing that mirrors anchored child-path suffixes under the
  merged object path while keeping offsets on the merge alias token.
- Added regression coverage for `frontmatter.merged_template.email`,
  `frontmatter.merged_template.flags`, `frontmatter.merged_template.flags[0]`,
  and explicit `frontmatter.merged_template.team`.
- Added policy coverage proving a label on a merged child path blocks only the
  merge alias token while the anchor source and explicit sibling remain
  readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Scalar alias target paths are implemented through M197, and conservative
	  merge-array expansion paths are implemented through M198. Bounded recursive
	  alias/merge chain expansion is implemented through M222. Complete YAML merge
	  precedence, complete tag typing, and complex YAML typing remain future work.

### M197: Conservative YAML Scalar Alias Target Paths

Status: complete

Acceptance criteria:

- A scalar frontmatter field with a leading YAML anchor can be dereferenced by
  scalar alias spans through stable target paths such as
  `frontmatter.alias_owner.__target` or
  `frontmatter.alias_contact.email.__target`.
- The alias token path, such as `frontmatter.alias_owner`, remains available and
  still points at the alias reference token.
- The target path points at the anchored scalar payload bytes and keeps the
  anchored scalar kind, so policy labels can protect the actual scalar data.
- Object and array aliases continue to use the M195 child-path expansion
  behavior rather than gaining a scalar target path.

Implemented:

- Added `append_markdown_scalar_alias_target_spans(...)` after alias and merge
  expansion post-processing. It scans emitted `alias` spans, so target paths
  work for top-level aliases, inline object alias fields, and inline array alias
  items.
- Added regression coverage for `frontmatter.alias_owner.__target` pointing at
  `tagged@example.com` while `frontmatter.alias_owner` still points at
  `*primary`, plus nested target paths such as
  `frontmatter.alias_contact.email.__target` and
  `frontmatter.alias_contact.backups[0].__target`.
- Added policy coverage proving a label on a nested scalar alias target path
  blocks the anchored payload bytes while the alias token remains readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Conservative merge-array expansion paths are implemented through M198, and
	  YAML null scalar kind spans are implemented through M199. Bounded recursive
	  scalar alias target resolution is implemented through M222. Complete YAML
	  merge precedence, complete tag typing, and complex YAML typing remain future
	  work.

### M198: Conservative YAML Merge-Array Frontmatter Expansion Paths

Status: complete

Acceptance criteria:

- A top-level Markdown frontmatter inline object can use a simple merge array
  such as `merged: {<<: [*contact_template, *phone_template], team: ops}` where
  every item is a single alias token pointing to a top-level anchored inline
  object.
- The merged object exposes child semantic paths from each anchored object, such
  as `frontmatter.merged.email` and `frontmatter.merged.phone`, unless an
  explicit field or earlier merge item already emitted that path.
- Merged child paths use the corresponding merge-array alias token byte range
  and kind `alias`, so policy labels block the specific merge reference token
  rather than inventing expanded bytes.
- Existing single-alias merge keys, scalar alias target paths, alias expansion
  paths, quoted keys, decorated payload spans, and inline object/array paths
  remain stable.

Implemented:

- Replaced single merge-reference extraction with conservative multi-reference
  extraction for `<<` values that are either `*anchor` or `[*a, *b]`.
- Reused merge expansion post-processing so each alias item mirrors anchored
  child-path suffixes under the merged object path while preserving that alias
  token's byte range.
- Added regression coverage for `frontmatter.merged_array_template.email`,
  `frontmatter.merged_array_template.flags`,
  `frontmatter.merged_array_template.phone`, and explicit
  `frontmatter.merged_array_template.team`.
- Added policy coverage proving a label on a merged-array child path blocks
  only the selected merge-array alias token while other merge aliases, anchor
  sources, and explicit siblings remain readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- YAML null scalar kind spans are implemented through M199. Bounded recursive
  alias/merge chain expansion is implemented through M222. Nested merge arrays,
  complete YAML merge precedence, complete tag typing, and complex YAML typing
  remain future work.

### M199: Conservative YAML Null Scalar Frontmatter Kind

Status: complete

Acceptance criteria:

- Markdown frontmatter scalar kind inference classifies YAML core null spellings
  `null`, `Null`, `NULL`, and `~` as kind `null`.
- Null kind works for top-level scalar fields and inline sequence items.
- Existing alias, bool, number, and string kind inference remains stable.
- Null field labels reuse existing fragment policy enforcement and allow
  unrelated sibling items to remain readable.

Implemented:

- Added null detection to `markdown_frontmatter_value_kind(...)` before bool and
  number inference.
- Added regression coverage for top-level `frontmatter.optional_owner` and
  inline sequence items `frontmatter.missing_values[0]` / `[1]`.
- Added policy coverage proving a deny label on a YAML null inline item blocks
  only that null item while a sibling remains readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Conservative YAML core tag kind overrides are implemented through M200.
  Timestamp/binary explicit tag kinds are implemented through M210. Set/omap
	  explicit container tag kinds are implemented through M211. Quoted scalar
	  semantic value decoding is implemented through M227. Full set/omap semantics,
	  complete YAML merge precedence, and complex YAML typing remain future work;
	  bounded recursive alias/merge chains are implemented through M222.

### M213: Conservative Quoted YAML Scalar Payload Spans

Status: complete

Acceptance criteria:

- Markdown frontmatter scalar spans for single-quoted and double-quoted scalar
  values exclude the quote delimiters, so fragment labels and reads target the
  raw scalar payload bytes.
- Quoted scalar values remain kind `string` even when the inner bytes look like
  numbers, booleans, nulls, or timestamps.
- Explicit YAML core scalar tags still override quoted scalar kind inference,
  for example `!!int "8"` is kind `number` while its span points at `8`.
- The parser remains conservative and does not normalize YAML scalar values or
  claim full YAML parsing. M227 adds a separate semantic value projection for
  decoded quoted scalar values while keeping these raw payload spans unchanged.

Implemented:

- Added `markdown_frontmatter_scalar_payload(...)` and
  `markdown_frontmatter_quoted_scalar_inner(...)` to derive scalar-only payload
  spans after tag/anchor decoration stripping.
- Applied quoted scalar payload spans to top-level frontmatter scalars, inline
  sequence scalar items, and inline mapping scalar fields without changing
  container detection.
- Updated `markdown_frontmatter_scalar_kind(...)` so untagged quoted scalars
  remain kind `string`, while explicit core tags still override the kind.
- Added regression coverage for quoted dates, quoted numeric/bool-looking
  scalars, explicit tagged quoted scalars, and policy reads over quoted scalar
  payloads.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is quote-delimiter span handling only. M227 adds decoded semantic values
  for quoted scalars without changing fragment spans. Semantic scalar
  normalization, complete YAML merge precedence, full YAML tag semantics, and
  complex YAML typing remain future work; bounded recursive alias/merge chains
  are implemented through M222.

### M212: Conservative Implicit YAML Timestamp Scalars

Status: complete

Acceptance criteria:

- Markdown frontmatter scalar kind inference classifies conservative unquoted
  ISO-like dates and datetimes as kind `timestamp`.
- Quoted date-looking scalars remain kind `string`.
- Inline sequence timestamp items use the same inference as top-level scalars.
- The parser must remain conservative and must not claim complete YAML
  timestamp parsing, timezone normalization, date validity beyond basic ranges,
  or semantic date conversion.

Implemented:

- Added `markdown_frontmatter_implicit_timestamp(...)` with narrow ASCII
  matching for `YYYY-MM-DD` and `YYYY-MM-DD[T|t| ]HH:MM:SS` plus optional
  fractional seconds and `Z` / `+HH:MM` / `-HH:MM` timezone suffixes.
- Kept quoted scalar values as strings before timestamp inference. M213 narrows
  their byte spans to the payload inside the quote delimiters.
- Added regression coverage for top-level implicit date/datetime scalars,
  quoted date strings, inline sequence date/datetime items, and policy labels
  on implicit timestamp payloads.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is conservative timestamp kind inference only. Full YAML timestamp
  grammar, calendar validation, timezone normalization, semantic date conversion,
  complete YAML merge precedence, full YAML tag semantics, and complex YAML
  typing remain future work; decoded quoted scalar semantic values are
  implemented through M227 and bounded recursive alias/merge chains are
  implemented through M222.

### M211: Conservative YAML Set And Omap Container Tag Kinds

Status: complete

Acceptance criteria:

- Markdown frontmatter extraction honors explicit YAML core `set` and `omap`
  tags on inline mapping/sequence containers.
- Parent container spans use kind `set` or `omap` while child field/item spans
  continue to expose conservative payload paths.
- Payload spans continue to exclude leading YAML tag/anchor decorations.
- The parser must remain conservative and must not claim full YAML set/omap
  semantics, duplicate-key handling, ordering guarantees beyond source byte
  order, or full YAML tag semantics.

Implemented:

- Added `markdown_frontmatter_container_kind(...)` so explicit container tags
  can override only container span kind while preserving existing object/array
  defaults.
- Extended `markdown_frontmatter_tag_kind(...)` to classify YAML core `set` and
  `omap` tags, including `tag:yaml.org,2002:*` URI spellings.
- Applied container kind overrides to top-level inline containers, inline
  mapping fields, and inline sequence container items.
- Added regression coverage for top-level `!!set` / `!!omap`, inline sequence
  set/omap container items, child paths under tagged containers, and fragment
  policy labels on tagged container payloads.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is explicit container tag classification only. Full YAML set/omap
	  semantics, duplicate-key handling, implicit type inference, quoted scalar
	  escape decoding, complete YAML merge precedence, full YAML tag semantics, and
	  complex YAML typing remain future work; bounded recursive alias/merge chains
	  are implemented through M222.

### M210: Conservative YAML Timestamp And Binary Tag Kinds

Status: complete

Acceptance criteria:

- Markdown frontmatter scalar kind inference honors explicit YAML core
  `timestamp` and `binary` tags.
- Core tag overrides work for short tags such as `!!timestamp` / `!!binary` and
  URI tags such as `!<tag:yaml.org,2002:timestamp>`.
- Payload spans continue to exclude leading YAML tag/anchor decorations, so
  policy labels apply to selected timestamp/binary payload bytes rather than
  tag tokens.
- The parser must remain conservative and must not claim full timestamp
  grammar, base64 validation, or full YAML tag semantics.

Implemented:

- Extended `markdown_frontmatter_tag_kind(...)` to classify YAML core
  `timestamp` tags as kind `timestamp` and YAML core `binary` tags as kind
  `binary`.
- Added regression coverage for top-level `!!timestamp` / `!!binary` fields
  and inline sequence items with URI timestamp and short binary tags.
- Added policy coverage proving a deny label on a URI-tagged timestamp payload
  blocks only that payload while a sibling binary payload remains readable.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This is explicit-tag kind classification only. Conservative implicit
	  timestamp inference is implemented through M212. Full timestamp grammar,
	  binary payload validation/decoding, full set/omap semantics, quoted scalar
	  escape decoding, complete YAML merge precedence, full YAML tag semantics, and
	  complex YAML typing remain future work; bounded recursive alias/merge chains
	  are implemented through M222.

### M200: Conservative YAML Core Tag Kind Overrides

Status: complete

Acceptance criteria:

- Markdown frontmatter scalar kind inference honors explicit YAML core scalar
  tags for `str`, `int`, `float`, `bool`, and `null`; M210 extends the same
  path to explicit `timestamp` and `binary`.
- Core tag overrides work for short tags such as `!!str`, local shorthand such
  as `!str`, and URI tags such as `!<tag:yaml.org,2002:str>`.
- Payload spans continue to exclude leading YAML tag/anchor decorations, so
  policy labels apply to the selected payload bytes rather than the decoration
  token.
- Custom tags such as `!pii` remain decoration-only unless they are recognized
  YAML core scalar tags.

Implemented:

- Added `markdown_frontmatter_scalar_kind(...)` to combine decoration-aware
  core tag kind overrides with existing payload-based kind inference.
- Added conservative tag-token classification for YAML core `str`, `int`,
  `float`, `bool`, and `null` tags, including the `tag:yaml.org,2002:*` URI
  spelling.
- Applied scalar kind overrides to top-level frontmatter scalars, inline
  sequence scalar items, and inline mapping scalar fields.
- Added regression coverage for top-level `!!str` / `!!null` values and inline
  sequence items with `!!str`, `!!int`, `!!float`, `!!bool`, `!!null`, and
  `!<tag:yaml.org,2002:str>` tags. M210 adds timestamp/binary coverage.
- Added policy coverage proving a deny label on a URI-tagged scalar payload
  blocks only that typed payload while a sibling typed scalar remains readable.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- M210 adds timestamp/binary coverage and M211 adds set/omap explicit container
  tag coverage. M213 adds conservative quoted scalar payload spans and M227 adds
  decoded quoted scalar semantic values. Complete YAML merge precedence, full
  YAML tag semantics, and complex YAML typing remain future work; bounded
  recursive alias/merge chains are implemented through M222.

### M222: Conservative Recursive YAML Alias And Merge Chain Expansion

Status: complete

Acceptance criteria:

- Markdown frontmatter alias expansion can derive child semantic paths through a
  bounded alias chain such as `alias_template_deep: *template_alias` where
  `template_alias` anchors an alias to an inline object template.
- Markdown frontmatter merge expansion can derive child semantic paths when the
  merge alias points at an anchored alias chain.
- Scalar alias target paths can resolve through a bounded alias-to-alias chain
  such as `scalar_alias_deep.__target`.
- Expanded alias/merge child paths still use the alias token byte range rather
  than inventing expanded bytes, preserving fragment-policy precision.
- Cycles and unsupported full-YAML semantics remain conservative non-claims.

Implemented:

- Made `append_markdown_alias_expansion_spans(...)` fixed-point so newly derived
  alias child paths can serve as sources for later alias references.
- Made `append_markdown_merge_expansion_spans(...)` fixed-point so merge
  references can consume alias-derived anchored paths.
- Added recursive scalar alias target resolution with cycle protection.
- Added regression coverage for `frontmatter.alias_template_chain.email`,
  `frontmatter.alias_template_deep.email`,
  `frontmatter.merged_template_chain.email`, and
  `frontmatter.scalar_alias_deep.__target`.
- Updated README, PRD, design, kernel contract, conformance, progress,
  requirements matrix, and complexity audit docs.

Remaining:

- This remains a conservative frontmatter extractor, not a full YAML parser.
  Unbounded/cyclic recursive semantics, complete YAML merge precedence, full YAML
  tag semantics, and complex YAML typing remain future work; decoded quoted
  scalar semantic values are implemented through M227.

### M230: POSIX-Like Cp Mv Destination Directory Semantics

Status: complete

Acceptance criteria:

- `cp(src_file, dst_dir, ...)` treats an active destination directory as a
  container and writes `dst_dir/<source-basename>`.
- `mv(src_file, dst_dir, ...)` treats an active destination directory as a
  container, writes `dst_dir/<source-basename>`, and deletes the original source
  ref.
- `cp(src_dir, dst_dir, ...)` recursively copies the source directory tree under
  `dst_dir/<source-basename>` when `dst_dir` already exists.
- `mv(src_dir, dst_dir, ...)` recursively moves the source directory tree under
  `dst_dir/<source-basename>` when `dst_dir` already exists.
- Existing ref/event audit, immutable-node identity, path lease checks,
  sticky-directory replacement checks, and integrity verification remain
  unchanged.

Implemented:

- Added destination-directory path resolution to `Workspace.cp(...)` and
  `Workspace.mv(...)` before existing file/subtree copy or move execution.
- Added a helper that recognizes explicit directory marker refs and implicit
  active directory subtrees as destination directories.
- Preserved existing behavior for non-directory destinations.
- Added regression coverage for file copy/move into existing directories and
  recursive directory copy/move into existing directories.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Directory moves still eagerly rewrite affected workspace refs for auditability
  rather than using a lazy namespace-move representation. Full POSIX
  `rename(2)`, descriptor lifetime, ACL, mmap, and FUSE syscall behavior remain
  outside the compact kernel.

### M229: Normalized JSON Array And Object Attribution

Status: complete

Acceptance criteria:

- `auto_propagate_fragment_policy_labels_by_normalized_json(...)` propagates a
  labeled JSON array when its minified JSON value appears exactly once in the
  output node.
- It propagates a labeled JSON object when its minified JSON value appears
  exactly once in the output node.
- Existing ambiguity protection remains: repeated normalized output matches are
  skipped rather than guessed.
- Existing exact-byte attribution remains exact-only and does not silently
  propagate pretty JSON array/object spans to minified output spans.
- Propagated fragment-label audit rows retain source node id and source range
  metadata, and existing fragment visibility rules block the mapped output span.

Implemented:

- Added
  `auto_propagate_fragment_policy_labels_by_normalized_json(source_node_id,
  output_node_id, agent_id, run_id=None, tool_call_id=None)`.
- Added deterministic JSON value projection for labeled JSON arrays and objects
  by parsing the source fragment and matching the minified JSON representation
  in the output node.
- Kept the normalized scalar API scoped to scalars while sharing conservative
  unique-output-match propagation logic with the JSON value API.
- Added regression coverage proving exact attribution skips pretty-to-minified
  JSON values, normalized JSON value attribution propagates array/object spans,
  duplicate array output matches are skipped, output fragment offsets are
  correct, and fragment visibility enforcement blocks the mapped object span.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is deterministic JSON projection, not canonical JSON across every
  external serializer. Locale-aware or prose-aware rendering, semantic
  entailment, paraphrase attribution, model-backed attribution, and broad
  cross-format extraction remain future work.

### M228: Normalized JSON Bool Null And Finite Number Attribution

Status: complete

Acceptance criteria:

- `auto_propagate_fragment_policy_labels_by_normalized_scalar(...)` propagates a
  labeled JSON boolean scalar when the normalized text `true` or `false` appears
  exactly once in the output node.
- It propagates a labeled JSON null scalar when `null` appears exactly once.
- It propagates a labeled finite non-integer JSON number such as `0.75` when the
  normalized finite number text appears exactly once.
- Existing ambiguity protection remains: repeated normalized output matches are
  skipped rather than guessed.
- Propagated fragment-label audit rows retain source node id and source range
  metadata, and existing fragment visibility rules block the mapped output span.

Implemented:

- Extended `normalized_json_scalar_bytes(...)` from JSON strings and
  integer-valued numbers to JSON booleans, nulls, and all finite JSON numbers.
- Kept signed/unsigned integer normalization behavior unchanged.
- Added regression coverage for bool/null/float propagation, duplicate bool
  ambiguity skipping, output fragment offset checks, and visibility denial on a
  propagated finite-number span.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- M229 adds JSON array/object value attribution. Locale-aware or prose-aware
  number rendering, semantic entailment, paraphrase attribution, model-backed
  attribution, and broad cross-format extraction remain future work.

### M227: Markdown Frontmatter Quoted Scalar Semantic Values

Status: complete

Acceptance criteria:

- `AnfsEngine.markdown_field_values(node_id)` returns `(field_path, value, kind)`
  rows over the same conservative frontmatter paths as `markdown_field_spans`.
- Raw `markdown_field_spans(...)` offsets and lengths remain unchanged so fragment
  policy labels still point at source bytes.
- Single-quoted YAML scalar values decode doubled apostrophes such as
  `'Ada''s'`.
- Double-quoted YAML scalar values decode common escapes including `\n`, `\"`,
  `\\`, `\xNN`, `\uNNNN`, and `\UNNNNNNNN`.
- Inline mapping and sequence parsing remain quote-aware when escaped quotes are
  followed by commas or colons inside quoted scalar values.
- This is semantic scalar value projection only, not full YAML parsing,
  normalization, complete tag semantics, or complex type inference.

Implemented:

- Added `MarkdownFieldValueRow` and `markdown_field_values(...)`.
- Added conservative single-quoted and double-quoted scalar decoders over already
  identified frontmatter spans.
- Made delimiter scanning skip escaped characters inside double-quoted YAML
  scalars so inline objects/sequences do not split on quoted commas.
- Added regression coverage for decoded newline/unicode escapes, escaped quotes
  inside inline mapping and sequence values, single-quoted doubled apostrophes,
  and unchanged raw source spans.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- Full YAML parsing, semantic scalar normalization beyond quoted escape decoding,
  complete YAML merge precedence, full tag semantics, duplicate-key handling, and
  complex YAML typing remain future work.

### M226: Exclusive ANFS Path Leases

Status: complete

Acceptance criteria:

- `Workspace.acquire_lock(path, ttl_ms=None, tool_call_id=None)` records an
  audited exclusive path lease owned by the current workspace agent/run and
  returns a `lock:*` token.
- `Workspace.release_lock(path, lock_id, tool_call_id=None)` releases only the
  owning active lease, and `Workspace.lock_info(path="")` lists active
  non-expired leases.
- Active leases block other agents/runs from mutating the locked path, child
  paths covered by a locked directory, or a parent directory operation that would
  cover a locked descendant.
- Lease enforcement applies to kernel mutation helpers for writes, answer writes,
  mkdir/touch, copy/link, recursive move/delete, and direct ref deletion paths.
- The feature remains an ANFS coordination primitive, not POSIX descriptor-level
  `fcntl`/`flock`, open-handle, mmap, ACL, process, or FUSE syscall semantics.

Implemented:

- Added mutable `workspace_path_locks` rows with immutable
  `acquire_lock`/`release_lock` audit events.
- Added lock ID generation, TTL cleanup, owner/run matching, ancestor/descendant
  conflict checks, and Python API methods.
- Added regression coverage proving another agent is blocked by a file lock, the
  owning agent can still overwrite, non-owner release is denied, released locks
  stop blocking writes, and directory locks block child creation.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- These are path leases over the ANFS kernel API. Descriptor-level POSIX
  `fcntl`/`flock`, open-handle lifetime semantics, mmap, ACLs, distributed lock
  services, and FUSE syscall integration remain future work.

### M225: POSIX-Like Setgid Directory Metadata Inheritance

Status: complete

Acceptance criteria:

- New regular files created by `write(...)` under a setgid directory inherit the
  parent directory gid metadata.
- Missing files created by `touch(...)` under a setgid directory inherit the
  parent directory gid metadata.
- New subdirectories created by `mkdir(...)` under a setgid directory inherit the
  parent directory gid metadata and the setgid mode bit.
- Inheritance uses existing workspace path metadata overlays and does not add OS
  process credential, ACL, setgid execution, descriptor, or FUSE syscall
  semantics.

Implemented:

- Added `inherit_parent_setgid_metadata(...)` over existing
  `workspace_path_metadata` rows.
- Applied inheritance to newly created content paths in `write_content_in_tx(...)`
  and to directory marker creation in both `mkdir_impl(...)` and
  `write_directory_marker_ref(...)`.
- Kept existing overwrite behavior from changing owner/gid metadata by applying
  inheritance only when the destination path is newly created or was previously
  deleted/archived.
- Added POSIX facade regression coverage for `write`, `touch`, and `mkdir` under
  a setgid parent directory.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is metadata inheritance only. Setuid/setgid execution semantics, ACLs,
  process credential impersonation, OS/container sandboxing, file descriptors,
  descriptor-level POSIX locks, mmap, and FUSE syscall integration remain future
  work.

### M224: POSIX-Like Sticky Directory Mutation Checks

Status: complete

Acceptance criteria:

- `delete(...)`, `rm(...)`, and `mv(...)` remain backward compatible when callers
  do not pass an effective identity.
- `delete(path, ..., uid, gid, groups)` and `rm(...)` reject mutation of an entry
  in a sticky directory when the effective uid is not `0`, not the parent
  directory owner, and not the entry owner.
- `mv(src, dst, ..., uid, gid, groups)` applies the same sticky check to source
  removal and to replacement of an existing destination entry in a sticky
  directory.
- The check uses existing path metadata overlays and does not add OS credential,
  descriptor, ACL, or FUSE syscall semantics.

Implemented:

- Extended Python bindings for `delete`, `rm`, and `mv` with optional
  `uid=None`, `gid=None`, and `groups=None` parameters after the existing
  `tool_call_id` parameter.
- Added effective identity validation shared with the sticky mutation checks.
- Added transaction-local sticky-directory checks over existing
  `workspace_path_metadata` mode/uid overlays.
- Kept worktree commit-back on the previous default path by not passing an
  effective identity into internal deletes.
- Added regression coverage for denied non-owner deletion, allowed entry-owner
  deletion, denied non-owner replacement, allowed parent-owner replacement, and
  clean integrity.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is explicit-identity sticky mutation checking only. Setuid/setgid
  execution semantics, ACLs, process credential impersonation, OS/container
  sandboxing, file descriptors, descriptor-level POSIX locks, mmap, and FUSE syscall integration remain
  future work.

### M223: POSIX Special Mode Bit Preservation

Status: complete

Acceptance criteria:

- `chmod(path, mode)` accepts POSIX special mode bits within the existing
  `0o0000..0o7777` mask.
- `stat_posix(...)` returns setuid, setgid, and sticky mode bits together with
  the file or directory type bits.
- `access(...)` continues to evaluate only owner/group/other rwx permission bits;
  special mode bits must not grant read, write, or execute access by themselves.
- The update remains an audited metadata overlay and does not claim OS execution,
  ACL, descriptor, or kernel syscall semantics.

Implemented:

- Confirmed the existing chmod overlay stores mode masks through `0o7777` and
  `stat_posix(...)` combines those bits with the POSIX-like file type bits.
- Added regression coverage for file setuid/setgid/sticky bit preservation via
  `stat_posix(...)`, chmod audit payloads, and unchanged effective-id access
  behavior.
- Added regression coverage for directory setgid/sticky bit preservation through
  `stat_posix(...)`.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is special-bit metadata preservation only. M224 adds explicit-identity
  sticky-directory mutation checks, but setuid/setgid execution semantics, ACLs,
  process credential impersonation, OS/container sandboxing, file descriptors,
  descriptor-level POSIX locks, mmap, and FUSE syscall integration remain future work.

### M221: POSIX-Like Root Effective Access Special Case

Status: complete

Acceptance criteria:

- `access(path, mode, uid=0, gid=...)` treats effective uid `0` as root-style
  read/write mode-bit bypass for existing mutable workspace paths.
- Root-style `X_OK` succeeds only when at least one execute bit is set.
- Fragment policy and workspace readability still block root-style read access.
- The behavior remains a derived adapter predicate, not OS process
  impersonation, ACL evaluation, or container sandboxing.

Implemented:

- Updated `posix_access_permission_bits(...)` so effective uid `0` returns
  read/write bits and only returns execute when any owner/group/other execute
  bit is present.
- Added regression coverage for root-style read/write over mode `000`, denied
  root-style execute without execute bits, allowed root-style execute after an
  execute bit is set, and fragment-policy read denial for root-style access.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This is a compact root-style access predicate only. M223 preserves
  setuid/setgid/sticky metadata bits, but ACLs, special-bit OS execution
  semantics, process credential impersonation, OS/container sandboxing, file
  descriptors, descriptor-level POSIX locks, mmap, and FUSE syscall integration remain future work.

### M220: POSIX-Like Effective Identity Access Checks

Status: complete

Acceptance criteria:

- `access(path, mode)` remains backward compatible for callers that do not pass
  an effective identity.
- `access(path, mode, uid, gid, groups=None)` supports non-negative effective
  uid/gid and supplementary group ids.
- When effective ids are supplied, access uses owner/group/other mode bits from
  `stat_posix(...)` uid/gid/mode fields.
- Read access still reuses existing path readability and active fragment policy
  gates before mode-bit success.
- Invalid masks, negative ids/groups, or supplying only one of uid/gid are
  rejected.
- The behavior remains a derived adapter predicate, not OS process
  impersonation, ACL evaluation, or a container sandbox.

Implemented:

- Extended the Python binding signature to
  `Workspace.access(logical_path, mode, uid=None, gid=None, groups=None)`.
- Added `posix_access_permission_bits(...)` to select owner/group/other
  permissions from `stat_posix(...)` metadata.
- Preserved current no-identity access behavior by continuing to use owner mode
  bits when uid/gid are omitted.
- Added POSIX facade regression coverage for owner access, effective group
  access, supplementary-group access, denied group write/other read, negative
  ids, missing gid, and negative supplementary groups.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This is effective-id mode-bit selection only. M221 adds a compact root-style
  access special case and M223 preserves setuid/setgid/sticky metadata bits, but
  ACLs, OS/container sandboxing, special-bit OS execution semantics, process
  credential impersonation, file descriptors, descriptor-level POSIX locks, mmap, and FUSE syscall
  integration remain future work.

### M219: Linked Immutable Node Metadata Overlay Synchronization

Status: complete

Acceptance criteria:

- A new `link(...)` destination path inherits existing source path metadata
  overlays when the source is a workspace path.
- `chmod(...)`, `chown(...)`, and `utime(...)` on a file path synchronize the
  corresponding metadata overlay across all active workspace file paths that
  still point at the same immutable node.
- Directory metadata remains path-level.
- Later writes to one linked path create a new immutable node and stop metadata
  synchronization with the old linked set.
- Audit payloads expose the affected logical paths for linked metadata updates.

Implemented:

- Added `linked_metadata_paths(...)` to find active same-node file paths in the
  current workspace.
- Updated chmod/chown/utime metadata writes to upsert overlays for every active
  linked path that still shares the same immutable node.
- Added `copy_workspace_metadata_overlay(...)` so `link(...)` inherits source
  path mode, uid/gid, and atime/mtime overlays when available.
- Added POSIX facade regression coverage for link metadata inheritance,
  linked-path chmod/chown/utime synchronization, affected-path audit payloads,
  and copy-on-write separation after writing a linked path.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This improves hard-link-like metadata behavior without introducing a mutable
  inode table. M220 and M221 add effective-id and root-style access checks, but
  open-handle link lifetime semantics, kernel dentry/cache behavior, descriptor-level POSIX locks,
  mmap, ACLs, and FUSE syscall integration remain future work.

### M218: POSIX-Like Immutable Node Links

Status: complete

Acceptance criteria:

- Workspace callers can use `link(src_path, dst_path, tool_call_id=None)` to
  create an audited destination workspace path pointing at the same immutable
  file node as the source path.
- `link(...)` rejects directory sources, explicit destination refs, workspace
  root destinations, and already existing destination paths.
- `stat_posix(...)` derives file `nlink` from active workspace refs that point
  at the same immutable node and derives file inode from the shared node
  identity for those refs.
- Later writes to either path create a new immutable node for that path, so the
  linked paths separate without mutating a shared inode.
- Integrity verification checks `link` ref-event linkage against a matching
  output result edge.

Implemented:

- Added `Workspace.link(src_path, dst_path, tool_call_id=None)` to the Python
  binding.
- Implemented `link` as a new workspace ref to the source immutable file node,
  with audited `link` event payload and source/result event edges.
- Updated `stat_posix(...)` file nlink/inode derivation for active workspace
  refs that share the same immutable node.
- Added integrity ref-event linkage validation for `link` events.
- Added POSIX facade regression coverage for link creation, shared nlink/inode,
  copy-on-write separation after writing one linked path, event payload/edges,
  and invalid destination/source rejection.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This is a hard-link-like immutable-node compatibility helper, not full POSIX
  mutable inode sharing. M219 adds linked metadata overlay synchronization, but
  open-handle link lifetime semantics, kernel dentry/cache behavior, descriptor-level POSIX locks,
  mmap, ACLs, and FUSE syscall integration remain future work.

### M217: Conservative YAML Block Sequence Alias And Merge Bookkeeping

Status: complete

Acceptance criteria:

- Block sequence scalar aliases such as `frontmatter.block_aliases[0]` record
  alias references and can expose scalar target paths such as
  `frontmatter.block_aliases[0].__target`.
- Block sequence object-field aliases such as
  `frontmatter.block_alias_field[0].email` record alias references and can
  expose scalar target paths.
- Block sequence inline mapping anchors such as
  `- &block_template {email: block-template@example.com, label: inherited}`
  can be referenced by sibling merge keys.
- Dash item inline mappings are not misparsed as shorthand `key: value` items
  solely because their inline container payload contains colons.
- The parser remains conservative and does not claim full YAML recursive
  alias/merge expansion or full merge precedence.

Implemented:

- Added shared frontmatter value-reference bookkeeping for anchors, aliases,
  and merge references.
- Applied that bookkeeping to block sequence scalar items and block sequence
  object-field values.
- Guarded dash-item shorthand parsing so inline mappings and sequences remain
  container payloads instead of being split at inner colons.
- Added regression coverage for block sequence scalar alias targets, block
  sequence field alias targets, sibling block sequence merge expansion, and
  explicit field precedence in a merged block sequence item.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is block-sequence reference bookkeeping only. Bounded recursive alias/merge
  chain expansion is implemented through M222, but nested merge arrays beyond
  conservative refs, complete YAML merge precedence, full YAML parsing, full YAML
  tag semantics, and complex YAML typing remain future work. Decoded quoted
  scalar semantic values are implemented through M227.

### M216: Audited POSIX Chown Ownership Overlay

Status: complete

Acceptance criteria:

- Workspace callers can use `chown(path, uid, gid, tool_call_id=None)` on
  logical workspace paths.
- `chown(...)` rejects explicit non-workspace refs, unsupported negative uid/gid
  values, and no-op `(-1, -1)` updates.
- `-1` preserves the existing uid or gid, matching the useful POSIX convention
  without requiring OS identity changes.
- The uid/gid overlay affects `stat_posix(...)` uid/gid fields and updates
  ctime for the metadata change.
- The update is audited with a `chown` event and links the target node for file
  paths without creating a new content node or duplicating file bytes.
- The implementation remains a compatibility metadata overlay. It must not make
  `access(...)` impersonate OS users/groups or claim ACL, file-descriptor, or
  kernel syscall fidelity.

Implemented:

- Added `Workspace.chown(logical_path, uid, gid, tool_call_id=None)` to the
  Python binding.
- Reused the existing `workspace_path_metadata` uid/gid columns so ownership is
  a path metadata overlay, not a new content state.
- Updated ownership through `stat_posix(...)` while preserving chmod mode
  overlays and utime timestamp overlays across later metadata updates.
- Added POSIX facade regression coverage for file chown, directory chown,
  `-1` preservation, invalid uid/gid rejection, no-op rejection, explicit-ref
  rejection, audit payloads, event edges, and overlay preservation.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a path-level uid/gid overlay only. ACLs, OS user/group impersonation,
  file descriptors, descriptor-level POSIX locks, mmap, kernel dentry/cache behavior, and FUSE syscall
  integration remain future work.

### M215: Audited POSIX Utime Timestamp Overlay

Status: complete

Acceptance criteria:

- Workspace callers can use `utime(path, atime_ms=None, mtime_ms=None,
  tool_call_id=None)` on logical workspace paths.
- `utime(...)` rejects negative timestamps and explicit non-workspace refs.
- When both timestamp arguments are omitted, `utime(...)` sets both atime and
  mtime to the current kernel timestamp.
- The timestamp overlay affects `stat_posix(...)` atime/mtime fields and
  updates ctime for the metadata change.
- The update is audited with a `utime` event and links the target node for file
  paths without creating a new content node or duplicating file bytes.
- The implementation remains a compatibility metadata overlay, not full POSIX
  implicit atime-on-read, ACL, file-descriptor, or kernel syscall fidelity.

Implemented:

- Added `atime_ms` to `workspace_path_metadata`, with initialization-time
  compatibility repair for existing metadata tables that predate the column.
- Added `Workspace.utime(logical_path, atime_ms=None, mtime_ms=None,
  tool_call_id=None)` to the Python binding.
- Updated `stat_posix(...)` to use stored path atime/mtime overlays when
  present, while continuing to derive default times from current refs.
- Added POSIX facade regression coverage for explicit file timestamp updates,
  no-argument current-time updates, directory timestamp updates, invalid
  timestamps, explicit-ref rejection, audit payloads, event edges, and
  preservation of timestamp overlays across later chmod calls.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a path-level timestamp overlay only. Implicit atime-on-read behavior,
  ACLs, OS user/group impersonation, file descriptors, descriptor-level POSIX locks, mmap, kernel
  dentry/cache behavior, and FUSE syscall integration remain future work.

### M214: Audited POSIX Chmod Mode Overlay

Status: complete

Acceptance criteria:

- Workspace callers can use `chmod(path, mode, tool_call_id=None)` on logical
  workspace paths.
- `chmod(...)` rejects unsupported mode bits and explicit non-workspace refs.
- The mode overlay affects `stat_posix(...)` mode bits and `access(...)`
  execute/read/write predicates.
- The update is audited with a `chmod` event and links the target node for file
  paths without creating a new content node or duplicating file bytes.
- The implementation remains a compatibility metadata overlay, not a full POSIX
  ACL, file-descriptor, or kernel sandbox model.

Implemented:

- Added `workspace_path_metadata` as a workspace/logical-path metadata overlay
  table with nullable mode/uid/gid/atime/mtime/ctime fields for the adapter
  surface.
- Added `Workspace.chmod(logical_path, mode, tool_call_id=None)` to the Python
  binding.
- Updated `stat_posix(...)` to combine existing ref/path/node facts with any
  stored path mode overlay.
- Updated `access(...)` indirectly through `stat_posix(...)`, so chmod mode
  changes affect `R_OK`/`W_OK`/`X_OK` checks while read visibility policy still
  applies.
- Added POSIX facade regression coverage for file chmod, directory chmod,
  invalid modes, explicit-ref rejection, audit payloads, and event edges.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a path-level mode overlay only. M220 adds effective-id mode-bit access
  checks and M223 proves special mode bit preservation, but ACLs, OS user/group
  impersonation, special-bit OS execution semantics, file descriptors, locks,
  mmap, kernel dentry/cache behavior, and FUSE syscall integration remain future
  work.

### M209: Derived POSIX Access Predicate

Status: complete

Acceptance criteria:

- Workspace callers can use `access(path, mode)` with an `os.access`-style mask
  for `F_OK`, `R_OK`, `W_OK`, and `X_OK`.
- Missing paths return `False`, while unsupported mode bits are rejected.
- Read checks must honor existing path readability and active fragment
  visibility policy.
- Write and execute checks must be derived from current workspace mutability and
  POSIX mode bits, including M214 chmod overlays when present.
- The API must be a predicate only; it must not write audit events or become an
  OS sandbox.

Implemented:

- Added `Workspace.access(logical_path, mode)` to the Python binding.
- Implemented access checks over `stat_posix(...)`, existing path readability,
  active fragment policy, workspace logical path mutability, and synthetic mode
  bits. M214 adds path-level chmod mode overlays to the same stat path.
- Added regression coverage for file and directory access masks, missing paths,
  invalid mode masks, and read access denial under fragment visibility policy.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is a derived adapter predicate, not a full POSIX permission model. M220
  adds effective-id owner/group/other mode-bit selection and M221 adds compact
  root-style access handling, but ACLs, OS/container sandboxing, process
  credential impersonation, and FUSE syscall-level access integration remain
  future work.

### M208: Derived POSIX Stat Adapter View

Status: complete

Acceptance criteria:

- Workspace callers can use `stat_posix(path)` to get a POSIX-like stat shape
  for root, directory, and file paths.
- The returned fields must include the existing `stat(...)` fields plus
  synthetic mode, nlink, uid, gid, inode, atime, mtime, and ctime values.
- The view must be derived from existing refs, paths, timestamps, and path
  metadata overlays rather than adding a second file-content state.
- Missing paths must continue to follow existing `stat(...)` error semantics.

Implemented:

- Added `Workspace.stat_posix(logical_path="")` to the Python binding.
- Implemented synthetic mode bits for regular files and directories, synthetic
  link counts, uid/gid placeholders, deterministic path/ref-derived inode
  values, timestamp fields derived from current ref update timestamps, and M214
  path-level chmod mode overlays when present.
- Added regression coverage for root, directory, and file `stat_posix(...)`
  fields in the POSIX facade test.
- Updated README, PRD, kernel contract, conformance, progress, and requirements
  matrix docs.

Remaining:

- This is an adapter metadata view, not full POSIX metadata fidelity. Implicit
  atime-on-read behavior, full mutable-inode metadata sharing, kernel
  dentry/cache behavior, and FUSE syscall integration remain future work. M218
  adds immutable-node link count and inode derivation for active workspace refs
  that share the same node.

### M207: POSIX-Like Write Range Zero-Fill Extension

Status: complete

Acceptance criteria:

- Workspace callers can use `write_range(path, offset, content)` when `offset`
  is beyond EOF and receive ordinary file-like zero-filled gap bytes.
- The implementation must not introduce sparse-hole metadata, mutable block
  state, or a second canonical file representation.
- Successful zero-fill extension must create a new immutable node, preserve
  lineage from the previous node, and record an audited `write_range` event.
- The audit payload must expose the requested offset, write length, old length,
  new length, and zero-filled gap length.

Implemented:

- Updated `write_path_range_impl(...)` so offsets beyond EOF resize the new
  byte vector with NUL bytes before appending caller content.
- Added checked arithmetic for large offsets and range lengths.
- Added `zero_fill_length` to `write_range` event payloads.
- Updated regression coverage from sparse-write rejection to zero-fill content,
  payload, edge, and ref-history checks.
- Updated README, PRD, design/kernel docs, conformance, progress, and
  requirements matrix docs.

Remaining:

- This provides POSIX-like read semantics for holes by materializing NUL bytes
  in the new immutable blob. It is not physical sparse-file storage, block delta
  storage, or full FUSE pwrite fidelity.

### M206: Checked Worktree Commit Busy Retry

Status: complete

Acceptance criteria:

- Concurrent checked worktree commits from the same materialized base must not
  expose transient SQLite busy lock errors as user-visible operation failures.
- At most one checked commit from the stale base may advance the workspace refs.
- A losing checked commit must retry after the winner commits, re-read current
  workspace refs, and fail through the manifest-lease stale-base path.
- The proof must collect multiprocessing worker results without relying on
  `multiprocessing.Queue.empty()`, which is not a reliable synchronization
  primitive.

Implemented:

- Wrapped `commit_worktree(...)` in the shared `with_sqlite_busy_retry(...)`
  helper so the whole scan/current-ref/manifest-check/write/event transaction
  retries on SQLite busy errors.
- Kept the retry outside canonical model changes: the commit still writes the
  same immutable nodes, ref audit rows, typed worktree edges, and grouped
  `worktree_commit` event.
- Hardened multiprocessing result collection in the conformance tests by
  reading expected result counts with timeouts and draining failure queues with
  `get_nowait()` instead of `Queue.empty()`.
- Verified the full demo after the fix.

Remaining:

- This proves the local SQLite checked-commit contention path, not a long soak
  or release-machine swarm. Larger contention sweeps remain future evidence.

### M205: POSIX-Like Truncate Compatibility

Status: complete

Acceptance criteria:

- Workspace callers can use `truncate(path, length)` as an existing-file
  truncate compatibility helper.
- Truncate rejects directory refs, explicit non-workspace refs, missing paths,
  and negative lengths.
- Truncate supports shrinking existing files and extending them with NUL bytes
  without introducing sparse-hole or mutable block state.
- A successful truncate creates a new immutable node, updates the workspace ref,
  and records lineage from the previous node.
- A successful truncate writes an audited `truncate` event with path, ref, old
  node, old length, new length, and typed input/output edges.
- Truncating to the existing length returns the current node without adding
  a no-op content event; explicit timestamp edits are handled by M215
  `utime(...)`.

Implemented:

- Added `Workspace.truncate(logical_path, length, tool_call_id=None)` to the
  Python binding.
- Implemented `truncate_path_impl(...)` over the shared workspace write helper
  so truncate uses the same CAS blob/node creation, FTS indexing, policy
  propagation, event edge insertion, and workspace ref update path as regular
  writes.
- Added regression coverage for same-length no-op behavior, shrinking,
  NUL-extension, missing-path rejection, directory rejection, negative-length
  rejection, audited `truncate` payload/edge shape, ref history, and clean
  integrity.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is POSIX-like truncate compatibility over immutable CAS nodes, not full
  POSIX/FUSE truncate fidelity. Sparse holes, file descriptors, mmap, locks,
  mode/mtime updates, and block-level delta storage remain future work or
  adapter-specific concerns.

### M201: POSIX-Like Path Range Reads

Status: complete

Acceptance criteria:

- Workspace callers can use `read_range(path, offset, length)` as a path-level
  `pread`-style compatibility helper without first resolving a node id.
- Range reads reject directory refs, preserve existing workspace draft and
  published/approved ref readability rules, and return empty bytes for ranges
  beyond EOF.
- Visibility fragment labels block only overlapping range reads, so an
  unrelated range in the same node remains readable.
- Explicit-purpose range reads preserve purpose capability checks and block only
  overlapping fragment labels for fragment-scoped purpose policy.
- Successful range reads write audited `read_range` events with path, ref, node,
  offset, requested length, actual length, purpose, and a typed input edge.

Implemented:

- Added `Workspace.read_range(logical_path, offset, length, purpose=None,
  tool_call_id=None)` to the Python binding.
- Implemented `read_path_range_impl(...)` over existing path/ref resolution,
  `read_node_range(...)`, and the append-only event audit path.
- Added `active_purpose_policy_blocks_ref_node_range(...)` so purpose fragment
  policy can evaluate only active fragment labels overlapping the requested
  byte range.
- Added regression coverage for path-level partial reads, EOF reads,
  `read_range` audit payload/edge shape, visibility fragment overlap blocking,
  and purpose fragment overlap blocking.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is POSIX-like path compatibility, not full POSIX/FUSE fidelity. Kernel
  mount behavior, file descriptors, mmap, locks, ACLs, uid/gid-based access
  decisions, full mutable hard-link semantics, streaming reads, and lazy
  namespace operations remain future work or adapter work outside canonical
  kernel state. M218 adds immutable-node link refs.

### M202: POSIX-Like Path Range Writes

Status: complete

Acceptance criteria:

- Workspace callers can use `write_range(path, offset, content)` as a
  path-level `pwrite`-style compatibility helper without introducing mutable
  block state.
- Range writes reject directory refs, explicit non-workspace refs, and negative
  offsets.
- Range writes support replacement within the existing file, append when the
  offset equals EOF, and zero-fill extension when the offset is beyond EOF.
- A successful range write creates a new immutable node, updates the workspace
  ref, and records lineage from the previous node.
- A successful range write writes an audited `write_range` event with path, ref,
  old node, offset, write length, zero-filled gap length, old length, new
  length, and typed input/output edges.

Implemented:

- Added `Workspace.write_range(logical_path, offset, content,
  tool_call_id=None)` to the Python binding.
- Split workspace write internals so regular `write(...)` and range writes share
  CAS blob/node creation, derived policy propagation, FTS indexing, event edge
  insertion, and workspace ref update behavior.
- Implemented `write_path_range_impl(...)` by reading the previous immutable
  node, constructing replacement/append/zero-fill extension bytes, then writing
  a new immutable node with the previous node as a derived source.
- Added regression coverage for replacement, append, zero-fill extension,
  directory rejection, preserved sibling/original content, `write_range` audit
  payload/edge shape, ref history, and clean integrity.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is POSIX-like write compatibility over immutable CAS nodes, not full
  POSIX/FUSE pwrite fidelity. Physical sparse holes, file descriptors, mmap,
  locks, atomic multi-writer pwrite arbitration beyond existing ref
  transactions, and block-level delta storage remain future work or
  adapter-specific concerns.

### M203: POSIX-Like Append Writes

Status: complete

Acceptance criteria:

- Workspace callers can use `append(path, content)` as an append-at-EOF
  compatibility helper.
- Appending to a missing file creates it; appending to an existing file creates
  a new immutable node with the previous node as lineage.
- Append rejects directory refs and explicit non-workspace refs.
- A successful append writes an audited `append` event with path, ref, old node,
  append length, old length, new length, and typed input/output edges.
- Append behavior is implemented over existing CAS/ref/event semantics, not
  mutable block state.

Implemented:

- Added `Workspace.append(logical_path, content, tool_call_id=None)` to the
  Python binding.
- Implemented `append_path_impl(...)` over the shared workspace write helper so
  append uses the same CAS blob/node creation, FTS indexing, policy propagation,
  event edge insertion, and workspace ref update path as regular writes.
- Added regression coverage for missing-file append creation, appending to an
  existing file, previous-node lineage, audited `append` payload/edge shape,
  directory rejection, ref history, and clean integrity.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is POSIX-like append compatibility, not full POSIX/FUSE append fidelity.
  File descriptors, O_APPEND interleaving guarantees across independent
  handles, locks, streaming writes, and block-level delta storage remain future
  work or adapter-specific concerns.

### M204: POSIX-Like Path Metadata Predicates

Status: complete

Acceptance criteria:

- Workspace callers can use `exists(path)`, `is_file(path)`, and `is_dir(path)`
  as convenience predicates over existing `stat(...)` semantics.
- The workspace root reports as an existing directory.
- Existing file paths report `exists=True`, `is_file=True`, and `is_dir=False`.
- Existing explicit or implicit directory paths report `exists=True`,
  `is_file=False`, and `is_dir=True`.
- Missing or deleted paths return `False` for all three predicates rather than
  raising `RefNotFoundError`.
- The predicates do not add canonical metadata state.

Implemented:

- Added `Workspace.exists(logical_path)`, `Workspace.is_file(logical_path)`, and
  `Workspace.is_dir(logical_path)` to the Python binding.
- Implemented all three over `stat_impl(...)`, converting only
  `AnfsError::RefNotFound` to `false` and preserving other errors.
- Added regression coverage for root, directory, file, missing path, and deleted
  path predicates in the POSIX facade tests.
- Updated README, PRD, design, kernel contract, conformance, progress, and
  requirements matrix docs.

Remaining:

- This is path metadata compatibility over existing ANFS stat semantics.
  `stat_posix(...)` now supplies derived adapter fields through M208, but
  ACLs, implicit atime-on-read behavior, full POSIX credential semantics, inode
  preservation beyond immutable-node links, and kernel dentry/cache behavior
  remain future work or adapter concerns. M220 adds effective-id mode-bit access
  selection.

### M194: POSIX-Like Touch Compatibility

Status: complete

Acceptance criteria:

- Workspace callers can use `touch(path)` to create a missing empty regular
  file through the existing workspace write path.
- Touching an existing regular file returns the current node id without changing
  ref version or appending a no-op write event. Explicit timestamp changes use
  M215 `utime(...)`.
- Touching a directory path is rejected.
- The behavior is covered by the POSIX facade regression and does not add new
  canonical tables or FUSE-specific state.

Implemented:

- Added `Workspace.touch(logical_path, tool_call_id=None)` to the Python
  binding.
- Implemented `touch_impl(...)` as an adapter-level compatibility helper over
  existing ref lookup and `write_impl(..., b"", ...)`.
- Added regression coverage for empty-file creation, stat/read results,
  existing-file no-op behavior, directory rejection, and clean integrity.
- Updated README, PRD, design, kernel contract, requirements matrix, and
  progress docs.

Remaining:

- This is POSIX-like tool compatibility, not full POSIX/FUSE fidelity. ACLs,
  descriptor-level POSIX locks, full mutable hard-link semantics, mmap, and kernel syscall
  behavior remain outside the compact kernel. M215 adds explicit path-level
  timestamp overlays, M218 adds immutable-node links, and M220 adds effective-id
  mode-bit access checks, but `touch(...)` itself remains a no-op on existing
  files.

### M136: SQLite Memory-Store Benchmark Baseline

Status: complete

Acceptance criteria:

- Benchmark callers can run a SQLite memory-store baseline independently with
  `--backend sqlite-memory`.
- `--backend all` includes ANFS, native POSIX + JSONL, and SQLite memory-store
  backends.
- The SQLite baseline exercises the same read/write/grep, merge, approval,
  replay, cache, cache-contention, and large-file accuracy checks where the
  scenario enables them.
- The baseline reports semantic limitations instead of presenting itself as an
  agent-native filesystem.

Implemented:

- Added plain SQLite `files(workspace, path, content)` storage with lightweight
  `audit_events`.
- Added SQLite baseline worker, merge, approval, replay, cache, contention, and
  large-file routines.
- Added `--backend sqlite-memory` to the benchmark CLI and included it in
  `--backend all`.
- Verified small `sqlite-memory` and `all` benchmark runs pass.

Remaining:

- Local RAG JSONL baseline is implemented through M147. External vector JSONL
  sidecar baseline is implemented through M154; managed vector DB service and
  ANN comparisons remain future work.

### M147: Local RAG JSONL Benchmark Baseline

Status: complete

Acceptance criteria:

- Benchmark callers can run a RAG-style retrieval baseline independently with
  `--backend rag-jsonl`.
- `--backend all` includes ANFS, native POSIX + JSONL, local RAG JSONL,
  external vector JSONL sidecar, MarkdownFS, and SQLite memory-store backends.
- The RAG baseline exercises the same read/write/search, merge, approval,
  replay, cache, cache-contention, and large-file checks where the scenario
  enables them.
- The baseline reports semantic limitations instead of presenting itself as an
  agent-native filesystem or vector database.

Implemented:

- Added a local append-only `documents.jsonl` retrieval index with tokenized
  lexical search.
- Added `rag-jsonl` worker, merge, approval, replay, cache, contention, and
  large-file benchmark paths.
- Added `retrieval_search` audit events and retrieval-index row/token counts.
- Added `--backend rag-jsonl` to the benchmark CLI and included it in
  `--backend all`.
- Verified small `rag-jsonl` and `all` benchmark runs pass.

Remaining:

- External vector JSONL sidecar baseline is implemented through M154; managed
  vector DB service and approximate nearest-neighbor benchmark sweeps remain
  future work.

### M148: Local Task-Memory Benchmark Adapter

Status: complete

Acceptance criteria:

- Benchmark callers can run a local LoCoMo-like task-memory workflow.
- Benchmark callers can compare `raw`, `extracted`, and `hybrid` memory modes.
- Synthetic conversation sessions become ANFS memory nodes/refs.
- Extracted memory facts become derived ANFS nodes with lineage to raw session
  nodes when extracted or hybrid mode is enabled.
- Questions retrieve evidence through audited `Workspace.query(...)` events.
- Answers cite retrieved memory refs and expose `answer_evidence_coverage(...)`
  rows.
- The benchmark reports retrieval recall@k, exact match, context bytes, derived
  lineage coverage, answer evidence coverage, integrity issues, and backend
  semantic limitations.
- A native files + JSONL comparison runs through the same synthetic questions
  without claiming ANFS-only citation/integrity semantics.

Implemented:

- Added `benchmarks/task_memory_benchmark.py` with `--backend anfs`,
  `--backend native-jsonl`, `--backend all`, and
  `--memory-mode raw|extracted|hybrid|all`.
- Added deterministic synthetic session/question generation with stable memory
  keys for reproducible local runs.
- ANFS path records immutable raw memory nodes, extracted derived fact nodes,
  published refs, audited query events, answer nodes, citation refs,
  retrieval-event coverage, derived lineage links, and integrity verification.
- Native JSONL path scans ordinary files and reports missing immutable CAS,
  typed citation edges, evidence coverage, lineage graph, and kernel integrity
  verification.
- Added `docs/benchmark_snapshots/task_memory_local_snapshot.json` as a local
  evidence snapshot.

Remaining:

- JSON/JSONL LoCoMo-style import and token-F1 evaluation are implemented
  through M153.
- Official real LoCoMo snapshot runs, model-generated answers, model-backed
  memory extraction, and vector DB task-memory comparisons remain future work.

### M153: LoCoMo-Style Dataset Import And Token-F1 Evaluator

Status: complete

Acceptance criteria:

- Task-memory benchmark callers can pass a real dataset-shaped JSON or JSONL
  file instead of only using synthetic sessions.
- Dataset sessions can be represented as raw memory refs.
- Dataset questions can provide one or more accepted answers.
- Evaluation reports token-F1 in addition to exact match.
- Dataset mode supports explicit recall/F1 thresholds without requiring perfect
  exact-match scores by default.

Implemented:

- Added `--dataset-path`, `--min-recall`, and `--min-token-f1` to
  `benchmarks/task_memory_benchmark.py`.
- Added flexible JSON/JSONL import for `sessions` / `conversations` /
  `dialogues` and `questions` / `qa` records.
- Added token normalization, exact-match over multiple accepted answers, and
  token-F1 scoring.
- ANFS dataset runs still use audited `Workspace.query(...)`, answer citation
  events, evidence coverage, and `verify_integrity()`.
- Native JSONL dataset runs use the same imported questions while still
  reporting missing ANFS-only semantics.

Remaining:

- Official LoCoMo dataset snapshots are not checked into this repository.
- Model-generated answer variants and model-backed memory extraction remain
  future work.

### M154: External Vector JSONL Benchmark Baseline

Status: complete

Acceptance criteria:

- Benchmark callers can run an external vector sidecar baseline independently
  with `--backend external-vector-jsonl`.
- `--backend all` includes ANFS, native POSIX + JSONL, local RAG JSONL,
  external vector JSONL sidecar, MarkdownFS, and SQLite memory-store backends.
- The sidecar indexes ordinary file contents outside the filesystem mutation
  primitive.
- Search uses deterministic hashed embeddings and cosine similarity so the
  benchmark has a no-service vector comparison.
- The baseline reports limitations instead of presenting itself as an
  agent-native filesystem, managed vector database service, or ANN index.

Implemented:

- Added deterministic token hashing and normalized vector embedding helpers to
  `benchmarks/agent_memory_benchmark.py`.
- Added append-only `vectors.jsonl` indexing with latest-document selection by
  workspace/path/content hash.
- Added `external-vector-jsonl` worker, merge, approval, replay, cache,
  cache-contention, and large-file benchmark paths.
- Added `vector_search` audit events and vector row/document/dimension counts.
- Added `--backend external-vector-jsonl` to the benchmark CLI and included it
  in `--backend all`.
- Refreshed `docs/benchmark_snapshots/agent_memory_local_snapshot.json` with
  the external vector sidecar backend included.
- Verified small `external-vector-jsonl`, small `all`, and default snapshot
  `all` benchmark runs pass.

Remaining:

- Managed vector database service baselines, approximate nearest-neighbor
  indexes, larger embedding collection sweeps, and task-memory vector DB
  comparisons remain future work.

### M155: Workspace SQLite Busy/Locked Transaction Retry

Status: complete

Acceptance criteria:

- Concurrent workspace writers that hit SQLite `BUSY` or `LOCKED` retry the
  full workspace operation instead of leaking a failed partial event.
- Read APIs that write audit events retry the read/event transaction as a unit.
- POSIX search auditing retries the search event transaction as a unit.
- Concurrent multi-process write/read/grep tests pass without `database is
  locked` worker failures.

Implemented:

- Added `with_sqlite_busy_retry(...)` and `is_sqlite_busy_or_locked(...)` to
  the kernel common helper layer.
- Wrapped `Workspace.write_impl`, `Workspace.read_ref_impl`, and POSIX search
  event recording in operation-level busy/locked retry.
- Expanded the concurrent event-sequence regression test to exercise
  multi-process write/read/grep, not only writes.
- Re-ran the agent-memory ANFS 2-worker/20-file reproduction that previously
  failed and confirmed it passes.
- Refreshed the local `--backend all` agent-memory snapshot after the fix.

Remaining:

- Apply the same operation-level retry pattern to other write-heavy engine
  admin paths if larger contention sweeps expose similar lock behavior.
- Larger contention-level benchmark sweeps remain future work.

### M156: Inline Blob Compaction Admin Workflow

Status: complete

Acceptance criteria:

- Operators can inspect inline blob pressure through `compaction_plan()`.
- Operators can dry-run inline blob compaction without mutating blob storage.
- Operators can explicitly move inline blob bytes to file-backed CAS object
  storage through a narrow admin API.
- Compaction must verify recorded size and SHA-256 hash before moving bytes.
- Compaction must not change node ids, ref history, event history, or CAS
  `hash` / `size` identity.
- Regression coverage proves dry-run behavior, successful compaction,
  continued node readability, clean integrity verification, and rejected
  `hash` / `size` mutation.

Implemented:

- Added `compact_inline_blobs(dry_run=True, limit=None) ->
  (candidate_count, candidate_bytes, compacted, dry_run)`.
- Added inline blob count and byte rows to `compaction_plan()`.
- Split out a `materialize_blob_file(...)` helper so admin compaction can write
  small blobs to CAS object files regardless of the normal inline threshold.
- Narrowed the `blobs_no_update` trigger from “no blob row updates” to “CAS
  identity is immutable”: `hash` and `size` remain protected, while storage
  representation can be updated by verified admin compaction.
- Added regression coverage for dry-run/no-op behavior, one-row bounded
  compaction, storage-kind transition, object bytes, node/ref identity
  preservation, integrity verification, and trigger rejection of `size`
  mutation.

Remaining:

- Full event/ref history archive export exists through M164. Physical event/ref
  archival compaction remains future admin-mode work.
- Larger compaction and VACUUM sequencing benchmarks remain future work.

### M157: Token Cost Profile Adapter

Status: complete

Acceptance criteria:

- Callers can record model-specific prompt overhead and pricing without relying
  on hard-coded provider price tables.
- Profile records are append-only audit facts with immutable rows.
- Current profile selection is derived from event sequence order.
- Answer cost estimates combine existing deterministic answer token accounting
  with the active profile for a named model.
- Cost math uses integer units and avoids floating-point rounding ambiguity.
- Regression coverage proves profile creation, latest-profile selection, cost
  calculation, negative price rejection, missing-profile rejection, row
  immutability, node readability, and clean integrity verification.

Implemented:

- Added `token_cost_profile_events` with immutable triggers.
- Added `set_token_cost_profile(model, input_price_micros_per_million_tokens,
  output_price_micros_per_million_tokens, prompt_overhead_tokens=0, ...)`.
- Added `token_cost_profiles(active_only=True)`.
- Added `answer_cost_estimate(answer_event_id, model) -> (model, estimator,
  input_tokens, output_tokens, total_tokens, input_cost_micros,
  output_cost_micros, total_cost_micros, profile_event_id)`.
- Added integrity diagnostics for token cost profile rows that reference the
  wrong event kind or for `set_token_cost_profile` events without exactly one
  profile row.
- Kept vendor tokenizer behavior and provider price freshness outside the
  compact kernel; callers supply profile values explicitly.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- Exact vendor tokenizer parity, prompt-template-specific tokenization, and
  external provider price synchronization remain adapter-level concerns.
- Semantic entailment and model-backed memory extraction remain future work.

### M158: Kernel Complexity Budget

Status: complete

Acceptance criteria:

- The kernel contract explicitly says when complexity belongs in canonical
  kernel state.
- The contract defines which agent-filesystem capabilities should remain
  projections or adapters.
- Future work has a review rule for avoiding redundant canonical
  representations.

Implemented:

- Added a `Complexity Budget` section to `docs/06_kernel_contract.md`.
- Kept canonical complexity limited to content identity, concurrency
  boundaries, provenance, policy enforcement, integrity, and admin safety.
- Classified provider tokenizers/prices, model generation, managed vector DBs,
  ANN services, FUSE, distributed coordination, benchmark-only formats, and
  task-specific extraction as adapter/projection concerns unless they become
  necessary for replay, policy, conflict detection, durable provenance, or
  integrity.
- Added a design pressure rule: new features default to projection/adapter;
  canonical state is justified only when it must participate in kernel
  invariants, and parallel representations must be rebuildable or avoided.

Remaining:

- Refactor pressure remains in `engine.rs`, `integrity.rs`, and
  `policy_labels.rs`; behavior should stay stable while internal modules are
  split only when reviewability requires it.

### M159: Explicit Schema Migration Plan And Apply

Status: complete

Acceptance criteria:

- Admins can inspect supported schema migration work without mutation.
- Admins can run supported schema migration execution with dry-run by default.
- Missing current migration metadata can be restored explicitly.
- Future schema versions and current-version name mismatches are not
  auto-repaired.
- Engine initialization continues to reject unsupported future schema versions
  before normal schema initialization.

Implemented:

- Added `schema_migration_plan() -> list[(version, name, status, action)]`.
- Added `apply_schema_migrations(dry_run=True) -> (planned, applied, dry_run)`.
- Changed current migration recording to avoid silently overwriting a
  mismatched current schema name.
- Routed schema initialization through the explicit migration apply path for
  the supported v1 schema.
- Added regression coverage for up-to-date plans, dry-run behavior, explicit
  missing-current restoration, name-mismatch rejection, future-version
  rejection, and future-only database pre-initialization rejection.

Remaining:

- Future non-v1 schema upgrades should add ordered migration steps to this
  explicit plan/apply path.

### M160: Agent-Memory Quick Matrix Snapshot

Status: complete

Acceptance criteria:

- A checked-in benchmark artifact covers more than one fixed workflow size.
- The snapshot varies agent count, file count, payload size, search
  cardinality, and large-file size.
- Cache reuse and cache contention checks run in every matrix case.
- The matrix compares ANFS against the existing native JSONL, local RAG JSONL,
  external vector JSONL, MarkdownFS, and SQLite memory-store baselines.
- The documentation records the command, scenario coverage, and concise
  summary so the artifact can be rerun.

Implemented:

- Added `docs/benchmark_snapshots/agent_memory_quick_matrix_snapshot.json`.
- Ran `benchmarks/agent_memory_benchmark.py --backend all --matrix quick`
  with 2-agent baseline, 4-agent scale-up, doubled files per agent, 4 KiB
  payloads, sparse search, and 256 KiB large-file scale-up.
- All six backends passed all six cases.
- Updated `docs/benchmark_snapshots/README.md` with the command and ANFS case
  summary.
- Updated the requirements matrix and design docs to cite the quick matrix
  snapshot as local sweep evidence.

Remaining:

- Full/release-scale matrix snapshots and stronger contention-level controls
  remain future work.

### M161: Same-Key Working-Set Cache Contention Snapshot

Status: complete

Acceptance criteria:

- A checked-in benchmark artifact exercises multiple workers contending for the
  same materialized working-set cache key.
- ANFS proves convergence to one managed cache row for the contended key.
- All workers observe the expected file count and no process failures.
- The run still includes ordinary write/read/search, merge, approval, replay,
  integrity, and large-file checks.

Implemented:

- Added
  `docs/benchmark_snapshots/agent_memory_same_key_contention_snapshot.json`.
- Ran `benchmarks/agent_memory_benchmark.py --backend all` with four cache
  workers, three repeats per worker, and `--cache-shared-key`.
- ANFS produced one miss and eleven hits across twelve same-key attempts,
  with all four workers observing the expected 22 files and one managed cache
  registry row.
- Updated `docs/benchmark_snapshots/README.md`,
  `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Heavier contention stress with more workers, larger workspaces, and repeated
  release-machine snapshots remains future work.

### M162: ANFS Full Local Matrix Snapshot

Status: complete

Acceptance criteria:

- A checked-in ANFS benchmark artifact covers the full local matrix rather than
  only the quick matrix.
- The artifact varies agent count, file count, payload size, search
  cardinality, and large-file size.
- Every case keeps the same invariant checks: read recall, grep recall, merge
  rows, replay rows, approval coverage, event sequence continuity, integrity,
  cache reuse, cache contention, and large-file range/chunk/cache checks.
- Documentation records the command and a concise case summary.

Implemented:

- Added `docs/benchmark_snapshots/agent_memory_anfs_full_matrix_snapshot.json`.
- Ran `benchmarks/agent_memory_benchmark.py --backend anfs --matrix full`
  with 2/4 agents, 16/32 files per agent, 256-byte/4 KiB payloads, dense/sparse
  search, and 128 KiB/256 KiB large-file cases.
- All 18 ANFS cases passed.
- The largest matrix cases covered 4 agents, 32 files per agent, 128 reads,
  128 merge rows, and 301 events.
- Updated `docs/benchmark_snapshots/README.md`,
  `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Release-machine snapshots with heavier parameters remain future work.

### M163: Multi-Backend Full Local Matrix Snapshot

Status: complete

Acceptance criteria:

- A checked-in benchmark artifact runs the full local matrix across all
  supported baseline backends, not only ANFS.
- The artifact preserves each backend's correctness checks and semantic
  limitation reporting.
- Documentation records the command and concise pass-rate summary.

Implemented:

- Added `docs/benchmark_snapshots/agent_memory_full_matrix_snapshot.json`.
- Ran `benchmarks/agent_memory_benchmark.py --backend all --matrix full` over
  the same 18 cases used by the ANFS full local matrix.
- All six backends passed all eighteen cases:
  ANFS, native JSONL, local RAG JSONL, external vector JSONL, MarkdownFS, and
  SQLite memory-store.
- Updated `docs/benchmark_snapshots/README.md`,
  `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Release-machine snapshots with heavier parameters remain future work.

### M164: Full History Archive Export

Status: complete

Acceptance criteria:

- Operators can export all canonical events and ref audit rows through the
  latest event sequence into a portable bundle.
- The archive export includes unrelated branches, event edges, referenced
  nodes, blob metadata, optional CAS object bytes, and the existing bundle
  checksum/signature metadata.
- The export must not mutate canonical rows, ref history, or integrity state.
- Regression coverage proves the history archive is not just the latest
  event's causal subgraph.

Implemented:

- Added `AnfsEngine.export_history_archive(output_dir, include_blobs=True,
  policy_label_excludes=None, signing_key=None, signer_id=None)`.
- Reused the event bundle schema and import path, while adding full-history
  queries for events, event edges, ref events, referenced nodes, and blobs.
- Added policy-label export preflight and optional HMAC-SHA256 signing with the
  same semantics as event/run bundle export.
- Added regression coverage in `tests/test_demo.py` that exports two unrelated
  branches plus an archive event, verifies all source event sequences and ref
  states are present, imports the signed archive bundle, and confirms the source
  database is unchanged and integrity-clean.
- Updated `README.md`, `docs/01_prd.md`, `docs/05_agent_filesystem_design.md`,
  `docs/06_kernel_contract.md`, and `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Physical event/ref archival compaction remains future admin-mode work and
  should require archive bundle verification plus the M166 replay checkpoint
  proof before deleting or compacting canonical history rows.

### M165: Shared-Ref Version Contention Regression

Status: complete

Acceptance criteria:

- Multiple independent `AnfsEngine` handles can race on the same published ref
  with the same stale `expected_version`.
- Exactly one lifecycle update may commit; losing writers must fail as
  `RefConflictError`, not silently overwrite the committed ref state or surface
  SQLite lock errors.
- Successful events keep contiguous `event_sequence` allocation and a single
  ref audit transition.
- Regression coverage verifies integrity after contention.
- A checked-in benchmark artifact exercises the same path over multiple rounds.

Implemented:

- Switched `approve(...)`, `reject_ref(...)`, and `archive_ref(...)` to
  `transaction_with_behavior(TransactionBehavior::Immediate)` so contended
  lifecycle decisions acquire the writer slot before reading `ref_version`.
- Added `_concurrent_stale_approval_worker(...)` to exercise independent
  process/connection contention.
- Added
  `test_concurrent_stale_ref_version_allows_only_one_shared_ref_update()` in
  `tests/test_demo.py`.
- The test races eight review workers approving the same ref at the same
  expected version, asserts one approval and seven conflicts, checks final
  `ref_version`, verifies there is exactly one `approve` event, confirms
  contiguous event sequences, and runs `verify_integrity()`.
- Added `benchmarks/ref_contention_benchmark.py`.
- Added `docs/benchmark_snapshots/ref_contention_local_snapshot.json`: five
  rounds with sixteen workers per round, five approvals, seventy-five
  conflicts, zero errors, contiguous event sequence allocation, and zero
  integrity issues.
- Updated `docs/03_progress_tracker.md`,
  `docs/benchmark_snapshots/README.md`,
  `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Longer-running swarm soak jobs and release-machine contention sweeps remain
  future work.

### M166: Ref-View Replay Checkpoints

Status: complete

Acceptance criteria:

- Operators can record a durable replay checkpoint for the ref view at a target
  event boundary.
- The checkpoint stores target event id, target source sequence, normalized
  prefix, inactive-ref mode, ref count, and deterministic checksum.
- Checkpoint rows are immutable and auditable through a
  `ref_view_checkpoint` event.
- Callers can list checkpoints and verify a checkpoint by recomputing
  `ref_view_at_event(...)`.
- Regression coverage proves valid checkpoints verify, direct checkpoint
  mutation is rejected, and a forged checkpoint row fails verification.

Implemented:

- Added `ref_view_checkpoints` schema table with immutable update/delete
  triggers.
- Added `AnfsEngine.create_ref_view_checkpoint(event_id=None, prefix=None,
  include_inactive=False, agent_id="checkpoint_agent")`.
- Added `AnfsEngine.ref_view_checkpoints()`.
- Added `AnfsEngine.verify_ref_view_checkpoint(checkpoint_id)`.
- Added deterministic replay-view checksum logic in `src/replay.rs`.
- Extended `verify_integrity()` to report missing checkpoint rows, checkpoint
  rows pointing at the wrong event kind, and checkpoint checksum/count
  mismatches.
- Updated `compaction_plan()` guidance so future archival compaction points to
  ref-view checkpoints rather than an undefined future proof.
- Added regression coverage in `tests/test_demo.py`.
- Updated `README.md`, `docs/01_prd.md`, `docs/03_progress_tracker.md`,
  `docs/05_agent_filesystem_design.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Physical event/ref archival compaction remains future admin-mode work. It
  should require both full-history archive verification and a valid ref-view
  checkpoint before deleting or compacting canonical history rows.

### M167: Archival Readiness Plan

Status: complete

Acceptance criteria:

- Operators can verify, without mutation, whether a full-history archive bundle
  and ref-view checkpoint satisfy the preconditions for future physical
  event/ref archival compaction.
- The bundle check must verify bundle authentication when requested, validate
  bundle closure, and prove the bundle covers the current full event history,
  event edges, and ref audit history.
- The checkpoint check must reuse `verify_ref_view_checkpoint(...)`.
- The final readiness row must be false if either proof is stale, invalid, or
  missing.
- Regression coverage proves a valid bundle/checkpoint pair passes and a stale
  bundle fails after another event is appended.

Implemented:

- Added `AnfsEngine.archival_readiness_plan(checkpoint_id, bundle_path,
  signature_key=None, require_signature=False)`.
- Added read-only full-history archive bundle verification in `src/bundle.rs`.
- The readiness plan returns rows for `history_archive_bundle`,
  `ref_view_checkpoint`, and `physical_archival_preconditions`.
- Added regression coverage in `tests/test_demo.py`.
- Updated `README.md`, `docs/01_prd.md`, `docs/03_progress_tracker.md`,
  `docs/05_agent_filesystem_design.md`, `docs/06_kernel_contract.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- Physical event/ref archival compaction remains future admin-mode work. The
  current API proves whether the archive/checkpoint preconditions are met but
  deliberately does not delete or compact canonical rows.

### M139: Chunk Embedding Projection

Status: complete

Acceptance criteria:

- Callers can attach finite, positive-norm embedding vectors to cached chunk
  index rows.
- Chunk embeddings must reference existing `node_chunk_index` rows rather than
  becoming an independent source of chunk truth.
- Callers can read back a stored chunk embedding.
- Callers can search chunk embeddings for current refs by lifecycle state,
  returning ref, node, chunk index, byte range, and cosine score.
- Chunk vector search applies ref/node visibility policy and fragment-range
  visibility policy.
- Integrity verification diagnoses malformed chunk embedding projection rows.

Implemented:

- Added `node_chunk_embeddings` derived projection table.
- Added `set_node_chunk_embedding(node_id, chunk_size, chunk_index, model,
  vector)`.
- Added `node_chunk_embedding(node_id, chunk_size, chunk_index, model)`.
- Added `vector_search_chunks(model, query_vector, state="published",
  limit=10, policy_label_excludes=None)`.
- `rebuild_derived_indexes()` clears chunk embeddings before clearing chunk
  rows, preserving repair semantics for rebuildable derived state.
- Added regression coverage for requiring cached chunk rows, chunk-level vector
  search ordering, fragment-range filtering, ref/node label filtering, and
  corrupt chunk embedding integrity diagnostics.

Remaining:

- Local RAG JSONL baseline is implemented through M147. External vector JSONL
  sidecar baseline is implemented through M154; managed vector DB service and
  ANN comparisons remain future work.

### M137: MarkdownFS Benchmark Baseline

Status: complete

Acceptance criteria:

- Benchmark callers can run a MarkdownFS-style baseline independently with
  `--backend markdownfs`.
- `--backend all` includes ANFS, native POSIX + JSONL, local RAG JSONL,
  external vector JSONL sidecar, MarkdownFS, and SQLite memory-store backends.
- The MarkdownFS baseline stores content as ordinary Markdown files and records
  audit entries as Markdown documents with frontmatter.
- The baseline exercises the same read/write/grep, merge, approval, replay,
  cache, cache-contention, and large-file accuracy checks where the scenario
  enables them.
- The baseline reports semantic limitations instead of presenting itself as an
  agent-native filesystem.

Implemented:

- Added Markdown audit event files under `audit/events/*.md`.
- Added MarkdownFS baseline worker, merge, approval, replay, cache,
  cache-contention, and large-file routines.
- Added `--backend markdownfs` to the benchmark CLI and included it in
  `--backend all`.
- Verified small `markdownfs` and `all` benchmark runs pass.

Remaining:

- Local RAG JSONL baseline is implemented through M147. External vector JSONL
  sidecar baseline is implemented through M154; managed vector DB service and
  ANN comparisons remain future work.

### M115: Working Tree Compatibility MVP

Status: complete

Acceptance criteria:

- Workspaces can be materialized to ordinary directories.
- Ordinary filesystem edits can be scanned and committed back into workspace
  refs.
- Added, modified, deleted, and unchanged files are reported.
- Modified files preserve lineage to their previous workspace node.
- Grouped commit metadata records the whole directory diff.
- Active policy labels can block materialization and commit.
- Merge-to-base remains conflict-checked through `merge_workspace(...)`.

Implemented:

- Added `materialize_workspace(workspace, output_dir, overwrite=False,
  atomic=True, policy_label_excludes=None)`.
- Added `commit_worktree(workspace, input_dir, agent_id, run_id=None,
  tool_call_id=None, delete_missing=True, require_manifest_match=False,
  policy_label_excludes=None)`.
- Added `cache_materialized_workspace(workspace, cache_root, cache_key=None,
  overwrite=False, atomic=True, policy_label_excludes=None)`.
- Added `cached_working_sets(workspace=None)`.
- Materialized worktrees include `.anfs-worktree-manifest.json`.
- The root `.anfs-worktree-manifest.json` path is reserved adapter metadata and
  cannot be materialized or committed as a workspace user file.
- Cached working sets persist manifest hashes and per-file path/ref/node/size
  metadata in SQLite and can be reused when the workspace view is unchanged.
- Commit-back uses existing workspace `write` / `delete` semantics, preserving
  ref audit, event edges, and node lineage.
- Successful commit-back writes a grouped `worktree_commit` event with per-path
  result metadata and typed `worktree_*` edges.
- Current workspace reads, per-file mutations, ref audit rows, event edges, and
  the grouped `worktree_commit` event are written in one SQLite transaction.
- `verify_integrity()` checks grouped worktree commit result, changed, and edge
  counts.
- Added regression coverage in `tests/test_demo.py`, including rollback of all
  file mutations when a later file write fails.

Remaining:

- No FUSE mount yet.

### M120: Cached Materialized Working Set Manager

Status: complete

Acceptance criteria:

- Workspaces can be materialized into managed cache directories.
- Managed caches record cache key, workspace, output directory, manifest hash,
  policy exclusions, file count, timestamp, and per-file metadata.
- Unchanged workspace views reuse an existing cache without rewriting files.
- Stale cache keys are rejected unless `overwrite=True`.
- Integrity verification detects missing cache manifests and materialized file
  byte mismatches.

Implemented:

- Added `materialized_working_sets` and `materialized_working_set_files`
  derived tables.
- Added `cache_materialized_workspace(...)`.
- Added `cached_working_sets(...)`.
- `verify_integrity()` validates cached working-set file counts, output
  manifests, per-file nodes, sizes, paths, and materialized bytes.
- `benchmarks/agent_memory_benchmark.py --cache-repeats N` measures first
  materialization miss and repeated cache hits for a managed working set.
- `benchmarks/agent_memory_benchmark.py --cache-workers N` measures concurrent
  managed working-set materialization with per-worker cache keys.
- `benchmarks/agent_memory_benchmark.py --cache-shared-key` measures
  same-cache-key contention.
- Cache materialization now serializes per cache key with a file lock and
  rechecks cache records before replacing an output directory, so concurrent
  same-key materializers can converge on one managed cache.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- No FUSE mount yet.
- A local cache hit-rate/contention snapshot exists under
  `docs/benchmark_snapshots`; larger working-set sweeps remain future work.

### M116: Selective Node Range Reads And Chunk Maps

Status: complete

Acceptance criteria:

- Callers can read byte ranges from immutable nodes.
- File-backed blobs use seek/read for range reads.
- Inline blobs support the same range API.
- Callers can derive chunk rows with chunk index, offset, size, and sha256.
- Invalid offsets, lengths, and chunk sizes are rejected with typed policy
  errors.

Implemented:

- Added `read_node_range(node_id, offset, length)`.
- Added `node_chunks(node_id, chunk_size=65536)`.
- `benchmarks/agent_memory_benchmark.py --large-file-bytes N` measures large
  file writes and repeated `read_node_range(...)` calls.
- Added regression coverage for inline blobs, file-backed blobs, EOF ranges,
  chunk hashes, and invalid parameters.

Remaining:

- A local 256 KiB large-file snapshot exists under `docs/benchmark_snapshots`;
  an ANFS-only 1 MiB large-file snapshot exists through M176. Larger
  release-machine file-size/range/chunk sweeps remain future work.

### M119: Persistent Node Chunk Cache

Status: complete

Acceptance criteria:

- Callers can persist chunk rows for an immutable node and chunk size.
- Callers can read persisted chunk rows without recomputing byte ranges.
- Rebuilding the same node/chunk-size cache is idempotent.
- Persisted chunk rows remain derived state, not canonical blob/node state.
- Integrity verification detects stale or corrupted chunk cache rows.

Implemented:

- Added `node_chunk_indexes` and `node_chunk_index` derived tables.
- Added `cache_node_chunks(node_id, chunk_size=65536)`.
- Added `cached_node_chunks(node_id, chunk_size=65536)`.
- `benchmarks/agent_memory_benchmark.py --large-file-bytes N` measures derived
  chunk map creation, chunk-cache build, and cached chunk-row reads.
- `verify_integrity()` checks chunk cache metadata, row counts, contiguous
  offsets, chunk sizes, and sha256 values against canonical node bytes.
- Added regression coverage in `tests/test_demo.py`.

Remaining:

- A local 256 KiB large-file snapshot exists under `docs/benchmark_snapshots`;
  an ANFS-only 1 MiB large-file snapshot exists through M176. Larger
  release-machine file-size/range/chunk sweeps remain future work.

### M176: ANFS 1 MiB Large-File Range And Chunk Snapshot

Status: complete

Acceptance criteria:

- The benchmark evidence must exercise a file-backed ANFS node larger than the
  existing 128 KiB / 256 KiB local matrix cases.
- The run must verify repeated range reads, chunk derivation, persistent
  chunk-cache build, and cached chunk-row readback.
- The same run must keep normal ANFS workflow checks enabled, including
  write/read/search, merge, approval, replay, working-set cache, and
  `verify_integrity()`.
- The snapshot must be checked into `docs/benchmark_snapshots` with the exact
  command and summary.

Implemented:

- Added `docs/benchmark_snapshots/agent_memory_large_file_1m_snapshot.json`.
- Ran an ANFS-only benchmark with `--large-file-bytes 1048576`,
  `--range-read-count 32`, `--range-size 8192`, and `--chunk-size 65536`.
- The snapshot passed with `range_ok=32`, `derived_chunks=16`,
  `cached_chunks=16`, contiguous event sequence, clean replay checks, and
  `integrity_issues=[]`.
- Updated `docs/benchmark_snapshots/README.md`,
  `docs/03_progress_tracker.md`, `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- This is local 1 MiB evidence, not a release-machine sweep over much larger
  files or many-file selective scans.

### M177: ANFS Large-Collection Local Snapshot

Status: complete

Acceptance criteria:

- The benchmark evidence must exercise a larger current-ref collection than the
  existing local full matrix.
- The run must keep concurrent write/read/search, merge, approval, replay,
  working-set cache, cache contention, large-file checks, event sequence, and
  integrity gates enabled.
- The snapshot must be checked into `docs/benchmark_snapshots` with the exact
  command and summary.

Implemented:

- Added `docs/benchmark_snapshots/agent_memory_large_collection_snapshot.json`.
- Ran an ANFS-only benchmark with four agents, sixty-four files per agent,
  sparse search, eight approvals, and two cache-contention workers.
- The snapshot passed with `read_recall=256`, `grep_recall=64`,
  `merge_rows=256`, `replay_rows=260`, `event_count=585`,
  `edge_count=1485`, contiguous event sequence, and `integrity_issues=[]`.
- Updated `docs/benchmark_snapshots/README.md`,
  `docs/03_progress_tracker.md`, `docs/05_agent_filesystem_design.md`, and
  `docs/07_agent_fs_requirements_matrix.md`.

Remaining:

- This is local collection-scale evidence, not a release-machine swarm,
  much-larger corpus, or distributed coordination result.
- Chunk-row embedding projection is implemented through M139.

<!-- Historical recommended answers retained below. -->

## Earlier Recommended Answers

1. Start with `open_workspace`, then implement real checkout in the next pass.
2. Add immutable-table triggers from day one.
3. Use single-node artifacts for the killer demo, but reserve manifest support in
   schema/API.
4. Use FTS5 only for MVP.
5. Add `ref_version` from day one.

## Definition Of Done For MVP Kernel

The MVP kernel is done when:

- the Rust core owns all database mutations;
- Python API is only a semantic facade;
- invalid lineage approval is rejected;
- valid lineage approval succeeds;
- ref changes are auditable;
- immutable data cannot be modified through normal API;
- tests pass repeatedly after reopening the database;
- project docs match the implemented behavior.
