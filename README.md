# DeepResearch Agent

## Overview / 项目概览

DeepResearch Agent 是一个面向证据驱动研究的命令行项目：将开放式问题规划为研究任务，搜索并读取外部来源，再经过分析与综合输出研究摘要。

当前已完成 **Phase 1 — Research 基础链路**、**Phase 2 — Parallel Research** 和 **Phase 3 — Evidence System**（3A 基础设施、3B Researcher 抽取、3C 跨任务聚合与复用）。系统动态生成研究 DAG，并行执行就绪任务，同时保留 Markdown Memo、压缩结果和可追溯证据。事实验证、带引用的完整报告及持久化留待后续阶段。

## Features / 当前能力

- 动态规划：根据问题选择互补的独立 discovery 子问题，生成并校验带依赖的 ResearchPlan。
- 并行研究：按批次执行依赖已满足的任务，使用可配置的并发上限控制 Researcher execution 数量。
- 搜索与读取：通过 Tavily 获取标题、URL 和短摘要，通过 HTTP 与 BeautifulSoup 读取 HTML 来源正文。
- 上下文隔离：每个任务创建独立 Agent、消息历史和 ToolBudget，接收依赖任务的压缩结果及有大小限制的 Claim/Evidence 来源链。
- 证据抽取：从实际成功读取的网页抽取原文片段，生成有 Evidence 引用的 Claim；任务局部 Bundle 由 Orchestrator 合并到运行级 Store。
- 多阶段输出：保留 discovery、analysis、synthesis 的职责，最终按计划顺序输出结果。
- 执行可观测性：记录任务 ID、工具与模型请求阶段、结果数量、耗时、等待进度及明确的失败原因。

## Development Phases / 开发阶段

Phase 1 建立完整 Research 主链路；Phase 2 升级任务拆分与并行执行；Phase 3 建立可追溯、可复用的证据数据。以下记录已完成阶段，并为 Phase 4～Phase 8 保留目标、范围与验收结构。

### Phase 1 — Research 基础链路

**Status: Completed**

#### 1. Goal / 阶段目标

将用户问题转化为可执行的研究计划，跑通“规划 → 获取资料 → 分析 → 综合 → 输出结果”的完整流程。重点是组件衔接、任务职责和依赖结果传递，执行方式为串行。

#### 2. Architecture / 组件职责

| 组件 | 职责 |
| --- | --- |
| Supervisor | 理解问题并生成研究计划，使用结构化输出或 JSON 回退解析；不负责搜索网页。 |
| ResearchPlan | 保存总体目标 `goal` 和任务列表 `tasks`，通过任务依赖表达研究流程。 |
| ResearchTask | 保存 `id`、`title`、`question`、`description`、`task_type`、`depends_on` 和 `status`。 |
| ResearchOrchestrator | 校验依赖，串行执行依赖已满足的任务，收集结果并更新任务状态。 |
| Researcher | 为当前任务创建独立 DeepAgent，根据任务类型使用工具，生成 Research Memo。 |
| web_search | 获取外部来源的标题、URL 与摘要；搜索结果作为研究线索，不直接生成研究总结。 |
| fetch_webpage | 读取 HTML 页面并提取标题、日期元数据和有限长度的正文。 |
| ToolBudget | 为每个任务单独限制搜索和网页读取次数。 |
| ResearchExecutionResult | 保存单任务的 `memo`、`compact` 和 `tools_used`。 |
| CompactResearchResult | 保存简短摘要、发现、来源与不确定信息，供依赖该任务的后续任务使用。 |
| ResearchResult | 汇总整个计划、各任务 Memo 和压缩结果，供调用者读取。 |

Phase 1 已有依赖字段和依赖校验，计划通常表现为 discovery → analysis → synthesis 的线性链路；这一阶段尚未实现多个任务同时执行。

#### 3. Workflow / 串行研究流程

```mermaid
flowchart TD
    Q[User Question] --> P[Supervisor]
    P --> Plan[ResearchPlan 与依赖校验]
    Plan --> O[ResearchOrchestrator 串行执行]
    O --> D[Discovery：搜索与读取来源]
    D --> DC[Discovery Memo 与 CompactResearchResult]
    DC --> A[Analysis：读取依赖结果并分析来源]
    A --> AC[Analysis Memo 与 CompactResearchResult]
    AC --> S[Synthesis：综合已有结果]
    S --> R[Research Results]
```

这是典型流程；任务依赖由计划表达。任务间传递的是压缩结果，不包含前一个 Agent 的工具历史或整页正文。

#### 4. Task Types / 执行职责

| 类型 | 执行职责 | 信息来源 |
| --- | --- | --- |
| `discovery` | 主动搜索新的外部资料，读取重要来源，为后续研究提供发现与来源线索。 | `web_search`、`fetch_webpage`，以及需要时的依赖上下文。 |
| `analysis` | 根据前置结果深入读取相关来源，分析方法、贡献、实验结果、差异或局限；资料不足时可补充搜索。 | 依赖任务的 CompactResearchResult 与本任务读取的来源正文。 |
| `synthesis` | 比较并整合已有发现，形成共识、差异和信息缺口；通常不重新搜索，明确缺口时可在预算内补充。 | 前置任务的压缩结果。 |

这三类任务的职责在 Phase 2 中延续。Phase 1 的来源读取由 Agent 根据提示调用工具，程序在缺少必要调用时提醒并检查；后续的程序预读机制见 Phase 2 的稳定性补充。

#### 5. Result / 阶段成果

Phase 1 已具备问题规划、任务拆分、依赖校验、搜索工具调用、HTML 网页读取、独立任务预算、多阶段 Research Pipeline，以及结构化结果传递能力。

