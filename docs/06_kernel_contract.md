# ANFS Kernel Contract

This document defines the stable kernel boundary. ANFS can expose many agent
tools, but the kernel is the small set of canonical state, invariants, and
mutations that every higher-level view must preserve.

## Boundary

The kernel is a local-first Rust + SQLite semantic filesystem core. It owns:

- immutable content-addressed blobs and nodes;
- mutable semantic refs;
- append-only events and typed event edges;
- ref lifecycle audit rows;
- workspace checkout base snapshots;
- durable policy labels;
- schema migrations and integrity verification.

Everything else is a projection or adapter:

- search/query are retrieval projections;
- replay and materialization are time-travel projections;
- bundle import/export is a portability projection;
- answer construction is an audit projection over generated outputs;
- working tree sync is a compatibility adapter for ordinary filesystem tools.

## Complexity Budget

ANFS should stay agent-native without becoming a bundle of every agent tool. The
kernel earns complexity only when the state or invariant cannot be reconstructed
from simpler facts.

Kernel complexity is justified for:

- canonical content identity: blobs, nodes, refs, and event order;
- concurrency boundaries: workspace bases, ref versions, and atomic mutation
  groups;
- provenance: typed event edges, ref audit, lineage, and answer citations;
- policy enforcement: durable labels, fragment labels, purpose gates, and
  shared visibility checks;
- integrity and admin safety: verification, dry-run plans, explicit GC,
  explicit VACUUM, and verified storage representation changes.

Kernel complexity is not justified for:

- provider-specific tokenizers, model price freshness, prompt templates, or
  generation logic;
- managed vector databases, ANN services, embedding model calls, or ranking
  services;
- FUSE or remote/distributed coordination before the worktree compatibility
  path proves the needed semantics;
- benchmark-only data formats or task-specific memory extraction logic;
- automatic canonical repair that cannot prove an exact safe mutation plan.

Design pressure rule: a new feature should be a projection/adapter by default.
Move it into canonical state only if it must participate in replay,
policy enforcement, conflict detection, durable provenance, or integrity
verification. Prefer one append-only fact table per durable invariant and avoid
parallel representations that can disagree unless one side is explicitly
rebuildable.

The active redundancy review is maintained in
`docs/09_complexity_audit.md`. Any proposal for FUSE, managed vector search,
model-backed extraction, richer policy algebra, remote coordination, or
physical history compaction should first pass that audit before adding
canonical state.

## Canonical Tables

These tables are canonical kernel state.

- `blobs`: content hash, size, and storage location. Blob bytes are canonical
  by hash and size.
- `nodes`: immutable objects pointing at blobs. Node id, blob hash, kind,
  media type, metadata, and creation time are canonical.
- `refs`: current mutable namespace view. `ref_name`, `node_id`, `ref_kind`,
  `state`, and `ref_version` are canonical current state.
- `events`: append-only semantic operations. Event id, kind, agent/run/tool
  context, workspace, payload, and timestamp are canonical event metadata.
- `event_sequence`: canonical total event order. Replay and bundles must use
  sequence order, not SQLite `rowid`.
- `event_edges`: canonical typed provenance links between events and nodes.
- `ref_events`: canonical append-only ref lifecycle audit. Ref history and
  event-time namespace views derive from this table.
- `ref_view_checkpoints`: canonical immutable replay proofs for a target event
  boundary. Checkpoints are preconditions for any future physical history
  compaction.
- `workspace_base_refs`: canonical checkout snapshot for conflict detection.
- `policy_label_events`: canonical append-only policy label audit and active
  label derivation.
- `policy_rule_events`: canonical append-only policy registry audit and active
  visibility-rule derivation.
- `policy_expression_rule_events`: canonical append-only policy expression
  registry audit and active expression-rule derivation.
- `purpose_policy_rule_events`: canonical append-only purpose policy registry
  audit and active explicit-purpose read/consume/search/query rule derivation.
- `agent_capability_events`: canonical append-only agent capability grant audit
  and active capability derivation.
- `purpose_capability_rule_events`: canonical append-only explicit-purpose
  capability requirement audit and active purpose authorization derivation.
