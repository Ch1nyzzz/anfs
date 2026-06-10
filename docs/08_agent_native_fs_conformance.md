# Agent-Native FS Conformance

This document defines how ANFS proves the local MVP is agent-native while still
remaining compatible with ordinary filesystem tools.

Source requirement context: Sarah Catanzaro, "File systems for agents",
Amplify Partners, May 27, 2026.
https://www.amplifypartners.com/blog-posts/file-systems-for-agents

## Executable Proof

The primary end-to-end proof is:

`tests/test_demo.py::test_agent_native_fs_conformance_preserves_kernel_invariants_with_existing_tools`

The checked ordinary-filesystem sync proof is:

`tests/test_demo.py::test_worktree_checked_commit_rejects_stale_materialized_base`

The checked ordinary-filesystem contention proof is:

`tests/test_demo.py::test_concurrent_checked_worktree_commits_allow_only_one_materialized_base`

It is intentionally a conformance scenario rather than a narrow unit test.

## What The Test Proves

1. Existing filesystem compatibility:
   `materialize_workspace(...)` writes a normal directory, then Python
   `pathlib` and `shutil` modify, delete, and copy files before
   `commit_worktree(...)` scans the directory back into ANFS. The POSIX-like
   workspace facade also covers `touch` for missing empty regular files and
   treats existing regular files as no-ops; explicit timestamp changes are
   covered by audited `utime(...)` metadata overlays. It also covers
   `read_range(...)` for path-level
   partial reads, including EOF reads, audited `read_range` events, and
   fragment policy denial only when the requested range overlaps the labeled
   bytes. `write_range(...)` covers path-level replacement, append, and
   zero-fill extension edits, rejects directories, records audited
   `write_range` events, and preserves lineage from the previous immutable
   node. `append(...)` covers
   append-at-EOF edits, including missing-file creation, previous-node lineage,
   audited `append` events, and directory rejection. `truncate(...)` covers
   existing-file shrink and NUL-extension edits, audited `truncate` events,
   previous-node lineage, same-length no-op behavior, and rejection of missing
   files, directories, and negative lengths. Metadata helpers `exists(...)`,
   `is_file(...)`, and `is_dir(...)` cover root directories, file paths,
   directory paths, missing paths, and deleted paths through the same stat
   semantics. `stat_posix(...)` covers derived adapter metadata for root,
   directory, and file paths, including mode, nlink, uid/gid, inode, and
   atime/mtime/ctime fields, including shared inode/link-count derivation for
   active workspace refs pointing at the same immutable file node. `link(...)`
   covers audited hard-link-like destination refs, metadata-overlay inheritance,
   and copy-on-write separation after later path writes. `chmod(...)` covers
   audited path-level mode overlays that affect `stat_posix(...)`, `access(...)`,
   active linked refs, and POSIX special mode bit preservation without adding
   ACL or kernel sandbox semantics. It also covers setgid directory metadata
   inheritance for newly created files and subdirectories.
   `chown(...)` covers audited path-level uid/gid overlays across active linked
   refs without making `access(...)` impersonate OS users or groups.
   `utime(...)` covers audited path-level atime/mtime overlays across active
   linked refs without creating a new content node or claiming implicit
   atime-on-read behavior.
   `acquire_lock(...)` / `release_lock(...)` / `lock_info(...)` cover audited
   exclusive ANFS path leases: another agent/run is blocked from writing a locked
   file, the owning agent/run can still mutate it, non-owners cannot release it,
   and directory leases block child creation until released.
   `rm(...)` / `delete(...)` / `mv(...)` preserve existing agent-tool behavior
   by default and enforce POSIX-like sticky-directory deletion/replacement rules
   when explicit effective uid/gid values are supplied.
   `cp(...)` and `mv(...)` cover destination-directory compatibility for regular
   files and recursive directory trees, placing the source basename under an
   existing destination directory.
   `access(...)` covers
   `F_OK`/`R_OK`/`W_OK`/`X_OK` checks, missing path behavior, unsupported mask
   rejection, optional effective uid/gid/group owner/group/other mode-bit
   selection, root-style effective uid `0` read/write mode-bit bypass with
   execute-bit requirements, and read denial when active
   fragment visibility policy blocks a file.
   Concurrent checked worktree commits from the same materialized base also
   prove whole-transaction busy retry: one commit advances, the loser re-reads
   current refs and returns a stale-manifest conflict instead of a transient
   SQLite lock error.

2. Kernel source of truth:
   committed edits become immutable nodes, ref audit rows, typed events, and
   lineage edges. The materialized directory is never canonical until committed.

3. Copy-on-write isolation:
   a workspace checked out from a base ref can be edited independently and then
   merged back through conflict-checked `merge_workspace(...)`.

4. Query and retrieval:
   `search(...)` and `query(...)` find committed workspace content and record
   auditable search/query events with exact result node edges. Answer evidence
   coverage inspection can later prove whether an answer citation was covered
   by those retrieval events.

5. Safe text search:
   user search text is converted to a literal FTS query, so ordinary file
   content such as `123-45-6789` is treated as text rather than a SQLite FTS
   expression.

6. Large artifact access:
   `read_node_range(...)`, `cache_node_chunks(...)`, and
   `cached_node_chunks(...)` support selective reads and persistent chunk
   planning over a large node.

7. Working-set materialization:
   `cache_materialized_workspace(...)` records and reuses a managed
   materialized working set when the workspace view has not changed.

