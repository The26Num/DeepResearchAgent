# Phase 8D — Docker Deployment & Documentation

本阶段添加部署文件和开源首页，保留 CLI / API 共用的 Pipeline。未改变 Verification、Citation、Checkpoint、模型参数或 Evaluation 决策逻辑。Python 包元数据补充了提示文件，避免非 editable 安装遗漏 `app/prompts/*.md`。

## 部署组成

| 文件 | 职责 |
| --- | --- |
| `Dockerfile` | Python 3.12 后端，非 root 用户，单 Uvicorn worker，API healthcheck。 |
| `frontend/Dockerfile` | Node 22 + `npm ci` 构建，最终镜像只保留 Nginx 与 dist，非 root 监听 8080。 |
| `deploy/nginx.conf` | 同源 API / Swagger 代理、SSE 无缓冲、Docker DNS 重解析、SPA 刷新入口。 |
| `docker-compose.yml` | 两个服务、健康依赖、loopback Web 端口、独立 SQLite Named Volume。 |
| `.dockerignore` | 排除真实环境文件、数据库、workspace、Git、虚拟环境及构建/测试产物。 |
| `.env.example` | 配置模板与 `WEB_PORT`；真实 `.env` 没有复制进镜像。 |

浏览器 → `127.0.0.1:8080` → Nginx → `backend:8000` → 既有研究后台线程 → `/data/research.db`。浏览器始终使用相对 `/api`，容器名 `backend` 仅在 Docker 网络内解析。仅 frontend 发布端口；后端没有主机端口映射。

SSE 使用 HTTP/1.1 转发、关闭 `proxy_buffering` / cache / gzip，读超时为 3600 秒；原应用继续发送心跳。Docker DNS resolver 定期重新解析 backend，允许后端容器重建后继续代理。连接断开不取消研究，页面重连并读取 Run 状态。

## 首次启动

需要 Docker Engine、Compose v2（支持 `up --wait`）。Windows 使用 Docker Desktop 的 Linux containers 模式。先确认：

```powershell
docker version
docker compose version
```

在项目根目录准备 `.env`，已有配置应保留，只在首次创建时复制模板：

```powershell
Copy-Item .env.example .env
# 编辑 MODEL_API_KEY / MODEL_BASE_URL / MODEL_NAME / TAVILY_API_KEY 后执行
docker compose up -d --build --wait
docker compose ps
```

- Web：http://127.0.0.1:8080/
- Swagger：http://127.0.0.1:8080/docs
- Health：http://127.0.0.1:8080/api/health
- SSE：`curl.exe -N http://127.0.0.1:8080/api/runs/RUN_ID/events`

运行示例：在 Web 输入问题 → Start Research → Task / 阶段 / 时间线 → 完成后打开已保存报告 → References / Evidence Explorer。真实研究需要有效模型、搜索服务与目标网页网络；容器启动不会自动发起 LLM 调用。

`WEB_PORT=8080` 可在 `.env` 中修改。Compose 将 `API_HOST=0.0.0.0`、`API_PORT=8000`、`API_MAX_CONCURRENT_RUNS=1`、`API_CORS_ORIGINS=[]`、`RESEARCH_DB_PATH=/data/research.db` 固定为部署值，覆盖本地开发设置。不要将 `VITE_*` 当作密钥配置。

不要公开打印 `docker compose config` / 容器环境：解析后的配置可能包含 env_file 的密钥。只检查配置可使用 `docker compose config --quiet`。

## 重启、升级与显式恢复

```powershell
docker compose logs -f backend
docker compose restart
docker compose down
docker compose up -d --wait
```

这些操作保留 Volume。`research_data` 是逻辑名称，实际卷名受 Compose 项目名影响，默认类似 `deepresearch_research_data`。保持项目名和数据目录一致。不要在日常重启使用 `down --volumes`，它会删除研究数据。

修改 `.env` 后应重建 backend 的容器环境；普通 restart 不重新载入 env_file：

```powershell
docker compose up -d --force-recreate --wait backend
```

代码修改后：`docker compose up -d --build --wait`。

正常停止最多给 backend 30 秒退出时间；若研究仍未结束，容器可能被终止。下一次启动沿用既有 Phase 6 机制，将遗留 RUNNING 标为 INTERRUPTED，保留安全快照。页面确认 `resumable=true` 后显式恢复，**不会自动重跑**。终止时尚未提交的操作可能重做；已经提交的阶段/任务跳过。

## 数据持久化与备份

Run、报告、证据和 Checkpoint 都在同一 SQLite 数据库中，不依赖镜像文件层。Named Volume 同时保留数据库、WAL / SHM 和 API 进程锁。事件仅保留在有界内存，不在 Volume 中历史重放。

一致性备份使用 SQLite backup API，不直接复制活跃 WAL 数据库的单个主文件：

```powershell
docker compose exec -T backend python -c "import sqlite3; a=sqlite3.connect('/data/research.db'); b=sqlite3.connect('/data/research-backup.db'); a.backup(b); b.close(); a.close()"
docker compose cp backend:/data/research-backup.db ./research-backup.db
```

将备份保存在容器卷以外的位置。备份包含研究问题、来源和报告，应按自己的数据保存要求管理。

### 从本地 CLI / API 库首次迁移

容器默认创建新库，不会自动读取宿主机 `workspace/research.db`。以下步骤只用于**首次、空目标 Volume**，不覆盖已有部署数据。先停止本地 API / CLI / Evaluation 对原库的研究执行，并保留原文件。