- `operation_capability_rule_events`: canonical append-only operation
  capability requirement audit and active lifecycle/review authorization
  derivation.
- `fragment_policy_label_events`: canonical append-only byte-range policy label
  audit and active fragment-label derivation.
- `run_events`: canonical run lifecycle audit.

## Derived Tables And Files

These are not canonical.

- `node_fts`: derived text index rebuilt from node bytes.
- `node_chunks(...)`: derived chunk metadata computed from canonical node bytes.
- `node_chunk_indexes` / `node_chunk_index`: persisted derived chunk metadata
  for selective large-file planning; rebuildable from canonical node bytes.
- `node_embeddings`: local embedding projection rows attached to immutable
  nodes; embeddings are caller-provided derived index data, not canonical state.
- `node_chunk_embeddings`: local embedding projection rows attached to cached
  chunk index rows; chunk embeddings are caller-provided derived index data, not
  canonical state.
- materialized replay directories: ordinary filesystem projections.
- working tree directories: ordinary filesystem projections.
- `materialized_working_sets` / `materialized_working_set_files`: managed
  cached working-set projections; rebuildable from current refs and canonical
  node bytes.
- bundle JSON checksums and optional HMAC-SHA256 signatures: portable export
  metadata for integrity/authentication preflight, not a replacement for
  canonical event/node/blob validation.

## Payload JSON

`events.payload_json` is audit metadata, not the primary relationship model.

Rules:

- Relationships that must be machine-checked belong in `event_edges`,
  `ref_events`, `workspace_base_refs`, or `policy_label_events`.
- Payload JSON may duplicate useful context such as filter parameters,
  citation counts, ref versions, and policy labels.
- Integrity checks must not rely only on payload JSON when a typed edge or
  audit row can express the invariant.
- Payload JSON can evolve as long as canonical rows remain valid and existing
  integrity checks still pass.

## Kernel APIs

Kernel APIs are operations that mutate canonical state or expose canonical
views:

- workspace lifecycle: `open_workspace`, `checkout`, `fork_workspace`;
- node/ref mutation: `write`, `publish`, `publish_manifest`, `delete`,
  `archive_ref`, `approve`, `reject_ref`;
- access audit: `consume`, `read_ref`;
- runs: `start_run`, `finish_run`, `get_run`, `runs`;
- refs/events: `get_ref`, `ref_history`, `event`,
  `answer_evidence_coverage`, `answer_token_accounting`,
  `answer_quote_support`, `events`, `node_events`;
- lineage: `lineage_nodes`, `lineage_graph`;
- policy: `set_policy_label`, `policy_labels`, `set_policy_rule`,
  `clear_policy_rule`, `policy_rules`, `set_policy_expression_rule`,
  `clear_policy_expression_rule`, `policy_expression_rules`,
  `set_purpose_policy_rule`, `clear_purpose_policy_rule`,
  `purpose_policy_rules`, `grant_agent_capability`,
  `revoke_agent_capability`, `agent_capabilities`,
  `set_purpose_capability_rule`, `clear_purpose_capability_rule`,
  `purpose_capability_rules`, `set_operation_capability_rule`,
  `clear_operation_capability_rule`, `operation_capability_rules`,
  `set_fragment_policy_label`,
  `fragment_policy_labels`, `propagate_fragment_policy_labels`,
  `auto_propagate_fragment_policy_labels`,
  `auto_propagate_fragment_policy_labels_by_normalized_scalar`,
  `auto_propagate_fragment_policy_labels_by_normalized_json`,
  `set_json_field_policy_label`, `set_markdown_field_policy_label`,
  `set_markdown_section_policy_label`;
- semantic fields: `json_field_spans`, `markdown_field_spans`,
  `markdown_field_values`,
  `markdown_section_spans`;
- embedding projection: `set_node_embedding`, `node_embedding`,
  `vector_search`, `set_node_chunk_embedding`, `node_chunk_embedding`,
  `vector_search_chunks`;
