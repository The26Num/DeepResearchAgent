# Phase 8C — Real-Time Research Streaming

本阶段通过 SSE 展示阶段和 Task 级执行进度。Pipeline 保留原有同步/异步调度、模型调用、验证规则、预算、Citation 和 Checkpoint 边界；没有改成 Token 流，没有引入 WebSocket、Redis、Celery 或 Phase 8D。

## 启动与使用

重启后端和前端，加载本次接口与页面更新：

```powershell
# 项目根目录，终端一
.\.venv\Scripts\python.exe -m app.api
```

```powershell
# 另开终端
cd E:\deepagent\frontend
npm ci
npm run dev
```

打开 **http://127.0.0.1:5173/**，提交研究后进入详情页。页面显示实时连接状态、当前阶段、Task 卡片、阶段状态与执行时间线。在研究计划页观察到 SQLite 确认 COMPLETED 时，会自动打开已保存报告；报告页会自动加载完成结果，在证据页阅读时则保留当前页面。刷新或重新进入时先读取 Run API，再连接正在运行的研究。

Vite 原有 `/api` 代理可直接传递 SSE，后端仍使用 8000 端口。已有 CLI 保持可用；同一个数据库仍须让 API、CLI、Evaluation 交替执行研究。

## 接口与帧格式

`GET /api/runs/{run_id}/events`

- Run 存在：返回 `200`、`Content-Type: text/event-stream`，使用 UTF-8。
- Run 不存在：开始流之前返回统一 JSON `404 RUN_NOT_FOUND`。
- 超过连接上限：返回统一 JSON `429 STREAM_CAPACITY_EXCEEDED`；前端继续通过状态 API 校准。
- 已完成、失败或中断的 Run：发送当前快照与对应终态事件，然后结束流，不重跑研究。

用现有 Run ID 查看原始事件：

```powershell
curl.exe -N -H "Accept: text/event-stream" http://127.0.0.1:8000/api/runs/RUN_ID/events
```

每个事件采用标准的 `id / event / data` 帧，空行分隔：

```text
id: a3b42c4cd84c4c1fb43f9fda433c7801
event: task_started
data: {"run_id":"RUN_ID","event_id":"a3b42c4cd84c4c1fb43f9fda433c7801","event_type":"task_started","timestamp":"2026-10-16T15:00:00Z","task_id":"D1","stage":"INITIAL_RESEARCH_BATCH","message":"任务开始执行","progress_data":{"task_status":"running"}}

```

示例省略未使用的可选字段；实际数据也可能包含 `null`、空 `tasks` / `phases` 数组。字段含义：

| 字段 | 含义 |
| --- | --- |
| `run_id` | 原有持久化 Run ID。 |
| `event_id` | 单个事件的 UUID；广播给不同客户端时保持一致，不作为持久化序列。 |
| `event_type` | 固定白名单中的类型。 |
| `timestamp` | 事件创建时的 UTC 时间。 |
| `task_id` | 适用时为原 Task ID，否则为 null。 |
| `stage` | 原 Pipeline 阶段/检查点标识。 |
| `message` | 程序生成的固定简短说明，不采用原始异常或模型文本。 |
| `progress_data` | 仅允许状态、计数、耗时、恢复次数、Task 状态和阶段状态。 |

`progress_data` 使用 Pydantic 白名单：`run_status`、`task_status`、`phase`、`phase_status`、`elapsed_seconds`、`task_count`、`completed_tasks`、`claims`、`evidence`、`sources`、`resume_count`、`tasks`、`phases`。快照中 Task 状态最多 32 条，阶段最多 7 条；不发送问题全文、网页正文、Evidence 正文、完整 LLM 消息、API Key、本地路径或原始错误详情。既有凭据脱敏仍在事件发送前执行。

## 事件与真实阶段

| 类型 | 发布时机 |
| --- | --- |
| `run_started` | 后台 worker 开始本次新建或恢复执行。 |
| `run_snapshot` | 连接建立以及必要的状态修复，包含当前 SQLite Run 状态和可用的进程内 Task / 阶段状态。 |
| `stage_changed` | 规划、研究批次等原有阶段切换。 |
| `plan_created` | 研究计划检查点保存成功。 |
| `task_started` | 原任务真正进入执行。 |
| `task_research_completed` | Researcher 已返回；仍等待合并和必要验证，不将 Task 标为最终完成。 |
| `verification_started / completed / failed` | 初始或补研究 Task 的既有验证阶段，Task ID 随事件提供。 |
| `task_completed` | 必要处理和验证完成，且相应安全检查点已保存。 |
| `task_failed` | 既有任务、合并或验证路径失败。 |
| `initial_research_completed` | 初始研究检查点保存成功。 |
| `gap_detection_started / completed` | 原有相关性与缺口检查阶段进入/保存完成。 |
| `reresearch_started / plan_created / completed` | 原有补研究阶段进入、计划保存、阶段结束；没有缺口时阶段也会正常结束，不意味着一定发起了新研究。完整类型为 `reresearch_plan_created` 等。 |
| `writer_started / completed` | Writer 进入及报告内容保存；内容保存后还需 Citation 阶段。 |
| `citation_started / report_completed` | Citation 整理进入及带引用的报告保存成功。 |
| `run_completed / failed / interrupted` | SQLite 已写入对应终态之后通知客户端，然后关闭事件流。 |
| `stream_reset` | 慢连接错过了部分有限缓冲事件，需要重新读取状态。 |
| `stream_error` | 事件流内部出错，只提供通用说明并要求状态校准。 |

