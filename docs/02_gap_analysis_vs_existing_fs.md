# Gap Analysis: ANFS vs Existing Filesystems

## Summary

Existing filesystems are strong at durable byte storage, path lookup, directory
projection, permissions, caching, and interoperability with operating system
tools. They are weak at agent workflow semantics.

ANFS should not compete with mature filesystems on POSIX breadth. ANFS should
provide the semantic layer that existing filesystems do not natively know how to
represent.

## What Existing Filesystems Already Do Well

### Durability

Modern filesystems already provide durable writes, journaling, crash recovery,
checksums in some systems, and predictable persistence semantics.

ANFS must not casually reimplement low-level storage durability. It should use
SQLite and an object directory carefully, then add agent semantics above them.

### Compatibility

The entire developer ecosystem expects files and directories. Compilers, test
runners, linters, grep, editors, shells, and package managers all work against
path-shaped data.

ANFS should preserve a filesystem-like interface at the workspace boundary.

### Access Control Basics

Unix permissions, ACLs, user/group ownership, and sandboxing are useful.

ANFS should not discard them. It should add agent identity, tool-call context,
lineage-aware policy, and artifact lifecycle semantics.

### Performance For Common IO

Existing filesystems are highly optimized for local file IO.

ANFS should avoid becoming a slow replacement for simple byte reads. The kernel
should focus on metadata correctness, content addressing, and transactionally
recorded causality.

## What Existing Filesystems Lack For Agents

### 1. Versioned Object Identity

Traditional filesystems identify content mostly through path and current bytes.
Paths are mutable. File contents can be overwritten in place.

ANFS needs:

- immutable blob hashes;
- immutable node ids;
- refs as explicit mutable names;
- ability to distinguish `artifact:patch@v1` from `artifact:patch@v2`;
- explicit link between a path view and an object version.

### 2. Causal Lineage

Existing filesystems cannot natively answer:

- What inputs produced this artifact?
- Which agent and tool call generated this file?
- Which patch did this test result validate?
- Which search result was used as context for this answer?

ANFS needs an event DAG with typed input/output edges.

Lineage check means the kernel verifies causal ancestry before allowing a
semantic transition. For approval, the check is:

```text
Start from evidence node.
Walk backward through event outputs to event inputs.
Require the target node to appear in that ancestor set.
```

If `test_result_v1` derives from `patch_v1`, it cannot approve `patch_v2`.

### 3. Artifact Lifecycle

File existence is not enough. An artifact can exist but still be unsafe for
downstream consumption.

ANFS needs lifecycle states:

- draft;
- published;
- approved;
- rejected;
- archived.

Most importantly, downstream agents should normally consume only published or
approved refs, not arbitrary files that happen to exist.

### 4. Write/Publish Separation

Traditional filesystems expose writes immediately at a path. That is dangerous
for multi-agent workflows because consumers can read partial or stale outputs.

ANFS needs:

- private workspace writes;
- atomic publish;
- immutable published versions;
- ref audit trail;
- explicit consume events.

### 5. Semantic Consistency Across Multiple Files

Many agent artifacts are multi-file states: patch plus manifest, test result
plus logs, plan plus generated code, report plus citations.

Existing filesystems do not know when a set of files is a coherent versioned
unit.

ANFS needs:

- commit-like artifact manifests;
- multi-node publish operations;
- transaction boundaries;
- precondition checks before ref updates.

### 6. Agent Identity And Tool Context

Existing filesystems know users and processes. Agent systems need more:

- logical agent id;
- role;
- run id;
- tool call id;
- workspace id;
- purpose of access.

This information must be recorded with events, not reconstructed from logs
after failure.

### 7. Lineage-Aware Policy

Path-level permissions cannot express rules like:

- Implementer can write patches but cannot approve them.
- Reviewer can approve only with evidence derived from the target.
- Agent may read A and B separately but may not export a derived combination.
- Test result can approve only the exact patch version it tested.