- replay/export: `ref_view_at_event`, `materialize_ref_view_at_event`,
  `create_ref_view_checkpoint`, `ref_view_checkpoints`,
  `verify_ref_view_checkpoint`, `export_event_bundle`, `export_run_bundle`,
  `export_history_archive`, `import_event_bundle`;
- workspace merge: `workspace_diff`, `workspace_conflicts`,
  `merge_workspace`;
- compatibility adapter: `materialize_workspace`, `worktree_readiness`,
  `commit_worktree` including optional worktree-manifest base checks;
- POSIX-like facade: workspace `read` / `cat`, `read_range`, `write_range`,
  `append`, `truncate`, `ls`, `stat`, `stat_posix`, `exists`, `is_file`,
  `is_dir`, `access`, `chmod`, `chown`, `utime`, `mkdir`, `touch`, `rm`, `cp`,
  `link`, `mv`, `find`, and `grep`;
- verification/admin: `verify_integrity`, `repair_plan`, `compaction_plan`,
  `archival_readiness_plan`, `vacuum_database`, `compact_inline_blobs`,
  `clean_orphan_objects`, `schema_status`, `require_schema_current`,
  `schema_migration_plan`, `apply_schema_migrations`, `rebuild_derived_indexes`,
  `set_retention_policy`, `retention_policies`, `run_retention_policy`.

Query/search/answer are accepted kernel-adjacent projections because they write
auditable semantic events and typed edges. They must remain built on canonical
refs, nodes, and event edges.

## Invariants

The kernel must preserve these invariants:

- Immutable tables cannot be updated or deleted through normal operation.
- Every event has exactly one event sequence row.
- Event sequence values are contiguous and monotonic.
- Every event edge references an existing event and node.
- Every ref audit row references a matching event and its old/new nodes when
  present.
- Current `refs` state is consistent with the latest ref audit rows created by
  kernel mutations.
- Workspace checkout base rows reference checkout events and matching typed
  source/workspace edges.
- Merge conflict detection uses checkout snapshots, not only current refs.
- Policy label rows reference `policy_label` events and exactly one typed
  subject edge.
- Ref/node policy labels may have multiple active values for the same
  `(subject_type, subject_id, label)`; a clear row with `value=NULL` clears all
  active values for that subject label.
- Policy rule rows reference `policy_rule` events and define supported
  visibility deny rules over active policy label values.
- Policy expression rule rows reference `policy_expression_rule` events and
  define supported `label` / `all` / `any` / `not` / `at_least` visibility
  expressions over active policy label values.
- Fragment policy label rows reference `fragment_policy_label` events, existing
  nodes, valid byte ranges, and one typed `fragment_policy_subject` edge.
- New artifact nodes with a blob hash matching existing labeled nodes propagate
  active fragment labels through independent `fragment_policy_label` events.
- New artifact nodes with explicit `derived_from_nodes` propagate active source
  node labels to output node labels and transformed source fragment labels to
  full-output fragment labels.
- Answer nodes treat citation nodes as derived inputs for policy propagation.
- Answer evidence coverage inspection derives coverage only from answer payload
  retrieval ids and existing query/search result edges; it must not create
  canonical state.
- Answer token accounting is a deterministic estimate over generated answer
  bytes and cited-node bytes; vendor tokenizers and provider price tables must
  remain adapter-level concerns.
- Token cost profiles are append-only adapter facts for caller-supplied model
  pricing and prompt overhead. Cost estimates may combine those profiles with
  deterministic answer token accounting, but must not imply provider price
  freshness or exact tokenizer parity.
- Integrity verification recomputes answer token accounting from canonical
  answer result and citation edges. The event payload is auditable metadata, not
  an unchecked source of truth.
- Answer quote support is a deterministic exact-byte overlap projection over
  the answer output edge and citation input edges. It is not semantic
  entailment and must not become a hidden model-scoring dependency.
- Explicit partial-output attribution can propagate active source fragment
  labels from a source byte range to a caller-provided output byte range.
- Automatic partial-output attribution can propagate active source fragment
  labels only when the labeled source bytes have one exact occurrence in the
  output node.
- Normalized scalar attribution can propagate active source fragment labels
  when a labeled JSON string, boolean, null, or finite number scalar has one
  normalized occurrence in the output node.
