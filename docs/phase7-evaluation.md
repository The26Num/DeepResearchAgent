# Phase 7 — Evaluation & Benchmarking

Phase 7 是独立的外部评估层。Full 模式通过 `ResearchOrchestrator.run()` 创建正常 SQLite Research Run，再读取最终安全快照。评估不会修改 Claim、Verification、Relevance、Gap、Writer、Grounding 或 Citation，也不会用 Judge 结果触发补研究。

## 使用

在项目目录激活虚拟环境后：

```powershell
python evaluate.py evaluation/benchmarks/research_benchmark_v2.json --limit 2
python evaluate.py evaluation/benchmarks/research_benchmark_v2.json --case offline_rl_001
python evaluate.py evaluation/benchmarks/research_benchmark_v2.json --case china_game_001
python evaluate.py evaluation/benchmarks/research_benchmark_v2.json --mode baseline --limit 2
python evaluate.py evaluation/benchmarks/research_benchmark_v2.json --mode compare --limit 2 --judge
```

- `--mode full|baseline|compare`，默认 `full`。
- `--case CASE_ID`、`--limit N` 在开始研究前校验。
- Judge 默认关闭，使用 `--judge` 开启，`--no-judge` 显式关闭。每份完成报告最多一次模型调用，无重试、无 schema 回退调用。
- `--db-path` 覆盖 Research Run 数据库，默认读取 `RESEARCH_DB_PATH`。
- `--output-dir` 默认 `workspace/evaluations`；每次生成新 UUID 目录。
- `--max-concurrency` 默认 3，控制 Full 内部研究并发；Case 与 Full/Baseline 顺序执行，避免观测数据相互混淆。
- `EVAL_MODEL_NAME` 可设置 Judge 模型，留空时使用当前主模型及相同 Provider 凭据。研究和 Baseline 均使用当前主模型。
- 每次启动都创建新 Full Research Runs，不按问题自动复用。某 Case 失败后继续后续 Case；最终任一 Case 失败返回码为 1。Judge 失败仅记录诊断，不把已完成的 Research Run 改为失败。
- 中断时保留已写评估文件与正常 Research Run。用 `python main.py --resume RUN_ID` 恢复研究；第一版不提供 Evaluation 自动恢复或自动重新评分。

## 模块

| 文件 | 职责 |
| --- | --- |
| `evaluate.py` | 独立 CLI；不扩张 `main.py` |
| `evaluation/models.py` | Benchmark、结果、比较、Baseline 和 Judge schema |
| `evaluation/dataset.py` | JSON 加载、重复 ID 校验、Case 选择 |
| `evaluation/benchmarks/research_benchmark_v2.json` | 当前实验：学术研究与行业研究两个 Case |
| `evaluation/benchmarks/research_benchmark_v1.json` | 保留的历史八例技术类 Benchmark |
| `evaluation/runner.py` | 正常持久化研究入口、失败隔离、Full/Baseline/Judge 调度 |
| `evaluation/metrics.py` | 只读确定性指标与结构性回归门 |
| `evaluation/telemetry.py` | 本地可选 LangChain callbacks，观察请求/用量/工具，不改变请求 |
| `evaluation/output.py` | 脱敏、JSONL、summary、CSV、比较与缺失值聚合 |
| `evaluation/baselines/simple_search_summary.py` | 独立 Search → Fetch → Summary |
| `evaluation/judge.py` | 单次、无工具、JSON + Pydantic 的评估 Judge |
| `tests/test_evaluation.py` | Phase 7 离线测试 |

另修改 `.env.example` 增加可选 Judge 模型名，`pyproject.toml` 打包 evaluation 与 JSON 数据集，README 记录阶段、用法和验收。研究核心代码不变。

## Benchmark 与结果结构

```text
BenchmarkDataset:
  benchmark_version: string | integer
  cases: list[BenchmarkCase]

BenchmarkCase:
  case_id: safe unique identifier
  question: string
  category: string
  question_type: discovery | comparison | trend | narrow_focus | null
  expected_dimensions: list[string]       # 可以为空
  notes: string
  gold_sources: optional list[URL]
  tags: list[string]

EvaluationResult:
  case_id, benchmark_version, variant
  run_id: string | null                  # Baseline 没有 Phase 6 Run
  status: COMPLETED | FAILED | INTERRUPTED
  metrics:
    research, quality, efficiency, timing, reliability
  judge_result: JudgeResult | null
  judge_usage: object
  errors: [{stage, error_type, error_message}]
  report_path: string | null

ComparisonResult:
  case_id, full_status, baseline_status, full_run_id
  full: flattened metric vector
  baseline: flattened metric vector
  delta: full - baseline, shared numeric values only
```

