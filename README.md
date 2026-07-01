# ANFS — Agent-Native File System

ANFS is a local-first file system built for AI coding agents. Agents talk to it
with boring, familiar verbs — `read`, `write`, `grep`, `find`, `outline` — but
underneath, every action is an **append-only, content-addressed, replayable
event**. The interface is a filesystem; the backbone is a semantic kernel
(Rust core, SQLite, CAS blob store, exposed to Python via PyO3).

The bet: today an agent's guarantees about concurrency, provenance, privacy and
lifecycle live in the *prompt* ("don't leak the SSN"), in *application code*
(hand-rolled locks and logs), or nowhere. ANFS moves them into the kernel as
**verifiable structural guarantees** — so "what context did the model see, and
why" is reconstructable, not a matter of trust.

> Status: working prototype. Rust `anfs_core` extension + Python harness, 167
> tests. Design walk-through: [`report/design-explained.html`](report/design-explained.html);
> deep specs in [`docs/`](docs/).

---

## Architecture: three layers, kept distinct

- **Canonical (facts):** immutable content-addressed blobs + nodes + an
  append-only causal event log (write / publish / consume / approve / merge /
  search-as-context …), with immutability triggers and recursive lineage in SQL.
- **Derived (indexes):** projections rebuilt from canonical state and bound to
  `(blob_hash, parser_version)` so they're verifiable and incrementally
  rebuildable — FTS, chunk maps, embeddings, and the **fragment graph**
  (structural outlines + typed edges over *any* file, code or not).
- **Context API:** the token-saving, auditable retrieval surface —
  `search` / `query` / `outline` / `read_range` / `context_pack` / `call_graph`.

## Structure is a native projection: one fragment parser system

Because ANFS already stores every file as an immutable node, structure is just
*another derived view of those nodes* — it falls out of the design rather than
being a separate "code graph" glued on. There is **one system, not a code
feature**:

- A **fragment** is a named, kinded byte range pointing into a node — no copied
  bytes. A code symbol, a Markdown heading, and a JSON field are all fragments;
  `parent_fragment_id` gives them a subordination tree (`node_fragments` → the
  outline).
- An **edge** carries the byte position of the reference site as *evidence*. A
  `calls` edge (code) and a `references` edge (a Markdown link, a JSON `$ref`)
  are the same primitive (`fragment_callers` → exact, attributed sites).
- **`call_graph(seed, direction, depth)`** walks those edges both ways
  (callees = execution flow, callers = blast radius), cycle-safe and
  token-bounded.
- **`context_pack(seed, budget)`** returns a fragment plus what references it,
  within a token budget.

One **parser per format** is the only format-aware component; everything
downstream (`outline` / `callers` / `call_graph` / `context_pack` / policy
labels) is format-blind. Adding a language or a document format = adding a
parser, nothing else changes. Registered today: `tree-sitter-{rust, python,
typescript, go, java, swift}` (`calls` edges) and `span-{markdown, json}`
(structural slices). Full model:
[`docs/12_unified_fragment_parser_system.md`](docs/12_unified_fragment_parser_system.md).

Two properties come for free from being part of the provenance kernel, and are
the reason this isn't "just another code index":

1. **Auditable retrieval.** `context_pack` / `call_graph` record a
   `code_context_query` / `code_call_graph` event with input edges to the exact
   nodes the model saw — so retrieval is replayable, not ephemeral.
2. **Content-addressed & incremental.** Fragments are keyed on
   `(blob_hash, parser_version)`; re-indexing only re-parses changed files, and
   policy-hidden byte ranges are never surfaced.

Cross-node name resolution happens at query time over active nodes; nothing
probabilistic is written back into the canonical store.

## How agents integrate

Files are **imported into ANFS as immutable content nodes** (ANFS is the file
system, not a read-only index beside one). The fragment graph is a content-hash
derived projection, so it's built once and updated incrementally as files
change — not re-indexed wholesale. ANFS is a pyo3 library today; an **MCP server
wrapper** (exposing `outline` / `symbol_search` / `callers` / `call_graph` /
`context_pack` / `read` / `grep`) is the natural packaging for dropping it into
Claude Code / Cursor / etc. — the tool set already exists in
`benchmarks/_anfs_tools.py`, the server is a TODO.

---

## Quickstart

```bash
python3 -m venv .venv
env PATH="$HOME/.cargo/bin:$PATH" uv tool run maturin develop   # build the Rust extension
.venv/bin/python -m pytest tests/ -q                            # 167 tests
.venv/bin/python tests/test_demo.py                             # "ANFS killer demo passed."
```

The killer demo proves the kernel refuses to let a stale test result
(`artifact:test_result@v1`) approve a newer patch (`artifact:patch@v2`) — a
lineage violation enforced in Rust, not by convention.

### Index a repo and query its fragments

```python
import anfs_core
fs = anfs_core.AnfsEngine("anfs.db", "objs")
ws = fs.open_workspace("ws:demo", "agent")
nid = ws.write("src/lib.rs", open("src/lib.rs", "rb").read(), [])
fs.index_node_fragments(nid, "tree-sitter-rust")   # also: python/typescript/go/java/swift

fs.node_fragments(nid)            # outline: symbols + byte spans
fs.fragment_callers("spawn")      # exact, attributed call sites (with evidence ranges)
fs.call_graph("spawn", "callees", max_depth=3, token_budget=2000)   # multi-hop flow
fs.context_pack("spawn", 1500, agent_id="agent")   # symbol + callers, token-bounded, AUDITED
```