ANFS needs policy checks over refs, nodes, events, lineage, and agent context.

### 8. Search With Version And Permission Semantics

Grep is useful but not enough. Vector search alone is also not enough.

ANFS needs search results that point to:

- node id;
- node version;
- content span;
- index version;
- index model;
- permission check result;
- lineage from source text to derived index node.

### 9. Replay And Debugging

Traditional filesystems preserve current state, not the complete causal history
of a multi-agent run.

ANFS needs:

- append-only events;
- ref mutation audit;
- workspace snapshots;
- input/output edges;
- policy decision records;
- ability to reconstruct a workspace as of a specific event.

### 10. Portable Agent Sessions

Multi-agent runs may cross processes, containers, machines, and regions.

ANFS eventually needs:

- export/import of reachable subgraphs;
- object bundle format;
- deterministic replay package;
- portable workspace snapshots;
- ability to move a run without losing trace, lineage, or policy context.

## Additional Missing Areas To Design

The current blueprint is strong, but a stable and rigorous system still needs
the following design work.

### Transaction And Isolation Model

We must define:

- whether readers see snapshot isolation;
- whether ref updates use compare-and-swap preconditions;
- what happens when two agents publish the same ref;
- which operations are retryable;
- how stale refs are detected.

Recommendation for MVP:

Use SQLite transactions plus optimistic concurrency on refs:

```text
update ref only if old_node_id/state/version matches expected value
```

### Ref Version Or Generation Counter

`updated_at` is useful for audit but weak for concurrency.

Add one of:

- `ref_version INTEGER NOT NULL`;
- or `etag TEXT NOT NULL`.

This gives compare-and-swap semantics and prevents lost updates.

### Immutability Enforcement

The design says only refs are mutable. The engine must enforce this.

Options:

- Rust API exposes no update/delete for immutable tables.
- SQLite triggers reject UPDATE/DELETE on blobs, nodes, events, and event_edges.
- Migrations are the only controlled escape hatch.

Recommendation:

Use both Rust API discipline and SQLite triggers in MVP.

### Schema Migration Discipline

Stable systems need versioned schema migrations.

Add:

```sql
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    applied_at INTEGER NOT NULL
);
```

### Error Taxonomy

The Python boundary needs typed errors, not generic exceptions. These are not
agent-level business rules. They are kernel-level error codes exposed through
PyO3 so upper layers can understand why the storage transition was rejected.

Analogy:

```text
Traditional FS: ENOENT, EACCES, EEXIST
ANFS: RefNotFound, PolicyDenied, RefConflict, LineageMismatch
```

Minimum:

- `LineageMismatchError`
- `RefConflictError`
- `RefNotFoundError`
- `NodeNotFoundError`
- `PolicyDeniedError`
- `StorageCorruptionError`
- `InvalidStateTransitionError`

### Lifecycle State Machine

State transitions must be explicit.

Suggested MVP transitions:

```text
draft -> published
published -> approved
published -> rejected
approved -> archived
rejected -> archived
```

Disallow:

```text
approved -> draft
rejected -> approved
archived -> published
```

### Policy Decision Log

Lineage validation is one policy. More will come.

Add policy decision events or payload records containing:

- policy name;
- decision;
- reason;
- target refs/nodes;
- agent context;
- timestamp.

### Garbage Collection

Append-only systems need retention strategy.

GC roots are the live starting points for reachability analysis. The collector
must preserve everything reachable from these roots.

Examples:

- approved resource refs;
- published or approved artifact refs;
- active workspace refs;
- pinned runs or sessions;
- retained audit/policy evidence;
- compliance or user-pinned objects.

Logical delete only changes current visibility. Physical delete happens later:

```text
live roots -> reachable nodes/events/blobs -> keep
not reachable + past retention window -> GC candidate
```

Questions:

