# Phase 6 — Persistence, Checkpoint and Resume

CLI 默认持久化到 `workspace/research.db`。使用标准库 `sqlite3`，只建立两张业务表；现有 EvidenceStore、VerificationStore、GapStore 继续负责研究数据语义，SQLite 保存它们的完整一致快照。

```powershell
python main.py "research question"
python main.py --resume RUN_ID
python main.py --list-runs
python main.py --resume RUN_ID --db-path workspace/research.db
```

`RESEARCH_DB_PATH` 可覆盖默认位置，`--db-path` 优先级更高。问题、`--resume`、`--list-runs` 互斥。直接传入相同问题会创建新的 UUID Run，只有显式 `--resume` 才恢复。

## 文件与职责

| 文件 | 职责 |
| --- | --- |
| `app/persistence/models.py` | `STATE_VERSION=1`、ResearchRun、RunStatus、ResearchRunState、EvidenceSnapshot、Checkpoint。 |
| `app/persistence/store.py` | SQLite 初始化、Run CRUD、原子 Checkpoint、版本化加载、密钥脱敏。 |
| `app/persistence/serialization.py` | JSON 恢复与版本、引用链、任务结果、阶段和报告完整性检查。 |
| `app/persistence/manager.py` | 记录最后安全状态，将 Worker 的调用许可/完整批次提交交回主调度线程。 |
| `app/research/orchestrator.py` | 一套 Pipeline 接受新状态或恢复状态，保留现有 ready-task DAG 和并发批次。 |
| `app/research/evidence_store.py` | 显式快照/恢复 canonical IDs、aliases、去重索引和 provenance。 |
| `app/research/verification_store.py` | 显式快照/恢复完整验证历史。 |
| `app/research/reresearch_budget.py` | 预算 snapshot/restore，调用许可先持久化再发请求。 |
| `app/research/gap_detection.py`、`app/agents/relevance.py` | 已有相关性结果跳过，完整 relevance batch 可形成安全快照。 |
| `app/config.py`、`.env.example` | `RESEARCH_DB_PATH`。 |
| `main.py` | New / Resume / List CLI；无散落 SQL。 |
| `.gitignore` | 排除 SQLite 主文件、WAL、SHM。 |
| `tests/test_persistence.py` | SQLite、任务/阶段恢复、预算、故障与完整性回归。 |
| `README.md`、本文档 | 用法、阶段状态和验收说明。 |

## SQLite 表结构

```sql
CREATE TABLE research_runs (
    run_id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    resume_count INTEGER NOT NULL DEFAULT 0,
    last_checkpoint_id INTEGER,
    error_type TEXT,
    error_message TEXT,
    state_version INTEGER NOT NULL
);

CREATE TABLE checkpoints (
    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES research_runs(run_id),
    sequence INTEGER NOT NULL,
    stage TEXT NOT NULL,
    state_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, sequence)
);
```

每个连接启用 `foreign_keys=ON`、`busy_timeout=5000`，数据库使用 WAL。`save_checkpoint()` 在 `BEGIN IMMEDIATE` 事务内计算该 Run 的下一 sequence、INSERT 快照、UPDATE Run 的 `last_checkpoint_id/current_stage/updated_at`，一起提交或回滚。保留所有快照，不做自动清理、增量 diff 或业务表关系化。

加载优先使用 Run 的 `last_checkpoint_id`，并验证对应快照归属；不擅自回退到其他历史版本。未知 Run 抛 `ResearchRunNotFoundError`，无快照抛 `NoCheckpointError`，断链/结果不一致抛 `StateIntegrityError`，版本不兼容抛 `IncompatibleCheckpointError`。

## JSON 状态

`ResearchRunState.model_dump(mode="json")` → JSON → SQLite TEXT。恢复使用 `json.loads`、`ResearchRunState.model_validate` 和 `validate_restored_state()`。不使用 pickle。