补研究类型采用项目拼写 **`reresearch_*`**。不会为了生成这些事件再次执行任务或重新 Verification。

`GET /api/runs/{run_id}` 同时增加可选公共字段 `phase_statuses`，从既有检查点标记、阶段和非敏感阶段开关恢复规划、资料研究、验证、缺口、补研究、Writer、Citation 状态。没有启用的阶段显示「未启用」，刷新后不依赖遗失的事件推测已完成阶段。

## 采集方式与可信边界

`app/research/events.py` 定义轻量 `EventSink` / `ProgressUpdate`。Orchestrator 在 `_enter()`、保存成功后的 `_save()`、任务与验证调用边界发布观察事件，不解析 `print(stdout)`，不修改 Agent 的 `invoke`、工具预算或研究决策。CLI 默认不绑定 sink；观察回调异常会被隔离，不触发模型重试或改变研究结果。

API Run Service 绑定 run_id 并补齐事件 ID、时间和固定说明，处理后台启动/失败/完成通知。终态通知在数据库状态落库后发布；如果回调缺失，SSE 仍定期读取 SQLite 元数据以修复终态。

前端的即时 Task 状态只用于展示，已经提交的 Task completion 不被旧事件退回 running。Run 的完成/失败以及报告开放始终由状态 API 确认；收到终态事件只促使重新查询，不直接把本地 Run 改成 COMPLETED。读取报告继续使用既有 GET 接口，不调用 Writer。

## 心跳、断开与重连

- 初始发送 `retry: 2000`。默认每 15 秒发送 SSE 注释 `: heartbeat`，注释不是 ResearchEvent。
- 服务端监听客户端断开，释放订阅；即使连接在响应头发送前断开，也释放连接额度。
- 关闭浏览器或断开 SSE **只结束订阅，后台研究继续**。
- 浏览器连接错误后关闭旧 EventSource，按 1、2、4、8、15 秒上限重连，每次重连重新查询状态。不会重新 POST 创建或 Resume。
- 无实时连接时，Run 状态沿用 2.5 秒查询；连接正常时降低到 15 秒做校准。独立时间线只记录本页实际收到的事件，最多保留 100 条。
- 每次连接先同步当前状态。连接内若缓冲溢出，发送 `stream_reset` / 快照并读取状态 API，优先恢复正确状态。
- 不提供完整历史重放，`Last-Event-ID` 不用于补发历史。服务重启后事件丢失，SQLite 检查点和显式 Resume 规则保持原样。
- `Cache-Control: no-cache, no-transform` 和 `X-Accel-Buffering: no` 减少代理缓存/缓冲；自行部署代理时也需允许持续的 SSE 响应。

## 配置与内存边界

沿用 `.env`：

| 配置 | 默认值 | 范围/作用 |
| --- | --- | --- |
| `API_SSE_HEARTBEAT_SECONDS` | 15 | 0.1–60 秒，注释心跳。 |
| `API_SSE_STATE_CHECK_SECONDS` | 5 | 0.1–30 秒，元数据校准间隔，只读 SQLite。 |
| `API_SSE_BUFFER_SIZE` | 128 | 16–512，每个活跃 Run 的事件环形缓冲上限。 |
| `API_SSE_MAX_CONNECTIONS` | 64 | 1–1024，当前 API 进程的 SSE 连接总上限。 |

每个订阅的唤醒通知合并发送，不为慢客户端累计无限回调。只保留活跃研究或有订阅的通道；Run 终止且无订阅后删除内存数据。没有事件持久化，没有新增 SQLite 表或 Checkpoint 写入，研究任务并发上限仍由 Phase 8A 控制。

## 验证

2026-10-16 验证通过：Python 全量 **847 项**（新增 16 项 SSE 测试），前端单元 **21 项**，Chrome 工作流 **15 项**；TypeScript 检查与 Vite 生产构建通过。

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm test
npm run test:e2e
npm run build
```

测试使用离线 Pipeline doubles / Mock API，不调用真实模型。覆盖：SSE 帧与响应头、正常连接、Task 顺序、安全完成检查点、完成/失败/中断、心跳、断开清理、真实 HTTP 连接、缓冲溢出、连接上限、状态补偿、前端重连、取消订阅、事件去重、有界时间线、回调引用更新、自动读取报告，以及旧 API、CLI、Checkpoint 和模型调用次数回归。

另用独立测试 SQLite、实际 FastAPI、Vite preview、Chrome 原生 EventSource 验证：创建研究 → SSE 即时 Task 卡片 → 时间线 → 流中断与重连 → 快照修复 → SQLite 确认完成 → 自动打开已保存报告。记录位于 `workspace/validations/phase8c/acceptance.json`，运行截图同目录。该验收没有修改原研究数据库，也没有真实模型请求。

## 已知限制

仍是单 API 进程中的轻量实时广播，不是多进程消息系统。事件不持久化，刷新或重启不会恢复完整旧时间线；页面用 SQLite 状态重建进度。长时间的单个模型/工具调用期间只会有心跳，没有每 Token 或网页正文流。状态校准和检查点边界可能短暂晚于即时事件，终态以 SQLite 为准。本阶段不提供取消研究、WebSocket、Redis、Celery、Docker 或 Phase 8D。

协议实现依据 [MDN SSE 格式、心跳和 EventSource 生命周期](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events)，HTTP 传输使用现有 [Starlette StreamingResponse](https://www.starlette.dev/responses/#streamingresponse)。