给用户的 Memo 默认使用简体中文，包含“研究任务”“主要发现”“重要来源”“不确定性与缺失信息”四部分。来源条目要求尽量提供标题、URL、可确认的年份或日期、相关性及关键点；未知年份应标为 `unknown`。Markdown 规则从 Memo 提取压缩结果，并对来源标题进行轻量去重。

### Phase 2 — Parallel Research

**Status: Completed**

#### 1. Goal / 阶段目标

在 Phase 1 的 Research 主链路上增加 **Task Decomposition + 独立 Researcher executions + Parallel Research**：先将问题拆为研究 DAG，再并行执行没有前置依赖或依赖已完成的任务。

本项目当前的“SubAgents”能力体现为 Orchestrator 为多个任务分别启动独立 Researcher execution。每次 execution 创建自己的 DeepAgent。当前代码使用 `subagents=[]`，并关闭默认 general-purpose subagent，没有启用 DeepAgents 内部的嵌套子代理或代理间 handoff。

#### 2. What Changed from Phase 1 / 相对 Phase 1 的变化

| 能力 | Phase 1 | Phase 2 |
| --- | --- | --- |
| 任务拆分 | 通常是一条 discovery → analysis → synthesis 链路。 | 根据问题动态生成多个互补、无依赖的 discovery 分支，再分析与综合。 |
| 规划过程 | 直接请求模型生成计划。 | 先识别独立搜索子问题，再生成并校验 ResearchPlan。 |
| 任务数量 | Supervisor 校验 3～6 项任务。 | Supervisor 校验 2～6 项任务，允许简单问题采用 discovery → synthesis。 |
| 计划结构 | 已有依赖字段与 DAG 校验，但常见计划偏线性。 | 显式支持多个独立分支汇合的 Research DAG。 |
| 执行方式 | 就绪任务逐个串行执行。 | 每批选择最多 `max_concurrency` 个就绪任务，同时执行。 |
| Researcher 状态 | 各任务已有独立 Agent 和预算，但同一时间只执行一个。 | 多个独立 execution 同时运行，分别持有消息、预算和依赖结果副本。 |
| 依赖处理 | 依赖结果存在后串行执行下游任务。 | 必须同时满足依赖状态为 `completed` 且结果已保存，才允许进入执行批次。 |
| 并发配置 | 没有并行执行上限参数。 | Orchestrator 构造参数 `max_concurrency`，默认值为 3。 |
| 执行与展示顺序 | 串行执行并打印结果。 | 任务按允许的依赖并行执行，最终结果仍按原始计划顺序输出。 |
| 失败处理 | 当前任务失败后终止流程。 | 等待当前批次收齐结果，保留并展示成功任务，报告失败及未执行任务，停止后续调度。 |

搜索后端切换、analysis 来源预读、超时与进度日志是随后补充的稳定性修复，下面单独说明；它们不属于 DAG 调度本身。

#### 3. Phase 2A — Task Decomposition Upgrade

Supervisor 使用同一个模型完成两步规划：

1. 读取 [decomposition.md](app/prompts/decomposition.md)，根据当前问题识别可独立搜索的互补子问题。
2. 将这些子问题与原始目标、当前日期交给 [supervisor.md](app/prompts/supervisor.md)，生成 ResearchPlan。

子问题数量由研究范围决定，通常选择 2～3 个，最多 4 个；这不是固定领域模板。提示要求每个子问题保留原始问题的完整核心主题、限定对象和时间范围，合并重复或包含关系明显的分支，不把“找论文”和“找综述”等来源体裁当成互补维度。

生成的计划由 Supervisor 校验：

- 总任务数为 2～6；每个任务必须显式提供 `task_type`，初始状态为 `pending`。
- discovery 必须满足 `depends_on=[]`；analysis 和 synthesis 必须依赖已有任务。
- 任务 ID 唯一，依赖不能重复、自引用、指向不存在的任务或形成循环。

典型结构为 D1、D2（必要时增加其他 discovery）→ A → S。简单问题也可以直接使用 discovery → synthesis，不要求固定五个任务。

Phase 2A 当时保留串行 Orchestrator，只升级规划与必要的 schema 描述；真正并行执行由 Phase 2B 引入。主题保持与分支互补由提示约束，当前尚无独立的语义质量验证器。

#### 4. Phase 2B — Parallel Execution

[ResearchOrchestrator](app/research/orchestrator.py) 使用批次式 DAG 调度：

1. 校验计划与初始任务状态。
2. 找出状态为 `pending`、所有依赖已 `completed` 且执行结果已保存的 ready tasks。
3. 按计划顺序选择最多 `max_concurrency` 个任务，置为 `running`。
4. 使用 `asyncio.gather(..., return_exceptions=True)` 并行等待这一批；同步 `Researcher.research()` 通过 `asyncio.to_thread()` 接入。
5. 收集成功结果，任务置为 `completed`；异常任务置为 `failed`，然后判断是否继续下一批。

批次大小直接限制并发数量，当前没有使用 Semaphore。即使某个任务先完成，也要等这一批全部返回后才重新计算 ready tasks。调度器支持依赖允许的任务并行，并不只针对 discovery；生成计划通常将独立 discovery 安排在第一批。

下游任务必须等待其 `depends_on` 中的全部任务完成。例如 A 依赖 D1、D2，S 依赖 A，则 A 不能在 D2 尚未完成时开始，S 也不能在 A 尚未完成时开始。

每个 execution 创建独立 Agent、消息历史、当前任务请求和 ToolBudget。传入的依赖 CompactResearchResult 使用深拷贝，任务间继续通过压缩结果传递信息，不共享前序 Agent 的原始消息。

失败处理保持简单：任一任务失败时，等待当前批次返回、展示成功任务的 Memo、列出失败原因和仍未执行的任务，随后终止运行。当前不继续调度其他批次，也不自动重规划或重跑研究。若仍有 pending tasks 却没有 ready tasks，则明确报错，避免调度循环无限等待。

