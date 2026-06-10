# Real-Workload Datasets and Task Sources

Survey of public datasets/benchmarks for validating ANFS with real agent
workloads (W1-W4), researched and source-verified 2026-06-09. Claims below
were checked against primary sources (HF dataset cards, GitHub READMEs,
arXiv abstracts); contamination and maintenance caveats are noted inline.

## W1: Coding agent tasks (test-verified success)

| Candidate | Scale | Auto-verify | License | Local run | Notes |
| --- | --- | --- | --- | --- | --- |
| **SWE-bench-Live** (recommended) | frozen lite 300 / verified 500; full split growing monthly (~50/mo) | FAIL_TO_PASS / PASS_TO_PASS unit tests, per-instance Docker image | MIT | Yes (local Docker harness) | Built from post-2024 issues to avoid training contamination; Microsoft-maintained, NeurIPS 2025 D&B; MultiLang (743) and Windows (61) splits added. https://github.com/microsoft/SWE-bench-Live |
| SWE-bench Lite | 300 test + 23 dev | same mechanism | (dataset card) | Yes | UTBoost (arXiv 2506.09289) found ~54.7% of Lite instances have annotation/parsing issues; pre-2024 PRs → contamination risk. Prefer Verified subset if used. https://huggingface.co/datasets/princeton-nlp/SWE-bench_Lite |
| Terminal-Bench 2.0 | ~89 tasks (community count, unconfirmed) | per-task test script + oracle solution | Apache-2.0 | Docker-based; exact dependency chain unverified | Now developed under the harbor framework; some tasks have env/nondeterminism issues — use `terminal-bench-2-verified`. https://github.com/laude-institute/terminal-bench |
| aider polyglot | 225 Exercism exercises, 6 languages (verified per-dir counts) | language test suites | no repo LICENSE; content (c) Exercism | Yes | Public Exercism solutions → high contamination; repo unmaintained since 2024-12. Use only as filesystem-workflow smoke test, not capability eval. https://github.com/Aider-AI/polyglot-benchmark |

## W2: Multi-agent concurrent collaboration

No ready-made "N agents concurrently edit + merge" benchmark exists. Build
from these pieces:

| Piece | What it gives | License | Notes |
| --- | --- | --- | --- |
| **CAID method** (arXiv 2603.21489, CMU) | Task-construction recipe: dependency-aware decomposition + per-agent isolated workspace (git worktree) + branch-and-merge; names the three failure modes (concurrent-edit interference, dependency sync, partial-progress merge) | — | Concept maps directly onto ANFS fork_workspace/merge_workspace; preprint, not peer-reviewed. https://github.com/JiayiGeng/CAID |
| **Commit0** | 57 core Python libraries to implement from scratch, unit-test verified | MIT | Local Docker backend exists (`--backend local`); CLI defaults to Modal cloud — switch explicitly. Parallel decomposition is CAID's layer, not native. https://github.com/commit-0/commit0 |
| **AgenticFlict** (arXiv 2604.03551) | 142k+ agent-generated PRs, 29,609 with real merge conflicts (27.67% rate), 336k+ conflict regions — realistic conflict distribution for task design | CC BY 4.0 (Zenodo, 148.5 MB; DOI 10.5281/zenodo.19396916) | Stores compact conflict representations only (hash + preview); full content must be re-mined from GitHub; no ground-truth resolutions — success criteria must be test-based and self-built. |
| GitGoodBench (JetBrains, ACL 2025 REALM) | 900 (full) / 120 (lite) git-operation tasks (merge, rebase) | (verify per dataset) | Single-agent git skills; needs composition into concurrent scenarios. https://github.com/JetBrains-Research/git-good-bench |

Recommended construction: take a Commit0 library (or our own repo with
reverted commits), CAID-style dependency-aware split across N agents on
forked ANFS workspaces with ~20% file overlap, merge back sequentially;
success = suite passes + every overlap surfaced as a conflict (compare
against native-FS control where overlapping writes silently clobber).

## W3: Privacy / leakage red-teaming

No dataset ships exactly "documents with field-level PII labels +
extraction prompts"; the kernel-level leak test is assembled from:

| Candidate | Scale | License | Auto-eval | Fit for ANFS |
| --- | --- | --- | --- | --- |
| **PrivacyLens** | 493 instances; seed → vignette → trajectory; explicit `sensitive_info_items` field annotations | CC-BY-4.0 | probing (multi-choice) auto; action-based eval needs an LLM judge | Best corpus source: its annotated sensitive items map directly to ANFS fragment policy labels. https://huggingface.co/datasets/SALT-NLP/PrivacyLens |
| **AgentDojo** | suites of agent tasks + prompt-injection attacks (counts not in README) | MIT | automated benchmark scripts (attack/defense matrix) | Actively maintained (v0.1.35, 2025-10; 722 commits). Source of injection attack patterns; ANFS purpose gates can be evaluated as a "defense". https://github.com/ethz-spylab/agentdojo |
| **InjecAgent** | 1,054 test cases; 17 user tools x 62 attacker tools; direct-harm + data-stealing splits | MIT | fully local; ASR-valid / ASR-all metrics scripted | Data-stealing split is the relevant half; 2024-era, moderate maintenance. https://github.com/uiuc-kang-lab/InjecAgent |
| ConfAIde | 4 tiers (per-tier counts undocumented) | MIT | eval script needs a model | Tests contextual-integrity *reasoning*, not mechanism-level blocking — weakest fit; low maintenance (5 commits). https://github.com/skywalker023/confaide |

Recommended assembly: embed PrivacyLens sensitive items into an ANFS
document corpus as labeled fragments; drive extraction attempts with
InjecAgent data-stealing cases + AgentDojo injection patterns; measure leak
rate with kernel labels on vs off vs a prompt-only baseline.

### Implemented: kernel-label leak differential

`benchmarks/leak_redteam_benchmark.py` (proof locked in
`tests/test_leak_redteam.py`) runs the mechanism half of this experiment
without an LLM. A corpus of documents with sensitive fields is loaded into
ANFS twice — once with field-level policy labels + deny rules active
("kernel_on"), once without ("kernel_off", the plain-filesystem / vanilla-RAG
control) — and probed across every retrieval surface (read_node,
read_node_range, answer citation, search discovery, query discovery). A leak
is the sensitive byte string reaching the caller. Result on the built-in
corpus: **kernel_off leaks on 100% of content probes; kernel_on leaks on 0%**,
integrity clean. Pass `--corpus <json>` to swap in a PrivacyLens-derived
corpus (same schema, string fields). The prompt-only baseline (does a model
obey "don't reveal PII") is deliberately excluded — it is model-dependent and
is exactly the unreliable alternative ANFS replaces with a kernel guarantee.

## W4: Long-term conversational memory

| Candidate | Scale | License | Scoring | Notes |
| --- | --- | --- | --- | --- |
| **LongMemEval** (recommended) | 500 questions; _S ~40 sessions/115k tokens, _M ~500 sessions, + oracle | MIT | official script uses a GPT-4o judge (`autoeval_label`), not pure EM/F1 — budget for judge calls or add an EM/F1 layer | Active: 2025-09 data cleaning; LongMemEval-V2 (agentic) announced 2026-05. HF-hosted JSON downloads. https://github.com/xiaowu0162/longmemeval |
| LoCoMo | 10 conversations (`data/locomo10.json` in repo), QA + event-summary annotations | LICENSE.txt exists, type unverified — check before publishing results | F1 commonly used by memory papers; official README does not document the scoring script | Small but the de-facto comparison point for memory systems (Mem0/Letta/Zep all report on it); community critiques of judge variance exist. A multiple-choice variant (Percena/locomo-mc10) is on HF. https://github.com/snap-research/locomo |

`benchmarks/task_memory_benchmark.py` already implements LoCoMo-style
import/recall; wiring the official `locomo10.json` plus LongMemEval_S is
the smallest path to a real W4 run.

## Cross-cutting cautions

- Contamination: SWE-bench Lite and aider polyglot are largely pre-2024
  public data; SWE-bench-Live exists specifically to counter this.
- Several "auto-verified" claims hide an LLM judge (LongMemEval, PrivacyLens
  action-based) — distinguish mechanical verification (tests, EM/F1) from
  judge-based scoring when reporting ANFS results.
- AgenticFlict re-mining cost is nontrivial; treat it as a conflict
  *distribution* reference first, a task source second.