| 字段 | 内容 |
| --- | --- |
| `state_version`、`goal` | 格式版本、原始目标。 |
| `research_plan` | 初始计划与各任务状态。 |
| `task_results` | 已安全保存的 Memo、compact、tool names、canonical bundle；无工具消息历史或 raw HTML。 |
| `evidence_store` | Source/Evidence/Claim、Source/Evidence aliases、alias origins、使用任务 provenance。 |
| `verification_results` | 包括 verification_id 的全部历史。 |
| `relevance_results`、`gaps` | Claim 相关性，稳定 Gap ID 和 OPEN/IMPROVED/RESOLVED 状态。 |
| `reresearch_summary`、`supplemental_results` | 补研究计划/任务状态、结果、失败信息与统计。 |
| `selected_gap_groups`、`reresearch_rounds_completed` | 已选组与完成轮次。 |
| `reresearch_budget` | 原最大调用额度、已占用次数、各阶段占用次数。 |
| `reresearch_initial_counts`、`reresearch_started_tasks` | 恢复后继续准确计算净新增数据和任务数。 |
| `verification_usage`、`relevance_usage`、`writer_usage` | 已保存阶段的统计，包含 Writer grounding usage。 |
| `writing_context`、`report_grounding` | 有界写作输入与 grounding 审计，不含模型对象。 |
| `research_report`、`rendered_report`、`citation_debug` | Grounding 后报告、最终 Markdown/References/citation associations 和当时的 debug 渲染开关。 |
| `completed_stages`、`phase_options` | 完成标记；明确列出的 verify_claims/detect_gaps/reresearch/write_report 开关。 |

不保存 Settings、`.env`、API Key、Authorization header、模型、Agent、tool function、线程锁、semaphore、回调或数据库连接。当前已配置密钥值用于保存前脱敏，包括错误文本。恢复时读取当前 `.env` 创建所需运行时对象；已保存的补研究调用上限保持原值，不能通过提高环境配置重置已有预算。

## 安全边界与恢复

| Checkpoint | 写入时机 | 恢复行为 |
| --- | --- | --- |
| `RUN_CREATED` | Run metadata 创建后、规划前。 | 规划中断也可显式恢复；此处允许 plan 尚不存在。 |
| `PLAN_CREATED` | 计划创建并验证 DAG 后。 | 不再调用 Supervisor。 |
| `TASK_RESEARCH_COMPLETED:<id>` | 该任务研究/抽取、canonical merge 和 Memo 渲染完整提交后。 | 不重做研究；若缺 Verification，只做缺失部分。 |
| `TASK_COMPLETED:<id>` | 对应 Verification 完成后；关闭验证时则在合并完成后。 | Task 保持 completed，现有 DAG 跳过它。 |
| `INITIAL_RESEARCH_COMPLETED` | 初始 DAG 全部任务完成。 | 初始 Research/Verification 不重跑。 |
| `RELEVANCE_BATCH_COMPLETED` | 一个完整且校验通过的 relevance batch 返回。 | 跳过已有决策，只处理缺失 Claim；此时尚不发布部分 Gap Detection 结果。 |
| `GAP_DETECTION_COMPLETED` | 完整相关性结果和程序化 Gap 均已生成。 | 不重复 relevance，也不生成重复 Gap。 |
| `RERESEARCH_STARTED` / `RERESEARCH_PLAN_CREATED` | 保存已选组/初始预算，以及校验完成的补研究计划。 | 不重新选择或规划已保存的任务，继续同一轮。 |
| `RERESEARCH_CALL_RESERVED` | 每次补研究模型请求发出前。 | 从最后安全图的副本只更新预算许可，避免崩溃后预算归零。 |
| `RERESEARCH_TASK_RESEARCH_COMPLETED:<id>` | 补研究结果完整合并后。 | 已有研究产物不重做，继续缺失验证。 |
| `RERESEARCH_TASK_COMPLETED:<id>` | 对应补研究 Verification 完成。 | 验证跳过；相关性仍只处理缺失部分。 |
| `RERESEARCH_RELEVANCE_BATCH_COMPLETED` | 补研究相关性完整批次完成。 | 已存相关性不重做。 |
| `RERESEARCH_COMPLETED` | 既有单轮补研究结束，包括预算耗尽/受控降级结束。 | 不再启动 Round 1。 |
| `WRITER_COMPLETED` | Writer + Report Grounding 全部完成。 | 加载 grounded ResearchReport，Writer/grounding 均跳过。 |
| `CITATION_COMPLETED` | 确定性 citation/References 渲染及完整性检查后。 | 加载 RenderedResearchReport，不重新渲染。 |
| `RUN_COMPLETED` | 最终产物齐全后。 | 显示 already completed 和既有报告，0 模型调用、0 citation rendering。 |

