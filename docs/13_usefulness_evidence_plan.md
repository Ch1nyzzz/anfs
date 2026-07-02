# 13 — 如何证明 ANFS 有用：证据计划与实验补充清单

本文回答一个问题：**ANFS 要靠什么证据证明自己有用，还缺哪些实验。**

结论来自两轮深度调研（2026-07，多 agent 对抗验证流程）：

1. **Benchmark 适配调研**：映射了 52 个主流 agent benchmark，对 fit 最高的 6 个
   候选做"专职反驳 + 工程可行性"双视角验证。结果：**6 个"接入 ANFS 即涨分"的
   主张全部被驳倒**（涨分杠杆已被更便宜的方案吃掉、ANFS 机制瞄准的失败模式不是
   主要丢分项、自家 tokio token-efficiency 数据反证检索主张），但 6 个的工程
   可行性全部成立（均 < 2 周）。
2. **Provenance 方向调研**：104 agent、13 条经 3 票对抗验证存活的发现。结论：
   领域正从"观察式追踪"转向"强制式溯源"，而 **"文件系统级、字节精确、
   强制 + 可重放"这一象限在最新综述（From Agent Traces to Trust,
   arXiv 2606.04990）的分类中是空白** —— 这正是 ANFS 的位置。

两轮调研指向同一个战略判断，与 `11_paper_plan.md` 的 thesis 一致并将其收紧：

> **不要在分数榜上证明有用（那条路已被逐项证伪）；
> 要在"性质"上证明有用 —— 强制性、可重放性、策略随血缘传播 ——
> 并用直接对照实验证明这些性质是观察式方案结构上给不了的。**

---

## 1. 证明策略：两条路线的取舍

### 路线 A：分数路线（"接上 ANFS，benchmark 涨分"）— 已证伪，不作为主线

对抗验证给出的三个结构性原因：

| 原因 | 证据 |
| --- | --- |
| 便宜方案已吃掉杠杆 | MLE-bench 的实验历史外置已被纯文件 journal 解决（AIDE/ML-Master）；LongMemEval 的事实取代机制 Zep/Graphiti 已发布（valid_at/invalid_at）；PrivacyLens 的"敏感信息不进上下文"已被纯 prompt 两段式实现（1-2-3 Check, +15–19pp） |
| ANFS 机制瞄准的不是主要丢分项 | LongMemEval-V2 错误由 reading error/gotchas 主导；MLE-bench 有效提交率与奖牌率脱钩（MLAB 44.3% valid / 0.8% medal） |
| 自家数据反证 | `token_efficiency_tokio.json`：ANFS 臂 40 次调用打满 max_steps、success=0，grep 基线 21 次调用 success=1；7 仓 QA 中 ANFS 零次把基线失败翻成成功 |

分数路线只保留**防御性/窄版**用途：C4 的"无回退"证明（本来就是分母），以及
少数有官方插件接口、能先做归因测量再决定投入的场景（见 E8/E9）。

### 路线 B：性质路线（"强制式溯源内核"）— 主线

文献恰好在 2026 上半年集中确认了这条路线的每一环：

- **日志不够**：时序 trace 日志结构上无法回答依赖问题（"Logs provide
  chronological observability, but dependency analysis requires graph
  structure", arXiv 2606.04990）；SBOM/运行时日志只提供碎片化证据
  （Agent-BOM, arXiv 2605.06812）。
- **工具层护栏不够**：会被绕过（agent 早先写好的脚本内部 `git commit`），
  强制必须下沉到 OS/FS 层；对 64 个 agent 项目 1361 条真实策略的测量显示
  16% 是 cross-event 型（"提交前必须先跑测试"——正是 lineage gate 的目标
  类别），81% 的仓库至少含一条（ActPlane, arXiv 2606.25189）。
- **签名不够**：in-toto/SLSA 式仅签名溯源防不住 LLM 洗白的派生载荷
  （signature-only 在 2/3 攻击类 ASR=1.00），血缘感知强制才压到 0
  （MemLineage, arXiv 2605.14421）。
- **策略标签在派生步丢失**：Fides 等 IFC 规划器的标签只在单次执行内有效
  （"labels are erased at the boundary of a planner run"）——这正是 Meta
  Trellis（arXiv 2606.29823）开放问题 "governed view maintenance"，也正是
  ANFS field-level 标签随 `derived_from` 结构传播所补的缺口。
- **修复能力从未被评估**：provenance-driven repair（失效过期记忆、隔离污染
  证据、回滚不安全变更）被综述 §7.5 明确列为现有基准从不评估的能力。