**后续稳定性补充：**

- 搜索从原来的 DDGS 切换到 Tavily API，保持 `web_search(query, max_results)` 及标题、URL、摘要返回格式；认证、限流、配额、超时和异常结果格式均明确报错。
- Phase 2 中，analysis 的压缩依赖上下文包含 URL 时，程序在模型执行前预读来源，优先交错选择不同依赖任务的来源，目标为最多 2 个可读页面。失败时在原有 3 次读取预算内尝试其他候选；非空正文才作为有效读取结果。Phase 3C 已在存在可用上游 Claim/Evidence 时改为优先复用，保留无结构化证据时的预读。
- 预读与模型后续调用共享同一个预算。成功正文和读取失败原因仅进入当前任务请求；全部来源不可读则明确失败，部分不可读则要求模型说明证据缺口。
- 普通模型请求默认配置 60 秒 HTTP 超时、0 次自动重试；Phase 3B 证据抽取使用独立的 120 秒请求时限。每项 Researcher 任务默认有 300 秒总时间预算，等待时每 15 秒输出阶段和耗时。任务超时后禁止新的工具与模型调用，丢弃迟到结果；已经发出的同步请求按底层超时收尾，不强行终止线程。

#### 5. Parallel Research Workflow / 并行工作流

```mermaid
flowchart TD
    Q[User Question] --> P[Supervisor：动态拆分与规划]
    P --> Plan[ResearchPlan / DAG]
    Plan --> O[ResearchOrchestrator / Ready Task Scheduler]
    O --> D1[D1：独立 Researcher execution]
    O --> D2[D2：独立 Researcher execution]
    O -. 可选分支 .-> D3[D3：独立 Researcher execution]
    D1 --> C[保存结果与状态；等待所有依赖完成]
    D2 --> C
    D3 --> C
    C --> A[Analysis：依赖上下文与本任务来源读取]
    A --> S[Synthesis：综合已有结果]
    S --> R[按 ResearchPlan 顺序展示 Research Results]
```

图中分支数是示意，实际数量来自 Supervisor 生成的计划；并发上限不要求每批必须填满。

#### 6. Example / 真实运行案例

问题：`调研 sparse reward offline reinforcement learning 的最新研究进展`

一次实际运行生成了以下计划：

| ID | 类型 | 子任务 | 依赖 |
| --- | --- | --- | --- |
| D1 | discovery | sparse reward offline reinforcement learning 在 Atari 游戏上的研究进展 | 无 |
| D2 | discovery | sparse reward offline reinforcement learning 在连续控制任务中的研究进展 | 无 |
| A | analysis | 分析两个方向的已有研究结果 | D1、D2 |
| S | synthesis | 综合总体研究进展 | A |

执行批次为：

```text
Batch 1：D1 + D2 并行执行
Batch 2：A，等待 D1、D2 都完成后开始
Batch 3：S，等待 A 完成后开始
最终展示：D1 → D2 → A → S
```

该次日志中 D1、D2 的工具与模型请求交错出现，A 实际预读 2 个来源，S 未调用 Web，四个任务均完成。此案例验证规划与调度流程；子任务划分是当次模型生成的结果，研究摘要中的年份、主题相关性及论断仍需核验。

#### 7. Result / 阶段成果

Phase 2 新增了动态 discovery 分支拆分、两步规划、宽 DAG、独立 Researcher execution 的并行运行、依赖感知调度、可配置并发上限，以及并发场景下的状态管理、依赖结果隔离和稳定结果展示。

已有 pytest 覆盖多个独立任务重叠执行、超过并发上限时分批、下游等待全部依赖、失败批次结束、结果顺序、状态隔离，以及来源预读和超时处理。Phase 2 的完成范围是规划与执行链路，尚不代表研究内容已经经过事实验证。

### Phase 3 — Evidence System

**Status: Completed**

- **Goal / 目标：** 将研究来源与发现整理为可追溯的证据数据。
- **Planned Scope / 计划范围：** Source、Evidence、Claim、Evidence Store，以及来源与论断之间的关联。
- **Completion Criteria / 预期验收：** 可以按研究任务和论断保存、检索证据，并回溯对应来源。

**Phase 3A — Evidence System Foundation：** 已实现 Source、Evidence、Claim、TaskEvidenceBundle 和内存 EvidenceStore，支持 URL 去重、引用完整性检查、原子合并及 Claim → Evidence → Source 查询。静态 Demo：`python demo_evidence.py`，无需 LLM 或网络。

**Phase 3B — Researcher Evidence Extraction：** Researcher 在研究结束后调用按当前候选构建的 JSON Schema，并使用 Pydantic 校验结果，选择原文候选片段并生成关联结论；不支持原生 JSON Schema 的服务使用一次 JSON 兼容回退并校验。程序将成功读取页面中的单句及相邻句组合提供为候选，保留一个完整机制或事实，并将新片段与上游 Evidence 统一编号。模型只选择候选及 Claim 支持关系，程序直接复制原文或复用已有 Evidence。Source 仅登记被选作证据的成功读取页面，不登记所有搜索候选。片段与引用校验只检查抽取来源，不判断 Claim 是否得到充分支持；每个正式 Claim 必须至少引用一条 Evidence，内部 ID 全部由程序生成。

Discovery 返回局部 Bundle；ResearchExecutionResult 同时携带 Memo 和 Bundle（兼容字段默认仍为 `None`），ResearchResult 提供运行级 `evidence_store` 与任务级 `evidence_bundles`。Orchestrator 在批次返回后统一 merge；Researcher 只接收只读快照，不直接写 Global Store。CLI 默认显示数量统计，`SHOW_EVIDENCE_DEBUG=true` 可额外显示 Claim → Evidence → Source 内容及 provenance。