当前实验使用 V2（`benchmark_version=2`），按 2026-10-13 的实验设计包含以下两个问题。用户表格中的“类型”存入 `category`（`academic_research` / `industry_research`）；两题的任务题型 `question_type` 均为 `discovery`。

| Case | 问题 | 类型 | expected_dimensions |
| --- | --- | --- | --- |
| `offline_rl_001` | 调研 2024—2026 年离线强化学习的主要研究进展，包括主要研究方向、代表性方法、当前挑战与发展趋势 | 学术研究 | 主要研究方向、代表性方法及核心思路、当前挑战与局限、发展趋势及证据依据 |
| `china_game_001` | 调研 2024—2026 年中国游戏产业的发展进展，包括市场变化、产品与品类趋势、技术发展以及游戏出海情况 | 行业研究 | 市场变化、产品与品类趋势、技术发展、游戏出海 |

学术题的说明要求区分 offline、online 和 offline-to-online 设定；行业题的说明要求注明数字的统计期与口径，区分实际数据和预测。两题都只覆盖运行时已公开的 2026 年资料，不预设具体论文、企业、产品或市场数字必须出现。这些 `notes` 是数据集说明，当前 Runner 仍按原有接口把 `question` 交给研究流程，Judge 使用问题与覆盖维度，不把 notes 作为额外研究指令。

V1 原有的 8 个技术类 Case 保留供历史实验复现；下面首次真实验收记录对应 V1，不能当作 V2 的实际实验结果。不保存固定答案、必选论文或 gold final report；`gold_sources` 可选，缺失时 source recall 为 null。

## 指标定义

所有分母为零的比率均为 **null / N/A**，不会伪装为 0 或满分。不输出主观加权总分。

| 指标 | 计算与含义 |
| --- | --- |
| `support_rate` | 最新有效 SUPPORTED 验证数 / 已验证 Claim 数；另输出 PARTIAL、UNSUPPORTED 比率。历史重复验证不重复计数 |
| `alignment_rate` | ALIGNED / 已判断 relevance 的 Claim 数；另输出 PARTIAL、MISALIGNED 比率 |
| `publishable_claim_rate` | 同时 SUPPORTED + ALIGNED 的 Claim / 至少有一种有效评估的 Claim（两种评估集合的并集） |
| `publishable_all_claim_rate` | 合格 Claim / 所有正式 Claim；配合 assessment completion、未验证数量暴露预算耗尽 |
| `assessment_completion_rate` | 同时具有两种有效评估的 Claim / 所有 Claim |
| `evidence_coverage` | 有 Evidence 引用的 Claim / 所有 Claim；引用是否断链另由结构性门检查 |
| `evidence_reuse_ratio` | `1 - unique_linked_evidence / total_claim_evidence_links`，同时保留存储 Evidence 数和链接数；高复用不等于高质量 |
| `source_utilization_rate` | 最终报告引用 Source / 存储 Source；另输出 accepted Source / 存储 Source |
| `source_recall` | 命中可选 gold URL 的存储 Source / gold Source 数；URL 使用现有规范化，不猜测版本等价 |
| `citation_integrity_pass` | Full 使用 Phase 5 的纯校验函数检查实际 Markdown、连续编号、Reference 使用、未知 Source、元数据与每个事实段落的完整 Claim→Evidence→Source 链 |
| `grounding_repair_rate` | Phase 5 repaired items / checked items；另记录原始 audit 非 SUPPORTED 数、fallback reason。这不代表修复后的报告仍有同样数量的错误 |
| `writer_claim_coverage` | 最终使用的 accepted Claim / 全部 accepted Claim；不代表研究目标覆盖率 |
| `task_coverage_rate` | 至少有一个本 Task 的 SUPPORTED + ALIGNED Claim 的原始 Task / 原始 Task 总数，另按 discovery/analysis/synthesis 输出 |
| `task_coverage_including_reresearch_rate` | 额外把合格补研究 Claim 按 `origin_task_id` 归因，作为独立诊断，不改变原始 Task 指标 |
| Gap counts | support/relevance/coverage；补研究前数量、之后未解决数（OPEN + IMPROVED）、严格 OPEN 数、RESOLVED/IMPROVED 数 |
| `gap_resolved_rate` | 被选择且 RESOLVED 的 Gap / 被选择 Gap ID 去重数 |
| `gap_improved_rate` | 被选择且 IMPROVED 的 Gap / 被选择 Gap ID 去重数 |
| `gap_repair_rate` | 被选择且 RESOLVED 或 IMPROVED 的 Gap / 被选择 Gap ID 去重数；未选择 Gap 不进入分母，group 数不作为 Gap 数 |
| `reresearch_publishable_yield` | 补研究新合格 Claim / 实际合并的补研究新 Claim；同时输出新增 Source/Evidence/Claim 数和停止原因 |
| source diagnostics | 使用已有 source_type 和已知 publication date 统计类型、年份分布及年份中位数。不推测未知日期，不按“越新越好”打分 |
| report diagnostics | 字符数、Unicode word-run 数；不代表中文分词结果，不把长度当质量 |
| duplicate paragraphs | 结构化正文和 limitations 去除首尾/合并空白并 casefold 后完全相同的额外出现数 / 段落数，无 embedding |
| duplicate limitations | 相同规范化 limitation 的额外出现次数，如出现 3 次计 2；只检测，不修改报告 |

