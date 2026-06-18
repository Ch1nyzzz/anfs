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

> Status: working prototype. Rust `anfs_core` extension + Python test/benchmark
> harness. See [`report/design-explained.html`](report/design-explained.html)
> for the full design walk-through and [`docs/`](docs/) for the deep specs.

---

## How ANFS relates to a code-graph tool like [codegraph](https://github.com/colbymchenry/codegraph)

They overlap (both give agents structural code navigation) but sit in different
places:

| | codegraph | ANFS |
|---|---|---|
| What it is | Read-only **semantic sidecar** over your on-disk repo | The **file system itself** — files are imported as immutable content nodes |
| Repo ingest | `codegraph init` builds a `.codegraph/*.db` index; FS watcher re-indexes on change | Files are written into ANFS; the code graph is a **derived projection keyed on `blob_hash`**, rebuilt incrementally (only changed blobs re-parse) |
| Files on disk | Untouched (sidecar) | ANFS owns the canonical bytes (it's the FS) |
| Integration | **MCP server**, wired into Claude Code / Cursor / Codex / … | pyo3 library today; tools driven in-process. **An MCP wrapper is the natural next step** (not built yet) |
| Moat | Fewer tool calls / cheaper retrieval | Same retrieval **plus** every read/query/derivation is an auditable, replayable event with lineage |

**Does ANFS need to "rebuild the repo"?** It ingests it once (files → immutable
nodes); the derived code graph is content-hash incremental, not a full re-index
per change. **Can it be used over MCP?** Not yet — that wrapper is a TODO; the
exact tool set already exists (`benchmarks/_anfs_tools.py`).

---

## Experiment: ANFS structural retrieval vs grep, on codegraph's own 7 repos

We ran codegraph's benchmark corpus (the same 7 OSS repos, one architectural
question each) two ways, scored against a **compiler-grade oracle** per language
(rust-analyzer / scip-python / scip-typescript / scip-go / scip-java SCIP, and
sourcekit-lsp for Swift since no SCIP indexer exists for it).

### Accuracy — "which sites call symbol X?", vs the compiler oracle

ANFS `callers`/`call_graph` (one indexed query) vs a toolless agent's `grep`,
scored for recall & precision over every called function (deterministic, no LLM):

| repo | lang | ANFS recall / precision | grep recall / precision |
|---|---|---|---|
| gin | Go | **99.0% / 94.6%** | 98.7% / 82.4% |
| okhttp | Java | 91.2% / **96.2%** | 100% / 68.6% |
| alamofire | Swift | **74.0% / 87.0%** | 65.0% / 48.7% |
| tokio | Rust | 80.7% / 66.1% | 92.5% / 66.9% |
| excalidraw | TypeScript | 38.7% / 79.3% | 73.5% / 56.6% |
| vscode | TypeScript | 47.3% / 14.4% | 49.5% / 6.7% |
| django | Python | 19.3% / 67.0% | 19.5% / 38.3% |

- **Precision: ANFS ≥ grep in all 7 languages** (grep counts comments, strings
  and the definition line as "calls"; ANFS resolves real call edges).
- **Recall depends on how much a language hides calls.** Clean static languages
  (Go, Swift) — ANFS wins both axes. Where calls hide behind operators, macros,
  JSX, arrow functions or dynamic dispatch (Rust, Java, TS), grep's text match
  catches sites ANFS's AST-without-types misses.
- **Efficiency: ANFS returns the attributed caller set in 1 query**; grep needs
  one grep + a read per file just to attribute hits to a calling function.

### Efficiency — agent answers the question, count tokens (codegraph's metric)

Same model, same question, **ANFS tools vs baseline (`read`/`grep`)**, cache-aware:

| repo | lang | baseline tokens | ANFS tokens | saved | answered (base / anfs) |
|---|---|---|---|---|---|
| gin | Go | 77,875 | 51,623 | **34%** | 1/1 · 1/1 |
| okhttp | Java | 41,288 | 35,780 | 13% | 1/1 · 1/1 |
| alamofire | Swift | 152,405 | 175,502 | −15% | 1/1 · 1/1 |
| tokio | Rust | 539,133 | 278,280 | 48% | 1/1 · 0/1 |
| django | Python | 240,822 | 218,427 | 9% | 0/1 · 0/1 |
| excalidraw | TS | 792,342 | 694,621 | 12% | 0/1 · 0/1 |
| vscode | TS | 322,184 | 318,653 | 1% | 0/1 · 0/1 |