**Phase 3C — Cross-Task Evidence Aggregation and Reuse：**

- 单次运行共用一个 Global EvidenceStore。Source 按规范化 URL 去重；移除 fragment、尾斜杠、默认端口，统一主机大小写。已知 arXiv `abs/html/pdf` 表示共用论文身份，保留明确的 `v1/v2`、查询参数与原始读取 URL；不做联网 canonical 解析。PDF 只参与身份匹配，读取工具仍不解析 PDF。
- Evidence 仅在 **相同 canonical Source + 空白规范化后完全相同的原文** 时去重；大小写和标点保留，不做语义合并。保留第一条 Evidence 的 ID、正文、location 和提取任务；Claim 不按文本去重。
- 合并事务同时维护 Source/Evidence 本地 ID 别名、canonical 引用、provenance 及反向索引：先重映射 `Evidence.source_id`，再重映射并去重 `Claim.evidence_ids`。任一引用或 ID 冲突使整次 merge 回滚。返回 Bundle 的 Evidence 只包含本任务最初提取的 canonical 片段；跨任务复用直接体现在 Claim 引用中，不复制片段。
- `Source.task_id` 始终表示首次发现任务，可通过只读属性 `discovered_by_task` 访问；`Evidence.task_id` 始终表示首次提取任务，可通过 `extracted_by_task` 访问。后续使用任务由 Store 的普通索引记录，`get_source_provenance()` / `get_evidence_provenance()` 返回不可变的 `used_by_tasks`，后者还提供 `referenced_by_claim_ids` 与 `is_cited`。这些元数据由 Store 根据实际合并和 Claim 引用更新，不引入可互相冲突的多个来源任务字段。
- Analysis 优先复用上游 Claim 和原文 Evidence，不强制重复预读；缺少具体细节时仍可使用现有搜索/读取预算。当没有可用结构化证据、只有来源 URL 时，保留 Phase 2 来源预读行为。这不是自动 Gap Detection 或 Re-Research。
- Synthesis 不注册 Web 工具，预算为 0/0；抽取只接受已有 upstream Evidence，不创建新 Source/Evidence。缺少 Evidence 的发现保留在 Memo 不确定性中，不产生无证据的正式 Claim。
- `build_evidence_context(task, store)` 默认选择 direct dependencies 的 Claims；Orchestrator 为 Synthesis 显式加入依赖链上的祖先任务，补充中间阶段可能遗漏的 discovery 维度。沿引用取得完整 Evidence 与 Source metadata，继承的 Evidence 保留原 ID。按任务交错选择完整支持链，最多 12,000 字符、12 Claims、24 Evidence；不能容纳的整组跳过，不截断单条支持链。不发送无关任务、完整 Store、页面正文或未引用片段。
- 未引用 Evidence 保留，避免提前删除潜在信息；通过 `is_cited` 和 `get_cited_evidence()` 区分已引用片段，默认不进入下游 context。每次 merge 打印新增/去重数量及 Claim 复用数量；最终 `source_reuse/evidence_reuse` 统计额外的不同任务使用关系，同一任务重复 merge 不重复计数。

Store 查询 API：

| 查询 | 接口 |
| --- | --- |
| 单个记录与支持链 | `get_claim`、`get_evidence`、`get_source`、`get_evidence_for_claim`、`get_source_for_evidence`、`get_sources_for_claim` |
| 按任务查询 | `get_claims_for_task`、`get_evidence_for_task`、`get_sources_for_task`；后两者包含本任务首次登记及跨任务实际使用的记录 |
| provenance 与引用状态 | `get_source_provenance`、`get_evidence_provenance`、`get_cited_evidence` |
| 反向引用 | `get_claims_using_evidence`、`get_evidence_from_source` |

Phase 3B extraction prompt、Schema 策略、输出额度及两阶段恢复规则保持不变：首轮 6 Claims / 160 字符 / 4 refs，截断恢复 3 / 90 / 3，额度翻倍且继续遵守现有 8,192 token 上限（默认 6,144 → 8,192）。未实现 Verifier、Citation 或 Persistence。

**Discovery 读取失败与 Memo 稳定性修复：**

- 成功读取要求真实、非空的 HTML 文本；调用过 `fetch_webpage`、搜索摘要或 PDF 失败响应都不算读取成功。
- 已知 arXiv/PMLR/OpenReview PDF 链接转为对应 HTML/摘要页，返回实际页面 URL、标题及可确认日期；不下载解析 PDF，不为普通网站猜测 HTML URL。每次工具调用最多尝试两次 HTTP 请求，继续遵守任务截止时间和原有读取次数预算。
- Discovery 无成功读取时最多追加一次 Agent 读取提醒；仍失败则按已返回的搜索 URL/已尝试 URL 继续读取替代页面或其他候选，避免重读已失败的内部替代页面，不使用 Memo 自造的链接。读取预算耗尽即停止；应用补读成功时仅重新生成一次 Memo，再调用原有 extraction。
- Discovery、Analysis、Synthesis 最终展示均由正式 Claim 重建，来源标题、URL、日期从关联 Source 直接读取。报告中的证据编号关联实际 Evidence 摘录预览，完整内容保留在 Store。自由生成的草稿仅用于引导抽取，不直接成为公开发现或下游结论；无正式 Claim 时只报告缺口。
- Synthesis 可以复用整个依赖链上的正式 Claim 和 Evidence，避免 discovery 的研究维度因中间 analysis 选择性抽取而完全消失；不读取无关任务。按任务轮询保留完整引用链，日志输出已覆盖/可用任务与 Claim 数，显式报告未覆盖分支。
- 模型请求前移除预算耗尽的搜索/读取工具，并注入当前剩余额度。模型仍返回已隐藏工具或超额并行调用时，在响应端拦截，避免进入工具执行；仅剩被拦截调用时结束 Agent pass，使用已有材料走原有的一次无工具 Memo 收尾。并行工具调用以锁保护额度预留，实际调用总数仍受原有上限约束。
- PMLR、alphaXiv、OpenReview 论文页、NeurIPS/ICLR 论文海报页及 Springer 论文页识别为 `paper`，不再仅识别 arXiv。分类表示论文来源页面，不表示已读取 PDF 或全文。
- 全部读取失败或没有正式 Claim 时，本分支只输出明确缺口，`compact.key_findings/sources` 为空，便于其余分支继续执行。此时 `completed` 表示执行结束，日志另标记 `discovery incomplete` / `coverage gap`；不表示已找到研究结论。
- 这些措施控制读取与输出 provenance，不判断 Evidence 在语义上是否支持 Claim；抽取模型仍可能判断错误，事实验证留待 Phase 4。