`structural_integrity_pass` 是轻量回归门：断开的 Claim/Evidence/Source、陈旧/错 Task 的评估、非合格 Claim 的事实引用及非法 citation 会失败。真实 Benchmark 不设置 alignment ≥ 0.8 或 Judge ≥ 3.5 等不稳定门槛。

Support/Relevance 指标复用研究系统已有判定，因此是**内部判定通过率**，不能独立证明事实正确或没有偏题。Gap Repair 同样沿用 Phase 4C 的近似状态，不能独立证明所有缺失内容已经充分补齐。Judge 是另外的有界观察，也不是真值。尤其是 source 日期缺失时，确定性指标不能证明“最新”覆盖充分。Evaluator 会拒绝 Benchmark question 与 Run goal 不一致的输入，避免把其他问题的 Run 错归到当前 Case。

## 用量、时间和可靠性

- Evaluation 期间使用 LangChain `register_configure_hook` 注册本地 callback，覆盖 ResearchRuntime 独立线程，不上传追踪数据。只保留阶段名、请求/响应数、tokens 和工具结果状态，不保留模型 prompt、模型完整响应、headers 或整页网页。
- 阶段包括 Supervisor、Researcher（含 memo recovery）、Extraction、Verification、Relevance、ReResearch Planner、Writer、Grounding；无法识别的请求仍计入 `unclassified`，不会丢失总数。Citation 模型调用为 0。
- 同名预算包装工具调用底层工具时，只统计叶节点一次；预算拒绝不算实际工具调用。Fetch 成功要求非空正文且没有 error。PDF failure 统计最终返回 PDF 错误的逻辑 fetch；成功转 HTML 的请求不算失败，不统计内部 HTTP 尝试数。
- LLM calls 统计 LangChain 模型 invocation start，包括失败、应用重试及在 callback 中被运行时/预算 guard 拒绝的尝试；因此不等于已完成的 Provider 请求数，配合 responses 读取。Provider SDK 内部隐藏 HTTP 重试不另拆成模型调用，配置文件保存 `model_max_retries`。默认当前项目为 0。
- `usage_coverage = usage_responses / model_responses`；这里 responses 是正常响应 callback 次数，SDK 解析截断响应时抛错可能不触发该 callback。另保留 calls、responses、未观察到响应的数量与响应率。完整 `total_tokens` 要求所有调用均返回 usage；否则为 null，已知部分存在 `observed_total_tokens`，不猜未知 token。
- 直接读取旧 Phase 6 state 的 `Evaluator` 仍可算质量指标；其 Researcher/Supervisor 等完整用量不存在，完整调用/token 总量为 null。不要把旧状态的有限计数伪装成完整运行成本。
- Research runtime 使用 monotonic 计时，不含之后的 Evaluation Judge；另保存 Run created→updated 的 elapsed（可能包括恢复等待）。Judge 时间和 tokens 单独保存。
- Reliability 保存 Run status、resume_count、checkpoint_count、was_resumed。Phase 6 没有完整历史状态事件表，因此已恢复 Run 是否曾 FAILED/INTERRUPTED 为 null，不由 resume_count 臆测。
- Summary 的均值使用完成 Run 的可用值，并记录每个 `metric_sample_count`。失败 Run 已知成本另汇总到 `all_runs_observed_costs`，防止把失败成本隐藏掉；Judge 成本单列。