```powershell
# 项目根目录，创建一致性迁移副本
.\.venv\Scripts\python.exe -c "import sqlite3; a=sqlite3.connect('workspace/research.db'); b=sqlite3.connect('workspace/research-migration.db'); a.backup(b); b.close(); a.close()"
docker compose build
docker compose create backend
docker compose cp ./workspace/research-migration.db backend:/data/research.db
# docker cp 的文件可能归 root；只调整这个迁移副本的属主
docker compose run --rm --no-deps --user 0 backend python -c "import os; os.chown('/data/research.db',10001,10001)"
docker compose up -d --wait
```

通过历史页检查原 Run ID、报告和可恢复状态。不要同时运行两个 backend、多个 Uvicorn worker，或让 CLI / Evaluation 在另一进程使用同一卷执行研究。只读历史查询可在容器内执行 `docker compose exec -T backend python main.py --list-runs`。

## 独立 Docker 验收

`tests/test_deployment.py` 包含默认执行的本地离线端到端用例，以及需要显式启用的 Docker 用例：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
$env:RUN_DOCKER_TESTS="1"
.\.venv\Scripts\python.exe -m pytest tests/test_deployment.py -q
Remove-Item Env:RUN_DOCKER_TESTS
```

Linux / macOS：`RUN_DOCKER_TESTS=1 .venv/bin/python -m pytest tests/test_deployment.py -q`。

测试创建随机 Compose 项目、临时 env_file、临时主机端口和独立 Named Volume。仅这个测试项目的资源会在结束时清理；不使用或删除原 `.env`、本机研究库或正常部署卷。

它复用生产镜像，通过只读挂载的 `tests/deployment/mock_backend.py` 注入离线输入和模型返回值；原 Orchestrator、Verifier / Relevance 校验、Writer、Grounding、Citation、API、SSE 与 SQLite 继续执行。测试 fixture 不进入生产镜像，不提供生产测试后门。

覆盖：

1. 构建与健康依赖、Web 静态资源、Swagger、JSON 404、SPA 深链接。
2. 创建 Run、经 Nginx 接收 Task / 完成 SSE、保存报告和完整证据链。
3. 在 D1/D2 安全完成后强制终止 backend，重启后识别 INTERRUPTED。
4. 显式 Resume，验证完成任务不重做，后续任务继续。
5. 重建 backend 容器、重启 frontend，原报告、引用和证据 ID 仍保持一致。
6. CLI 历史可读、已完成 Run 拒绝重复执行、镜像内没有 `/app/.env`、浏览器资源不含测试密钥。

## 验证记录

2026-10-17：当前开发机未检测到 Docker CLI、Docker Desktop、Docker Engine 或 WSL。**容器构建、Nginx 启动和 Named Volume 重启验收未执行，不能视为通过。** 显式启用但缺少 Docker 时，Docker 用例会报错；默认测试中它会跳过并说明原因。

本地验证结果：

| 检查 | 结果 |
| --- | --- |
| Python 全量回归 | **868 passed, 1 skipped**，101.75 秒；跳过项为需显式启用的 Docker 验收。 |
| 新增离线部署流程 | 真实 API / Pipeline 校验、报告 / 证据生成、应用重启后读取及已完成 Run 拒绝重复执行通过。 |
| 前端单元 | **21 passed**。 |
| Chrome 工作流 | **15 passed**，含 SSE 重连、报告、引用、证据链、刷新与错误显示。 |
| TypeScript / Vite | 类型检查与生产构建通过。 |
| Compose 配置 | 通过 compose-spec 官方 JSON Schema 校验；未执行 Docker CLI / Engine。 |
| Python Wheel | 构建成功，包含全部 **9 个 prompt 文件**、API / 事件模块；隔离目录导入与提示读取通过。 |
| 凭据检查 | Git 可发布文件、Wheel 和浏览器 dist 均未发现当前配置的密钥值；实际值未输出。 |
| CLI | 使用独立空测试库执行 `main.py --list-runs`，退出码 0；原库未修改。 |
| 文档 | 首页、部署说明及历史文档的本地链接目标检查通过。 |
| Docker / Nginx / Volume | **未执行**，等待可用的 Docker 环境。 |

验证产物位于未提交的 `workspace/validations/phase8d/`，包含规范 Schema、Wheel 和检查结果。全部使用离线测试数据，不重跑线上研究，不增加真实模型或搜索调用。

## 单实例限制与后续方向

- 默认 loopback、本机可信访问；没有鉴权、多租户、TLS 或公网访问控制。部署文件不是公网安全网关。
- 只有一个 backend 和 worker；没有 Redis、Celery、Kubernetes、分布式队列或水平扩容。
- JSON Object 不强制完整 Schema，当前重复字段等验证失败仍可能发生；本阶段未改变验证规则来隐藏这些错误。
- 容器依赖网络、有效服务凭据和模型可靠性。Docker 不提高事实准确率、不减少既有研究调用，也不保证 Token 成本。
- 基础镜像标签和 Python 依赖范围没有内容摘要锁定；后续可增加经过 Linux 验证的依赖锁定与 CI 镜像构建。

实现参考：[Docker build context / dockerignore](https://docs.docker.com/build/concepts/context/)、[Compose 健康依赖](https://docs.docker.com/compose/how-tos/startup-order/)、[Volume 生命周期](https://docs.docker.com/engine/storage/volumes/)、[Nginx proxy buffering](https://nginx.org/en/docs/http/ngx_http_proxy_module.html#proxy_buffering)。