所有 durable write 都在主调度线程执行。并行研究保持原有整批等待机制；Worker 内部完成但尚未返回、合并和验证的任务，不算可恢复的 completed Task。运行时遇到任意半完成状态不会在异常处理里补写快照。

恢复把 `running/failed` 转回 `pending`，保留 `completed`。如果存在安全产物则复用它，仅补缺失验证；没有产物的中断 Task 从头执行，获得新的局部 ToolBudget。恢复不续接 HTTP 请求。已有 Source/Evidence/Claim/Gap/Verification ID 原值不变；当前 ID 使用 UUID，无需递增计数器。去重索引由 canonical 数据重建，aliases/provenance 原样恢复，重复 merge 仍幂等。

补研究的许可在 Worker 请求前通过主事件循环持久化。为防止重启绕过预算，进程在“许可已提交、HTTP 尚未开始”之间退出时，该许可仍计费；这可能减少剩余可用次数，不会额外增加调用额度。SQLite 写失败时不放行请求。预算耗尽仍沿用 Phase 4C 的受控结束行为，未验证材料不会进入 Writer。

普通异常只更新 `FAILED/error_type/error_message/current_stage`；Ctrl+C / async cancellation 标记 `INTERRUPTED`。恢复沿用 run_id，`resume_count += 1`，清空之前错误。Run metadata 的活动阶段可领先于最近安全快照，恢复只看快照和数据完成状态。

嵌入式 API 传入 `run_store=ResearchRunStore(...)` 启用持久化；省略时继续原有内存行为。同步 `run/resume` 和异步 `arun/aresume` 共用同一执行流程。第一版仅用于单机、同一个 Run 单个活动进程，不支持同时启动两个进程恢复同一 Run。

## 交付问题逐项答复

1. 修改文件见“文件与职责”表；真实验收脚本/日志仅位于 Git 忽略的 workspace。
2. 默认数据库为 `workspace/research.db`。
3. `research_runs` 结构见 SQL。
4. `checkpoints` 结构见 SQL。
5. ResearchRunState 字段见 JSON 状态表。
6. Pydantic JSON dump/validate，dataclass 字段由 Pydantic 明确恢复。
7. 是否使用 pickle：**No**。
8. 安全边界见 Checkpoint 表。
9. 每个快照写入时机见同表；异常处理不另存半状态。
10. 状态从持久化 ResearchPlan/ReResearchPlan 恢复。
11. RUNNING → PENDING；有安全产物则只继续未完成验证。
12. COMPLETED Task 不重新执行。
13. EvidenceStore 显式 restore canonical records、aliases、provenance，并重建索引。
14. Source / Evidence / Claim IDs 完全保持原值。
15. 当前生成器为 UUID；恢复时不调用生成器，不需计数器迁移。
16. 已有 VerificationResult 跳过，缺失才调用 Verifier。
17. 已有 ClaimRelevanceResult 跳过，缺失才调用 assessor。
18. 已完成 Gap Detection 不重复创建 Gap，ID/status 保持。
19. 补研究计划、任务状态、完成轮次和完成阶段一起恢复；完整 Round 1 不重跑。
20. 全局补研究预算保存；许可在发请求前落盘。
21. WRITER_COMPLETED 后不调用 Writer 或 grounding。
22. CITATION_COMPLETED 后不重新渲染 Citation。
23. RUN_COMPLETED 后显示既有报告，不进行研究或模型调用。
24. Failure 不覆盖最后安全 checkpoint，只更新 Run metadata。
25. KeyboardInterrupt / async cancellation 标记 INTERRUPTED，保留安全快照。
26. API Key 不保存；不保存配置/headers，文本和错误脱敏，测试扫描 SQLite 内容。
27. 恢复使用 `python main.py --resume RUN_ID`。
28. 查询使用 `python main.py --list-runs`。
29. 恢复日志包含 Run ID/Goal、checkpoint stage/sequence、任务与图数据数量、验证/相关性数量、Gap/预算计数、跳过列表、下一阶段。
30. Phase 6 离线测试结果与真实验收见下文。
31. Phase 2～5 回归结果见下文。
32. 真实运行的 run_id、中断边界、跳过任务、恢复阶段、最终状态、ID/调用审计见下文。