- Normalized JSON value attribution can propagate active source fragment labels
  when a labeled JSON scalar, array, or object has one normalized/minified JSON
  value occurrence in the output node. This is deterministic JSON projection,
  not semantic entailment or paraphrase matching.
- `repair_plan` may classify verifier issues and recommend next actions, but
  must not mutate canonical rows, derived rows, or object bytes.
- `compaction_plan` may count canonical history, inactive refs, GC candidates,
  inline blob pressure, active pins, active retention policies, derived
  projection rows, and SQLite freelist pages, but must not run GC, VACUUM,
  inline compaction, archival, canonical repair, or derived repair.
- `vacuum_database` may run SQLite `VACUUM` only when called with
  `dry_run=False`. It must not mutate canonical rows, archive history, repair
  integrity issues, or compact inline blobs.
- `compact_inline_blobs` may move verified inline blob bytes to file-backed CAS
  object storage only when called with `dry_run=False`. It may update blob
  storage representation fields, but must not change blob `hash` or `size`,
  node ids, refs, events, or ref history.
- `clean_orphan_objects` may remove verified hash-shaped object files that are
  not owned by file-backed `blobs` metadata only when called with
  `dry_run=False`. It must not mutate SQLite rows, canonical blob metadata,
  refs, nodes, events, or audit history.
- Retention policy rules are append-only audit facts. `run_retention_policy`
  must derive the current enabled rule from event sequence order, compute a
  current GC cutoff from `min_age_ms`, preserve dry-run by default, and delegate
  physical deletion only to the existing `collect_garbage` executor.
- `commit_worktree(..., require_manifest_match=True)` must verify the
  materialized `.anfs-worktree-manifest.json` against current workspace node ids
  in the same transaction that applies worktree mutations. A changed, missing,
  or newly appeared current path is a ref conflict, not an implicit merge.
- `commit_worktree(...)` must reject symlinks and other special filesystem
  entries rather than following them. The worktree adapter may accept regular
  files and directories, but it must not import bytes through link traversal.