## Baseline 与 Judge

Baseline 是独立文件内的两次主模型调用：一次生成至多三个聚焦搜索 query，再使用与 Full 相同的 Tavily `web_search` 和 `fetch_webpage`，交错选取不同 query 的前排 URL，最多读四页，最后一次生成引用 `[S1]` 的总结。读取失败时使用并标明 search snippet，不虚构成功读取。没有 EvidenceStore、Verification、Relevance、Gap 或主 Orchestrator 的开关组合，不故意削弱模型和工具。

Baseline 的 citation 检查仅回答引用的 Source ID 是否被提供，**不等同 Full 的完整 provenance/citation 校验**；比较文件保留 `citation_check_scope`，没有 Claim/Task 的指标为 null。共享的内容质量比较来自同一 Judge rubric；未开启 Judge 时这些比较为 N/A。

Judge 输入只有 question、expected_dimensions、final report 与最终引用来源的简表/短摘录。每个引用来源最多三段 600 字符摘录，不输入完整网页、Memo 或整个 EvidenceStore。当前 Judge 上下文上限为 36000 字符，输出最多 4096 token。上下文超限明确记录失败，不静默裁剪报告，也不补第二次调用。有限摘录可能不能覆盖来源的全部事实，因此 unsupported 表示“在所提供片段中未得到支持”，不直接判为现实中的错误。Full 和 Baseline 使用相同字段、来源片段额度及 rubric，不给 Judge 系统名称。

Rubric：0 very poor、1 poor、2 partial、3 good、4 strong；统一评 Goal Relevance、Coverage、Groundedness、Clarity。每个 expected dimension 返回 COVERED/PARTIAL/MISSING，程序按 1/0.5/0 平均计算 `dimension_coverage_score`。空维度列表跳过 coverage，返回 null。Unsupported rate 用 Judge 列出的 unsupported statement 数 / 其 assessed statement 数计算，并明确这是 LLM 指标。

## 输出

```text
workspace/evaluations/<eval_id>/
  config.json
  benchmark.json
  cases.jsonl
  summary.json
  summary.csv
  comparison.json                # compare 模式
  <case_id>.full.md
  <case_id>.baseline.md           # baseline/compare
  <case_id>.baseline.json
```

`cases.jsonl` 每 Case 一行，顶层保存主要模式结果的 case_id/run_id/status/metrics/judge_result/errors，`results.full`、`results.baseline` 保留各模式独立结果。每 Case 完成立即 append，随后更新 summary/CSV；compare 额外保存 metric vector 与 full-minus-baseline delta，缺失值为 JSON null / CLI N/A。没有总分。

`config.json` 保存 UUID、benchmark 路径/版本、时间、选定 Case、mode、模型名、temperature、并发、工具/补研究/Writer 限额、Judge 开关/模型与 rubric version、可选 git commit。只用配置白名单，不保存 Settings、API key、环境或客户端。所有写出路径使用同一脱敏器处理已配置 secret、Authorization 和 URL credential 参数，运行产物在 Git ignore 内。

## 32 项交付核对

