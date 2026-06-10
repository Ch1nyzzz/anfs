# ANFS Complexity Audit

This document answers whether ANFS is becoming too redundant or complex while
still trying to satisfy agent-native filesystem requirements.

Source requirement context: Sarah Catanzaro, "File systems for agents",
Amplify Partners, May 27, 2026.
https://www.amplifypartners.com/blog-posts/file-systems-for-agents

## Verdict

ANFS is broad, but the current architecture is not yet unjustifiably
redundant. The system becomes too complex only if projections and adapters begin
creating new canonical truth.

The kernel should stay small enough to own:

- content identity;
- ordered mutations;
- copy-on-write workspace boundaries;
- provenance and policy propagation;
- integrity and admin safety preconditions.

Everything else should remain a projection, adapter, benchmark, or external
service.

## Why The Breadth Exists

The Amplify filesystem-for-agents argument points at a specific bundle of needs:
agents want familiar file/path operations, low-latency small-file loops,
concurrent coordination, working-set materialization, query over unstructured
files, dynamic execution-aware policy, and efficient access to larger
collections.

Those needs force ANFS to expose more than a normal local filesystem, but they
do not require every capability to become kernel state. The compatibility
surface can stay file-like while the semantic kernel remains responsible only
for invariants that ordinary filesystems do not prove.

## Redundancy Review

| Area | Looks Redundant Because | Current Decision | Complexity Rule |
| --- | --- | --- | --- |
| POSIX-like workspace API, ordinary worktree sync, and future FUSE | They all expose file operations. | Keep all upper surfaces compatible, but make ordinary worktree sync the proven adapter first. FUSE should be a mount adapter over existing semantics, not a new truth source. | No FUSE-specific canonical state unless POSIX metadata becomes part of replay or conflict detection. |
| `search`, `query`, FTS, embeddings, chunk embeddings, and benchmark vector baselines | They all retrieve context. | Keep retrieval indexes as projections. FTS/chunk/vector rows are allowed only because they are rebuildable or caller-supplied derived data. | Managed vector DBs, ANN services, ranking models, and embedding calls stay outside the kernel. |
| bundles, archive export, replay checkpoints, readiness plans, and future compaction | They all relate to portability/history. | Keep them as a safety chain: export proves history closure, checkpoint proves replay boundary, readiness proves whether physical compaction is safe. | Do not physically delete canonical event/ref history until archive and checkpoint verification are both explicit and passing. |
| policy labels, fragment labels, rule events, purpose gates, and capability rules | Policy has several append-only audit tables. | Keep the complexity because policy must compose through query, materialization, export/import, worktree sync, answer creation, and derived outputs. | Centralize enforcement and avoid parallel active-policy stores unless one side is an immutable audit and the other is rebuildable. |
| answer evidence, token accounting, quote support, and cost profiles | They are answer-adjacent features. | Treat them as audit projections over generated artifacts. Cost profiles are caller-supplied adapter facts, not provider integrations. | No model generation, prompt template system, live pricing sync, or provider tokenizer logic in the kernel. |
| benchmarks and baselines | They add many formats and workflows. | Keep them out of canonical state. They prove requirements and compare alternatives. | Benchmark-only schemas and task-specific memory extraction must not become kernel tables. |

## Complexity Failure Signals

Stop and redesign if a proposed feature does any of the following:

- adds canonical state that is not needed for replay, policy enforcement,
  conflict detection, durable provenance, or integrity verification;
- stores the same durable fact in two places where neither side is explicitly
  rebuildable;
- gives adapters such as FUSE, vector DBs, model calls, or benchmark baselines
  different semantics from the Python/Rust kernel API;
- mutates canonical history automatically without a separately verifiable
  archive, checkpoint, and readiness proof;
- expands policy with another enforcement path instead of routing through the
  shared visibility/purpose gates.

## Simplification Backlog

These are the places where ANFS can get simpler without weakening the
agent-native proof:

1. ~~Replace transitional `use super::*` imports in `engine.rs` and
   `workspace.rs` with explicit imports once the API surface settles.~~
   Done: both files now use explicit imports; `lib.rs` no longer leaks
   incidental std/pyo3 imports through the glob.
2. Split large review-heavy modules such as `engine.rs`, `policy_labels.rs`,
   `workspace.rs`, `bundle.rs`, and `worktree.rs` only when the split improves
   reviewability without changing public Python behavior. Done so far:
   `verify_integrity` is now a 31-line orchestrator over 25 `check_*` helpers,
   and the conservative Markdown/YAML/JSON span parser moved from
   `manifest.rs` into `span_parser.rs` (manifest.rs keeps blob/chunk IO and
   the field-span API).
3. Keep answer/token/cost/model-adjacent behavior in projection modules and
   avoid pulling provider-specific behavior into canonical state.
4. Keep FUSE evaluation behind the ordinary worktree conformance proof; FUSE
   should reuse `read`, `write`, `commit`, policy, and conflict semantics.
5. Treat physical archival compaction as an admin operation that consumes
   archive/checkpoint/readiness proofs instead of becoming background magic.
6. Policy enforcement is centralized: every fragment/purpose policy deny or
   filter outside `policy_labels.rs` routes through the `ensure_*` /
   `*_hidden` gates in `visibility.rs`. New read or materialization paths must
   use those gates instead of calling the policy predicates directly.
7. Shared kernel helpers exist for previously copy-pasted patterns:
   `require_ref` (refs.rs), `upsert_workspace_path_metadata` (workspace.rs),
   and the single `with_sqlite_busy_retry` in `common.rs`. Reuse them instead
   of reintroducing inline variants.

## Test Design

The proof that ANFS is agent-native should stay executable and requirement
mapped:

- file/path compatibility: materialize a normal directory, edit it with
  ordinary filesystem tools, and commit it back;
- low-latency small files: benchmark repeated small reads/writes/searches
  against native filesystem and JSONL baselines;
- concurrency: run multi-process writers against shared refs and prove one
  lifecycle decision commits while stale writers receive conflicts;
- copy-on-write: checkout, fork, edit, and merge workspace refs without leaking
  draft changes;
- query/materialization: record search/query events with exact result edges and
  materialize replay views at event boundaries;
- dynamic policy: label whole refs/nodes and byte ranges, then prove denied
  labels block search, export/import, replay materialization, worktree sync, and
  conservative semantic frontmatter paths including alias/merge chains;
- derived data policy: create answers and derived writes that inherit source
  labels and expose evidence coverage;
- large files: prove range reads, chunk maps, cached chunks, and chunk-level
  projections do not change canonical node/blob identity;
- portability/reset/fork: export bundles, verify full-history archives, create
  replay checkpoints, and run readiness plans before any physical compaction;
- integrity: finish every conformance scenario with `verify_integrity() == []`.

The current proof lives in `docs/08_agent_native_fs_conformance.md` and
`tests/` (one module per requirement area; see `tests/test_conformance.py` for the conformance proof). New features should add to that proof only when they
exercise a kernel invariant; otherwise they belong in benchmark snapshots or
adapter-level tests.