**与 Trellis 的定位句**（论文可直接用）：
Trellis 定义了经验图的查询/治理操作语义（Resume/Reuse/Train/Replay/Audit/
Govern）；ANFS 提供使这些操作**可强制、可重放**的存储内核。上下层互补，
非竞争。Trellis 开放问题 ↔ ANFS 已有机制的映射：governed view maintenance
↔ C1 标签结构传播；并发树搜索一致性（"最终一致性、界未定义"）↔ C2 显式
冲突 + exactly-one-wins；bi-temporal memory ↔ 追加式事件日志（transaction
time 侧）。

---

## 2. 现有证据盘点与缺口

对照 `11_paper_plan.md` 的 claim 表：

| Claim | 已有证据 | 缺口（本文实验清单对应项） |
| --- | --- | --- |
| C1 隐私 | 机制对照 100%→0%；真实 LLM 对照 prompt-only 75% / scrubber 25% / ANFS 0% | 真实语料（PrivacyLens）+ labeler 召回瓶颈的诚实刻画 → **E4** |
| C2 并发 | 三方对照（plain dir/git/ANFS）；16 进程 exactly-one-wins | 外部 harness 落地 + 形式化契约 → **E5, E7** |
| C3 溯源 | 派生闭包 100% vs git 0%；任意事件边界重放 | **没有对照实验证明"观察式方案做不到"** → **E2, E3**（本文最重要的两个新实验） |
| C4 兼容 | 5/5 修 bug 无回退；边界成本 66ms@50 / 278ms@200 files | SWE-bench-Live 规模化（防御性，非涨分主张） |
| C5 记忆 | scaffold + 快照（合成数据全对，无差异） | 归因测量先行，再决定是否投入 → **E9** |
| （检索/token） | 7 仓精度全胜 grep；token 省 −15%~+48% | **自家 tokio 反证未修复，信誉风险** → **E1** |

关键洞察：**C3 是 thesis 里最独特的 claim，却是目前唯一没有"status-quo
可见失败"对照实验的**（现有对照 git 太弱，真正的 status-quo 是 LangSmith 式
trace 日志和工具层护栏）。E2/E3 补的就是这一块。

---

## 3. 实验清单

优先级：P0 = 信誉修复（先做）；P1 = 论文主线新实验；P2 = 已有 claim 的真实
数据落地；P3 = 可选/防御性。每项含预注册判据 —— 判据不满足时的动作也写明，
避免事后合理化。

### P0

#### E1 — 修复 token-efficiency 评测的 ANFS 臂并重跑（零成本，先做）

- **目的**：消除自家反证。当前 `token_efficiency_tokio.json` 中 ANFS 臂
  40 次调用打满 max_steps、success=0，是任何外部检索主张的直接反例。
- **设计**：诊断 ANFS 臂不收敛的原因（步数上限过低 vs 工具提示导致的过度
  细粒度导航），修复后同一模型 3 seeds 重跑 7 仓。
- **预注册判据**：正确性（key_facts）相对 grep 基线的**净翻转数 ≥ 0**，
  且 Go/Java/Swift 上 token 节省保持。若净翻转 < 0：收窄 README 与论文中
  的检索主张为"精度 + 单调用归因 + 可审计"，**删除** token/正确率增益表述。
- **工作量**：0.5–1 天。 **对应**：README 检索节、C1 检索线。

### P1 — 论文主线（证明"性质是观察式方案给不了的"）

#### E2 — 强制覆盖差集实验：FS 层 vs 工具层护栏（headline 实验）

- **目的**：没有任何已发表工作直接测量过"文件系统层能拦、工具调用层拦不住
  的违规占多大比例"（ActPlane 只有定性论证 + 合规提升总数）。这一个数字就是
  ANFS 论文最有力的差异化证据，同时顺带产出开销与误拦率数据。
- **设计**：
  1. 语料：复用 ActPlane 的 1361 条真实 agent 策略中 cross-event 子集
     （约 16%，~218 条；其分类法公开），映射到 ANFS 可表达的 lineage-gate /
     标签规则；无法映射的如实报告为覆盖边界。
  2. 三臂，同一多 agent 编码工作负载 + 注入的违规脚本（含"绕过工具层"类：
     agent 生成的脚本内部执行 git commit / 读密钥文件 / 覆写他人产物）：
     (a) 无护栏；(b) 工具调用中介护栏（Agent-Sentry 式，在 tool-call 层做
     同样的策略检查）；(c) ANFS lineage gate + 策略标签。
  3. 指标：每臂的违规拦截率（按策略类别分层：single-event vs cross-event
     vs 绕过工具层）、**误拦率**（false gate：合法操作被拒）、每操作开销。