**验收：** 全部 287 项离线测试通过（含 21 项 Phase 3C 测试及 Phase 2 并行、Phase 3B fallback 回归）。真实 CLI 对原始 sparse reward offline RL 查询运行成功；另一次真实验收运行随机检查 Discovery / Analysis / Synthesis 各一个 Claim，全部 44 条正式引用均为 canonical ID、来源可查询、provenance 一致，Synthesis 局部 Source/Evidence 均为 0。该轮 Store 为 Sources=1、Evidence=12、Claims=11、Source reuse=2、Evidence reuse=16；D1 返回空 Bundle，跨层支持主要来自 D2，D1+D2 多来源共同支持的情况另由单测覆盖。这些计数取决于模型和搜索结果，不是固定数量或内容质量保证。

```mermaid
flowchart TD
    D1[D1 Researcher] --> B1[TaskEvidenceBundle]
    D2[D2 Researcher] --> B2[TaskEvidenceBundle]
    D3[D3 Researcher] --> B3[TaskEvidenceBundle]
    B1 --> G[Orchestrator: Global EvidenceStore]
    B2 --> G
    B3 --> G
    G --> M[Source / Evidence 去重、ID 重映射、Provenance 合并]
    M --> A[Analysis: 复用直接依赖 Evidence]
    A --> S[Synthesis: 仅复用上游 Evidence]
    S --> C[Claim]
    C --> E[Evidence]
    E --> O[Original Source]
```

### Phase 4 — Verifier Loop

**Status: Planned**

- **Goal / 目标：** 对研究结果进行验证，并围绕具体缺口补充研究。
- **Planned Scope / 计划范围：** Research → Verify → Gap → Re-Research 的反馈循环。
- **Completion Criteria / 预期验收：** 能识别缺少支持或存在冲突的论断，生成补充任务，并保留验证依据。

### Phase 5 — Writer + Citation

**Status: Planned**

- **Goal / 目标：** 将已验证的研究结果组织为完整 Deep Research Report。
- **Planned Scope / 计划范围：** Writer、报告结构、引用关联与引用校验。
- **Completion Criteria / 预期验收：** 报告中的关键论断与可追溯的来源引用对应。

### Phase 6 — Persistence

**Status: Planned**

- **Goal / 目标：** 保存研究状态并支持中断后继续执行。
- **Planned Scope / 计划范围：** Checkpoint、SQLite、研究项目与结果持久化、Resume Research。
- **Completion Criteria / 预期验收：** 重启后能够恢复计划、任务状态和已有结果，继续未完成的研究。

### Phase 7 — Evaluation

**Status: Planned**

- **Goal / 目标：** 定量评估研究系统的质量与执行表现。
- **Planned Scope / 计划范围：** Benchmark、Quantitative Evaluation、可重复的评测流程。
- **Completion Criteria / 预期验收：** 在固定问题集合上比较研究质量、覆盖度与运行表现。

当前工程单元测试用于验证程序行为，不等同于研究质量 Benchmark 或 Evaluation 系统。

### Phase 8 — UI + Docker + README

**Status: Planned**

- **Goal / 目标：** 提供可演示、可部署的项目交付形式。
- **Planned Scope / 计划范围：** UI、Docker、Final Demo、Deployment、Project Packaging 与最终文档整理。
- **Completion Criteria / 预期验收：** 通过统一入口启动项目，并完成演示、部署和使用文档。

## Current Architecture / 当前实现