- `commit_worktree(...)` and `worktree_readiness(...)` must reject relative
  worktree paths containing `\`, because that byte is a POSIX filename
  character but a separator for Windows-like tools.
- The root `.anfs-worktree-manifest.json` path is reserved adapter metadata.
  Worktree scans may ignore that local file, but materialization and commit
  must reject a canonical workspace user path with the same name rather than
  silently overwriting, deleting, or importing it.
- Workspace lifecycle and workspace adapter APIs must validate workspace ids
  before recording events or mutating refs. Valid workspace ids use
  `ws:<ascii-name>` with ASCII letters, digits, `.`, `-`, or `_`; `/` is
  reserved for separating workspace refs from logical paths.
- `schema_status` may expose migration rows and compatibility states, but must
  not mutate schema metadata.
- `schema_migration_plan` may classify schema migration work and safe actions,
  but must not mutate schema metadata.
- `apply_schema_migrations` must default to dry-run. It may execute only
  supported safe migration actions and must reject unsupported future versions
  or current-version name mismatches rather than auto-repairing them.
- `require_schema_current` must fail if the current migration is missing or an
  unsupported future schema version is recorded. Engine initialization must
  reject future schema versions before normal schema initialization.
- `rebuild_derived_indexes` may repair only rebuildable derived state:
  `node_fts` rows and persisted chunk indexes. It must not mutate canonical
  blobs, nodes, refs, events, policy rows, or audit rows.
- `vector_search` must operate over current refs, caller-selected lifecycle
  state, and the shared visibility policy; embedding rows are projections over
  immutable node ids, not authoritative content.
- `vector_search_chunks` must operate over current refs, cached chunk rows,
  caller-selected lifecycle state, ref/node visibility policy, and
  fragment-range visibility policy.
- Workspace `read_range(path, offset, length, ...)` must resolve the path or
  explicit ref, reject directory refs, preserve normal ref readability and
  purpose-capability checks, enforce active visibility and purpose policies for
  only the requested byte range, return empty bytes for reads beyond EOF, and
  write an audited `read_range` event with a typed input edge to the node.
- Workspace `write_range(path, offset, content, ...)` must mutate only workspace
  logical file paths, reject directories, allow replacement, append-at-EOF, or
  zero-fill extension beyond EOF, create a new immutable node rather than
  mutable block or sparse-hole state, preserve lineage from the previous node,
  and write an audited `write_range` event.
- Workspace `append(path, content, ...)` must mutate only workspace logical file
  paths, reject directories, create missing files, append to existing files by
  creating a new immutable node, preserve previous-node lineage when present,
  and write an audited `append` event.
- Workspace `truncate(path, length, ...)` must mutate only workspace logical
  file paths, reject directories, explicit refs, missing paths, and negative
  lengths, shrink or NUL-extend existing files through a new immutable node,
  preserve previous-node lineage, write an audited `truncate` event, and avoid
  sparse-hole or mutable block state.
- Workspace `exists(path)`, `is_file(path)`, and `is_dir(path)` must reuse
  `stat(...)` semantics, report missing or deleted paths as `false`, and avoid
  adding canonical metadata state.
- Workspace `stat_posix(path)` must expose synthetic POSIX-like mode, nlink,
  uid, gid, inode, and atime/mtime/ctime fields derived from existing refs,
  paths, timestamps, and workspace path metadata overlays. For active workspace
  file refs that point at the same immutable node, file nlink and inode must be
  derived from that shared node identity, and metadata overlays must be
  inherited by new `link(...)` paths and synchronized across active linked refs
  while they still point at the same node.
- Workspace `access(path, mode, uid=None, gid=None, groups=None)` must expose an
  `os.access`-style derived predicate for `F_OK`, `R_OK`, `W_OK`, and `X_OK`,
  report missing paths as `false`, reuse current readability and fragment
  visibility policy for read checks, use workspace path mutability and POSIX
  mode bits for write and execute checks, support optional non-negative
  effective uid/gid/group ids for owner/group/other mode-bit selection, support
  effective uid `0` as a root-style read/write mode-bit bypass while still
  requiring some execute bit for `X_OK`, and avoid claiming OS process
  impersonation, ACL, or sandbox semantics.
- Workspace `chmod(path, mode, tool_call_id=None)` must record an audited
  workspace logical-path mode overlay that affects `stat_posix(...)` and
  `access(...)`. It must synchronize active linked file paths that still point
  at the same immutable node, preserve POSIX special mode bits such as setuid,
  setgid, and sticky in `stat_posix(...)` while `access(...)` continues to
  evaluate owner/group/other rwx bits, apply setgid directory inheritance to
  newly created regular files, touched files, and subdirectories by inheriting
  the parent gid and preserving setgid on subdirectories, reject explicit refs,
  and must not duplicate file contents, add ACL semantics, or claim setgid
  execution or full kernel syscall fidelity.
- Workspace `chown(path, uid, gid, tool_call_id=None)` must record an audited
  workspace logical-path uid/gid overlay that affects `stat_posix(...)`. It
  must reject explicit refs, invalid negative values, and no-op `(-1, -1)`
  updates, must synchronize active linked file paths that still point at the
  same immutable node, must preserve immutable node/blob contents, and must not
  make `access(...)` impersonate OS users or groups.
- Workspace `utime(path, atime_ms=None, mtime_ms=None, tool_call_id=None)` must
  record an audited workspace logical-path time overlay that affects
  `stat_posix(...)`. It must reject explicit refs and negative timestamps, must
  synchronize active linked file paths that still point at the same immutable
  node, must preserve immutable node/blob contents, and must not claim implicit
  atime updates on read or full kernel syscall fidelity.
- Workspace `acquire_lock(path, ttl_ms=None, tool_call_id=None)` must record an
  audited exclusive ANFS path lease for the caller's workspace agent/run and
  return a `lock:*` token. Active leases must block other agents/runs from
  mutating the locked path, descendants covered by a locked directory, or a
  parent directory operation that would cover a locked descendant.
  `release_lock(path, lock_id, tool_call_id=None)` must delete only the owning
  active lease, and `lock_info(path="")` must report active non-expired leases.
  These leases are kernel coordination metadata, not POSIX descriptor-level
  `fcntl`/`flock`, process, mmap, or FUSE semantics.
- Workspace `link(src_path, dst_path, tool_call_id=None)` must record an
  audited hard-link-like workspace ref to the same immutable file node. It must
  reject directory sources, existing destinations, explicit destination refs,
  and root destinations; create no new content node; inherit existing source
  path metadata overlays for workspace sources; and keep later writes as
  path-local immutable-node replacements rather than full mutable POSIX inode
  sharing.
- Workspace `cp(src_path, dst_path, ...)` and
  `mv(src_path, dst_path, ...)` must treat an active destination directory as a
  container and place the source basename under that directory for both regular
  file and recursive directory operations. This is a compatibility path
  resolution rule over existing refs/events, not a lazy namespace move or FUSE
  syscall semantic.
- Workspace `delete(path, tool_call_id=None, uid=None, gid=None, groups=None)`,
  `rm(...)`, and `mv(src_path, dst_path, tool_call_id=None, uid=None, gid=None,
  groups=None)` must preserve existing agent-tool behavior when no effective
  identity is supplied. When effective uid/gid are supplied, deletion and
  replacement under a sticky directory must be allowed only for effective uid
  `0`, the sticky parent directory owner, or the entry owner. This must use
  existing path metadata overlays and must not claim full OS credential,
  descriptor, ACL, or FUSE syscall semantics.
- Search, query, answer, replay, export/import, and working tree materialization
  must share the same active ref/node visibility policy.
- Explicit-purpose Workspace read, consume, search, and query operations must
  enforce active purpose capability requirements for the calling agent when
  such requirements exist. `purpose=None` remains the compatibility path and
  must not require a capability.
- Lifecycle/review operations must enforce active operation capability
  requirements for the calling agent when such requirements exist. The current
  kernel-enforced operations are `approve`, `reject_ref`, and `archive_ref`.
- Markdown frontmatter extraction is a conservative parser: it supports
  top-level and indented nested `key: value` fields, parent object spans,
  scalar sequence item paths from block or inline sequences such as
  `frontmatter.tags[0]`, sequence object item fields such as
  `frontmatter.people[0].name`, inline sequence object item fields such as
  `frontmatter.contacts[0].email`, inline object scalar fields such as
  `frontmatter.contact.email`, bounded nested inline object fields such as
  `frontmatter.contact.manager.email`, bounded nested inline array item paths
  such as `frontmatter.contacts[0].meta.flags[1]`, quoted-key bracket paths such as
  `frontmatter["owner.email"]`, tag/anchor-decorated scalar or inline payloads,
  alias token spans such as `frontmatter.alias_owner` with kind `alias`,
  conservative top-level inline-object alias expansion paths such as
  `frontmatter.alias_template.email`, conservative recursive alias/merge chain
  expansion paths such as `frontmatter.alias_template_deep.email` and
  `frontmatter.merged_template_chain.email`, conservative merge-array
  expansion paths such as `frontmatter.merged_array_template.phone`, scalar
  alias target paths such as `frontmatter.alias_owner.__target` and
  `frontmatter.scalar_alias_deep.__target`,
  block-sequence alias/anchor/merge bookkeeping such as
  `frontmatter.block_aliases[0].__target` and
  `frontmatter.block_merge_items[1].email`, YAML null scalar kind spans such
  as `frontmatter.optional_owner`, explicit YAML core tag kind overrides such
  as `!!str 7`, `!!timestamp 2026-06-09`,
  `!!binary SGVsbG8=`, or `!<tag:yaml.org,2002:str> 42`, explicit container
  tags such as `!!set {read: null}` and `!!omap [{first: one}]`, conservative
  unquoted ISO timestamp scalars such as `2026-06-09`, quoted scalar payload
  spans that exclude quote delimiters while preserving string kind unless an
  explicit core tag overrides it, decoded semantic values through
  `markdown_field_values(...)` for single-quoted doubled apostrophes and common
  YAML double-quoted escapes, and `|` / `>` block scalar spans. Alias and
	  merge expansion paths point at the
	  alias token byte range; scalar alias target paths point at the anchored scalar
	  payload bytes. It is not a full YAML parser and does not support unbounded or
	  cyclic recursive merge/alias semantics, complete YAML merge precedence, full
	  YAML tag semantics beyond conservative core scalar kind overrides, or complex
	  YAML typing.
- Markdown body section extraction is a conservative ATX/Setext heading span
  parser: it skips opening frontmatter, fenced code blocks, and indented code
  blocks for heading detection, exposes
  conservative paragraph/paragraph-line/inline-strong/inline-strong-text/
  inline-emphasis/inline-emphasis-text/inline-link/inline-link-destination/
  inline-link-title/inline-link-label/reference-link/reference-link-label/
  reference-link-reference/reference-link-resolved-destination/
  reference-link-resolved-title/inline-image/
  inline-image-alt/inline-image-destination/inline-image-title/
  reference-image/reference-image-alt/
  reference-image-reference/reference-image-resolved-destination/
  reference-image-resolved-title/inline-code/
  autolink/autolink-target/list/list-item/task-checkbox/blockquote/
  blockquote-line/table/table-align/table-row/table-cell/code/html/
  link-reference/link-reference-label/link-reference-destination/
  link-reference-title/thematic-break spans, but it is not a full Markdown AST parser.
  Inline strong, inline emphasis, inline link, reference-link, inline image,
  reference-image, and autolink spans must not be emitted for byte ranges that
  overlap conservative inline-code spans. Conservative inline delimiter
  detection must ignore delimiter bytes escaped by an odd number of immediately
  preceding backslashes. Inline-code extraction must support same-line matching
  backtick runs of one or more backticks while keeping the same `inline-code`
  path family. Direct inline link/image destination extraction must preserve
  balanced unescaped parentheses inside destination byte ranges and must expose
  angle-bracketed destination payload bytes without the surrounding delimiters.
  Link-reference-definition destination extraction must also expose
  angle-bracketed payload bytes without the surrounding delimiters. Direct
  inline/reference title extraction must expose trailing quoted or
  parenthesized title payload spans when present. Reference label matching must
  normalize backslash-escaped ASCII punctuation without changing the raw label
  byte spans exposed by reference-label and link-reference-label children.
  Duplicate link-reference definitions must resolve to the first normalized
  definition while preserving later duplicate component spans. Link-reference
  definitions must support immediate next-line title-only continuations for
  quoted or parenthesized titles.
- Search and query text inputs must be converted to safe literal FTS
  expressions before reaching SQLite `MATCH`; caller text is not trusted as an
  FTS query language.
- Worktree commit events must have payload result/changed/edge counts backed by
  typed `worktree_*` edges.
- Worktree commit canonical state changes must be single-transaction: current
  workspace reads, per-file write/delete events, ref audit rows, event edges,
  and the grouped `worktree_commit` event either all persist or all roll back.
- History archive export must include all canonical events and ref audit rows
  through the selected latest source sequence, plus referenced edges, nodes, and
  blobs. It is a portable projection only and must not delete, mark archived, or
  compact canonical rows.
- Ref-view checkpoints must store an immutable target event, source sequence,
  prefix, inactive-ref mode, ref count, and checksum over the replay view.
  Verification recomputes the view and must not mutate canonical rows. Physical
  history compaction must not proceed without archive proof plus a valid replay
  checkpoint for the retained boundary.
- Archival readiness planning must be read-only. It may verify a full-history
  archive bundle and a ref-view checkpoint, but must not delete, rewrite, or
  compact canonical event/ref history.
- Import validates bundle reference closure and destination-side policy labels
  before writing canonical rows.
- Derived indexes and materialized directories can be rebuilt from canonical
  blobs, nodes, refs, and events.

## Compatibility Rule

The external user experience can look like a normal filesystem, but ordinary
directory contents are never the kernel source of truth until committed through
ANFS. Working tree adapters must scan, validate, and translate file changes
into kernel mutations rather than mutating canonical tables directly. Checked
worktree commits must validate the materialized manifest against current
workspace node ids inside the mutation transaction so concurrent stale
materialized bases fail before canonical refs advance.