- **预注册判据**：(c) 在"绕过工具层 + cross-event"类别上拦截率显著高于
  (b)，开销 < 10%（对标 ActPlane 的 1.9–8.4%），误拦率 < 5%。若 (b)≈(c)：
  论文改写为"工具层与 FS 层强制等价的条件刻画"，仍是可发表结论。
- **工作量**：~2 周（策略映射 4–5 天、违规场景构造 3–4 天、三臂跑批 +
  分析 3–4 天）。 **对应**：C1+C2+C3 合体；论文 §evaluation 主图。

#### E3 — Provenance-driven repair 评测（自定义评测，填公认空白）

- **目的**：综述 2606.04990 §7.5 明确指出"失效过期记忆、隔离受污染证据、
  回滚不安全状态变更"从未被任何基准评估。ANFS 的 lineage 闭包 + CAS 重放
  + gate 是这些动作的强制机制。没有现成基准 = 我们定义规则。
- **设计**：
  1. 多 agent 会话中注入污染源（MemLineage 式：不可信外部输入经 LLM 改写
     后成为看似合法的派生产物，再被下游 agent 消费 2–3 跳）。
  2. 事后给出污染源，两臂执行修复：(a) trace-log 臂 —— 只有 LangSmith 式
     时序日志，靠文本搜索/LLM 推断找污染后代；(b) ANFS 臂 ——
     `lineage_nodes(污染源, downstream)` 闭包 + gate 拒绝未修复派生物参与
     后续 approve。
  3. 指标：污染后代识别 recall / precision、修复完整性（残留污染引用数）、
     修复用时、误隔离数。
- **预注册判据**：ANFS 臂后代识别 recall = 100%（结构保证，须实测确认）
  且 precision 显著高于 trace-log 臂；trace-log 臂在 ≥3 跳派生链上 recall
  可见下降。若 trace-log 臂 recall 也接近 100%：说明该工作负载派生链太浅，
  加深链路与改写强度后重跑；仍持平则删除该实验。
- **工作量**：1–1.5 周。 **对应**：C3 的"status-quo 可见失败"对照。

#### E7 — 并发契约形式化 + 属性测试（写作为主，低成本高回报）

- **目的**：把 C2 从"我们有冲突检测"升级为"我们声明了明确的隔离契约"。
  Trellis 自承并发统计只有"最终一致性、正确性界未定义"；TOKI
  （arXiv 2606.06240）提供了现成的形式化词汇：隔离级别、admitted
  anomalies、provenance-preserving belief updates（败方事实保留在审计行）。
- **设计**：用 TOKI 词汇写出 ANFS 的并发契约（哪些异常被排除、哪些被承认、
  ref 生命周期的隔离前置条件），为每条契约补一个属性测试/并发测试（现有
  16 进程 harness 扩展）。注意：TOKI 中"每个把 LLM judge 留在写路径的基线
  都承认至少一种异常"这一强论断经对抗验证被否决，**不可引用**。
- **工作量**：3–5 天。 **对应**：C2；直接回应 Trellis 开放问题。

### P2 — 已有 claim 的真实数据落地

#### E4 — PrivacyLens 语料上的 C1（定位为系统论文曲线，不做 leaderboard 主张）

- **背景（对抗验证结论）**："机械打标 → 泄漏归零 → 上榜"这条链已被驳倒：
  seed 只给抽象类型不给字节位置（连原论文都用 LM+人工抽取敏感条目）；用
  真值打标 = 测试时答案泄漏；字段粒度整段删除会毁 helpfulness（敏感与任务
  必需信息被对抗性交织在同一自由文本字段）。
- **诚实的设计**：架构 = LM labeler（定位敏感 span）+ ANFS 内核强制（标签
  一旦打上，字节结构性不可达 + 随派生传播）。报告三条曲线而非单一泄漏率：
  (1) labeler 召回率（瓶颈，如实报告）；(2) 泄漏率 vs prompt-only / regex
  scrubber / 1-2-3 Check 式两段 prompt 流水线；(3) helpfulness 保持率。
  卖点是**离线可审计 + 对不合规执行体的强制性**（prompt 流水线两者皆无）。