1. 新增/修改文件见上方模块表；研究核心未改。
2. BenchmarkCase 字段见结构定义。
3. 当前 V2 共 2 个 Case，历史 V1 的 8 个 Case 保留。
4. 当前实验覆盖学术研究与行业研究，两题均为多维度 discovery；历史 V1 覆盖四类题型和五个技术领域。
5. EvaluationResult 分研究/质量/效率/时间/可靠性、可选 Judge 和错误。
6. 确定性指标见指标表，附原始计数、日期/复用/重复诊断。
7. Support Rate 使用最新有效验证，SUPPORTED / verified。
8. Alignment Rate 为 ALIGNED / relevance assessed。
9. Publishable Rate 为 SUPPORTED∩ALIGNED / assessed union，另输出全部 Claim 的比率。
10. Task Coverage 按原始 Task 是否有合格 Claim，另按任务类型和补研究归因展示。
11. Full Citation Integrity 检查实际 Markdown 和完整关联；Baseline 明确仅检查 Source ID。
12. Gap Repair 分母只包含被选中的去重 Gap ID。
13. Re-Research Yield 为补研究新合格 Claim / 实际新增 Claim。
14. Tool/LLM/Token 用量通过本地只读 callbacks，完整值和已观察子总量分开。
15. Runtime 使用 monotonic；Run metadata elapsed 另存，Judge 另计。
16. limitation 规范化后的完全重复，统计额外出现数，不改报告。
17. Baseline 为 query planning → Search → Fetch → 单次 Summary。
18. Baseline 与 Full 使用相同 Search/Fetch 工具和当前主模型。
19. 没有故意削弱 Baseline。
20. Judge 输入只有问题、维度、报告、引用来源与短摘录。
21. 每 Report 最多 1 call，无 retry/fallback。
22. Goal Relevance、Coverage、Groundedness、Clarity，统一 0–4 rubric。
23. 输出位于 `workspace/evaluations/<eval_id>/`。
24. 生成 `summary.json`。
25. 生成每 Case 一行的 `cases.jsonl`。
26. 生成 `summary.csv`。
27. 单 Case 失败不阻断后续 Case，compare 中 Full 普通失败也仍执行 Baseline。
28. 不保存 API key；配置白名单和错误/文本脱敏。
29. Phase 7 离线测试及最新数量见下方验收记录。
30. Phase 2–6 既有测试保持通过。
31. 首次真实 `--limit 2` 结果见验收记录。
32. Full/Baseline 的 relevance/coverage/grounding、calls/tokens/search/runtime 对比见验收记录；不强求 Full 胜出。

## 验收记录

初次离线验收：Phase 7 新增 53 项测试通过，全量 **720 passed**（667 项既有回归 + 53 项新增）。覆盖实际 ChatOpenAI HTTP 模拟、独立线程 callbacks、同名工具去重、usage 缺失、失败请求、SQLite Run 与失败恢复点、Case 隔离、比较模式、Judge 零/单次调用、未知 status 和无研究反向修改。

最终离线验收增加错问题 Run 拒绝用例：**721 passed**（667 项既有回归 + 54 项 Phase 7 用例）。正常研究 CLI、Phase 2–6 代码和 `.env` 未修改；没有增加 Phase 8 功能或新依赖。

### 第一次真实 Full（2026-10-12）

模型为当前 `.env` 的 `Qwen/Qwen2.5-7B-Instruct`，没有临时替换模型或放宽研究逻辑。

```powershell
python evaluate.py evaluation/benchmarks/research_benchmark_v1.json --limit 2
```

Evaluation ID：`4381a669a4684a9ebef68be21f942ea9`，退出码 0，两例均完成，Judge 默认关闭。

| Case | Run ID | Support | Alignment | Publishable | Task coverage | Citation | 重复 limitation |
| --- | --- | ---: | ---: | ---: | ---: | --- | ---: |
| python_cache_001 | ab47e68b37ce4e19b4a8a93e0db2d249 | 0.652 | 0.783 | 0.522 | 0.75 | pass | 2 |
| sqlite_wal_001 | 2e793ad9124444fb95b863c85a3b09c5 | 1.000 | 1.000 | 1.000 | 0.75 | pass | 0 |

| Case | LLM starts / responses | 完整 tokens | 已观察 tokens | Search / Fetch | Fetch 成功 / 失败 | Runtime |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| python_cache_001 | 37 / 36 | N/A | 166,382 | 4 / 6 | 6 / 0 | 313.9s |
| sqlite_wal_001 | 28 / 28 | 147,256 | 147,256 | 4 / 4 | 3 / 1 | 169.3s |