## 验收记录

2026-10-12：全量 **667 项测试通过**（原有 625 项 + Phase 6 新增 42 项，21.47 秒）。Phase 2～5 回归全部保持通过。新增测试使用真实临时 SQLite，覆盖事务中途失败回滚、序列与 latest pointer、JSON/ID/alias/provenance/dedup 恢复、任务与完整阶段跳过、两次失败后恢复、验证缺失、relevance 完整批次恢复、KeyboardInterrupt、零合格报告、损坏图和版本、CLI/密钥排除、补研究计划/任务/整轮、并发 Worker 主线程写库、预算许可跨崩溃保存及余额耗尽。补研究运行时重建缺口通过回归用例固定。

真实验收使用当前 `.env` 模型与 Tavily 服务，验证数据库独立放在 `workspace/phase6_live.db`，没有更改生产模型、密钥或超时配置。测试脚本通过仅测试的 `failure_injector` 在安全边界抛出 **KeyboardInterrupt**，没有模拟执行到一半的 HTTP 恢复，也不是实际按键。

| 项目 | 实际结果 |
| --- | --- |
| 问题 | Python functools.cache / functools.lru_cache 的容量、适用场景与线程安全保证，优先官方文档。 |
| Run ID | `fd1f6631a3574e37aabb85cdcc22c97c` |
| 规划故障恢复 | 首次规划请求超时，Run=FAILED、latest=RUN_CREATED；对同一个 Run 显式恢复成功。 |
| 中断阶段 | INITIAL_RESEARCH_COMPLETED，sequence=11，status=INTERRUPTED。 |
| 中断时数据 | 已完成 task1/task2/task3/task4，2 Source / 24 Evidence / 13 Claim / 13 VerificationResult，4 次批量验证调用。 |
| 恢复方法 | 独立进程运行 `main.py --resume <id> --db-path workspace/phase6_live.db`。 |
| 跳过工作 | Supervisor、4 个初始 ResearchTask、它们的搜索/抓取/抽取及全部初始 Verification。 |
| 恢复阶段 | Gap Detection → 一轮补研究 → Writer/grounding → Citation → RUN_COMPLETED。 |
| 最终状态 | COMPLETED，resume_count=2（规划故障恢复一次、安全中断恢复一次），latest sequence=37。 |
| 最终图 | 2 Source / 29 Evidence / 21 Claim；相对中断快照只有补研究新增 5 Evidence / 8 Claim。 |
| ID/去重审计 | 原有全部 Source/Evidence/Claim ID 保持；13 个原验证 ID 集合完全一致，无重复初始验证或来源。 |
| 调用成本 | 恢复后初始 Research calls=0、初始 Verification calls=0；避免重跑 4 个研究任务和已有 4 次批量验证。补研究实际使用 12/12 次许可，完整统计保留。 |
| 最终报告 | 10 accepted Claims，Writer 1 次调用，grounding 后保存报告，1 条 Reference；通过完整链路与渲染校验。 |
| 再恢复已完成 Run | CLI exit=0，输出 already completed 和既有报告；没有模型请求、研究、Writer/grounding 或 citation rendering，没有新 Checkpoint。 |
| Secrets | 整个验收 SQLite 内容扫描，不含当前配置的 API Key/Secret。 |

验收产物：`workspace/phase6_live_acceptance.json`、`phase6_live_recovery_interrupt.log`、`phase6_live_resume.log`、`phase6_live_completed.log`、`phase6_live_report.md`。这些文件和数据库均被 Git 忽略。此验收验证恢复与成本行为，不作为报告研究覆盖质量或事实判断准确率的保证。