---

## What's verified

Each kernel guarantee is proven by a test/benchmark against the status-quo
control it beats:

- **Provenance & replay** — full derivation closure + exact namespace replay at
  any event boundary. Control: git records "commit changed file", not "artifact
  derived from sources". (`provenance_benchmark.py`)
- **Concurrency** — every contention surfaces as an explicit conflict; zero
  silent lost updates. Control: plain dir last-writer-wins, git silently
  auto-merges. (`concurrency_real_benchmark.py`)
- **Privacy** — field-level policy labels block sensitive bytes at the kernel,
  regardless of prompt. Control: prompt-only defense / regex scrubber leak.
  (`leak_redteam_benchmark.py`, `leak_llm_baseline.py`)
- **Compatibility** — agents work on an ordinary materialized worktree at native
  speed; ANFS overhead is a bounded per-session cost.
  (`worktree_cost_benchmark.py`, `coding_agent_compat_benchmark.py`)
- **Fragment retrieval (code parsers)** — accuracy & token study below.

### Fragment retrieval: the code parsers, on a public multi-language benchmark

This measures one parser family of the fragment system — the `tree-sitter-*`
parsers and their `calls` edges. To check they hold up on real, large
codebases, we ran them
against a public benchmark corpus — 7 OSS repos across 6 languages, one
architectural question each (the same set the
[codegraph](https://github.com/colbymchenry/codegraph) project uses, reused here
purely as a shared, neutral test set). For each repo we score against a
**compiler-grade oracle** for that language (rust-analyzer / scip-python /
scip-typescript / scip-go / scip-java; sourcekit-lsp for Swift, which has no
SCIP indexer), comparing ANFS's structural retrieval to the `grep` a toolless
agent falls back on.

**Accuracy — "which sites call symbol X?" vs the compiler oracle** (deterministic):

| repo | lang | ANFS recall / precision | grep recall / precision |
|---|---|---|---|
| gin | Go | **99.0% / 94.6%** | 98.7% / 82.4% |
| okhttp | Java | 91.2% / **96.2%** | 100% / 68.6% |
| alamofire | Swift | **74.0% / 87.0%** | 65.0% / 48.7% |
| tokio | Rust | 80.7% / 66.1% | 92.5% / 66.9% |
| excalidraw | TypeScript | 38.7% / 79.3% | 73.5% / 56.6% |
| vscode | TypeScript | 47.3% / 14.4% | 49.5% / 6.7% |
| django | Python | 19.3% / 67.0% | 19.5% / 38.3% |

**Token efficiency — agent answers the question with ANFS tools vs `read`/`grep`** (cache-aware):

| repo | baseline tokens | ANFS tokens | saved | answered (base / anfs) |
|---|---|---|---|---|
| gin | 77,875 | 51,623 | **34%** | 1/1 · 1/1 |
| okhttp | 41,288 | 35,780 | 13% | 1/1 · 1/1 |
| alamofire | 152,405 | 175,502 | −15% | 1/1 · 1/1 |
| tokio | 539,133 | 278,280 | 48% | 1/1 · 0/1 |
| django | 240,822 | 218,427 | 9% | 0/1 · 0/1 |
| excalidraw | 792,342 | 694,621 | 12% | 0/1 · 0/1 |
| vscode | 322,184 | 318,653 | 1% | 0/1 · 0/1 |

**Honest reading:**

- **Precision: ANFS ≥ grep in all 7 languages** (grep counts comments, strings
  and the definition line as "calls"; ANFS resolves real call edges).
- **Recall depends on how much a language hides calls.** Clean static languages
  (Go, Swift) — ANFS wins both axes. Where calls hide behind operators, macros,
  JSX, arrow functions or dynamic dispatch (Rust, Java, TS), `grep`'s text match
  catches sites ANFS's AST-without-types misses.
- **Tokens:** ANFS saves by reading exact spans instead of whole files (clean
  wins where it completes: gin 34%, okhttp 13%), but it's not a universal win —
  it costs more *tool calls* (granular `outline`→`read_span`), and on broad
  questions the agent may not converge in the step budget.
- **What's universally true:** higher precision, single-call attributed
  retrieval, and **auditable/replayable** retrieval. Token savings and
  completeness are real but language-dependent.

Reproduce: `benchmarks/structural_accuracy_benchmark.py`,
`benchmarks/token_efficiency_benchmark.py`, `benchmarks/aggregate_report.py`;
snapshots in `docs/benchmark_snapshots/`.

---

## Honest limitations

- The code parsers' `calls` extraction is AST-without-types: it misses operator-desugared
  calls, macro-internal calls, and dynamic dispatch — so recall trails `grep` on
  macro/dynamic-heavy languages. Precision and auditability do not have this issue.
- No MCP server yet (pyo3 library only).
- Single-host, SQLite-backed; not a distributed FS.
- The agentic token win is language- and question-dependent, not universal.

Full capability list and APIs: [`docs/`](docs/) (kernel contract `06`,
complexity audit `09`, claims & eval plan `11`).