Python 例经历一次 `LengthFinishReasonError` 后由既有 compact recovery 恢复，该调用没有正常 response usage 回调，因此不把已观察的 token 小计当作完整总量。SQLite 例记录到一次网页 SSL 证书失败；补研究产出了 Evidence 但没有新 Claim，yield 为 N/A，而不是虚构产出。两例 Report/Citation 与 Run 均已落盘，Checkpoint 数分别 37、25。

### 第一次真实 Full vs Baseline

```powershell
python evaluate.py evaluation/benchmarks/research_benchmark_v1.json --mode compare --limit 2 --judge
```

Evaluation ID：`1dc04578b1674361b60e6168f52d0b6a`。创建两个新的 Full Run，没有复用第一次实验；每个完成的 Report 调用 Judge 一次，共 3 次。Research 调用和 token 数不包含额外 Judge 成本，Judge 的 2,196 / 3,070 / 2,312 tokens 分别保存在 `judge_usage`。

| Case / Variant | Status | Goal relevance /4 | Coverage /4 | Dimension coverage /1 | Groundedness /4 | Clarity /4 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Python / Full | COMPLETED | 3 | N/A | 0.833 | 3 | 3 |
| Python / Baseline | COMPLETED | 4 | 3 | 1.000 | 3 | 4 |
| SQLite / Full | FAILED | N/A | N/A | N/A | N/A | N/A |
| SQLite / Baseline | COMPLETED | 3 | N/A | 1.000 | 3 | 3 |

| Case / Variant | LLM starts | Tokens | Search / Fetch | Runtime |
| --- | ---: | ---: | ---: | ---: |
| Python / Full | 42 | 184,110 | 6 / 7 | 200.9s |
| Python / Baseline | 2 | 5,759 | 3 / 4 | 44.8s |
| SQLite / Full（失败成本） | 12 | 51,947 | 2 / 2 | 66.1s |
| SQLite / Baseline | 2 | 4,028 | 2 / 4 | 26.3s |

Python Full Run 为 `055a5355ffc04b1a94e1cd0140b785c8`；SQLite Full Run 为 `970467094d644d1788b373950a1696a7`。后者失败原因是已有研究流程的 discovery 任务 `task2` 未执行新来源搜索：`Researcher did not search for new sources`。它的最后安全 Checkpoint 是 `TASK_COMPLETED:task1`（共 4 个）；Case 失败被保存后，该题 Baseline 仍正常执行。整个 compare 退出码 1，cases_completed=1、cases_failed=1，Full completed=1/2，Baseline completed=2/2。

这里没有为改善 Benchmark 分数而恢复重跑或修改研究逻辑。失败 Run 可用正常 `main.py --resume` 继续，但原始失败结果应保留用于可靠性观察。

Judge 两份报告未返回整体 Coverage 分数，保持 null/N/A，不按零平均、不额外重试；逐维度 COVERED/PARTIAL/MISSING 均已返回，程序计算的 dimension coverage 仍可用。Judge 的整体评分和 unsupported 清单也可能不一致，应结合原始理由审查，不作为事实真值或回归硬门。

Python 配对样本中 Baseline 的 Goal Relevance 与 Clarity 更高、Groundedness 相同，且 token/时间成本更低。SQLite Full 失败，不能拿其局部质量指标与完成的 Baseline 报告作质量胜负比较。只有两题、一次同模型 Judge 的观察，不能据此推断一般领域上的总体优劣。

### 本地验收产物

- `workspace/evaluations/4381a669a4684a9ebef68be21f942ea9/`
- `workspace/evaluations/1dc04578b1674361b60e6168f52d0b6a/`
- `workspace/phase7_full_live.log`
- `workspace/phase7_compare_live.log`
- `workspace/phase7_live_acceptance.json`
- `workspace/audit_phase7.py`：只读核对 Run/Checkpoint，并可零 LLM 调用重生成比较展示。

审计确认 4 个 Full Run ID 互不相同，正常阶段持久化完成，失败 Run 保留恢复点，Baseline 在预算内，Judge 每报告 ≤1 call，20 个评估/日志产物未包含已配置密钥。另只读评估先前 Phase 6 的 `c4d27d602726427b8fbf69a5ca33713c` 快照，检测到 task coverage=0.75、重复 limitation=1、未验证 Claim=2、补研究 publishable yield=0，并确认输入 state 未改变；旧快照缺失完整 token 用量保持 N/A。