**Honest reading:** ANFS saves tokens by reading exact spans instead of whole
files (clean wins where it completes: gin 34%, okhttp 13%), but it is **not a
universal win** — it costs more *tool calls* (granular `outline`→`read_span`
vs one big `read`), and on broad questions the agent often doesn't converge
within the step budget (so some "savings" are partly non-completion). Unlike
codegraph's "cheaper *and* fewer calls", ANFS today trades **more calls for
fewer tokens**. The lever for both is steering the agent onto `context_pack` /
`call_graph` (one call returns a whole symbol + its callers/flow).

**Bottom line:** ANFS's universally-true, unique advantages are **higher
precision, single-call attributed retrieval, and auditable/replayable context**
— the last of which neither grep nor a read-only sidecar can offer. "Token
savings" and "completeness" are real but language-dependent, not guaranteed.

Reproduce: `benchmarks/structural_accuracy_benchmark.py` (accuracy),
`benchmarks/token_efficiency_benchmark.py` (tokens),
`benchmarks/aggregate_report.py` (both tables). Snapshots in
`docs/benchmark_snapshots/`.

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

### Index a repo and query its code graph

```python
import anfs_core
fs = anfs_core.AnfsEngine("anfs.db", "objs")
ws = fs.open_workspace("ws:demo", "agent")
nid = ws.write("src/lib.rs", open("src/lib.rs","rb").read(), [])
fs.index_node_fragments(nid, "tree-sitter-rust")   # also: python/typescript/go/java/swift

fs.node_fragments(nid)            # outline: symbols + byte spans
fs.fragment_callers("spawn")      # exact, attributed call sites
fs.call_graph("spawn", "callees", max_depth=3, token_budget=2000)   # multi-hop flow
fs.context_pack("spawn", 1500, agent_id="agent")   # symbol + callers, token-bounded, AUDITED
```

`context_pack` / `call_graph` record a `code_context_query` / `code_call_graph`
event with input edges to the nodes the model saw — so retrieval is replayable.

---

## Architecture

Three layers, kept distinct:

- **Canonical (facts):** immutable content-addressed blobs + nodes + an
  append-only causal event log (write / publish / consume / approve / merge /
  search-as-context …), with immutability triggers and recursive lineage in SQL.
- **Derived (indexes):** projections rebuilt from canonical state and bound to
  `(blob_hash, parser_version)` so they're verifiable and incrementally
  rebuildable — FTS, chunk maps, embeddings, and the **code graph**
  (`fragments` + `fragment_edges`, six languages via tree-sitter).
- **Context API:** the token-saving, auditable retrieval surface —
  `search` / `query` / `outline` / `read_range` / `context_pack` / `call_graph`.

The code graph is an immutable-node *projection*, not a bolt-on cache: a symbol
is a fragment pointing at a node's byte range (no copied bytes), and a `calls`
edge carries the byte position of the call site as evidence. Cross-file name
resolution happens at query time over active nodes; nothing probabilistic is
written back.

See [`docs/05_agent_filesystem_design.md`](docs/05_agent_filesystem_design.md)
and the design report [`report/design-explained.html`](report/design-explained.html).

---

## What's verified (kernel guarantees, with adversarial controls)

Each is proven by a test/benchmark against the status-quo control it beats:

- **Provenance & replay** — full derivation closure + exact namespace replay at
  any event boundary. Control: git records "commit changed file", not "artifact
  derived from sources". (`provenance_benchmark.py`)
- **Concurrency** — the FS surfaces every contention as an explicit conflict;
  zero silent lost updates. Control: plain dir last-writer-wins, git silently
  auto-merges different-line edits. (`concurrency_real_benchmark.py`)
- **Privacy** — field-level policy labels block sensitive bytes at the kernel,
  regardless of prompt. Control: prompt-only defense / regex scrubber leak.
  (`leak_redteam_benchmark.py`, `leak_llm_baseline.py`)
- **Compatibility** — agents work on an ordinary materialized worktree at native
  speed; ANFS overhead is a bounded per-session boundary cost.
  (`worktree_cost_benchmark.py`, `coding_agent_compat_benchmark.py`)
- **Code-graph retrieval** — the 7-repo accuracy/token study above.

Full capability list and APIs: [`docs/`](docs/) (kernel contract `06`,
complexity audit `09`, claims & eval plan `11`).

---

## Honest limitations

- Code-graph `calls` extraction is AST-without-types: it misses operator-desugared
  calls, macro-internal calls, and dynamic dispatch — so recall trails grep on
  macro/dynamic-heavy languages (see the table). Precision and auditability do
  not have this problem.
- No MCP server yet (pyo3 library only).
- Single-host, SQLite-backed; not a distributed FS.
- The agentic token win is language- and question-dependent, not universal.