- **先行证伪实验（30 分钟，通不过就降级）**：取 50 条轨迹，(a) 只用 seed
  推导 span 的覆盖率（预期接近 0，确认 labeler 必要性）；(b) 用真值定位 +
  字段粒度置空后的 helpfulness 损失（确认粒度问题的量级）。
- **工作量**：8–13 天（含 labeler 3–5 天）。 **对应**：C1 的
  "*Needed: real PrivacyLens corpus*"。

#### E5 — STORM harness 状态后端替换（C2 的外部落地）

- **背景**：CoAgent（arXiv 2606.15376）的 contended workload 未公开且状态
  底座是 K8s/数据库（FS 不可承载）；但其伴生系统 **STORM**
  （github.com/dreamyang-liu/STORM，OpenHands SDK + Docker，评测
  Commit0-Lite/PaperBench）的状态层就是**逐文件版本计数器做写时校验** ——
  ANFS（版本化节点 + 显式冲突 + 溯源）是近乎 drop-in 的后端替换。
- **设计**：三臂 —— STORM 原版版本计数器 / GitWorktree / ANFS 后端，同一
  多 agent 任务集。指标：冲突检出、静默丢失更新数、token 开销、任务完成率。
- **定位**：诚实写成"把 OCC 协调从 harness 应用层下沉到存储内核"的工程
  论证 + 免费获得的溯源/重放能力，**不声称超越已发表协调协议**。
- **预注册判据**：静默丢失 = 0（结构保证）、任务完成率无回退、token 开销
  ≤ 版本计数器臂的 1.2 倍。
- **工作量**：5–8 天。 **对应**：C2 落地 + C4。

#### E6 — 端到端 case study（paper plan 评估计划第 4 条，保留并升级）

一次多 agent 编码会话在同一 session 内展示：并行编辑冲突被 surface（C2）、
密钥文件对日志 agent 结构性不可见（C1）、事后完整血缘重建 + 从污染点修复
（C3/E3 机制）、agent 全程原生速度工作（C4）。一张图承载全部 thesis。
在 E2–E5 基础上组装，~3 天。

### P3 — 可选/防御性（先做便宜的归因测量，再决定投入）

#### E8 — MLE-bench Lite 三臂消融（卖点：零开销可回放溯源，不承诺涨分）

三臂：(a) AIDE 原版 journal；(b) journal 换 ANFS 版本化节点 + lineage-gated
提交验证；(c) 安慰剂 = 纯 JSONL 日志 + "退出前重验最终提交"的 5 行钩子。
**预注册判据**：若 (b) 对 (c) 的 medal 率差 ≤ seed 间标准误（±2pts），则
放弃机制特异性主张，只保留"(b) 以 ≈0 开销免费获得完整可回放溯源"的
工程结论（这本身仍可写入论文 cost 节）。用 Lite 子集（22 赛题）+ 12h 预算
控制算力。7–11 工程日 + 算力。

#### E9 — LongMemEval-V2 memory backend（先归因，后集成）

该基准有官方插件契约（`Memory.insert/query`，无需 MCP）。**但先做半天的
归因测量**：取 AgentRunbook-C 在 Small 上的 113 个错误，数"过期值被当作
当前值呈现 / 矛盾值无标记并列"的占比；**< 15–20 个则整个方向放弃**。
若可观，再做最小对照：先在现有文件方案上加"按时间排序 + 标注 superseded"
的 prompt 级改动 —— 若 prompt 改动就拿到全部收益，归因链断，同样放弃。
全部通过才投入 8–12 天的后端实现。

#### E10 — 补充定向调研：监管侧证据形态（motivation 缺环）

Provenance 调研中监管线（EU AI Act 第 12 条日志/记录保存义务、NIST AI RMF、
金融/医疗审计的具体证据形态要求）没有产出任何存活的已验证声明。论文
motivation 若要引用合规驱动，需要一轮针对法规原文与审计实务的定向调研
（1–2 天），否则 motivation 只引学术痛点。

---

## 4. 不做清单（同样重要）

