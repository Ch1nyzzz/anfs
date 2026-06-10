# ANFS Rust Refactor Plan

Goal: keep the Python API stable while splitting the Rust kernel into modules
that match ANFS primitives and operational subsystems.

## Current State

`src/lib.rs` used to contain every concern in one file: PyO3 bindings, errors,
shared record types, schema DDL, CAS storage, events, refs, lineage, manifests,
workspace merge, replay, bundles, GC, runs, and integrity verification.

First split completed:

- `src/errors.rs`: typed Rust errors and Python exception mapping.
- `src/types.rs`: shared row aliases and serializable bundle/manifest/replay
  structs.
- `src/schema.rs`: first true Rust subsystem module with explicit
  `pub(crate)` exports for schema initialization and migration inspection.
- `src/blob.rs`: true Rust module for CAS hashing, blob materialization, blob
  path resolution, and blob metadata validation.
- `src/common.rs`: shared connection locking, content media inference, time,
  and ID helpers.
- `src/naming.rs`: workspace/ref naming helpers and ref-kind inference.
- `src/checkout.rs`: checkout base projection and checkout base audit capture.
- `src/events.rs`: true Rust module for event insertion, event sequence
  allocation/backfill, event edges, and policy decision event helpers.
- `src/runs.rs`: true Rust module for run lifecycle rows and run audit events.
- `src/observability.rs`: true Rust module for ref history, event detail,
  event stream, and node-event query helpers.
- `src/query.rs`: shared query helpers used by converted modules and remaining
  root-included subsystems.
- `src/gc_snapshot.rs`: true Rust module for GC roots/candidates/collection,
  GC pins, and namespace snapshots.
- `src/workspace_ops.rs`: true Rust module for workspace diff/conflict helpers,
  checkout-base comparison, and node existence checks.
- `src/replay.rs`: true Rust module for ref view reconstruction and
  filesystem materialization.
- `src/refs.rs`: true Rust module for ref state mutations and ref audit rows.
- `src/lineage.rs`: true Rust module for lineage traversal and lineage
  approval coverage.
- `src/manifest.rs`: true Rust module for manifest reads and node/blob byte
  reads.
- `src/bundle.rs`: true Rust module for event/run bundle export and bundle
  import validation/materialization.
- `src/integrity.rs`: true Rust module for database/object-store integrity
  verification.
- `src/engine.rs`: PyO3 and internal methods for `AnfsEngine`.
- `src/workspace.rs`: PyO3 and internal methods for `Workspace`.

Root-level `include!` wiring has been fully removed. `src/lib.rs` is now a PyO3
registration facade with shared class definitions, not a textual aggregator of
subsystem source files.

`engine.rs` and `workspace.rs` currently import through `use super::*` as a
transition boundary. A later cleanup can replace that with explicit imports once
the API settles.

## Target Module Shape

Planned module boundaries:

- `engine.rs`: `AnfsEngine` PyO3 methods and engine internal implementation.
- `schema.rs`: SQLite DDL, migrations, immutable triggers, schema inspection.
- `blob.rs`: CAS materialization, object paths, blob reads/writes, blob
  metadata validation.
- `common.rs`: connection locking, clock, generated IDs, and content media
  inference.
- `naming.rs`: ref naming, workspace path projection, and ref-kind inference.
- `checkout.rs`: checkout base snapshot/projection helpers.
- `events.rs`: event insertion, event sequence allocation, event listing,
  event edge insertion.
- `refs.rs`: refs, ref audit rows, lifecycle state transitions, ref history.
- `lineage.rs`: lineage traversal, graph APIs, approval coverage checks.
- `manifest.rs`: manifest node creation, child parsing, child metadata checks.
- `workspace.rs`: `Workspace` PyO3 methods and workspace read/write/publish
  implementation.
- `replay.rs`: ref view reconstruction and filesystem materialization.
- `bundle.rs`: event/run bundle export/import.
- `runs.rs`: run lifecycle and run audit rows.
- `gc.rs`: roots, candidates, collection, pins.
- `integrity.rs`: `verify_integrity()` and its diagnostic helpers.
- `policy.rs`: policy decision events and future configurable policy checks.

## Refactor Order

Use small verified moves. After each move, run:

```bash
cargo fmt --check
cargo check
uv tool run maturin develop
.venv/bin/python -m pytest tests/ -q
```

Recommended order:

1. Replace `use super::*` in `engine.rs` and `workspace.rs` with explicit
   imports when the method surface stabilizes.
2. Split very large validation/transport modules only if the internal sections
   become hard to review: `integrity.rs` and `bundle.rs` are explicit modules
   now, but still large.

## Non-Goals

- Do not change Python API names or argument order during this refactor.
- Do not change SQLite schema shape merely to make module extraction easier.
- Do not split PyO3 classes across multiple crates yet.
- Do not introduce an ORM or external database abstraction.

## Review Checklist

- Public Python behavior stays unchanged.
- Module functions are `pub(crate)` only when another module needs them.
- Shared structs remain in `types.rs`; avoid redefining tuple shapes.
- Each subsystem keeps its append-only and lineage invariants.
- Full demo/test script passes after every extraction step.
