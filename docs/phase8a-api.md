# Phase 8A — FastAPI Backend

本阶段提供 HTTP 入口和单进程后台执行，不实现 React、SSE、Docker 或 Phase 8B。Supervisor、Researcher、验证、缺口、补研究、Writer、Citation 和 Evaluation 的算法与调用预算保持原样。

## 启动

在项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m app.api
```

默认地址为 `http://127.0.0.1:8000`，Swagger UI 为 `http://127.0.0.1:8000/docs`，OpenAPI 为 `/openapi.json`。仅启动、查状态、读报告无需模型调用；真正研究仍使用已有 `.env` 模型与 Tavily 配置。

也可以使用 Uvicorn 标准入口（此方式的 host/port 由命令行指定）：

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000 --workers 1
```

**同一数据库一次只由一个执行进程使用。** API 使用单个 worker，不使用 `--reload`、多 worker 或多个服务实例。运行 API 前先停止使用同一数据库执行研究的 CLI / Evaluation 进程。CLI 与 API 可以交替使用同一数据库和 Run ID，也可分别配置不同数据库。API 启动时会将数据库内遗留的 `RUNNING` 标为 `INTERRUPTED`，因此不能把正在由其他进程使用的数据库交给它。

API 在数据库旁使用 `.api.lock` 操作系统文件锁，重复启动 API 实例会明确失败，且不会将正在研究的 Run 错标为中断。锁文件可以保留，进程退出后由操作系统释放锁。这只是防止不支持的部署方式；CLI / Evaluation 不获取该 API 锁，仍须按上述规则交替执行。

## 配置

沿用现有 Settings 和 `.env`，不新增凭据格式。

| 环境变量 | 默认值 | 含义 |
| --- | --- | --- |
| `RESEARCH_DB_PATH` | `workspace/research.db` | CLI/API 共用的 Phase 6 SQLite 数据库；请求不能指定路径。 |
| `API_HOST` | `127.0.0.1` | `python -m app.api` 监听地址。 |
| `API_PORT` | `8000` | 监听端口，1–65535。 |
| `API_MAX_CONCURRENT_RUNS` | `1` | 同时执行的研究 Run 数，1–8；与单 Run 内原有 DAG 并发上限分别控制。 |
| `API_CORS_ORIGINS` | `["http://localhost:5173","http://127.0.0.1:5173"]` | JSON 数组，只接受明确的 localhost / loopback HTTP(S) Origin；不接受 `*`、外部域名、用户名或路径。`[]` 禁用跨域访问。 |

只允许 GET/POST 和 Content-Type 跨域，不允许跨域凭据。API 不返回 Settings、密钥、数据库路径或原始异常文本。研究自身的原有错误详情仍由 SQLite 脱敏保存。

## 接口

| 接口 | 成功 | 功能 |
| --- | --- | --- |
| `GET /api/health` | 200 | 应用及 SQLite 读取检查，返回 `{"status":"ok"}`。 |
| `POST /api/runs` | 202 | 校验并持久化问题与初始检查点，后台执行研究，立即返回 `run_id`、`status`。 |
| `GET /api/runs` | 200 | 最近更新的 Run；`limit` 默认 20，范围 1–100。 |
| `GET /api/runs/{run_id}` | 200 | 问题、持久化状态/阶段、UTC 时间、恢复次数、检查点可用性、可恢复标记、计划/任务及补研究任务状态、公开错误摘要。 |
| `GET /api/runs/{run_id}/report` | 200 | 完成 Run 的结构化 `report`、已保存的 `markdown` 和 `references`，保留 Citation 编号与 URL。 |
| `POST /api/runs/{run_id}/resume` | 202 | 校验 Phase 6 检查点，后台调用现有 `resume(run_id)`，跳过已完成任务和阶段。 |

创建请求：

```json
{"question":"调研离线强化学习的最新发展"}
```

仅接受 `question` 字段，去除首尾空白后长度为 1–10000 字符。HTTP 请求不接受模型配置、凭据、任意本地文件路径或阶段开关。

新建 Run 在规划前可能尚无 `plan`。任务状态来自最近的安全检查点，阶段来自已有 `set_stage()`，不保证逐秒或逐工具更新；第一版通过轮询查询。`created_at` / `updated_at` 使用带时区的 ISO 8601；`updated_at` 是最后持久化更新时间，不是专门的完成时间。

状态沿用 Phase 6 的 `RUNNING / FAILED / INTERRUPTED / COMPLETED`，不新增队列状态。满额返回 429，**不创建新 Run，不建立无限等待队列**。`resumable` 表示失败/中断且存在已验证检查点，接收恢复请求时仍检查当前状态及容量。

常见错误统一为：

```json
{"error":{"code":"RUN_ALREADY_RUNNING","message":"This research run is already running."}}
```

| HTTP | code | 说明 |
| --- | --- | --- |
| 404 | `RUN_NOT_FOUND` | Run 不存在。 |
| 409 | `RUN_ALREADY_RUNNING` | 同一 Run 正在执行或已获后台执行许可。 |
| 409 | `RUN_ALREADY_COMPLETED` | Run 已完成，不再次研究；直接读取报告。 |
| 409 | `REPORT_NOT_READY` | Run 尚未完成，包含失败/中断情况。 |
| 409 | `REPORT_UNAVAILABLE` | 旧 Run 关闭过报告写作等情况下，没有可读取的已保存报告；不调用 Writer 补写。 |
| 409 | `CHECKPOINT_UNAVAILABLE` / `CHECKPOINT_INVALID` | 无检查点或 Phase 6 完整性/版本校验失败，不启动恢复。 |
| 422 | `INVALID_REQUEST` | 请求不符合 Schema，不回显可能含敏感值的输入。 |
| 429 | `RUN_CAPACITY_EXCEEDED` | 后台研究容量已满，稍后重试。 |
| 503 | `BACKGROUND_START_FAILED` / `SERVER_STOPPING` | 后台启动失败或服务正在退出，保留检查点。 |
| 500 | `INTERNAL_ERROR` | 请求内部异常，不暴露原始异常、密钥或本地路径。 |

研究失败通过状态接口返回 `FAILED` 和 `error`，查询这个已存在 Run 本身仍是 200；报告读取仍返回 409。公开错误摘要区分超时、失败和中断，完整的脱敏诊断可在本机数据库和运行日志中查看。

## PowerShell 使用示例

```powershell
$body = @{ question = "调研离线强化学习的最新发展" } | ConvertTo-Json
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/api/runs `
    -ContentType "application/json; charset=utf-8" -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$runId = $created.run_id
Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$runId"
# 状态为 COMPLETED 后读取；未完成时 HTTP 409。
$report = Invoke-RestMethod "http://127.0.0.1:8000/api/runs/$runId/report"
$report.markdown
# 仅在 FAILED / INTERRUPTED 且 resumable=true 时恢复。
Invoke-RestMethod -Method Post "http://127.0.0.1:8000/api/runs/$runId/resume"
```

