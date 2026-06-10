# Design Discussion Notes

This document records clarifications from project discussion. It should be
folded into the PRD as implementation decisions stabilize.

## 1. Kernel Errors Are Bottom-Layer Rejections

Typed errors such as `LineageMismatchError`, `RefConflictError`, and
`InvalidStateTransitionError` do not mean agents are responsible for enforcing
ANFS rules.

The intended flow is:

```text
agent requests operation
    -> Rust ANFS kernel validates invariants
    -> valid transition commits atomically
    -> invalid transition is rejected
    -> typed error code is returned to the upper layer
```

This is similar to traditional filesystem errors:

```text
open(path)  -> ENOENT if missing
write(path) -> EACCES if denied
rename()    -> EEXIST if target conflicts
```

ANFS equivalents include:

- `LineageMismatchError`: evidence does not causally derive from the target.
- `RefConflictError`: a ref changed concurrently.
- `InvalidStateTransitionError`: lifecycle transition is forbidden.
- `PolicyDeniedError`: policy rejected the operation.

Python exceptions are only the PyO3 exposure format. The conceptual primitive is
a Rust `AnfsError` enum representing kernel-level error codes.

## 2. Lineage Check

Lineage check verifies causal ancestry in the event DAG.

For approval:

```text
target = artifact being approved
evidence = test result, review result, or other proof
```

The kernel must prove:

```text
target node is an ancestor of each evidence node
```

Example:

```text
patch_v1 -> test_result_v1
patch_v2
```

`test_result_v1` can support approval of `patch_v1`. It cannot support approval
of `patch_v2`.

Correct traversal:

```text
start at evidence node
walk backward from event outputs to event inputs
require target node to appear
```

If not found, the approve operation must not update refs and must return
`LineageMismatchError`.

## 3. Blob Table Growth

Content-addressed storage is append-heavy by design. The `blobs` table will grow
because new content creates new immutable blob records.

This is acceptable if managed deliberately:

- deduplicate by SHA-256;
- store large content in a sharded object directory;
- avoid an overly high inline threshold;
- consider binary `BLOB(32)` hash storage instead of hex `TEXT`;
- implement retention and garbage collection.

The biggest long-term growth may come from events, edges, traces, logs, and
search/index chunks, not only blobs.

## 4. GC Roots

GC roots are the live starting points used by garbage collection.

Examples:

- current resource refs;
- published or approved artifact refs;
- active workspace refs;
- pinned runs/sessions;
- audit records required by policy;
- user-pinned objects.

The collector preserves everything reachable from those roots. Objects that are
unreachable and past their retention window can become physical deletion
candidates.

Normal ANFS operations are append-only. Physical deletion is a controlled
maintenance operation, not something agents perform directly.

## 5. Add Semantics

Traditional filesystem creation:

```text
nano foo.txt
save
    -> directory entry foo.txt
    -> inode
    -> mutable blocks
```

ANFS logical creation:

```text
write("foo.txt", content)
    -> Blob(hash(content))
    -> Node(blob_hash)
    -> Event(kind="write")
    -> event_edges(input dependencies, output node)
    -> Ref(ws:agent/foo.txt -> node, state="draft")
    -> ref_events audit row
```

Publish is a separate semantic transition:

```text
publish("foo.txt", "artifact:foo@v1")
    -> Event(kind="publish")
    -> Ref(artifact:foo@v1 -> node, state="published")
    -> ref_events audit row
```

## 6. Delete Semantics

Traditional filesystem deletion:

```text
rm foo.txt
    -> remove directory entry
    -> maybe free inode/blocks later
```

ANFS logical deletion:

```text
delete("foo.txt")
    -> archive/delete the ref from the current view
    -> write ref_events audit row
    -> optionally write lightweight semantic event
    -> keep node/blob/event until GC proves unreachable
```

Delete does not erase historical facts immediately. It changes visibility and
lifecycle in the current view.

## 7. Avoid Event Explosion

ANFS should not record every low-level syscall. Recording every `open`,
`stat`, editor swap file, compiler temp file, or grep internal block read would
make the system too large and too complex.

ANFS records semantic workflow boundaries, including model-visible filesystem
tool operations when their results become part of agent context:

- write;
- publish;
- consume;
- approve/reject;
- archive/delete ref;
- checkout;
- read/cat when a ref or workspace path is intentionally read by an agent tool;
- search when search results become agent context.

Principle:

```text
ANFS is semantic causality storage, not syscall-level audit.
```

## 8. POSIX-Like Tool Surface

The upper layer should feel familiar to models and existing tooling. ANFS
should expose a POSIX-like tool surface at the workspace boundary:

- `read` / `cat`;
- `write`;
- `ls`;
- `stat`;
- `mkdir`;
- `rm`;
- `mv`;
- `cp`;
- `find`;
- `grep`.

These tools are a compatibility facade, not the core storage model. They should
map onto ANFS semantic operations over immutable nodes, mutable refs, lifecycle
states, events, and lineage.

Examples:

```text
read("src/db.py")
    -> resolve visible workspace/resource/artifact ref
    -> read immutable node bytes
    -> record read_ref event with agent_id, run_id, tool_call_id, purpose

write("src/db.py", content)
    -> create Blob(hash(content))
    -> create immutable Node(blob_hash)
    -> update workspace draft Ref(ws:agent/src/db.py -> node)
    -> record write event and ref_events audit row

grep("timeout", "src/")
    -> search visible refs/nodes under the path projection
    -> return familiar path/line/snippet results
    -> record search event with exact result nodes in event_edges
    -> store compact result counts in payload_json

rm("src/db.py")
    -> remove/archive the ref from the current workspace view
    -> record delete/archive event and ref_events audit row
    -> preserve historical nodes/blobs/events until GC proves them unreachable
```

This preserves the usability of normal filesystem tools while changing the
meaning of their effects. A model can keep using operations it understands, but
the lower layer turns those operations into auditable, replayable, versioned
facts.

Performance should be judged against the semantic work being performed, not
only against bare syscalls. ANFS should not lose on model-visible workflows such
as search, copy, delete, and concurrent multi-agent writes, but audited
operations pay for extra facts that POSIX does not record. Directory `mv` is the
clearest example: eager ref rewriting preserves per-ref auditability, while
native `rename(2)` only changes a directory entry. Matching native rename
latency would require a lazy namespace-move primitive.

ANFS should not chase full POSIX fidelity as the primary product goal. Full
POSIX includes hard links, device files, precise lock behavior, mmap semantics,
and many edge cases that would pull the project into low-level filesystem
engineering. The intended target is a practical POSIX-like agent tool layer
backed by the ANFS semantic kernel.

The product meaning of ANFS is:

```text
Give agents a familiar filesystem interface, while making every meaningful
operation versioned, policy-aware, causally attributable, and replayable.
```