8. Worktree base leases:
   `commit_worktree(..., require_manifest_match=True)` rejects a stale
   materialized directory when current workspace paths changed, disappeared, or
   appeared after materialization. Refreshing the worktree writes a new manifest
   and permits the checked commit.

9. Worktree entry safety:
   `worktree_readiness(...)` reports unsupported ordinary-filesystem entries,
   and `commit_worktree(...)` rejects symlinks instead of following them, so
   normal-directory compatibility does not import bytes outside the materialized
   tree through link traversal. The suite also creates a POSIX filename with a
   backslash and proves readiness/commit reject the cross-platform ambiguous
   path before any grouped `worktree_commit` event is recorded. The root
   `.anfs-worktree-manifest.json` path is reserved adapter metadata: the suite
   proves a canonical user file with that path is rejected by materialize/commit
   and a local metadata file at that path is not imported as user content.

10. Checked worktree contention:
   two spawned processes can try to commit separate ordinary directories
   materialized from the same workspace base. The test proves one complete
   checked commit advances, the stale loser returns `RefConflictError`, no
   second `worktree_commit` event is recorded, and the final namespace is not a
   partial mix of both worktrees.

11. Dynamic policy at field/fragment level:
   `set_json_field_policy_label(...)` maps `$.customer.ssn` to a byte-range
   fragment label. The worktree copy proves exact byte-identical copies inherit
   active fragment labels through new audit events. An active fragment
   visibility deny rule blocks search and whole-workspace materialization for
   labeled nodes. Focused regression tests also prove explicit partial-output
   attribution, unique exact-byte automatic attribution, and unique normalized
   JSON scalar attribution for strings, booleans, nulls, and finite numbers, plus
   unique normalized JSON array/object value attribution through minified JSON
   projection.

12. Integrity:
   the scenario ends with `verify_integrity() == []`, proving the generated
   event/ref/policy/worktree/cache state satisfies kernel invariants.

## What It Does Not Prove Yet

- No FUSE or mounted POSIX adapter is proven.
- No distributed multi-machine filesystem semantics are proven.
- Exact byte-identical copies, explicit derived outputs, explicit
  partial-output attribution, unique exact-byte automatic attribution, unique
  normalized JSON scalar attribution for strings, booleans, nulls, and finite
  numbers, unique normalized JSON array/object value attribution,
  and Markdown body ATX/Setext heading sections and conservative
  paragraph/paragraph-line/inline-strong/inline-strong-text/
  inline-emphasis/inline-emphasis-text/inline-link/inline-link-label/
  inline-link-destination/inline-link-title/reference-link/reference-link-label/
  reference-link-reference/reference-link-resolved-destination/reference-link-resolved-title/inline-image/inline-image-alt/
  inline-image-destination/inline-image-title/reference-image/reference-image-alt/
  reference-image-reference/reference-image-resolved-destination/reference-image-resolved-title/inline-code/autolink/autolink-target/list/list-item/task-checkbox/blockquote/
  blockquote-line/table/table-align/table-row/table-cell/code/html/
  link-reference/link-reference-label/link-reference-destination/
  link-reference-title/thematic-break body blocks propagate policy labels.
  Current conservative inline parsers suppress spans for delimiter bytes escaped
  by an odd number of immediately preceding backslashes, and inline-code spans
  support same-line matching backtick runs. Direct inline link/image
  destination spans preserve balanced unescaped parentheses and expose
  angle-bracketed payloads without surrounding delimiters; link-reference
  definition destinations also expose angle-bracketed payloads. Direct
  inline/reference title spans include trailing quoted or parenthesized titles.
  Reference label matching normalizes backslash-escaped ASCII punctuation while
  raw label spans remain available for policy. Duplicate link-reference
  definitions resolve against the first normalized definition while later
  duplicate component spans remain visible. Link-reference definitions support
  immediate next-line title-only continuations for quoted or parenthesized
  titles.
	  Conservative Markdown frontmatter object/list/inline-sequence-item/
	  inline-sequence-object-field/inline-object-field/nested-inline-object/
	  nested-inline-array-item/quoted-key/tag-anchor-decorated/alias-token/
	  conservative alias-expansion/recursive-alias-merge-chain/merge-array/scalar-alias-target/
	  block-sequence-alias-anchor-merge-bookkeeping/null-scalar/core-tag-kind
  including timestamp/binary explicit tags,
  set/omap explicit container tags, conservative unquoted ISO timestamp scalar
  inference, quoted scalar payload spans excluding quote delimiters, decoded
  semantic values for single-quoted doubled apostrophes and common YAML
  double-quoted escapes through `markdown_field_values(...)`, and block-scalar
  spans are supported. Full YAML parsing, deeper Markdown AST semantics, and
  broader cross-format semantic fields are still future work.
- Operation capability rules prove lifecycle/review role enforcement for
  `approve`, `reject_ref`, and `archive_ref`, but this is not a complete
  downstream execution sandbox or OS-level role system.
- This conformance scenario does not exercise the local embedding projection;
  current query proof remains FTS/text-first.
- The conformance suite includes local spawned-process checked-worktree
  contention, but does not publish large-scale contention results; broader
  contention and cache-reuse measurements live in the benchmark suite.

These gaps should remain explicit so ANFS stays a compact semantic kernel plus
adapters, instead of expanding into a redundant general-purpose filesystem.