| 文件或模块 | 当前职责 |
| --- | --- |
| [main.py](main.py) | CLI 入口，读取研究问题并调用同步 `run()`；失败时打印错误并返回非零退出码。 |
| [supervisor.py](app/agents/supervisor.py) | 两步动态规划、结构化输出与 JSON 回退、任务数量、类型、状态和依赖结构校验。 |
| [task.py](app/schemas/task.py)、[plan.py](app/schemas/plan.py) | 定义任务、计划与依赖校验；状态只使用 `pending`、`running`、`completed`、`failed`。 |
| [orchestrator.py](app/research/orchestrator.py) | 批次式 DAG 调度、并发限制、依赖副本、失败汇总及稳定顺序展示。 |
| [researcher.py](app/agents/researcher.py) | 独立任务 Agent、来源预读、Memo 与压缩结果生成，并返回局部 Evidence Bundle。 |
| [evidence_extraction.py](app/research/evidence_extraction.py)、[evidence_context.py](app/research/evidence_context.py) | 结构化抽取、来源片段与引用检查、程序 ID 构建，以及有大小限制的上游证据传递。 |
| [evidence_report.py](app/research/evidence_report.py)、[budget_middleware.py](app/research/budget_middleware.py) | 从正式 Claim 与 canonical 来源重建报告，并在模型请求前隐藏已耗尽的来源工具。 |
| [web_search.py](app/tools/web_search.py) | Tavily basic 搜索；结果去重和摘要截断，不返回生成答案或整页原文。 |
| [fetch_webpage.py](app/tools/fetch_webpage.py) | HTML 标题、日期与正文提取；已知 PDF 使用对应 HTML/摘要页，不解析 PDF；报告读取错误。 |
| [tool_budget.py](app/research/tool_budget.py)、[runtime.py](app/research/runtime.py) | 每任务独立调用预算、截止时间、请求超时约束和阶段日志。 |
| [compact.py](app/research/compact.py)、[context.py](app/research/context.py) | 从 Memo 提取压缩结果，并选择、限制下游依赖上下文。 |
| [source_dedup.py](app/research/source_dedup.py) | 保留约定的 Memo 章节，对来源标题做规范化去重。 |
| [research_result.py](app/schemas/research_result.py) | 定义 SourceSummary、CompactResearchResult、ResearchExecutionResult；Bundle 为可选兼容字段。 |
| [source.py](app/schemas/source.py)、[evidence.py](app/schemas/evidence.py)、[claim.py](app/schemas/claim.py)、[evidence_bundle.py](app/schemas/evidence_bundle.py) | Phase 3A 来源、证据片段、论断与任务级传输结构。 |
| [evidence_store.py](app/research/evidence_store.py) | 运行级内存 Store：URL/原文精确去重、原子 ID 重映射、跨任务 provenance、引用状态及反向查询。 |
| [config.py](app/config.py)、[llm.py](app/llm.py) | 从环境变量和 `.env` 读取配置，集中创建 OpenAI 兼容模型，支持阿里云百炼 Qwen 与旧版 SiliconFlow 配置。 |
| [app/prompts](app/prompts) | 独立保存拆分、计划与 Researcher 指令。 |

## Installation / 安装

需要 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

macOS/Linux 的激活命令为 `source .venv/bin/activate`。

## Configuration / 环境配置

复制 [.env.example](.env.example) 为 `.env`，填写模型与 Tavily 的密钥。`.env` 已被 Git 忽略。

```dotenv
MODEL_API_KEY=replace-with-your-api-key
MODEL_NAME=qwen3.7-flash
MODEL_BASE_URL=https://<your-workspace-id>.cn-beijing.maas.aliyuncs.com/compatible-mode/v1
TAVILY_API_KEY=replace-with-your-api-key
MODEL_REQUEST_TIMEOUT_SECONDS=60
EVIDENCE_EXTRACTION_TIMEOUT_SECONDS=120
EVIDENCE_EXTRACTION_MAX_TOKENS=6144
EVIDENCE_EXTRACTION_ENABLE_THINKING=false
MODEL_MAX_RETRIES=0
RESEARCH_TASK_TIMEOUT_SECONDS=300
RESEARCH_PROGRESS_INTERVAL_SECONDS=15
SHOW_EVIDENCE_DEBUG=false
```

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `MODEL_API_KEY` | OpenAI 兼容模型服务密钥，与 `MODEL_BASE_URL` 成对配置，优先于旧版 SiliconFlow 配置。 | 无 |
| `MODEL_BASE_URL` | 模型服务的 OpenAI 兼容 API 地址；百炼填写控制台提供的工作空间地址。 | 无 |
| `MODEL_NAME` | 对应服务支持的模型 ID，例如 `qwen3.7-flash`；运行研究时必填，模型需支持工具调用。 | 无 |
| `SILICONFLOW_API_KEY` | 旧版 SiliconFlow 密钥，仅在两个通用配置项均为空时使用。 | 无 |
| `SILICONFLOW_BASE_URL` | 旧版 SiliconFlow 模型服务地址。 | `https://api.siliconflow.cn/v1` |
| `TAVILY_API_KEY` | Tavily 搜索密钥，执行搜索时必填。 | 无 |
| `MODEL_REQUEST_TIMEOUT_SECONDS` | 规划及研究模型请求的 HTTP 超时设置。 | `60` |
| `EVIDENCE_EXTRACTION_TIMEOUT_SECONDS` | 证据抽取单次请求的 HTTP 超时，受任务剩余时间约束。 | `120` |
| `EVIDENCE_EXTRACTION_MAX_TOKENS` | 证据抽取常规请求生成 token 上限，范围 512–8192；截断恢复最多加倍且不超过 8192。 | `6144` |
| `EVIDENCE_EXTRACTION_ENABLE_THINKING` | 百炼 Qwen3.7 的证据抽取请求是否开启思考；仅作用于抽取，不修改规划和研究模型的思考模式。 | `false` |
| `MODEL_MAX_RETRIES` | 模型客户端自动重试次数；不代表自动重跑研究任务。 | `0` |
| `RESEARCH_TASK_TIMEOUT_SECONDS` | 单项 Researcher 任务的总时间预算，含来源读取及模型调用。 | `300` |
| `RESEARCH_PROGRESS_INTERVAL_SECONDS` | 任务等待进度输出间隔。 | `15` |
| `SHOW_EVIDENCE_DEBUG` | 额外打印 Claim、Evidence 及来源内容；默认只显示数量统计。 | `false` |

Supervisor、Researcher 和 EvidenceExtractor 共用此模型入口。通用密钥和地址必须成对配置，缺少任意一项时直接报错，避免混用不同服务的配置。继续使用 SiliconFlow 时，将 `MODEL_API_KEY`、`MODEL_BASE_URL` 留空，并设置 `SILICONFLOW_API_KEY` 和对应的 `MODEL_NAME`。

Supervisor 先尝试 JSON Schema 结构化输出；不支持或校验失败时，请求纯 JSON 并使用 Pydantic 校验。无法获得有效计划时明确报错。

### 并发配置

并发上限在 Orchestrator 初始化参数中配置，CLI 默认使用 3。当前没有对应的环境变量或 CLI 参数。

