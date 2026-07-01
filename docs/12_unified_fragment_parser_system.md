# The Unified Fragment Parser System

> Thesis: **There is no "code graph."** There is one structural projection —
> the *fragment parser system* — and code is simply the instance where the
> parser is `tree-sitter-*` and the edge kind is `calls`. Markdown headings,
> JSON fields, YAML keys, and contract clauses are the same primitive under
> different parsers. Everything downstream (`outline`, `callers`, `call_graph`,
> `context_pack`, policy labels) is format-blind and stays that way.

This document makes that framing the single, load-bearing explanation for all
structural navigation in ANFS, replaces "code graph" as a marketed noun, and
lists the concrete work left to finish the contract. It is the direction-of-
travel companion to `06_kernel_contract.md` (the invariants) and
`09_complexity_audit.md` (the rule that this reframe must *shrink*, not grow,
the kernel).

## 1. Two primitives, one contract

Every structural view in ANFS reduces to exactly two derived tables over an
immutable node's bytes. No third mechanism is allowed.

### fragment — a named, kinded byte range (the "slice")

`fragments` (`schema.rs:449`): a projection over one node's blob, never a new
blob.

| column | meaning |
| --- | --- |
| `fragment_id` | content-derived id: `hash(blob_hash, parser, path, start, end)` |
| `node_id`, `blob_hash` | which immutable node/blob this slices |
| `parser`, `parser_version` | which parser produced it (rebuild key) |
| `kind` | `function` / `struct` / `heading` / `object-field` / `clause` … |
| `name`, `path` | symbol / field path for lookup |
| `byte_start`, `byte_end` | the exact range — no copied bytes |
| `parent_fragment_id` | **containment within the node** (section ⊃ subsection, class ⊃ method, object ⊃ field) |

A code symbol *is* a fragment. A Markdown heading *is* a fragment. Nothing
about the table knows "code."

### fragment_edge — a typed, evidenced relation (the "link")

`fragment_edges` (`schema.rs:475`): a directional relation between fragments,
carrying the **byte position of the evidence** at the reference site.

| column | meaning |
| --- | --- |
| `src_fragment_id` | the referencing fragment |
| `edge_kind` | `calls` / `references` / `imports` / `includes` … |
| `dst_name` | the referenced name (resolved to candidates at query time, never written back) |
| `dst_fragment_id` | resolved target, or NULL when external |
| `evidence_node_id`, `evidence_start`, `evidence_end` | where the reference physically occurs |

Multiplicity is the deterministic status: one edge per reference site, one edge
per resolution candidate. No probability or confidence is stored.

### The three relation channels (no redundancy)

Structural relations decompose into exactly three, each with one home:

1. **Definition** ("which node holds this fragment") — encoded by `node_id`
   itself. Never an edge (`schema.rs:447`).
2. **Containment** ("which fragment holds this fragment") — `parent_fragment_id`.
   Intra-node hierarchy.
3. **Reference** ("this fragment points at that name") — `fragment_edges`.
   Cross-fragment, cross-node, always with byte evidence.

`call_graph`, `callers`, and `context_pack` are just walks and packings over
these three channels. They contain no format logic.

## 2. The parser contract

A **parser** is the *only* format-aware component. Its entire job:

```
parser: (node bytes) -> (fragments with kind/name/path/parent, edges with kind/dst_name/evidence)
```

Invariants a parser must honor (already enforced by `index_node_fragments`,
`fragments.rs:589`):

- **Rebuildable & content-addressed.** Output is bound to
  `(node_id, blob_hash, parser, parser_version)`. Re-indexing an unchanged blob
  is a no-op (`fragments.rs:605`); a changed blob re-parses only that node.
- **Nothing probabilistic written back.** Cross-node name resolution happens at
  query time over active nodes, never persisted (`schema.rs:471`).
- **Evidence, not assertion.** Every edge carries the byte range that justifies
  it, so retrieval built on it is auditable.
- **Format-blind downstream.** The parser is the last place "format" exists.
  `node_fragments` / `fragment_callers` / `call_graph` / `context_pack` /
  fragment policy labels read the generic tables only.

Parser selection is caller-supplied today (`index_node_fragments(nid,
"span-json")`); a `media_type -> parser` registry can auto-dispatch later
without changing anything downstream (`fragments.rs:65`).

## 3. What is already true

The registry (`run_parser`, `fragments.rs:67`) already treats all formats as
peers reducing to the same two output shapes:

| parser | language | kind examples | edges |
| --- | --- | --- | --- |
| `tree-sitter-rust` | rust | function/struct/enum/trait/impl/mod/macro | `calls` |
| `tree-sitter-python` | python | function/class | `calls` |
| `tree-sitter-{typescript,go,java,swift}` | … | generic def kinds | `calls` |
| `span-markdown` | markdown | frontmatter field / heading / section / block | `references` (inline links) |
| `span-json` | json | object-field / value | `references` (`$ref`) |

