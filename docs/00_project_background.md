# ANFS Project Background

## One-Sentence Goal

Build an agent-native filesystem backbone where files are still the interface,
but immutable content, versioned facts, causal events, lifecycle-aware refs, and
policy checks are the actual system semantics.

## Manifesto

Traditional filesystems are built around:

```text
path -> mutable bytes
```

That model is too weak for multi-agent work. Agents do not merely open files;
they derive artifacts, consume evidence, publish patches, run tools, approve
results, search context, and pass intermediate state to other agents.

ANFS replaces path-centric truth with four primitives:

```text
Blob is content.
Node is truth.
Event is causality.
Ref is view.
```

Paths remain useful because tools, agents, compilers, tests, and humans already
understand filesystem-shaped workspaces. But paths are only a view. The durable
truth is the immutable object graph underneath.

## Core Problem

Today's filesystem knows path, bytes, mtime, permissions, and owner. It usually
does not know:

- which agent generated a file;
- which tool call produced an artifact;
- which input versions were consumed;
- whether a test result belongs to patch v1 or patch v2;
- whether an artifact is draft, published, approved, or rejected;
- whether a derived output violates policy;
- whether a semantic search result points to the current object version;
- how to replay a failed multi-agent run.

This causes semantic failures that ordinary filesystems cannot detect. A common
example:

```text
Tester validates patch v1.
Coder later publishes patch v2.
Reviewer accidentally approves v2 using test_result_for_v1.
The path names still look plausible, but the causal graph is wrong.
```

ANFS must make this class of error impossible or at least detectable at the
kernel boundary.

## Core Design Clarifications

ANFS is a bottom-layer semantic kernel. Agents do not enforce lineage,
lifecycle, or policy rules themselves. Agents submit operations; the ANFS kernel
either commits a valid state transition or rejects it.

This is analogous to a traditional filesystem returning `ENOENT`, `EACCES`, or
`EEXIST`. In ANFS, the equivalent kernel-level errors include lineage mismatch,
ref conflict, invalid lifecycle transition, missing refs, and policy denial.

ANFS is also not a syscall-level audit system. It should not record every
`open`, `read`, `stat`, editor swap file, compiler temporary file, or grep
intermediate. It records semantic causality: write, publish, consume, approve,
reject, archive/delete ref, checkout, and search-as-context when those actions
matter to agent workflow correctness.

## Target Architecture

```text
Agent / Tools
    |
Filesystem-like Interface
    |
Object-Level Virtual Workspace
    |
Storage Backbone
    |-- Content Store
    |-- Metadata DB
    |-- Version / Snapshot Manager
    |-- Artifact Lineage Graph
    |-- Event / Trace Log
    |-- Search / Semantic Index
    |-- Policy Engine
```

The system should feel like a workspace to the agent, but behave like a
transactional, content-addressed, lineage-aware artifact system underneath.

## Four Primitives

### Blob

Blob is immutable content.

- Stores raw bytes.
- Addressed by cryptographic hash, initially SHA-256.
- Contains no lifecycle, path, agent, run, or semantic metadata.
- May be stored inline for small content and externally for large content.

### Node

Node is immutable fact.

- Wraps a blob with objective metadata.
- Represents a resource, artifact, skill, index, or memory snapshot.
- Has no mutable lifecycle state.
- Is never updated or deleted by normal engine operations.

### Event

Event is immutable causality.

- Records a state transition or derivation.
- Connects input nodes to output nodes through typed edges.
- Carries agent, run, workspace, tool call, and payload context.
- Forms the lineage DAG used for replay, auditing, and policy validation.

### Ref

Ref is mutable view.

- Named pointer to a node.
- The only mutable primitive.
- Carries lifecycle state such as draft, published, approved, rejected, and
  archived.
- A workspace directory tree is a dynamic projection over refs with a namespace
  prefix.

## Resource, Artifact, Skill, Memory

These are not separate storage primitives. They are semantic categories over
nodes and refs.

- Resource: user or business-visible durable input/output.
- Artifact: run-scoped generated object such as patch, test result, log,
  review, plan, or trace.
- Skill: versioned agent capability package.
- Memory: cross-run derived state, backed by explicit lineage from resources,
  artifacts, events, and skills.

## Design Priorities

1. Stable and rigorous semantics before feature breadth.
2. Immutable data first; mutation only through audited refs.
3. Write and publish are separate operations.
4. Every important transition has an event.
5. Every event can be replayed or audited.
6. Every approval is evidence-bound.
7. Search results must resolve to versioned, permission-checked nodes.
8. Local-first MVP before distributed operation.

## Non-Goals For MVP

- Full POSIX/FUSE compatibility.
- Distributed consensus.
- External graph database.
- External vector database as a required dependency.
- Transparent replacement of the operating system filesystem.
- Broad workflow engine features before the storage semantics are correct.