```python
from app.research.orchestrator import ResearchOrchestrator

orchestrator = ResearchOrchestrator(max_concurrency=3)
result = orchestrator.run("调研 sparse reward offline reinforcement learning 的最新研究进展")
```

### 工具与上下文边界

| 任务类型 | 最大搜索次数 | 最大网页读取次数 |
| --- | --- | --- |
| discovery | 3 | 4 |
| analysis | 2 | 3（包含程序预读） |
| synthesis | 0 | 0 |

每个任务独立计数，工具结果携带剩余预算。超额调用返回明确错误。Agent 每次调用配置 40 步上限，任务总时间预算覆盖该任务的全部调用与补充提醒。

- 搜索默认请求 5 条结果，最多返回 8 条，每条摘要最多 500 字符；Tavily 请求的连接与读取超时分别为 5 秒、30 秒。
- HTML 正文默认最多 5000 字符、调用硬上限 8000 字符，HTTP 超时设置为 15 秒；analysis 预读每页最多 2500 字符。
- 当前任务的初始输入（系统提示、任务、依赖上下文与预读正文）最多 28000 字符；预读来源 JSON 内容还受最多 8000 字符的限制。
- 单个 compact result 的序列化长度目标为最多约 3000 字符，最多 6 条 findings、6 个来源和 4 条不确定信息。
- 单个压缩摘要依赖块最多 2800 字符；无结构化证据的兼容调用默认总摘要上下文最多 8000 字符。真实 Researcher 的摘要与结构化证据共用 16000 字符预算，优先保留完整证据链；过长时缩减摘要，保留当前问题，无法容纳必要信息时明确报错。
- 上游结构化证据最多 12000 字符；按依赖任务轮询选择完整 Claim 支持链，最多 12 条 Claim、24 条 Evidence，不切断引用。结构化抽取输入（含提示及当前候选 Schema）最多 48000 字符，每页最多 5000 字符；来源标题和 URL 只传一次，原文候选和编号保持完整。正常请求要求最多 6 条 Claim，每条最多 160 字符及 4 个支持引用；程序仍限制每任务最多 12 条新 Evidence。

工具请求超时还会根据任务剩余时间缩短。HTTP 超时限制连接或读取等待，不是整个研究任务的总时限；总时限由 ResearchRuntime 单独管理，包含结构化抽取。任务间传递 compact result 和选取的结构化证据，不传整页正文或前序消息历史。

证据抽取对每次模型调用单独传入请求超时及生成 token 上限，不修改并行任务共享的模型配置。原生 JSON Schema 不可用时的兼容调用重新计算任务剩余时间。日志显示输入字符数、候选数和请求限制；若模型请求仍超时，错误明确指出是摘要生成后的证据抽取失败，不发布不完整 Bundle，并沿用失败后停止下游调度的规则。请求超时不触发自动重跑研究或 JSON 兼容回退。

若服务以 `finish_reason=length` 截断输出，抽取最多恢复一次：保留同一任务与全部原文候选，仍允许最多 6 条 Claim，但缩短为每条最多 90 字符和 3 个引用，并把输出 token 上限调整为配置值的两倍（最低 1024、最高 8192）。百炼 Qwen3.7 默认仅在抽取请求中关闭思考，减少思考 token 对 JSON 生成额度的占用；规划及研究阶段不受影响。每次请求受任务剩余时间约束，不重做搜索，不续接残缺 JSON。原生 Schema 兼容回退最多一次，因此包含截断恢复时，抽取最多发起 3 次模型请求；再次截断会明确失败，不发布残缺结果。

抽取 Schema 为每次请求提供当前合法候选编号的枚举。若服务仍返回越界编号，程序舍弃引用该编号的整条 Claim 及无效独立选项，保留其他完整支持链，并记录候选数量、非法编号和舍弃数量。全部引用无效时不生成正式 Claim 或虚构来源。类型、字段格式和原文来源检查仍然保留。

默认首次抽取使用 6144 token，截断恢复使用 8192 token。设置 `SHOW_EVIDENCE_DEBUG=true` 时，终端还会显示正式 Claim 的 ID、完整 evidence_ids，以及每条 Evidence 的 source_id、来源标题和 URL，便于逐条检查引用链。

如果研究结束时返回空正文、工具响应或尚未完成的工具调用，Researcher 会记录消息类型与模型结束原因，并最多补写一次 Memo：使用当前任务、已成功读取的页面及依赖上下文，直接调用不带工具的模型，不重复搜索，仍受原有任务总时限和输入长度约束。补写仍为空、返回工具调用或模型明确拒绝时继续明确失败，不使用前面的规划文字充当摘要。

12 条新 Evidence 的限制由程序统一控制，不依赖模型分别遵守每个数组的长度。程序先校验全部引用及原文，再按模型输出顺序优先保留 Claim 的完整支持链；超出容量就舍弃整条 Claim，随后以剩余容量补充独立证据。已有上游 Evidence 不占新证据额度。日志显示舍弃的 Claim 和候选数量；不为保留某条 Claim 而删掉它的一部分支持引用。

## Usage / 运行方式

命令行运行：

```powershell
python main.py "调研 sparse reward offline reinforcement learning 的最新研究进展"
```

也可直接运行 `python main.py`，按提示输入问题。终端会显示研究计划、执行批次、任务类型、依赖、依赖上下文字符数、工具与模型阶段、耗时、调用统计、压缩结果字符数和各任务 Memo。

异步应用使用 `arun()`：

```python
from app.research.orchestrator import ResearchOrchestrator

async def research(query: str):
    return await ResearchOrchestrator(max_concurrency=3).arun(query)
```