原有 CLI 命令保持可用：

```powershell
.\.venv\Scripts\python.exe main.py "调研离线强化学习的最新发展"
.\.venv\Scripts\python.exe main.py --resume RUN_ID
.\.venv\Scripts\python.exe main.py --list-runs
```

## 执行与恢复边界

HTTP 层不执行研究算法。`ResearchOrchestrator.prepare_run()` 只保存 Run ID 与 `RUN_CREATED` 检查点，随后后台线程调用同一个 `run(..., prepared_run_id=...)`；同步入口在工作线程中建立原有 asyncio 调度循环。显式恢复直接调用 Phase 6 `resume()`，不会新建 Run 或重建恢复算法。

Run 的持久化状态、阶段和报告均以原有 SQLite 表为准；内存集合只做当前进程的容量和重复执行控制。API 的新启动适配共用初始状态构建与 `_protected_execute()` / `_execute()`，不增加 LLM 调用。SQLite 表结构及 `state_version=1` 保持不变。

服务重新启动后不自动研究，遗留 `RUNNING` 会转为 `INTERRUPTED`；保留原阶段和检查点，用户显式 Resume。完整阶段的模型请求不会因为轮询或读取报告而重跑。检查点之后尚未完成的操作可能在恢复时重新执行，这沿用 Phase 6 语义，无法恢复半途中的 HTTP 请求。

优雅关闭停止接收新研究并等待已在执行的线程结束，不声称能够强行取消同步模型/工具请求；强制结束进程后靠检查点恢复。本版本无持久化待执行队列、多进程或分布式调度、用户鉴权/隔离、取消接口、SSE 和前端；默认仅供本机开发使用。

实现采用 FastAPI 官方的 [lifespan 生命周期](https://fastapi.tiangolo.com/advanced/events/)、[同步端点线程池](https://fastapi.tiangolo.com/async/)和[明确 CORS Origin](https://fastapi.tiangolo.com/tutorial/cors/)机制。

## 验证

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_api.py tests/test_persistence.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

API 测试使用真实临时 SQLite 和受控 Mock Pipeline，覆盖非阻塞创建、事件循环响应、报告原样读取、全局并发和重复执行竞态、失败落库、重启/显式恢复、Phase 6 跳过阶段、CLI 双向兼容、统一错误、CORS 和密钥保护。测试不调用真实 LLM、搜索或 Writer 服务。

2026-10-16 验收：新增 38 项用例，全量 **826 passed in 38.20s**，`pip check` 通过。使用独立 SQLite 实际启动 `python -m app.api`，health、Swagger、OpenAPI、历史查询全部 HTTP 200，随后原 CLI 的 `--list-runs` 退出码为 0。该启动验收没有创建研究 Run 或调用模型，不改动现有研究数据库。记录位于 `workspace/validations/phase8a_api/acceptance.json`。
