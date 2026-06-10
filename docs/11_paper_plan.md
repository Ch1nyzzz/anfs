# ANFS Paper Plan

Working plan for turning ANFS into a paper. This is a living document; the
thesis and claim table drive what we build and measure next.

## Thesis

Guarantees that agent systems currently attempt at the prompt layer (system
prompts: "do not reveal PII"), the application layer (ad-hoc advisory locks,
manual logging), or not at all, are probabilistic and fail under real
workloads. Moving those guarantees into a filesystem kernel — one that owns
content identity, ordered mutations, copy-on-write boundaries, provenance, and
field-level policy — makes them **structural and verifiable** at acceptable
cost.

The argument is *mechanism vs. exhortation*: for each guarantee there is a
status-quo approach that fails, and a control that demonstrates the failure
on the same workload where ANFS does not.

## Claim table

Each claim must pair with a control that visibly fails. "Status quo" is the
realistic alternative, not a strawman.

| # | Claim | Status-quo that fails | Control in our harness | Metric | State |
| --- | --- | --- | --- | --- | --- |
| C1 | Field-level policy blocks sensitive data at the kernel, prompt-independent | prompt-only "don't reveal PII"; regex/PII scrubber | no-label corpus; **real LLM (deepseek-v4-flash)** prompt-defense + scrubber arms | content leak rate across retrieval surfaces | **Done (mechanism + real baselines):** mechanism control 100% / ANFS 0%; real-LLM prompt-defense **75%**, scrubber **25%**, ANFS **0%** (same model/prompt/attacks, only context differs). *Needed: real PrivacyLens corpus.* |
| C2 | The filesystem coordinates concurrent writers; no silent lost updates | shared dir / object store, last-writer-wins; app-level advisory locks; **git** | plain dir, **git-merge**, and **real spawned processes** racing a shared ANFS db | silently lost updates; surfaced conflicts; disjoint false-positive rate | **Done (mechanism + real parallelism + git):** deterministic control loses N-1 silently / ANFS 0; under real OS-process contention exactly one writer wins, N-1 get clean conflicts, disjoint all commit, integrity clean; three-way vs git below. |
| C3 | Every artifact's lineage is reconstructable and replayable | git commit granularity; no field-level derivation; ad-hoc logs | git history of the same session | lineage coverage; replay fidelity; integrity | **Partial:** conformance proof exists. *Needed: framed as a claim with a git baseline + a coverage metric.* |
| C4 | Compatibility: agents use ANFS like a normal FS with no regression | (this is the denominator, not a differentiator) | native filesystem, same task | success-rate delta (must be ~0); overhead | **Done (real agent + cost):** deepseek-v4-flash fixes 5/5 bug tasks identically in native and ANFS-round-tripped arms (0 regressions, integrity clean); deterministic edit-battery round-trip is byte-faithful; boundary cost 66ms@50 files / 278ms@200 files, inner edit loop at native speed. *Larger: SWE-bench-Live.* |
| C5 | Cross-session memory retrieval is competitive | flat files / JSONL / vector store | those baselines | recall, token cost (LoCoMo/LongMemEval) | **Partial:** task_memory_benchmark scaffold exists; needs real model + official datasets. |

C1 and C2 are the strongest differentiators (control fails dramatically and
visibly). C4 is a baseline that makes the others credible, not a result on its
own. Lead the paper with C1/C2.

## C2: three-way control (plain dir vs git vs ANFS)

git is the honest strong baseline — it is application-level coordination with a
merge protocol, not a strawman. From `benchmarks/concurrency_real_benchmark.py`:

| scenario | plain dir | git | ANFS |
| --- | --- | --- | --- |
| whole-file concurrent edit | silent last-writer-wins | conflict flagged | conflict surfaced |
| same file, different lines | silent last-writer-wins | **auto-merged silently** | conflict surfaced |

