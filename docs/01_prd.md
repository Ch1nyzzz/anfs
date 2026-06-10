# ANFS MVP PRD

## Product Name

ANFS: Agent-Native File System

## MVP Objective

Implement a local-first ANFS kernel that exposes a Python API backed by a Rust
core, SQLite metadata store, content-addressed blob storage, immutable lineage
events, mutable refs, and native lineage validation.

The MVP is considered successful when it can reject an invalid approval where a
reviewer attempts to approve patch v2 using test evidence derived only from
patch v1.

## Architecture

```text
Python semantic API
    |
PyO3 binding
    |
Rust core engine
    |
SQLite metadata DB + CAS object directory
```

## Strict Implementation Constraints

- Core engine must be written in Rust.
- Python API must be exposed through PyO3.
- Build system should use maturin.
- SQLite access should use rusqlite and raw SQL.
- The first implementation should be single-process/local-first.
- SQLite must run in WAL mode.
- No ORM.
- No external graph database.
- No external vector database in the kernel MVP.
- No FUSE driver in MVP.

## Data Model

The kernel should use the following logical tables. SQL can evolve, but the
semantics must remain stable.

### blobs

Immutable byte content.

Required fields:

- `hash TEXT PRIMARY KEY`
- `size INTEGER NOT NULL`
- `storage_kind TEXT NOT NULL`
- `storage_uri TEXT`
- `inline_content BLOB`

Rules:

- Hash algorithm: SHA-256 for MVP.
- Inline threshold: 64 KiB for the first prototype; production should evaluate
  a lower threshold such as 4-16 KiB to keep SQLite compact.
- Hashes should eventually be stored as binary `BLOB(32)` rather than hex
  `TEXT` if table/index size becomes important.
- `storage_kind` is `inline` or `file`.
- `verify_integrity()` must compare recorded `size` with actual inline or
  file-backed byte length.
- Blob rows are append-only by engine contract.

### nodes

Immutable facts over blobs.

Required fields:

- `node_id TEXT PRIMARY KEY`
- `blob_hash TEXT NOT NULL`
- `kind TEXT NOT NULL`
- `media_type TEXT`
- `metadata_json TEXT`
- `created_at INTEGER NOT NULL`

Rules:

- Valid MVP kinds: `resource`, `artifact`, `skill`, `index`, `memory`.
- Node lifecycle state is forbidden in this table.
- Node rows are append-only by engine contract.

### refs

Mutable names and lifecycle views.

Required fields:

- `ref_name TEXT PRIMARY KEY`
- `node_id TEXT NOT NULL`
- `ref_kind TEXT NOT NULL`
- `state TEXT NOT NULL`
- `updated_at INTEGER NOT NULL`

Rules:

- This is the only mutable core table.
- Valid MVP states: `draft`, `published`, `approved`, `rejected`, `archived`.
- Ref names must include namespace prefixes, such as:
  - `ws:coder/src/db.py`
  - `artifact:patch/db_fix@v1`
  - `resource:repo/main@v1`
  - `memory:repo/debugging_notes@v1`

### events

Immutable causal transitions.

Required fields:

- `event_id TEXT PRIMARY KEY`
- `kind TEXT NOT NULL`
- `agent_id TEXT`
- `run_id TEXT`
- `tool_call_id TEXT`
- `workspace_id TEXT`
- `payload_json TEXT`
- `created_at INTEGER NOT NULL`

Rules:

- Valid MVP kinds include `checkout`, `fork_workspace`, `write`, `publish`,
  `publish_manifest`, `approve`, `reject`, `search`, `consume`, `read_ref`,
  `delete_ref`, `archive_ref`, `merge_workspace`, `run_start`, `run_finish`,
  and `policy_decision`.
- Event rows are append-only by engine contract.

### event_edges

Typed multi-input/multi-output edges for lineage.

Required fields:

- `event_id TEXT NOT NULL`
- `direction TEXT NOT NULL`
- `node_id TEXT NOT NULL`
- `role TEXT NOT NULL`
- `logical_path TEXT`

Rules:

- `direction` is `input` or `output`.
- `verify_integrity()` must diagnose any edge whose direction is outside that
  domain.
- A write event must connect derived inputs to the produced node.
- A publish event must connect the workspace node to the published artifact view.
- An approve event must connect target and evidence nodes.
- `verify_integrity()` must diagnose missing required edge roles for core
  event kinds such as `write`, `publish`, `approve`, lifecycle, workspace, and
  snapshot events.

### event_sequence

Append-only monotonic event ordering.

Required fields:

- `event_id TEXT PRIMARY KEY`
- `seq INTEGER NOT NULL UNIQUE`

Rules:

- Every event must have exactly one sequence row.
- `seq` is the canonical replay/export ordering.
- Replay and bundle queries must not depend on SQLite `rowid` for event order.
- Existing events can be backfilled by inserting sequence rows without mutating
  the immutable `events` table.
- New sequence numbers must be allocated by a database-owned monotonic allocator,
  not by application-side `MAX(seq)+1`, so multiple engine handles cannot race
  on the same sequence number.

### ref_events

Audit log for every ref mutation.

Required fields:

- `ref_name TEXT NOT NULL`
- `event_id TEXT NOT NULL`
- `old_node_id TEXT`
- `new_node_id TEXT`
- `old_state TEXT`
- `new_state TEXT`

Rules:

- Every create, update, state transition, and publish of a ref must write a
  `ref_events` row.
- `new_node_id` must always resolve to an immutable node.
- `old_node_id`, when present, must resolve to an immutable node.
- For a given ref, the first audit row must have no old node/state.
- For every later row of the same ref, `old_node_id` / `old_state` must match
  the previous row's `new_node_id` / `new_state`.
- Replay and debugging depend on this table because `refs` is mutable.
- `verify_integrity()` must diagnose missing or dangling ref audit node
  references and audit-chain discontinuities.
- `verify_integrity()` must diagnose ref audit rows whose mutation event lacks
  a matching typed edge for the audited ref/node. This keeps `ref_events`
  anchored in the causal DAG instead of becoming a detached append-only log.

### node_fts

Optional MVP full-text index.

Required fields:

- `node_id UNINDEXED`
- `body`

Rules:

- FTS is a derived index, not canonical data.
- Search results must resolve back to node ids.
- `verify_integrity()` must diagnose missing FTS rows for searchable text,
  JSON, and ANFS manifest nodes.
- `verify_integrity()` must diagnose orphaned, duplicated, or stale FTS rows.

### runs

Current lifecycle view for first-class agent runs.

Required fields:

- `run_id TEXT PRIMARY KEY`
- `agent_id TEXT`
- `workspace_id TEXT`
- `state TEXT NOT NULL`
- `metadata_json TEXT`
- `created_at INTEGER NOT NULL`
- `updated_at INTEGER NOT NULL`
- `ended_at INTEGER`

Rules:

- Valid states are `active`, `succeeded`, `failed`, and `cancelled`.
- A run starts in `active`.
- A run may transition from `active` to one terminal state.
- Current run state is queryable without replaying the whole event log.

### run_events

Append-only audit rows for run lifecycle transitions.

Required fields:

- `run_id TEXT NOT NULL`
- `event_id TEXT NOT NULL`
- `old_state TEXT`
- `new_state TEXT NOT NULL`
- `old_metadata_json TEXT`
- `new_metadata_json TEXT`

Rules:

- Every run start/finish transition writes a `run_events` row.
- `run_events` rows are append-only by engine contract.
- `verify_integrity()` must verify that each current `runs` row matches the
  latest append-only `run_events` audit row for that run.
- `run_start` events must have exactly one `run_events` audit row whose
  transition is `None -> active`.
- `run_finish` events must have exactly one `run_events` audit row whose
  transition is `active -> terminal`.
- For a given run, the first audit row must have no old state/metadata.
- For every later row of the same run, `old_state` / `old_metadata_json` must
  match the previous row's `new_state` / `new_metadata_json`.
- The `events.run_id` and run event payload must match the audited `run_id` and
  lifecycle state.

## Core API

### AnfsEngine

`AnfsEngine(db_path, objects_dir)`

Responsibilities:

- Open SQLite.
- Enable WAL mode.
- Apply schema migrations and record the current kernel schema version.
- Reject unsupported future schema versions before normal initialization.
- Create object directory.
- Enforce append-only contracts.

Required methods:

- `checkout(base, workspace, agent_id, run_id=None, tool_call_id=None) -> Workspace`
- `fork_workspace(source_workspace, target_workspace, agent_id, run_id=None, tool_call_id=None) -> Workspace`
- `open_workspace(workspace, agent_id, run_id=None) -> Workspace`
- `approve(target_ref, evidence_refs, agent_id, run_id=None, tool_call_id=None, expected_version=None) -> None`
- `reject_ref(target_ref, agent_id, reason=None, run_id=None, tool_call_id=None, expected_version=None) -> None`
- `archive_ref(ref_name, agent_id, run_id=None, tool_call_id=None, expected_version=None) -> None`
- `get_ref(ref_name) -> RefInfo`
- `read_node(node_id) -> bytes`
- `read_node_range(node_id, offset, length) -> bytes`
- `node_chunks(node_id, chunk_size=65536) -> list[(chunk_index, offset, size, sha256)]`
- `cache_node_chunks(node_id, chunk_size=65536) -> list[(chunk_index, offset, size, sha256)]`
- `cached_node_chunks(node_id, chunk_size=65536) -> list[(chunk_index, offset, size, sha256)]`
- `rebuild_derived_indexes() -> (fts_rows_rebuilt, chunk_indexes_cleared)`
- `repair_plan() -> list[(issue, classification, action, mutable_scope)]`
- `compaction_plan() -> list[(area, count, action, mutable_scope)]`
- `archival_readiness_plan(checkpoint_id, bundle_path, signature_key=None, require_signature=False) -> list[(check, passed, detail, count)]`
- `vacuum_database(dry_run=True) -> (freelist_pages_before, freelist_pages_after, ran)`
- `compact_inline_blobs(dry_run=True, limit=None) -> (candidate_count, candidate_bytes, compacted, dry_run)`
- `clean_orphan_objects(dry_run=True, limit=None) -> (candidate_count, candidate_bytes, removed, dry_run)`
- `schema_status() -> list[(version, name, applied_at, status)]`
- `schema_migration_plan() -> list[(version, name, status, action)]`
- `apply_schema_migrations(dry_run=True) -> (planned, applied, dry_run)`
- `require_schema_current() -> None`
- `set_node_embedding(node_id, model, vector) -> None`
- `node_embedding(node_id, model) -> Optional[list[float]]`
- `vector_search(model, query_vector, state="published", limit=10, policy_label_excludes=None) -> list[(ref_name, node_id, score)]`
- `set_node_chunk_embedding(node_id, chunk_size, chunk_index, model, vector) -> None`
- `node_chunk_embedding(node_id, chunk_size, chunk_index, model) -> Optional[list[float]]`
- `vector_search_chunks(model, query_vector, state="published", limit=10, policy_label_excludes=None) -> list[(ref_name, node_id, chunk_index, offset, size, score)]`
- `json_field_spans(node_id) -> list[(json_path, offset, length, kind)]`
- `markdown_field_spans(node_id) -> list[(field_path, offset, length, kind)]`
- `markdown_field_values(node_id) -> list[(field_path, value, kind)]`
- `markdown_section_spans(node_id) -> list[(section_path, offset, length, kind)]`
- `manifest_children(node_id) -> list[(path, node_id)]`
- `manifest_child_records(node_id) -> list[(path, node_id, role, media_type, digest, size)]`
- `snapshot_namespace(prefix, snapshot_ref, agent_id, kind="resource", run_id=None, tool_call_id=None) -> node_id`
- `start_run(run_id, agent_id=None, workspace_id=None, metadata_json=None) -> RunInfo`
- `finish_run(run_id, state="succeeded", metadata_json=None) -> RunInfo`
- `get_run(run_id) -> RunInfo`
- `runs(state=None, agent_id=None, workspace_id=None) -> list[RunInfo]`
- `policy_decisions(target_ref=None, policy=None, decision=None, reason_code=None) -> list[(event_id, payload_json)]`
- `query(prefix=None, text=None, state=None, agent_id=None, run_id=None, event_kind=None, media_type=None, created_after_ms=None, created_before_ms=None, policy=None, decision=None, reason_code=None, limit=100, policy_label_excludes=None, purpose=None) -> list[(ref_name, node_id, ref_kind, state, ref_version, node_kind, media_type, node_created_at, last_event_seq, last_event_kind, snippet)]`
- `ref_history(ref_name) -> list[(ref_name, event_id, event_kind, agent_id, old_node_id, new_node_id, old_state, new_state, created_at)]`
- `event(event_id) -> (event metadata, list[(direction, node_id, role, logical_path)])`
- `answer_evidence_coverage(answer_event_id) -> list[(citation_ref, citation_node_id, covered, covering_event_count)]`
- `answer_token_accounting(answer_event_id) -> (schema, estimator, answer_tokens, citation_tokens, total_tokens, citation_count, retrieval_event_count)`
- `set_token_cost_profile(model, input_price_micros_per_million_tokens, output_price_micros_per_million_tokens, prompt_overhead_tokens=0, agent_id="cost_profile_agent", run_id=None, tool_call_id=None) -> event_id`
- `token_cost_profiles(active_only=True) -> list[(model, estimator, input_price_micros_per_million_tokens, output_price_micros_per_million_tokens, prompt_overhead_tokens, event_id, agent_id, created_at)]`
- `answer_cost_estimate(answer_event_id, model) -> (model, estimator, input_tokens, output_tokens, total_tokens, input_cost_micros, output_cost_micros, total_cost_micros, profile_event_id)`
- `answer_quote_support(answer_event_id, min_quote_bytes=16) -> list[(citation_ref, citation_node_id, supported, longest_exact_bytes)]`
- `events(after_seq=0, limit=100, kind=None, agent_id=None, workspace_id=None, run_id=None, created_after_ms=None, created_before_ms=None, payload_contains=None, tool_call_id=None) -> list[(seq, event_id, kind, agent_id, workspace_id, created_at, input_count, output_count)]`
- `node_events(node_id, direction=None, role=None, after_seq=0, limit=100) -> list[(seq, event_id, kind, agent_id, workspace_id, created_at, input_count, output_count)]`
- `lineage_nodes(node_id, direction="ancestors") -> list[node_id]`
- `lineage_graph(node_id, direction="ancestors") -> list[(seq, event_id, event_kind, from_node_id, from_role, from_path, to_node_id, to_role, to_path)]`
- `ref_view_at_event(event_id, prefix=None, include_inactive=False, policy_label_excludes=None) -> list[(ref_name, node_id, ref_kind, state, source_event_id, created_at)]`
- `create_ref_view_checkpoint(event_id=None, prefix=None, include_inactive=False, agent_id="checkpoint_agent") -> (checkpoint_id, target_event_id, target_seq, prefix, include_inactive, ref_count, checksum, agent_id, created_at)`
- `ref_view_checkpoints() -> list[(checkpoint_id, target_event_id, target_seq, prefix, include_inactive, ref_count, checksum, agent_id, created_at)]`
- `verify_ref_view_checkpoint(checkpoint_id) -> (checkpoint_id, valid, expected_checksum, actual_checksum, expected_ref_count, actual_ref_count)`
- `materialize_ref_view_at_event(event_id, output_dir, prefix=None, include_inactive=False, overwrite=False, atomic=True, policy_label_excludes=None) -> list[(ref_name, relative_path, node_id, size)]`
- `materialize_workspace(workspace, output_dir, overwrite=False, atomic=True, policy_label_excludes=None) -> list[(path, ref_name, node_id, size)]`
- `Workspace.read_range(path, offset, length, purpose=None, tool_call_id=None) -> bytes`
- `worktree_readiness(workspace, input_dir, policy_label_excludes=None) -> list[(check, passed, detail, count)]`
- `cache_materialized_workspace(workspace, cache_root, cache_key=None, overwrite=False, atomic=True, policy_label_excludes=None) -> (cache_key, output_dir, file_count, reused)`
- `cached_working_sets(workspace=None) -> list[(cache_key, workspace_id, output_dir, manifest_hash, file_count, materialized_at)]`
- `commit_worktree(workspace, input_dir, agent_id, run_id=None, tool_call_id=None, delete_missing=True, require_manifest_match=False, policy_label_excludes=None) -> list[(path, status, node_id)]`
- `export_event_bundle(event_id, output_dir, include_blobs=True, policy_label_excludes=None, signing_key=None, signer_id=None) -> (bundle_path, event_count, node_count, blob_count)`
- `export_run_bundle(run_id, output_dir, include_blobs=True, policy_label_excludes=None, signing_key=None, signer_id=None) -> (bundle_path, event_count, node_count, blob_count)`
- `export_history_archive(output_dir, include_blobs=True, policy_label_excludes=None, signing_key=None, signer_id=None) -> (bundle_path, event_count, node_count, blob_count)`
- `import_event_bundle(bundle_path, bundle_objects_dir=None, policy_label_excludes=None, signature_key=None, require_signature=False) -> (root_event_id, event_count, node_count, blob_count)`
- `gc_roots(include_workspaces=True) -> list[(ref_name, node_id, state)]`
- `gc_candidates(include_workspaces=True, older_than_ms=None, limit=None) -> list[(hash, size, storage_kind)]`
- `collect_garbage(include_workspaces=True, dry_run=True, agent_id="gc", older_than_ms=None, limit=None) -> list[(hash, size, storage_kind, action)]`
- `pin_ref(ref_name, reason=None, agent_id="gc", run_id=None) -> pin_id`
- `unpin_gc_root(pin_id, agent_id="gc", run_id=None) -> None`
- `gc_pins(active_only=True) -> list[(pin_id, action, ref_name, node_id, reason, created_at)]`
- `set_retention_policy(policy_name, include_workspaces=True, min_age_ms=None, limit=None, enabled=True, agent_id="retention_agent", run_id=None) -> None`
- `retention_policies(active_only=True) -> list[(policy_name, effect, include_workspaces, min_age_ms, limit, event_id, created_at)]`
- `run_retention_policy(policy_name, dry_run=True, agent_id="retention_agent") -> list[(hash, size, storage_kind, action)]`
- `set_policy_label(subject_type, subject_id, label, value, agent_id, run_id=None, tool_call_id=None) -> None`
- `policy_labels(subject_type=None, subject_id=None, label=None, active_only=True) -> list[(subject_type, subject_id, label, value, event_id, agent_id, created_at)]`
- `set_fragment_policy_label(node_id, offset, length, label, value, agent_id, run_id=None, tool_call_id=None) -> None`
- `fragment_policy_labels(node_id=None, label=None, active_only=True) -> list[(node_id, offset, length, label, value, event_id, agent_id, created_at)]`
- `propagate_fragment_policy_labels(source_node_id, source_offset, source_length, output_node_id, output_offset, output_length, agent_id, run_id=None, tool_call_id=None) -> int`
- `auto_propagate_fragment_policy_labels(source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None) -> int`
- `auto_propagate_fragment_policy_labels_by_normalized_scalar(source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None) -> int`
- `auto_propagate_fragment_policy_labels_by_normalized_json(source_node_id, output_node_id, agent_id, run_id=None, tool_call_id=None) -> int`
- `set_json_field_policy_label(node_id, json_path, label, value, agent_id, run_id=None, tool_call_id=None) -> None`
- `set_markdown_field_policy_label(node_id, field_path, label, value, agent_id, run_id=None, tool_call_id=None) -> None`
- `set_markdown_section_policy_label(node_id, section_path, label, value, agent_id, run_id=None, tool_call_id=None) -> None`
- `set_policy_rule(label, value=None, effect="deny", scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `clear_policy_rule(label, value=None, scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `policy_rules(scope=None, subject_type=None, label=None, active_only=True) -> list[(scope, subject_type, label, value, effect, event_id, agent_id, created_at)]`
- `set_policy_expression_rule(expression_json, effect="deny", scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `clear_policy_expression_rule(expression_json, scope="visibility", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `policy_expression_rules(scope=None, subject_type=None, active_only=True) -> list[(scope, subject_type, expression_json, effect, event_id, agent_id, created_at)]`
- `set_purpose_policy_rule(purpose, label, value=None, effect="deny", subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `clear_purpose_policy_rule(purpose, label, value=None, subject_type="*", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `purpose_policy_rules(purpose=None, subject_type=None, label=None, active_only=True) -> list[(purpose, subject_type, label, value, effect, event_id, agent_id, created_at)]`
- `grant_agent_capability(target_agent_id, capability, agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `revoke_agent_capability(target_agent_id, capability, agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `agent_capabilities(agent_id=None, capability=None, active_only=True) -> list[(agent_id, capability, effect, event_id, policy_agent_id, created_at)]`
- `set_purpose_capability_rule(purpose, capability, effect="require", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `clear_purpose_capability_rule(purpose, capability, agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `purpose_capability_rules(purpose=None, capability=None, active_only=True) -> list[(purpose, capability, effect, event_id, agent_id, created_at)]`
- `set_operation_capability_rule(operation, capability, effect="require", agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `clear_operation_capability_rule(operation, capability, agent_id="policy_agent", run_id=None, tool_call_id=None) -> None`
- `operation_capability_rules(operation=None, capability=None, active_only=True) -> list[(operation, capability, effect, event_id, agent_id, created_at)]`
- `workspace_diff(base, workspace) -> list[(path, status, base_node_id, workspace_node_id)]`
- `workspace_conflicts(base, workspace) -> list[(path, checkout_node_id, current_base_node_id, workspace_node_id)]`
- `merge_workspace(base, workspace, agent_id, run_id=None, tool_call_id=None) -> list[(path, status, ref_name, node_id)]`

### policy registry

Must:

1. Let callers record append-only visibility policy rules through
   `set_policy_rule(...)`.
2. Let callers clear active rules without deleting history through
   `clear_policy_rule(...)`.
3. Let callers inspect active or historical registry rows through
   `policy_rules(...)`.
4. Support `scope="visibility"`, `effect="deny"`, and `subject_type` values
   `*`, `ref`, `node`, and `fragment`.
5. Interpret `value=None` as a wildcard rule value; literal `"*"` label values
   are rejected to keep wildcard semantics unambiguous.
6. Apply active visibility deny rules to active ref/node policy label values
   through the shared visibility resolver.
7. Allow multiple active values per ref/node policy label. A clear operation
   with `value=None` clears all active values for that subject/label.
8. Preserve `policy_label_excludes` as an explicit caller-provided exclusion
   mechanism alongside registry-driven value rules.
9. `verify_integrity()` must diagnose malformed policy-rule audit rows and
   orphan `policy_rule` events.
10. Let callers record append-only visibility expression rules through
    `set_policy_expression_rule(...)`.
11. Let callers clear expression rules without deleting history through
    `clear_policy_expression_rule(...)`.
12. Support expression JSON using `label` leaves and `all`, `any`, `not`, and
    threshold `at_least` operators over active label values.
13. Apply active expression rules through the shared visibility resolver for
    ref, node, and fragment subjects.
14. `verify_integrity()` must diagnose malformed policy-expression audit rows,
    invalid expression JSON, and orphan `policy_expression_rule` events.
15. Let callers record append-only purpose deny rules through
    `set_purpose_policy_rule(...)`.
16. Let callers clear active purpose rules without deleting history through
    `clear_purpose_policy_rule(...)`.
17. Let callers inspect active or historical purpose registry rows through
    `purpose_policy_rules(...)`.
18. Support non-empty purpose strings, `effect="deny"`, and `subject_type`
    values `*`, `ref`, `node`, and `fragment`.
19. Apply active purpose deny rules to active ref/node labels and active
    fragment labels when `read_ref(...)`, POSIX-style `read(...)`,
    `consume(...)`, `Workspace.search(...)`, or `Workspace.query(...)` receives
    an explicit purpose.
20. Preserve `purpose=None` as the compatibility path; normal visibility rules
    still apply, but purpose-specific gates do not.
21. `verify_integrity()` must diagnose malformed purpose-rule audit rows and
    orphan `purpose_policy_rule` events.
22. Let callers grant and revoke append-only agent capabilities through
    `grant_agent_capability(...)` and `revoke_agent_capability(...)`.
23. Let callers inspect active or historical agent capability rows through
    `agent_capabilities(...)`.
24. Let callers record append-only purpose capability requirements through
    `set_purpose_capability_rule(...)`.
25. Let callers clear purpose capability requirements without deleting history
    through `clear_purpose_capability_rule(...)`.
26. Apply active purpose capability requirements before explicit-purpose
    workspace `read_ref(...)`, POSIX-style `read(...)`, `consume(...)`,
    `Workspace.search(...)`, and `Workspace.query(...)`.
27. Preserve `purpose=None` as the compatibility path for capability checks.
28. Let callers record and clear append-only operation capability requirements
    through `set_operation_capability_rule(...)` and
    `clear_operation_capability_rule(...)`.
29. Apply active operation capability requirements before lifecycle/review
    operations such as `approve(...)`, `reject_ref(...)`, and
    `archive_ref(...)`.
30. `verify_integrity()` must diagnose malformed capability audit rows and
    orphan `agent_capability` / `purpose_capability_rule` /
    `operation_capability_rule` events.

### Workspace

Required methods:

- `write(path, content, derived_from_nodes=None, tool_call_id=None) -> node_id`
- `write_range(path, offset, content, tool_call_id=None) -> node_id`
- `append(path, content, tool_call_id=None) -> node_id`
- `truncate(path, length, tool_call_id=None) -> node_id`
- `publish(path, artifact_ref_name, kind="artifact", tool_call_id=None) -> node_id`
- `publish_manifest(paths, artifact_ref_name, kind="artifact", tool_call_id=None) -> node_id`
- `consume(ref_name, purpose=None, tool_call_id=None) -> node_id`
- `read_ref(ref_name, purpose=None, tool_call_id=None) -> bytes`
- `stat(path="") -> (path, kind, ref_name, node_id, state, ref_version, media_type, size)`
- `stat_posix(path="") -> (path, kind, ref_name, node_id, state, ref_version, media_type, size, mode, nlink, uid, gid, inode, atime_ms, mtime_ms, ctime_ms)`
- `exists(path) -> bool`
- `is_file(path) -> bool`
- `is_dir(path) -> bool`
- `access(path, mode, uid=None, gid=None, groups=None) -> bool`
- `chmod(path, mode, tool_call_id=None) -> None`
- `chown(path, uid, gid, tool_call_id=None) -> None`
- `utime(path, atime_ms=None, mtime_ms=None, tool_call_id=None) -> None`
- `cp(src_path, dst_path, tool_call_id=None) -> node_id`
- `link(src_path, dst_path, tool_call_id=None) -> node_id`
- `delete(path, tool_call_id=None, uid=None, gid=None, groups=None) -> None`
- `rm(path, tool_call_id=None, uid=None, gid=None, groups=None) -> None`
- `mv(src_path, dst_path, tool_call_id=None, uid=None, gid=None, groups=None) -> node_id`
- `search(query, scope="published", tool_call_id=None, policy_label_excludes=None, purpose=None) -> SearchResult`
- `query(prefix=None, text=None, state=None, agent_id=None, run_id=None, event_kind=None, media_type=None, created_after_ms=None, created_before_ms=None, policy=None, decision=None, reason_code=None, limit=100, tool_call_id=None, policy_label_excludes=None, purpose=None) -> QueryRefRows`
- `answer(path, content, citation_refs, tool_call_id=None, policy_label_excludes=None, retrieval_event_ids=None) -> node_id`

## Operation Semantics

ANFS records semantic state transitions, not every low-level filesystem syscall.
The kernel should not create heavy events for ordinary `open`, `read`, `stat`,
editor swap files, or compiler temporary files. It should record operations that
change or depend on workflow meaning: write, publish, consume, approve, reject,
checkout, search-as-context, and ref deletion/archive.

### write

Must:

1. Hash content.
2. Insert blob if missing.
3. Insert immutable node.
4. Insert write event.
5. Insert input edges for `derived_from_nodes`.
6. Insert output edge for the new node.
7. Upsert workspace ref as `draft`.
8. Insert ref audit row.
9. Commit atomically.

### publish

Must:

1. Resolve workspace path to current draft node.
2. Insert publish event.
3. Add event edges.
4. Upsert artifact/resource ref as `published`.
5. Insert ref audit row.
6. Commit atomically.

### checkout / snapshot

Checkout and snapshot are semantic view operations. They must record enough
lineage to prove which immutable nodes were projected into a workspace or frozen
into a manifest.

Workspace lifecycle APIs must reject unsafe workspace/branch names before
recording events or refs. A workspace name must be `ws:<ascii-name>` using only
ASCII letters, digits, `.`, `-`, and `_` after the `ws:` prefix. Slashes are
not allowed in the workspace id because `/` separates a workspace ref from its
logical file path.

Checkout with a non-empty `base` must:

1. Resolve base refs or a published/approved manifest ref.
2. Project each base child into the workspace namespace.
3. Record checkout base snapshots in `workspace_base_refs`.
4. Insert a `checkout` event.
5. Add `base_source:*` input edges and `workspace_view:*` output edges.
6. `verify_integrity()` must diagnose base checkout events missing either edge
   family.
7. `verify_integrity()` must diagnose `workspace_base_refs` rows that do not
   reference a `checkout` event or whose source/workspace projection is not
   backed by matching typed checkout edges.

Fork workspace must:

1. Validate and normalize source and target workspace names.
2. Reject identical source and target workspaces.
3. Copy the source workspace's active `draft` refs into target workspace refs
   without copying blob bytes or creating new nodes.
4. Reject target refs that already exist rather than overwriting them.
5. Insert a `fork_workspace` event with source/target workspace payload,
   agent, run, tool-call, and target workspace context.
6. Add `fork_source:*` input edges from source workspace refs to immutable
   nodes.
7. Add `fork_workspace_view:*` output edges from immutable nodes to target
   workspace refs.
8. Insert ref audit rows for each target workspace ref.
9. `verify_integrity()` must diagnose fork events missing typed source/view
   edges and fork ref audit rows without matching `fork_workspace_view:*`
   output edges.

Snapshot namespace must:

1. Resolve active refs under the source namespace.
2. Exclude deleted and archived refs.
3. Create an immutable manifest node.
4. Publish the snapshot ref to that manifest.
5. Insert a `snapshot_namespace` event.
6. Add `snapshot_child:*` input edges and exactly one `snapshot_manifest`
   output edge.
7. `verify_integrity()` must diagnose malformed snapshot events missing those
   required edge roles.
8. `verify_integrity()` must diagnose snapshot events whose `snapshot_child:*`
   input edges do not match the output manifest body's child path/node list.

### publish manifest

Must:

1. Resolve every workspace path to a current draft node.
2. Create a canonical manifest JSON body listing child paths, node ids, role,
   media type, content digest, and size.
3. Insert manifest blob and immutable manifest node.
4. Insert `publish_manifest` event.
5. Add input edges for child nodes and one output edge for the manifest node.
6. Upsert artifact/resource ref as `published`, pointing to the manifest node.
7. Insert ref audit row.
8. Commit atomically.
9. `verify_integrity()` must validate manifest schema, child node existence,
   duplicate child paths, and child digest/media-type/size consistency.
10. `verify_integrity()` must diagnose `publish_manifest` events without at
    least one `manifest_child:*` input edge or without exactly one `manifest`
    output edge.
11. `verify_integrity()` must diagnose `publish_manifest` events whose
    `manifest_child:*` input edges do not match the output manifest body's
    child path/node list.

Approval semantics:

- Evidence may derive directly from the manifest node.
- Or evidence may collectively cover every child node in the manifest.
- Evidence covering only a subset of children must not approve the manifest.

### approve

Must:

1. Resolve target ref to target node.
2. If `expected_version` is provided, require the current target ref version to
   match before lineage validation or policy events are written.
3. Deny self-review if the approving agent produced or published the target
   node; write a `review_separation` deny policy event before returning.
4. Resolve evidence refs to evidence nodes.
5. For each evidence node, verify that the target node is an ancestor of the
   evidence node in the lineage DAG.
6. Insert a `policy_decision` event with `decision="deny"` before returning if
   evidence does not cover the target.
7. If any evidence does not derive from target, raise `LineageMismatchError`.
8. Insert a `policy_decision` event with `decision="allow"` if validation passes.
9. Insert approve event with target and evidence edges.
10. Update target ref state to `approved`.
11. Insert ref audit row.
12. Commit atomically.
13. `verify_integrity()` must diagnose non-merge `policy_decision` events whose
    `evidence_refs` payload count does not match `policy_evidence:*` input
    edges, or that lack exactly one `policy_target` input edge.

### reject_ref

Must:

1. Resolve target ref to target node.
2. If `expected_version` is provided, require the current target ref version to
   match before writing the reject event.
3. Require a valid lifecycle transition to `rejected`; in the MVP this means
   `published -> rejected`.
4. Deny self-review if the rejecting agent produced or published the target
   node; write a `review_separation` deny policy event before returning.
5. Insert a `reject` event with optional reason payload.
6. Add input and output edges for the rejected target node.
7. Update target ref state to `rejected`.
8. Insert ref audit row.
9. Commit atomically.
10. `verify_integrity()` must diagnose reject events without exactly one
    `target` input edge or without exactly one `rejected_target` output edge.

Correct lineage query direction:

```text
start = evidence_node
required ancestor = target_node
```

In SQL terms, the recursive traversal walks from outputs back to inputs until it
finds the target node.

Lineage validation is a kernel invariant, not an agent responsibility. The agent
does not decide whether evidence is valid. The Rust core proves the relationship
inside the DAG before mutating the target ref.

### consume

Must:

1. Resolve target ref to current node.
2. Allow only `published` or `approved` refs.
3. Reject explicit purposes when active purpose capability rules require a
   capability the workspace agent does not hold.
4. Reject explicit purposes blocked by active purpose policy rules for the
   target ref, node, or node fragments.
5. Insert a `consume` event with agent, run, workspace, tool call, and payload
   containing `ref_name`, `node_id`, `state`, `ref_version`, and optional
   `purpose`.
6. Add an input edge from the consume event to the consumed node with role
   `consumed` and the consumed ref name as `logical_path`.
7. Return the consumed node id.
8. `verify_integrity()` must diagnose consume events without exactly one
   `consumed` input edge.

### search

Must:

1. Search the derived FTS index for refs in the requested lifecycle scope.
   `published` and `approved` scopes are global lifecycle views; `draft` scope
   is restricted to the caller workspace namespace.
2. Return matching node ids and snippets without changing refs or nodes.
3. Insert a `search` event with agent, run, workspace, query, scope, result
   count, and a `results` array containing each result's `ref_name`, `node_id`,
   lifecycle `state`, and `ref_version`.
4. Add input edges from the `search` event to returned result nodes using
   `search_result:<index>` roles and the matched ref name as `logical_path`.
5. Commit the search event atomically with the recorded result set.
6. `verify_integrity()` must diagnose search events whose `result_count`
   payload does not match the number of `search_result:*` input edges.

Search is a semantic event because retrieved context can influence later agent
outputs. It is not modeled as canonical source data and must not mutate blobs,
nodes, or refs.

### answer construction

Must:

1. Let callers write generated answers as immutable workspace nodes.
2. Require cited evidence refs and resolve them to current readable refs.
3. Reject citations whose current ref or node has an excluded active policy
   label.
4. Insert an `answer` event with agent, run, workspace, tool call, content hash,
   content size, citation count, and citation ref/node/version metadata.
5. Add input edges from the answer event to cited nodes using
   `answer_citation:<index>` roles and cited ref names as `logical_path`.
6. Add one output `result` edge to the answer node.
7. Optionally link the answer to prior same-workspace `query` / `search` event
   ids.
8. Reject retrieval event ids that do not exist, are not `query` / `search`, do
   not belong to the answer workspace, or do not cover every cited ref/node.
9. `verify_integrity()` must diagnose answer events whose `citation_count`
   payload does not match `answer_citation:*` input edges or whose output
   result edge is missing.
10. `verify_integrity()` must diagnose invalid or non-covering retrieval event
    ids in answer payloads.
11. Let callers inspect retrieval coverage for an existing answer through
    `answer_evidence_coverage(answer_event_id)`, returning one row per cited
    ref/node with a boolean coverage flag and count of covering retrieval
    events.
12. Record deterministic token-estimate accounting in the answer event payload
    and answer node metadata using the `ceil_bytes_div_4` estimator over answer
    bytes and cited-node bytes.
13. Let callers inspect that accounting through
    `answer_token_accounting(answer_event_id)`. Model-specific tokenizers and
    provider price-table freshness are intentionally outside the kernel, but
    callers can record append-only token cost profiles for prompt overhead and
    pricing estimates through `set_token_cost_profile(...)` and inspect
    derived costs with `answer_cost_estimate(answer_event_id, model)`.
14. `verify_integrity()` must diagnose missing, malformed, or inconsistent
    answer token-accounting payloads by recomputing expected estimates from the
    answer result node and cited input nodes.
15. Let callers inspect deterministic exact-quote support through
    `answer_quote_support(answer_event_id, min_quote_bytes=16)`, recomputing the
    longest exact byte overlap between the answer output node and each cited
    input node. Semantic entailment remains outside the kernel.

### read_ref

Must:

1. Resolve target ref to current node.
2. Allow `published` and `approved` refs globally.
3. Allow `draft` refs only when the ref belongs to the caller workspace
   namespace.
4. Reject other lifecycle states and cross-workspace draft reads with
   `PolicyDeniedError`.
5. Reject explicit purposes when active purpose capability rules require a
   capability the workspace agent does not hold.
6. Reject explicit purposes blocked by active purpose policy rules for the
   target ref, node, or node fragments.
7. Return bytes for the resolved node.
8. Insert a `read_ref` event with agent, run, workspace, tool call, and payload
   containing `ref_name`, `node_id`, `state`, `ref_version`, and optional
   `purpose`.
9. Add an input edge from the `read_ref` event to the read node with role
   `read_ref` and the read ref name as `logical_path`.
9. `verify_integrity()` must diagnose read events without exactly one
   `read_ref` input edge.

### selective node reads

Must:

1. Let callers read a byte range from an immutable node through
   `read_node_range(...)`.
2. Reject negative offsets and non-positive lengths.
3. Return an empty byte string when the requested offset is beyond EOF.
4. For file-backed blobs, use seek/read rather than whole-file reads.
5. Let callers derive chunk metadata through `node_chunks(...)`.
6. Return chunk index, offset, size, and sha256 for each chunk.
7. Treat chunk rows as derived metadata, not canonical storage state.
8. Let callers persist a derived chunk index through `cache_node_chunks(...)`
   and read it without recomputation through `cached_node_chunks(...)`.
9. `verify_integrity()` must diagnose persisted chunk indexes whose blob
   metadata, row count, offsets, sizes, or sha256 values diverge from canonical
   node bytes.
10. Let callers rebuild derived indexes through `rebuild_derived_indexes()`,
    reconstructing `node_fts` from canonical bytes and clearing persisted chunk
    indexes without mutating canonical state.
11. Let callers attach byte-range policy labels to immutable nodes through
    `set_fragment_policy_label(...)`.
12. Reject invalid fragment ranges before writing label events.
13. Apply active `visibility` / `deny` rules over fragment label values to
    overlapping `read_node_range(...)` calls.
14. Conservatively block whole-node projections when a node contains an active
    denied fragment.
15. `verify_integrity()` must diagnose malformed fragment-label audit rows,
    invalid ranges, missing subject edges, and orphan `fragment_policy_label`
    events.
16. Let callers attach local embedding vectors to persisted chunk rows through
    `set_node_chunk_embedding(...)`; reject embeddings for chunks that are not
    present in `node_chunk_index`.
17. Let callers search chunk embeddings through `vector_search_chunks(...)`,
    returning the ref, node, chunk index, byte range, and cosine score.
18. Apply ref/node visibility policy and fragment-range visibility policy to
    chunk vector search results.
19. `verify_integrity()` must diagnose malformed chunk embedding rows, missing
    cached chunk targets, invalid vectors, and stale norms.
16. Propagate active fragment policy labels to newly written artifact nodes
    whose bytes exactly match an existing labeled node, recording independent
    fragment-label audit events for the new node.
17. Propagate active node labels from explicit `derived_from_nodes` to new
    output nodes.
18. Propagate active fragment labels from transformed `derived_from_nodes` to
    full-output fragment labels on new output nodes.
19. Treat answer citation nodes as derived inputs for policy propagation.
20. Let callers propagate active source fragment labels to precise output byte
    ranges through `propagate_fragment_policy_labels(...)` when the
    source-to-output attribution is known.
21. Let callers conservatively propagate active source fragment labels through
    `auto_propagate_fragment_policy_labels(...)` when a labeled source byte span
    appears exactly once in an output node.
22. Let callers conservatively propagate active source fragment labels through
    `auto_propagate_fragment_policy_labels_by_normalized_scalar(...)` when a
    labeled JSON string, boolean, null, or finite number scalar has a unique
    normalized occurrence in an output node.
23. Let callers conservatively propagate active source fragment labels through
    `auto_propagate_fragment_policy_labels_by_normalized_json(...)` when a
    labeled JSON scalar, array, or object has a unique normalized/minified JSON
    value occurrence in an output node.
24. Let callers derive JSON field spans through `json_field_spans(...)`.
25. Let callers attach field-level policy labels to JSON fields through
    `set_json_field_policy_label(...)`, which resolves a JSON path into a
    byte-range fragment label.
26. Let callers derive Markdown frontmatter field spans through
    `markdown_field_spans(...)`, including top-level scalar fields, indented
    nested scalar fields, conservative parent object spans, scalar sequence item
    paths from block or inline sequences such as `frontmatter.tags[0]`, block
    sequence object item fields such as `frontmatter.people[0].name`, inline
    sequence object item fields such as `frontmatter.contacts[0].email`, inline
    object scalar fields such as `frontmatter.contact.email`, nested inline
    object fields such as `frontmatter.contact.manager.email`, nested inline
    array item paths such as `frontmatter.contacts[0].meta.flags[1]`,
    quoted-key bracket paths such as `frontmatter["owner.email"]`, alias token
    spans such as `frontmatter.alias_owner` with kind `alias`, conservative
    top-level inline-object alias expansion paths such as
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
    tags such as `!!set {read: null}` and `!!omap [{first: one}]`,
    conservative unquoted ISO timestamp scalars such as `2026-06-09`, quoted
    scalar payload spans that exclude quote delimiters while remaining kind
    `string` unless an explicit core tag overrides the kind, and `|` / `>`
    block scalar spans. Conservative value parsing must ignore leading YAML
    tag/anchor decorations such as `!pii &primary value` when computing payload
    spans. Alias and merge expansion spans must use the alias token byte range
    rather than inventing expanded bytes; scalar alias target paths point at the
    anchored scalar payload bytes.
27. Let callers derive Markdown frontmatter semantic values through
    `markdown_field_values(...)` over the same conservative paths as
    `markdown_field_spans(...)`. It must preserve span offsets for fragment
    policy use while decoding single-quoted doubled apostrophes and common YAML
    double-quoted escapes for scalar values, without claiming full YAML parsing
    or scalar normalization.
28. Let callers attach field-level policy labels to Markdown frontmatter fields
    through `set_markdown_field_policy_label(...)`, which resolves a
    `frontmatter.<key>`, nested `frontmatter.<parent>.<key>`, quoted-key
    bracket path, or supported sequence path into a byte-range fragment label.
29. Let callers derive Markdown body ATX/Setext heading section spans and
    conservative paragraph/paragraph-line/inline-strong/inline-strong-text/
    inline-emphasis/inline-emphasis-text/inline-link/
    inline-link-label/inline-link-destination/inline-link-title/reference-link/
    reference-link-label/reference-link-reference/
    reference-link-resolved-destination/reference-link-resolved-title/
    inline-image/inline-image-alt/
    inline-image-destination/inline-image-title/reference-image/reference-image-alt/
    reference-image-reference/reference-image-resolved-destination/
    reference-image-resolved-title/inline-code/
    autolink/autolink-target/list/list-item/task-checkbox/blockquote/
    blockquote-line/table/table-align/table-row/table-cell/code/html/
    link-reference/link-reference-label/link-reference-destination/
    link-reference-title/thematic-break body block spans through
    `markdown_section_spans(...)`, while suppressing current conservative
    inline spans for backslash-escaped delimiter bytes and supporting
    same-line matching inline-code backtick runs plus balanced unescaped
    parentheses inside direct inline link/image destinations, angle-bracketed
    direct inline link/image and link-reference-definition destination payload
    spans, and trailing quoted or parenthesized direct inline/reference titles.
    Reference label matching must normalize backslash-escaped ASCII punctuation
    while preserving raw label byte spans. Duplicate link-reference definitions
    must resolve to the first normalized definition while preserving raw spans
    for later duplicate definitions. Link-reference definitions must support
    immediate next-line title-only continuations for quoted or parenthesized
    titles.
30. Let callers attach policy labels to Markdown body heading sections and body
    blocks through `set_markdown_section_policy_label(...)`, which resolves a
    `body.<slug>` or `body.<slug>.<block_kind>.<n>` path into a byte-range
    fragment label.

### delete / archive

Delete is a logical view operation, not immediate physical erasure.

For a workspace path delete, the kernel must:

1. Resolve the workspace ref.
2. Insert a lightweight delete/archive event if the ref represents meaningful
   workflow state.
3. Mark the ref as `archived` or `deleted` according to the lifecycle model, or
   remove it only if replay semantics are explicitly not required for that
   namespace.
4. Insert a `ref_events` audit row.
5. Commit atomically.
6. `verify_integrity()` must diagnose `delete_ref` events without exactly one
   `deleted_ref` input edge.

For an engine-level archive, `verify_integrity()` must diagnose `archive_ref`
events without exactly one `archived_ref` input edge.

The underlying node and blob remain immutable facts until controlled garbage
collection proves they are not reachable from any retention root.

Physical object deletion is not part of normal agent operations. It belongs to a
maintenance/GC subsystem.

### garbage collection

Must:

1. Treat published and approved refs as live roots.
2. Treat workspace draft refs as live roots by default.
3. Walk lineage ancestry from live roots before declaring a blob unreachable.
4. Provide `gc_candidates(...)` as read-only analysis.
5. Provide `compaction_plan()` as read-only operational planning over event/ref
   history size, inactive refs, unreachable blobs, active pins, active
   retention policies, orphan object files, derived projection rows, and SQLite
   freelist pages.
6. Provide `vacuum_database(dry_run=True)` as a narrow SQLite freelist
   maintenance path with dry-run enabled by default.
7. `vacuum_database(dry_run=False)` may run SQLite `VACUUM`, but must not
   archive history, mutate canonical rows, repair integrity issues, or compact
   inline blobs.
8. Provide `collect_garbage(..., dry_run=True)` with dry-run enabled by default.
9. Physically collect only unreachable file-backed blobs.
10. Verify object hash before removing any file-backed blob.
11. Record physical collection in an append-only `gc_blob_events` table and a
   `gc_collect` event.
12. Keep immutable blob/node/event metadata even after object bytes are
   collected.
13. `verify_integrity()` must diagnose `gc_collect` events with missing or
    invalid `candidate_count` payloads.
14. `verify_integrity()` must diagnose `gc_collect` events whose physical
    `gc_blob_events` audit rows exceed the recorded candidate count.
15. `verify_integrity()` must diagnose `gc_blob_events` rows that reference a
    non-`gc_collect` event.
16. Skip inline blobs because their bytes live inside immutable SQLite rows.
17. Make `verify_integrity()` aware of GC-collected file blobs so expected
   missing object files are not reported as corruption.
18. A historical `gc_blob_events` row is an audit fact, not a permanent CAS
    tombstone. If the same hash is physically materialized again, reads and
    integrity verification must use the current object file.
19. Support retention windows with `older_than_ms`; only collect an unreachable
    blob when every node that references it was created at or before the cutoff.
20. Support bounded GC batches with `limit`; dry-run and physical collection
    must apply the same ordered candidate limit.
21. Provide `clean_orphan_objects(dry_run=True, limit=None)` as a noncanonical
    object-store maintenance path. It may delete only hash-shaped object files
    under `objects_dir` that are not owned by file-backed `blobs` metadata, and
    must verify size and SHA-256 hash before removal. It must not mutate
    SQLite rows, refs, nodes, blobs, events, or audit history.
22. Verify that each current mutable `refs` row matches the latest append-only
    `ref_events` audit row for that ref.
23. `pin_ref(...)` must record a `gc_pin` event, a `gc_pin_events` audit row,
    and one typed `gc_pin_root` input edge for the pinned node/ref.
24. `unpin_gc_root(...)` must record a `gc_unpin` event, a `gc_pin_events`
    audit row, and one typed `gc_unpin_root` input edge for the unpinned
    node/ref.
25. `verify_integrity()` must diagnose GC pin audit rows whose action does not
    match the referenced event kind or whose typed root edge is missing.
26. `verify_integrity()` must diagnose `gc_pin` / `gc_unpin` events without
    exactly one `gc_pin_events` audit row.
27. `set_retention_policy(...)` must record append-only retention policy events
    with enabled/disabled state, workspace inclusion, optional minimum age, and
    optional bounded batch size.
28. `retention_policies(active_only=True)` must derive enabled policies from
    event sequence order.
29. `run_retention_policy(..., dry_run=True)` must compute the current cutoff
    from `min_age_ms` and delegate to the same bounded `collect_garbage(...)`
    executor, preserving dry-run by default.
30. Disabled retention policies must not run.
31. `verify_integrity()` must diagnose retention policy audit rows with invalid
    effect, invalid limits, invalid age, wrong event kind, or missing audit row.

### workspace diff

Must:

1. Interpret `base` as a ref namespace prefix.
2. Interpret `workspace` as a workspace ref namespace prefix.
3. Map refs in both namespaces to logical paths.
4. Return stable sorted entries with one of:
   - `added`
   - `modified`
   - `deleted`
   - `unchanged`
5. Never mutate refs, nodes, blobs, or events.

This is the comparison primitive needed before merge/review/publish workflows.

### workspace conflicts

Must:

1. Persist checkout-time base refs when `checkout(base, workspace)` runs.
2. Compare three views:
   - checkout base snapshot;
   - current base namespace;
   - current workspace namespace.
3. Report a conflict only when both workspace and current base changed relative
   to the checkout base, and they do not converge to the same node.
4. Never mutate storage.

This is the conflict detection primitive needed before merge.

### working tree compatibility

Must:

1. Let callers materialize active workspace refs into a normal directory through
   `materialize_workspace(...)`.
2. Write a `.anfs-worktree-manifest.json` manifest with workspace, path, ref,
   node, and size metadata.
3. Reject unsafe relative paths and policy-excluded refs/nodes before writing a
   materialized worktree.
4. Let callers materialize a managed cached working set through
   `cache_materialized_workspace(...)`.
5. Cache metadata must include cache key, workspace, output directory, manifest
   hash, policy exclusion labels, file count, timestamp, and per-file
   path/ref/node/size rows.
6. Reuse an existing cached working set when the current workspace view and
   cache output directory still match the stored manifest hash.
7. Reject stale or unmanaged cache directories unless `overwrite=True`.
8. `verify_integrity()` must diagnose cached working sets whose file counts,
   output manifests, materialized files, or bytes diverge from recorded nodes.
9. Let callers edit files with ordinary tools and commit the directory through
   `commit_worktree(...)`.
10. Detect `added`, `modified`, `deleted`, and `unchanged` files by comparing
   ordinary directory contents with current workspace refs.
11. Write added/modified files through normal workspace `write` semantics.
12. Preserve lineage for modified files by deriving from the previous workspace
   node.
13. Mark missing files as deleted when `delete_missing=True`.
14. Enforce the same active ref/node policy label gate as query, search, answer,
   replay, and bundle export.
15. Reject symlinks and other special filesystem entries during worktree
    readiness/commit instead of following them, so ordinary-directory
    compatibility cannot import bytes outside the materialized tree through
    link traversal. Reject relative worktree paths containing `\` to avoid
    cross-platform path ambiguity.
16. Treat the root `.anfs-worktree-manifest.json` path as reserved adapter
    metadata: ignore it during local worktree scans and reject it as a
    materialized or committed workspace user file.
17. When `require_manifest_match=True`, require
    `.anfs-worktree-manifest.json` to match the current workspace node ids
    before applying any worktree mutation.
18. A checked worktree commit must reject stale materialized bases when a
    current path changed, disappeared, or appeared after materialization.
19. Commit the current workspace read, optional manifest lease check,
    added/modified writes, deleted refs,
    ref audit rows, event edges, and grouped commit event in one SQLite
    transaction so canonical state cannot partially advance.
    Concurrent checked commits from the same materialized base must allow at
    most one base to advance and must reject stale losers before mutation.
20. Insert a grouped `worktree_commit` event with result count, changed count,
    edge count, per-path status metadata, and typed `worktree_*` edges.
21. `verify_integrity()` must diagnose grouped worktree commit events whose
    payload counts do not match result metadata or typed edge counts.
22. The POSIX-like workspace facade must provide `touch(path)` as a
    compatibility helper that creates a missing empty regular file through
    normal workspace `write` semantics, returns an existing regular file node
    without mutating metadata, and rejects directory paths. Explicit timestamp
    changes are handled by `utime(...)`.
23. The POSIX-like workspace facade must provide `read_range(path, offset,
    length, purpose=None, tool_call_id=None)` as a path-level `pread`-style
    compatibility helper. It must reject directories, preserve existing
    readability and purpose-capability checks, enforce visibility and purpose
    fragment policies only when the requested byte range overlaps the active
    fragment label, return empty bytes for ranges beyond EOF, and record an
    audited `read_range` event with path/ref/node/range metadata.
24. The POSIX-like workspace facade must provide `write_range(path, offset,
    content, tool_call_id=None)` as a path-level `pwrite`-style compatibility
    helper. It must reject directories, reject explicit non-workspace refs,
    allow append at EOF, zero-fill writes beyond EOF without adding sparse-hole
    state, preserve lineage from the previous node, create a new immutable CAS
    node for changed bytes, and record an audited `write_range` event with
    path/ref/old-node/range metadata.
25. The POSIX-like workspace facade must provide `append(path, content,
    tool_call_id=None)` as an append-at-EOF compatibility helper. It must reject
    directories and explicit non-workspace refs, create a missing file when no
    path exists, preserve lineage from the previous node when appending to an
    existing file, create a new immutable CAS node for appended bytes, and
    record an audited `append` event with path/ref/old-node/length metadata.
26. The POSIX-like workspace facade must provide `truncate(path, length,
    tool_call_id=None)` as a path-level truncate compatibility helper for
    existing regular files. It must reject directories, explicit non-workspace
    refs, missing paths, and negative lengths; preserve lineage from the
    previous node; shrink files or extend them with NUL bytes; create a new
    immutable CAS node for changed bytes; record an audited `truncate` event
    with path/ref/old-node/old-length/new-length metadata; and avoid adding
    sparse-hole or mutable block state.
27. The POSIX-like workspace facade must provide `exists(path)`,
    `is_file(path)`, and `is_dir(path)` as metadata convenience helpers over
    `stat(...)` semantics. Missing or deleted paths must return `False`; other
    stat errors must still propagate.
28. The POSIX-like workspace facade must provide `stat_posix(path)` as a
    derived adapter view over `stat(...)` with synthetic mode, nlink, uid, gid,
    inode, and atime/mtime/ctime fields. Mode, ownership, and time values may
    use workspace path metadata overlays created by `chmod(...)`, `chown(...)`,
    and `utime(...)`; file contents, path existence, and node identity must
    remain based on existing refs and immutable nodes. For active workspace
    file refs that point at the same immutable node, `stat_posix(...)` may
    expose the shared node identity as a stable inode seed and use the count of
    those refs as the file link count. File metadata overlays must be inherited
    by new `link(...)` paths and synchronized across active linked refs while
    they still point at the same immutable node.
29. The POSIX-like workspace facade must provide
    `access(path, mode, uid=None, gid=None, groups=None)` as an
    `os.access`-style derived adapter predicate for `F_OK`, `R_OK`, `W_OK`, and
    `X_OK` masks. It must report missing paths as `False`, reuse current
    readability and active fragment policy for read checks, use workspace path
    mutability and POSIX mode bits for write/execute checks, support optional
    non-negative effective uid/gid/group ids for owner/group/other mode-bit
    selection, support effective uid `0` as a root-style read/write mode-bit
    bypass while still requiring some execute bit for `X_OK`, and avoid adding
    ACL, OS process impersonation, or OS sandbox semantics.
30. The POSIX-like workspace facade must provide `chmod(path, mode,
    tool_call_id=None)` as an audited workspace path metadata update. It must
    reject explicit refs, preserve immutable node/blob contents, affect
    `stat_posix(...)` and `access(...)`, synchronize active linked file paths
    that still point at the same immutable node, preserve POSIX special mode bits
    such as setuid/setgid/sticky in `stat_posix(...)` while `access(...)`
    continues to evaluate owner/group/other rwx bits, apply setgid directory
    inheritance to newly created regular files, touched files, and subdirectories
    by inheriting the parent gid and preserving setgid on subdirectories, and
    avoid claiming full POSIX ACL, descriptor, setgid execution, or kernel syscall
    fidelity.
31. The POSIX-like workspace facade must provide `chown(path, uid, gid,
    tool_call_id=None)` as an audited workspace path metadata update. It must
    support `-1` as preserve-existing for uid or gid, reject explicit refs,
    reject unsupported negative values, reject a no-op `(-1, -1)` update,
    preserve immutable node/blob contents, affect `stat_posix(...)`,
    synchronize active linked file paths that still point at the same immutable
    node, and avoid claiming OS user/group impersonation or ACL semantics.
    `access(...)` may use the stored uid/gid fields only when explicit effective
    ids are supplied by the caller.
32. The POSIX-like workspace facade must provide `utime(path, atime_ms=None,
    mtime_ms=None, tool_call_id=None)` as an audited workspace path metadata
    update. It must reject explicit refs and negative timestamps, preserve
    immutable node/blob contents, affect `stat_posix(...)`, set both atime and
    mtime to current kernel time when both timestamp arguments are omitted, and
    synchronize active linked file paths that still point at the same immutable
    node while avoiding full POSIX atime-on-read or kernel syscall fidelity.
33. The workspace facade must provide audited exclusive ANFS path leases through
    `acquire_lock(path, ttl_ms=None, tool_call_id=None)`,
    `release_lock(path, lock_id, tool_call_id=None)`, and `lock_info(path="")`.
    Active leases must be owned by a workspace agent/run, expire by TTL, block
    other agents/runs from mutating covered paths and directory subtrees, and
    reject non-owner release attempts. This is an agent coordination primitive,
    not POSIX descriptor-level `fcntl`/`flock`, open-handle, mmap, ACL, or FUSE
    syscall fidelity.
34. The POSIX-like workspace facade must provide `link(src_path, dst_path,
    tool_call_id=None)` as an audited hard-link-like workspace ref operation.
    It must reject directory sources, explicit destination refs, workspace root
    destinations, and existing destination paths. It must create no new content
    node, must point the destination ref at the same immutable source node, must
    inherit existing source path metadata overlays when the source is a
    workspace path, must record input/output event edges, and must let later
    writes to either path create a new immutable node for that path rather than
    claiming full mutable POSIX inode sharing.
35. The POSIX-like workspace facade must support optional effective
    `uid/gid/groups` on `delete(...)`, `rm(...)`, and `mv(...)`. When no
    effective identity is supplied, existing agent-tool delete/move behavior must
    remain backward compatible. When effective ids are supplied and the source
    parent directory or replaced destination parent directory has sticky mode bit
    `0o1000`, deletion or replacement must be allowed only for effective uid `0`,
    the sticky parent directory owner, or the entry owner. This must use existing
    workspace path metadata overlays and must not claim full OS credential,
    descriptor, ACL, or FUSE syscall semantics.
36. The POSIX-like workspace facade must make `cp(src_path, dst_path, ...)` and
    `mv(src_path, dst_path, ...)` treat an active destination directory as a
    container: regular files copy/move to `dst_path/<source-basename>`, and
    recursive directory copy/move operations copy/move the source directory tree
    under `dst_path/<source-basename>`. This must reuse existing ref/event audit,
    path lease, sticky-directory, and immutable-node behavior rather than adding
    FUSE syscall or lazy namespace state.
37. Keep merge-to-base as a separate conflict-checked operation; worktree commit
    updates the workspace, not the published base namespace.

### merge workspace

Must:

1. Check workspace conflicts before mutating base refs.
2. Insert a `policy_decision` event with `decision="deny"` and reject the whole
   operation with `RefConflictError` if conflicts exist.
3. Insert a `policy_decision` event with `decision="allow"` if there are no
   conflicts.
4. Use checkout base snapshot, not current-base diff alone, to decide which
   workspace paths changed.
5. Apply changes atomically:
   - `added`: create base ref;
   - `modified`: update base ref node;
   - `deleted`: archive base ref.
6. Insert a `merge_workspace` event.
7. Insert event edges for merged nodes.
8. Insert `ref_events` for every base ref mutation.
9. Never mutate blobs, nodes, events, event edges, or checkout base snapshots.
10. `verify_integrity()` must diagnose `workspace_merge` policy decisions whose
    `conflict_count` or `change_count` payloads are not supported by
    `merge_conflict:*` / `merge_change:*` input edges.
11. `verify_integrity()` must diagnose `merge_workspace` events whose ref audit
    rows do not match `merge_output:*` and `merge_deleted:*` edge counts, or
    whose `merge_input:*` and `merge_output:*` counts diverge.
12. `verify_integrity()` must diagnose each `merge_workspace` ref audit row
    whose audited ref/node is not backed by a matching `merge_output:*` edge for
    published changes or `merge_deleted:*` edge for archived deletions.

If `base` is a published or approved snapshot manifest ref, merge must use the
manifest children as the checkout base view and must apply mutations to the
source namespace recorded in the snapshot manifest metadata. It must not create
or mutate child refs under the snapshot ref name itself.

### observability queries

Must:

1. Let callers inspect a ref's lifecycle through `ref_history(ref_name)`.
2. Order ref history by audit insertion order, not wall-clock time, because
   multiple events can share the same millisecond timestamp.
3. Let callers inspect a specific event through `event(event_id)`.
4. Return event metadata and typed `event_edges` together.
5. Let callers reconstruct the latest ref view at or before an event through
   `ref_view_at_event(event_id, prefix=None, include_inactive=False,
   policy_label_excludes=None)`.
6. Filter `deleted` and `archived` refs from time-travel views by default.
7. Let callers materialize a reconstructed ref view into a normal directory
   through `materialize_ref_view_at_event(...)`.
8. Reject unsafe materialized paths such as absolute paths, `.`, `..`, empty
   segments, and backslash-containing segments that could become path separators
   on another platform.
9. Write a replay manifest in the output directory mapping relative paths back
   to ref names, node ids, sizes, event id, and prefix.
10. Reject materialization if two refs would produce the same relative output
    path.
11. Reject existing materialized target files and replay manifests by default;
    allow replacement only when `overwrite=True` is explicitly passed. Overwrite
    means replacing the replay view, so stale files from a previous
    materialization must not remain even when `atomic=False`.
12. Stage new replay directories and explicit overwrites in a temporary sibling
    directory before publishing when `atomic=True`.
13. Let callers export a portable event bundle containing causal events, edges,
   nodes, ref audit rows, blob metadata, and optional CAS object bytes.
14. Let callers import a portable event bundle into a fresh or compatible ANFS
   database.
15. Import should restore immutable evidence tables and `ref_events`, but should
   not create live `refs` automatically.
16. Import into a non-empty database must reject existing event-edge or
   ref-event rows that share the imported primary key but differ in content.
17. Raise `EventNotFoundError` for missing events.
18. Never mutate ANFS metadata during read-only replay, materialization, or
   bundle export.
19. Import must reject bundles whose internal references are not closed:
   `root_event_id` must exist in `events`, event edges must reference bundled
   events and nodes, ref audit rows must reference bundled events and nodes, and
   nodes must reference bundled blobs.
20. Import must rebuild derived local indexes such as `node_fts` from immutable
   blob bytes for searchable nodes, so a valid imported bundle verifies cleanly
   without treating FTS as canonical bundle payload.
21. Metadata-only bundles exported with `include_blobs=False` may be imported
   into a compatible database only when the destination already has matching
   CAS blobs by hash and size; fresh databases still require object bytes.
22. Exported bundle events must include their source `event_sequence.seq`.
   Import must validate non-duplicated positive source sequences and preserve
   the source relative event order when assigning destination `event_sequence`
   rows. Legacy bundles without `source_seq` remain array-order compatible.
23. Import with `policy_label_excludes` must reject bundles whose included
    node ids or ref-audit ref names are currently blocked by active destination
    labels, before writing canonical rows or object bytes.
24. Export with `signing_key` must add an HMAC-SHA256 signature over the
    canonical bundle metadata payload used for `bundle_checksum`.
25. Import with `signature_key` must verify a present HMAC-SHA256 signature
    before writing canonical rows or object bytes.
26. Import with `require_signature=True` must reject unsigned bundles before
    writing canonical rows or object bytes.
27. Import with `require_signature=True` must require `signature_key`; merely
    observing that a signature field exists is not authenticated provenance.

## MVP Killer Demo

The first acceptance test must prove this sequence:

1. Coder writes and publishes patch v1.
2. Tester writes and publishes test result v1 derived from patch v1.
3. Coder writes and publishes patch v2.
4. Reviewer attempts to approve patch v2 using test result v1.
5. Rust core raises `LineageMismatchError`.

## Stability Requirements

- All multi-table mutations must happen inside transactions.
- Failed operations must leave no partial state.
- SQLite WAL mode must be enabled.
- Foreign keys must be enabled.
- Event ordering must be explicit through `event_sequence`.
- Ref updates must be auditable through `ref_events`.
- The Rust API must avoid exposing raw mutation hooks for immutable tables.
- Errors must be typed kernel error codes exposed meaningfully at the Python
  boundary.

## Kernel Error Taxonomy

Errors are not business logic delegated to the agent. They are bottom-layer
rejections of invalid state transitions. The Rust core should use an internal
`AnfsError` enum and PyO3 should expose matching Python exception classes.

Minimum MVP errors:

- `LineageMismatchError`: evidence does not derive from the target node.
- `RefConflictError`: a mutable ref changed since the caller's expected version.
- `InvalidStateTransitionError`: lifecycle transition is forbidden.
- `RefNotFoundError`: requested ref does not exist.
- `NodeNotFoundError`: requested node does not exist.
- `EventNotFoundError`: requested event does not exist.
- `PolicyDeniedError`: policy engine rejected the operation.
- `StorageCorruptionError`: metadata and object storage integrity disagree.

## Test Requirements

Minimum tests:

- Blob deduplication by hash.
- Inline vs file blob storage threshold.
- Write creates node, event, edge, draft ref, and ref audit.
- Publish creates event, published ref, and ref audit.
- Approve succeeds when evidence derives from target.
- Approve fails when evidence does not derive from target.
- Ref mutation history is reconstructable through `ref_events`.
- Event metadata and edges are queryable through `event(event_id)`.
- Namespace views are reconstructable at a specific event through
  `ref_view_at_event(event_id, prefix)`.
- Namespace views are materializable into a normal directory for replay/debug.
- Causal event bundles are exportable with event, edge, node, ref audit, and
  blob evidence.
- Event and run bundle export can preflight active ref/node policy label
  exclusions and reject the export before writing bundle files.
- Exported event bundles include a metadata checksum and tampered checked
  bundles are rejected during import.
- Exported event bundles can include an HMAC-SHA256 metadata signature, and
  import can require and verify that signature before writing bundle contents.
- Run bundles are exportable with every event in the run and the causal upstream
  evidence those events depend on.
- Search-as-context records are auditable through `event(event_id)` and
  `events(kind="search")`.
- Multiple engine handles can write events concurrently without duplicate or
  gapped event sequence numbers under normal SQLite writer-lock scheduling.
- Causal event bundles are importable into a fresh ANFS database and remain
  replayable from `ref_events`.
- Reopening the same DB preserves all state.

## Future Requirements

Not required for MVP, but schema and API should avoid blocking:

- Snapshot export/import.
- Workspace branch diff.
- Policy engine beyond lineage checks.
- Richer policy decision schema is implemented for current policy decisions
  through stable `reason_code` values.
- Semantic embedding index as derived artifacts.
- Tool-call-level trace integration.
- Multi-process writer stress tests.
- Remote object store.
- Distributed artifact registry.