- When can unreferenced blobs be deleted?
- Are rejected artifacts retained forever?
- Are private drafts retained after workspace deletion?
- What is the legal/compliance retention policy?

MVP can avoid physical deletion, but the design should name GC as a future
subsystem. Logical deletion/archive should be represented through refs and
`ref_events` from the start.

### Add/Delete Semantics Compared With Traditional FS

Traditional file creation:

```text
path -> inode -> blocks
```

ANFS logical file creation:

```text
ref -> node -> blob
     + semantic event
     + lineage edges
     + ref_events audit
```

Traditional file deletion:

```text
remove directory entry
maybe free inode/blocks when link count and open handles reach zero
```

ANFS logical deletion:

```text
record ref archive/delete
write ref_events audit
preserve node/blob/event until GC proves unreachable
```

Therefore, ANFS delete means "remove this name from the current view" or "move
this ref to an archived/deleted lifecycle state." It does not mean "erase the
historical fact immediately."

### Semantic Events, Not Syscall Audit

Naively recording every filesystem operation would make ANFS too complex and
too large.

ANFS should not record every:

- `open`;
- `read`;
- `stat`;
- editor temporary file;
- compiler temporary output;
- grep-internal file access.

ANFS should record semantic workflow boundaries:

- write meaningful artifact/resource content;
- publish;
- consume published/approved artifact;
- approve/reject;
- archive/delete ref;
- checkout workspace;
- search when results become agent context.

Principle:

```text
ANFS is semantic causality storage, not syscall-level audit.
```

### Integrity Verification

ANFS should support verification commands:

- rehash all file-backed blobs;
- detect missing object files;
- validate foreign keys;
- validate event edge reachability;
- detect refs pointing to missing nodes;
- validate lifecycle transition history.

### Multi-Node Artifacts

Real artifacts are often bundles, not single files.

The design needs a canonical way to represent:

- patch containing multiple changed files;
- test result containing summary, logs, coverage, and environment;
- report with citations and generated figures.

Options:

- manifest node containing JSON listing child node ids;
- event edges with role-specific child nodes;
- both.

Recommendation:

Use manifest nodes for durable artifact identity and event edges for lineage.

### Canonical Serialization

If nodes or manifests are hashed, JSON must be canonical.

Recommendation:

Define canonical JSON serialization rules before hashing manifest content.

### Time Source And Determinism

Timestamps are useful but should not be the source of ordering truth.

Recommendation:

- Use event ids or monotonic sequence numbers for ordering.
- Keep timestamps for human audit.

### Workspace Diff And Merge

Agent collaboration needs comparison and merge semantics.

Needed operations:

- diff workspace against base snapshot;
- diff artifact versions;
- merge selected refs;
- detect conflicts by base node id and current node id.

### Search Index Freshness

Semantic and text indexes can go stale.

Needed metadata:

- source node id;
- source blob hash;
- index node id;
- indexer version;
- model id for embeddings;
- created_at;
- invalidation rules.

### Security Boundary

ANFS is not automatically a sandbox.

Clarify:

- filesystem semantics are separate from process isolation;
- untrusted tools still need OS/container sandboxing;
- ANFS policy controls object access and publication, not arbitrary syscalls.

### Backup And Recovery

Stable systems need recovery procedures.

Needed:

- backup SQLite DB and object directory consistently;
- restore and verify;
- recover from DB/object mismatch;
- export run bundle for debugging.

## Highest-Priority Missing Pieces

For a stable and rigorous MVP, the most important missing pieces are:

1. Ref compare-and-swap or generation counter.
2. SQLite triggers or equivalent enforcement for immutable tables.
3. Complete ref mutation audit through `ref_events`.
4. Explicit lifecycle state transition validation.
5. Typed error taxonomy at the Python boundary.
6. Policy decision logging.
7. Multi-node artifact manifest model.
8. Integrity verification command.
9. Schema migration discipline.
10. Clear transaction isolation semantics.