| 不做 | 原因（均经对抗验证） |
| --- | --- |
| SWE-bench 家族 / Terminal-Bench 等分数榜主张 | 信息丢失杠杆已被便宜方案吃掉；ANFS 机制不对准主要丢分项 |
| LongMemEval-S 主榜竞争 | SOTA 83–93% 靠稠密检索/抽取配方，与存储语义正交；ANFS 词法 FTS 在决定性轴上无差异化 |
| 审计图表示（Agent-BOM 类） | 三个月内 4 篇同类（Agent-BOM/Agent Audit/GRADE/AgentRiskBOM），赛道拥挤且是观察式 |
| 观察式 tracing 竞争（LangSmith/Langfuse/OTel GenAI） | 该层已成熟；ANFS 应站在"日志结构性不够"的批评侧 |
| CoAgent 的 K8s/活集群争用单元 | 状态不经过文件系统，论文自己论证 FS 级版本化不适用 |
| pdfQA/AirQA 类多模态文档 QA | 无 PDF parser；~50% 题目卡在表格/公式/图，非 ANFS 能力 |

---

## 5. 论文定位与必引文献

**定位句**：ANFS 占据"文件系统级、字节精确、强制式 + 可重放"象限 ——
该象限在 2606.04990 的分类（观察式追踪 + 运行时护栏）中是空白。注意
**不可自称"唯一的强制式溯源系统"**：Agent-Sentry（2603.22868）在工具
中介层、ActPlane 在 syscall 层、MemLineage 在记忆条目层已有强制 ——
差异化必须落在"文件系统层 + 字节级历史 + 任意事件边界重放 + field-level
标签传播"的组合上。

必引并逐一区分（除注明外均为 2026 年 3–6 月 arXiv 预印本，引用时称
"提出的框架"而非"已验证结果"；投稿前需重扫，领域三个月一个样）：

| 工作 | 关系 | 区分轴 |
| --- | --- | --- |
| Trellis (2606.29823, Meta) | 上层互补 | 我们是其 Audit/Govern 操作的强制存储内核；三个开放问题的映射见 §1 |
| ActPlane (2606.25189) | 最近邻（OS 层强制） | 它实时 eBPF、无重放/无 CAS/整对象标签；我们 CAS+因果边+field-level+任意边界重放 |
| MemLineage (2605.14421) | 最近邻（记忆层强制） | 同为血缘门控，但作用于记忆条目层非 FS 层；其 signature-only 失败结果可借用作动机 |
| From Agent Traces to Trust (2606.04990) | 综述/分类 | 其 "execution provenance" 定义与 ANFS 抽象同构；我们的象限在其分类中空白；§7.5 repair 空白由 E3 填 |
| PROV-AGENT (2508.02866, IEEE e-Science 2025，已评审) | 观察式代表 | 装饰器插桩只记录不强制；其"既有方法无法关联 agent 元数据与下游结果"可作动机引用 |
| Agent-BOM (2605.06812) | 相邻图表示 | 观察式/事后重建；借其"语义鸿沟"论证粒度轴，不在表示上竞争 |
| Auditable Agents (2604.05485, USC) | 框架级立场 | "无可审计性即无问责"；其 6 项目 617 项安全发现（自动扫描，FP≈13%）作生态现状引用 |
| ActiveGraph (2605.21997，单作者) | log-first 先例 | 运行时框架层事件日志+确定性投影；无字节级 CAS、无强制 |
| TOKI (2606.06240，单作者) | 形式化词汇来源 | 借其隔离级别/admitted anomalies 表述 E7 契约；其最强论断已被否决，不可引用 |
| DIFC 经典（Jif/Flume/HiStar）、PASS、版本化 FS | 见 11_paper_plan §related work | 维持原区分论证 |

---

## 6. 建议的执行顺序

```
第 1 周     E1（0.5–1天）→ E7（3–5天）→ E4 的 30 分钟证伪实验 → E9 的半天归因测量
第 2–3 周   E2（headline，~2周），并行 E3（1–1.5周）
第 4 周     E5（5–8天）；E4 若证伪实验通过则启动（8–13天，可与 E5 并行）
第 5 周     E6 端到端 case study（~3天）+ E10 补充调研（1–2天）
机动        E8/E9 仅在其 gate 通过后投入
```

P0+P1（E1/E2/E3/E7）合计约 4 周，产出论文的全部差异化证据；P2 再加 2–3 周
把 C1/C2 落到真实语料与外部 harness。全部预注册判据在动手前写入各实验的
snapshot 说明，判据不满足时按本文写明的降级动作执行，不事后调整主张。

---

*调研原始材料：两轮 workflow 的完整验证输出（52 候选清单、6 候选逐条反驳
意见与 first-experiment 设计、13 条 provenance 发现的原文引句与 URL）存于
会话 scratchpad（`anfs_benchmark_research_full.json`、
`provenance_research_full.json`）；核心结论已同步至本文，快照数字出处为
`docs/benchmark_snapshots/README.md`。*