已经运行事件循环的应用应调用 `arun()`，同步 `run()` 会对此明确报错。成功时返回的 ResearchResult 包含 `plan`、`memos`、`compact_results`、`evidence_bundles`、`evidence_store`；CLI 将结果显示在终端，当前没有自动持久化功能。

## Testing / 工程验证

安装开发依赖后运行离线测试：

```powershell
python -m pytest -q
```

测试涵盖计划与依赖校验、上下文压缩、来源去重、工具返回格式和预算，以及并行调度、并发限制、状态隔离、来源预读和超时处理。Phase 3 测试还覆盖结构化抽取及截断 fallback、原文片段匹配、禁止无证据 Claim、跨任务精确去重与 ID 重映射、provenance、反向查询、合并回滚、依赖 context 隔离、Analysis 复用及按需读取、Synthesis 禁止新增证据。真实研究运行另外需要可用的模型服务、Tavily API Key，以及可访问目标网页的网络。

## Current Limitations / 当前限制

- 动态任务拆分和格式要求依赖模型遵循提示，当前没有自动核验各分支的主题覆盖、互补性或研究结论。
- 搜索摘要、非空页面正文和任务 `completed` 都不代表论断已经通过事实验证；年份、方法、实验范围和引用相关性仍需检查。
- 只读取可访问的 HTML 页面，不解析 PDF；网站可能拒绝自动读取，正文截断也可能遗漏关键实验细节。
- Memo 压缩依赖 Markdown 章节规则，模型偏离格式时可能丢失部分来源或细节。Memo 来源按标题轻量去重；Store 按 URL 及已知 arXiv 表示保守匹配身份，不做跨网站论文解析。
- 调度按整批等待；一个任务失败后停止后续批次，没有自动重规划、研究任务重试、Verifier Loop 或 Re-Research。
- 任务超时限制调用方等待并阻止后续步骤，不能强行取消正在运行的同步 HTTP 请求；底层请求与框架线程仍需按自身超时收尾。
- Evidence/Claim 抽取依赖模型；越界候选引用按整条 Claim 舍弃，其他格式错误、原文不匹配、恢复后仍截断或请求超时会明确失败。空的抽取结果不产生正式 Claim，成功的结构化关系也不表示已经完成事实验证。
- 结果及 EvidenceStore 保存在内存中；尚无完整报告 Writer、引用校验系统、checkpoint、SQLite 持久化或恢复执行。

## Roadmap

| 阶段 | 状态 | 范围 |
| --- | --- | --- |
| Phase 1 — Research 基础链路 | Completed | Supervisor、ResearchPlan、Researcher、Search Tool、串行多阶段研究与压缩结果传递。 |
| Phase 2 — Parallel Research | Completed | Phase 2A 动态任务拆分；Phase 2B 独立 Researcher executions、DAG 调度与受限并行执行。 |
| Phase 3 — Evidence System | Completed | Phase 3A 数据模型与内存 Store；3B 结构化抽取；3C 跨任务去重、复用、provenance 与完整链路查询。 |
| Phase 4 — Verifier Loop | Planned | Research → Verify → Gap → Re-Research。 |
| Phase 5 — Writer + Citation | Planned | Deep Research Report、Writer 与引用关联及校验。 |
| Phase 6 — Persistence | Planned | Checkpoint、SQLite、项目持久化与 Resume Research。 |
| Phase 7 — Evaluation | Planned | Benchmark、Quantitative Evaluation 与可重复的研究质量评测。 |
| Phase 8 — UI + Docker + README | Planned | UI、Docker、Final Demo、Deployment、Project Packaging 与最终文档整理。 |

## Project Structure / 项目结构

```text
deepagent/
├── main.py                      # CLI 入口
├── demo_evidence.py             # Phase 3A 静态 Demo，无 LLM 或网络
├── pyproject.toml               # 依赖、安装与 pytest 配置
├── .env.example                 # 环境配置模板
├── app/
│   ├── config.py                # Settings
│   ├── llm.py                   # 模型创建
│   ├── agents/
│   │   ├── supervisor.py        # 动态规划
│   │   └── researcher.py        # 独立任务研究与来源准备
│   ├── prompts/
│   │   ├── decomposition.md     # 独立子问题拆分
│   │   ├── supervisor.md        # ResearchPlan 生成
│   │   ├── researcher.md        # 研究职责与 Memo 格式
│   │   └── evidence_extraction.md # 编号片段选择与 Claim 关联
│   ├── research/
│   │   ├── orchestrator.py      # 批次式 DAG 调度
│   │   ├── evidence_store.py    # 内存证据 Store 与 Bundle 合并
│   │   ├── evidence_extraction.py # 结构化抽取与局部 Bundle 构建
│   │   ├── evidence_context.py  # 上游证据链的有界传递
│   │   ├── runtime.py           # 时间预算与进度日志
│   │   ├── tool_budget.py       # 每任务工具调用预算
│   │   ├── compact.py           # Memo 压缩
│   │   ├── context.py           # 依赖上下文
│   │   └── source_dedup.py      # 来源标题去重
│   ├── schemas/
│   │   ├── task.py              # ResearchTask
│   │   ├── plan.py              # ResearchPlan 与依赖校验
│   │   ├── research_result.py   # Memo 配套的结构化结果
│   │   ├── source.py            # Source 身份与 URL 规范化
│   │   ├── evidence.py          # 有限长度的证据片段
│   │   ├── claim.py             # Claim 与 Evidence 引用
│   │   ├── evidence_bundle.py   # 任务局部证据产物
│   │   └── evidence_extraction.py # 无内部 ID 的模型输出结构
│   └── tools/
│       ├── web_search.py        # Tavily 搜索
│       └── fetch_webpage.py     # HTML 读取
├── tests/                       # pytest 工程测试
└── workspace/                   # 本地运行与诊断产物，除 .gitkeep 外不提交
```