All of them persist through the one format-blind writer and are queried through
the one format-blind reader. Every parser now populates the containment tree and
emits reference edges, so `outline` / `callers` / `call_graph` behave the same
over code, prose, and data.

## 4. Status — the contract is complete

All three gaps below are **done** (`fragments.rs`, `engine.rs`). No new tables
were added; gap 3 deleted code.

1. **Containment hierarchy — DONE.** `parent_fragment_id` is derived once, in
   `index_node_fragments`, as the tightest *strictly larger* span that contains
   each fragment by byte range (`derive_parents`). It is format-agnostic: code
   `impl ⊃ method`, JSON `.customer ⊃ .customer.ssn`, Markdown `# ⊃ ## ⊃ block`
   all nest, so one rule builds the whole subordination tree. `node_fragments`
   now returns the parent as a 7th column. Equal ranges never nest (no
   self/cycles); ties break deterministically.

2. **Span-parser edges — DONE.** `span-json` emits a `references` edge from the
   holder field of every `$ref` to the pointer's last segment
   (`json_reference_edges`); `span-markdown` emits a `references` edge from the
   enclosing section of every inline `[text](dest)` link, skipping fenced/inline
   code and images (`markdown_reference_edges`). `fragment_callers` /
   `fragment_callees` no longer filter `edge_kind = 'calls'`, so `callers` /
   `call_graph` traverse `calls` (code) and `references` (docs/data) through the
   identical API — "what references this schema / heading" falls out for free.
   Edges carry byte evidence exactly like `calls`.

3. **Converge the duplicate policy span surface — DONE.** Field policy labels no
   longer re-parse the file per call. `set_json_field_policy_label`,
   `set_markdown_field_policy_label`, and `set_markdown_section_policy_label` now
   resolve their byte range through `fragment_span_by_path`, which indexes the
   node on demand and reads the slice straight from the `fragments` table — so a
   field is sliced **once** (by the parser) and the label rides that canonical
   slice. The `span-markdown` parser emits frontmatter fields *and* body
   sections/blocks, so all Markdown structure lives in one table. The three
   redundant single-lookup finders (`json_field_span`, `markdown_field_span`,
   `markdown_section_span`) were **deleted** — this gap removed code. The
   introspection plurals (`json_field_spans`, `markdown_field_spans`) remain as
   thin views over the same span producers.

## 5. Retire "code graph" as a noun

- README / `report/` currently headline "the code graph is a native
  projection." Reframe to: **the fragment parser system is the native
  projection; code navigation is the `tree-sitter-* + calls` instance of it.**
- Keep the code-precision benchmark (it is a real quality signal), but present
  it as *one parser family's accuracy*, alongside eventual Markdown/JSON
  structural-recall numbers, under the same system.
- API names already generalize (`node_fragments`, `fragment_callers`,
  `call_graph`, `context_pack`) — no rename needed. The reframe is
  documentation + finishing the two span-parser halves, not an API break.

## 6. Why this shrinks the kernel

Against the five axes (`anfs-first-principles`):

- **Agent-friendly:** one mental model — "files are nodes, parsers slice nodes
  into fragments with parents and evidenced edges, everything else walks that."
  No per-format special cases to learn.
- **Provenance:** every fragment/edge is content-addressed and evidence-backed;
  policy labels ride the same slices retrieval uses, so "what the model saw /
  why it was allowed" is one story for code, docs, and data.
- **Token-thrifty:** `context_pack`/`call_graph` bounds apply uniformly; a
  Markdown outline or JSON subtree is packed by the same budgeted walk as code.
- **Robust:** rebuildable projections bound to `(blob_hash, parser_version)`;
  no probabilistic state in canonical rows.
- **Structurally clear:** this is the point. Removing "code graph" as a separate
  feature and collapsing the duplicate span surface makes the kernel *smaller*
  and its explanation *single-threaded*, satisfying `09_complexity_audit.md`'s
  bar for rejecting redundant canonical state.

## 7. Conformance target

The system is "complete" when a single test proves the same walk over
heterogeneous parsers:

- index a `.rs`, a `.md`, and a `.json` node,
- assert `node_fragments` returns a parented hierarchy for all three,
- assert `call_graph`/`callers` traverses `calls` (code) and `references`
  (markdown link, json `$ref`) through the identical API,
- assert a fragment policy label set on a JSON field and on a Markdown section
  is enforced by the same `context_pack`/search gate,
- end with `verify_integrity() == []`.

Until then, `docs/07` should list Markdown/JSON structural relations as
`Partial` under the same row as code, not as separate features.