ANFS's delta vs git is not "git loses data" (it catches whole-file conflicts).
It is: (1) the guarantee is enforced at the filesystem layer on every write
with no opt-in commit/branch/merge protocol; (2) same-artifact concurrent edits
are always surfaced for review rather than auto-merged — git silently merges
non-overlapping hunks, which can be logically wrong. State this as a tradeoff
(git's auto-merge is often desirable), not a slam dunk.

**Bug found by the harness:** the real-process harness exposed
`merge_workspace` surfacing a raw `SQLITE_BUSY` ("database is locked") under
4-way contention instead of a clean `RefConflictError`, because it used a
DEFERRED transaction (read→write upgrade busy is not covered by busy_timeout).
Fixed by taking the write lock up front with `TransactionBehavior::Immediate`,
matching the already-hardened ref-lifecycle paths. This is the value of real
parallelism over simulation: the simulation could not have caught it.

## Evaluation plan

1. **Real baselines, not just on/off.** C1 needs a prompt-only LLM defense and
   a PII-scrubber baseline; C2 needs a git-merge baseline and ideally real
   concurrent processes; C4 needs native-FS. The on/off control is the floor,
   not the comparison.
2. **Cost.** Done in part: `benchmarks/worktree_cost_benchmark.py` shows ANFS
   taxes the session *boundary* (materialize + commit + integrity: ~66ms for 50
   files, ~278ms for 200), not the per-edit inner loop (the worktree is an
   ordinary directory at native speed). Still reframe `agent_memory_benchmark.py`
   as the small-file direct-API overhead study (ANFS vs native vs SQLite-store).
3. **Real workloads.** PrivacyLens (C1), SWE-bench-Live (C4) with a real coding
   agent through the worktree adapter, LoCoMo/LongMemEval (C5). See
   `docs/10_workload_datasets.md` for vetted sources and licenses.
4. **End-to-end case study.** One multi-agent coding session where all claims
   appear together: parallel edits (C2), a detected merge conflict (C2), a
   secrets file kept out of a logging agent's context (C1), full lineage
   reconstructed afterward (C3). One figure for the whole thesis.
5. **Distinguish mechanical verification from judge-based scoring.** Tests /
   EM / F1 are hard evidence; LLM-judge metrics (LongMemEval, PrivacyLens
   action eval) are softer — report them separately.

## Related work positioning

- **Decentralized Information Flow Control** (Jif, Flume, Asbestos, HiStar):
  the closest lineage — label propagation + policy enforcement at OS/language
  level. Reviewers will ask "what's new vs DIFC?" Our delta: (a) sub-file /
  field-level spans over *unstructured* artifacts (Markdown/JSON), (b) labels
  unified with content-addressed CoW + provenance + replay in one kernel, (c)
  propagation through agent-specific operations (answer/citation/derived
  outputs). Argue this explicitly.
- **Versioned / snapshotting filesystems** (Git, ZFS, NILFS, btrfs): give CoW
  and history but not field-level policy, not derivation-aware provenance, not
  query.
- **Provenance systems** (PASS, Burrito, ProvWasm): provenance capture without
  the policy/CoW/agent-operation integration.
- **Agent memory systems** (MemGPT/Letta, Mem0, Zep): C5 comparison points;
  they are application-layer stores, not a filesystem with kernel guarantees.
- **Agent-FS efforts** (Turso AgentFS, Archil, Vortex): contemporary; position
  ANFS as the one that makes policy + provenance + concurrency kernel
  invariants rather than performance front-ends.

## Limitations (state these honestly)

- **Mechanism, not semantics.** ANFS guarantees labeled *bytes* do not traverse
  the API. It cannot stop *semantic* leakage: a paraphrase of a secret, an
  inference from non-sensitive fields, or a model memorizing content across
  turns. C1's guarantee is byte-level and prompt-independent; it is not a
  claim about model behavior. This boundary is the same mechanism-vs-
  interpretation distinction seen elsewhere in our work and should be stated
  as a scope, not hidden.
- **Conservative span parsing.** Field-level labels rely on conservative
  Markdown/YAML/JSON span extraction; unrecognized structure falls back to
  coarser (whole-node) protection — safe but less precise.
- **Single-node kernel.** Current proofs are local-first; distributed
  coordination is future work.
- **Synthetic corpora in mechanism harnesses.** C1/C2 mechanism results use
  built-in corpora; real-dataset runs (PrivacyLens, SWE-bench-Live, LoCoMo)
  are required before any external claim.

## Current artifacts

- C1: `benchmarks/leak_redteam_benchmark.py`, `tests/test_leak_redteam.py`
- C2: `benchmarks/concurrency_merge_benchmark.py`, `tests/test_concurrency_merge.py`
- C3: `tests/test_conformance.py`, `docs/08_agent_native_fs_conformance.md`
- C5 scaffold: `benchmarks/task_memory_benchmark.py`
- cost scaffold: `benchmarks/agent_memory_benchmark.py`
