# ANFS Project Documents

ANFS stands for Agent-Native File System. This folder is the source of truth for
the project's purpose, product requirements, architectural constraints, and
progress tracking.

Documents:

- [00_project_background.md](00_project_background.md): project motivation,
  design manifesto, and core primitives.
- [01_prd.md](01_prd.md): MVP product requirements for the Rust + SQLite + PyO3
  kernel.
- [02_gap_analysis_vs_existing_fs.md](02_gap_analysis_vs_existing_fs.md):
  what ANFS must provide beyond today's filesystems.
- [03_progress_tracker.md](03_progress_tracker.md): implementation milestones,
  acceptance criteria, and open decisions.
- [04_design_discussion_notes.md](04_design_discussion_notes.md): clarified
  design decisions from project discussion.
- [05_agent_filesystem_design.md](05_agent_filesystem_design.md): agent
  filesystem design target inspired by current filesystem-for-agents arguments,
  mapped to ANFS architecture, APIs, and benchmarks.
- [06_kernel_contract.md](06_kernel_contract.md): kernel canonical state,
  invariants, API boundary, and projection/adapter split.
- [07_agent_fs_requirements_matrix.md](07_agent_fs_requirements_matrix.md):
  requirements matrix against the Amplify filesystem-for-agents article.
- [08_agent_native_fs_conformance.md](08_agent_native_fs_conformance.md):
  executable conformance proof for agent-native filesystem behavior and
  ordinary filesystem compatibility.
- [09_complexity_audit.md](09_complexity_audit.md): redundancy and complexity
  audit that keeps kernel state separate from projections, adapters, external
  services, and benchmarks.

Current priority:

Build a stable and rigorous local-first ANFS kernel. The first milestone is not
full POSIX implementation or distributed scale. The system should expose a
POSIX-like tool surface where useful, but those tools must be backed by a native
engine that can prove, reject, and replay agent artifact lineage
deterministically.

Every new feature should also pass the complexity audit: it enters canonical
kernel state only when it is required for replay, policy, conflict detection,
durable provenance, or integrity.
