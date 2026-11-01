# Phase 8B — React Frontend

React + TypeScript + Vite 提供研究工作台、Run 详情、已保存报告和只读证据探索。只使用 React Hooks 和 React Router 管理页面与请求状态，不实现研究算法、SSE、Docker 或 Phase 8C。

## 启动

需要 Node.js 22.12+ 和已有 Python 虚拟环境。两个终端均从项目根目录开始。

终端一，启动或重新启动后端；此前已经运行的 Phase 8A 服务需要重启，才能加载新增的 Evidence 只读接口：

```powershell
.\.venv\Scripts\python.exe -m app.api
```

终端二，首次安装后启动前端：

```powershell
cd frontend
npm ci
npm run dev
```

在浏览器打开 **http://127.0.0.1:5173/** 使用页面。后端为 `http://127.0.0.1:8000`，Swagger 仍是 `http://127.0.0.1:8000/docs`；后端根地址 `/` 仍不提供 React 首页。

Vite 默认将浏览器的 `/api` 请求代理到 `http://127.0.0.1:8000`，无需复制 Python `.env` 或向浏览器提供模型密钥。前端 Node 进程不直接访问 SQLite，也不执行 Research Pipeline。

## 页面

| 路径 | 功能 |
| --- | --- |
| `/` | 输入问题、开始研究、查看最近 20 个 Run 及状态、按问题或 ID 搜索近期记录。示例问题只填入输入框，不自动研究。 |
| `/runs/:runId` | 显示原研究问题、完整 Run ID、状态、阶段、更新时间、计划任务、任务状态、依赖关系与补研究任务；允许恢复的失败/中断任务提供显式恢复按钮。 |
| `/runs/:runId/report` | 读取后端保存的完整 Markdown 报告，保留标题、摘要、发现、分析、局限、结论及 References；侧栏按原 Citation 编号显示原始来源 URL，并可下载原始 Markdown。 |
| `/runs/:runId/evidence` | 选择/搜索 Claim，按任务筛选，查看关联 Evidence、Source 和已有 Verification / Relevance 结果。`?claim=ID` 可定位选择，刷新后保留。 |

创建成功后自动跳转 Run 详情。刷新、复制链接或重新打开带 `runId` 的地址，都从后端读取持久化数据，不依赖浏览器内存保存研究结果。

运行中的 Run 每 2.5 秒轮询状态；进入终态后停止详情轮询。证据页在研究中每 3.5 秒刷新，读取同一个安全检查点中的证据链。最近历史每 10 秒刷新、健康状态每 30 秒检查。请求按返回后再安排下一次的方式串行轮询，切换页面会中止旧请求并忽略过期返回。网络错误保留最近一次成功读取的真实状态，不把网络断线伪装成研究失败。

任务进度仅统计已有计划中完成的任务，不表示 Verification、Writer 和 Citation 整条 Pipeline 的完成百分比；它们由后端 `current_stage` 显示。任务状态延续 Phase 8A 的安全检查点粒度。

## 配置

可将 `frontend/.env.example` 复制为 `frontend/.env.local`：

```dotenv
VITE_API_BASE_URL=/api
API_PROXY_TARGET=http://127.0.0.1:8000
```

| 配置 | 用途 |
| --- | --- |
| `VITE_API_BASE_URL` | 构建进浏览器的公开 API 前缀，默认 `/api`。修改后需重启 Vite，生产包需重新 build。 |
| `API_PROXY_TARGET` | 仅供 Vite dev / preview 的 Node 代理读取，不暴露给浏览器。后端端口变化时修改此项。 |

不要将 `MODEL_API_KEY`、`TAVILY_API_KEY` 或其他凭据放入任何 `VITE_*` 变量。Vite 的 env 目录仅为 `frontend/`，不会加载项目根目录的模型 `.env`。默认同源代理避免跨域；如改成直接请求后端地址，继续使用后端已有的明确本地 CORS Origin 配置。

## 新增只读 API

`GET /api/runs/{run_id}/evidence` 从现有 SQLite 的最新安全检查点读取：

- `checkpoint_available`、`checkpoint_stage`
- `claims`、`evidence`、`sources`
- `verification_results`：每个 Claim 已保存的最近一次验证结果
- `relevance_results`：已经保存的目标相关性结果

Run 不存在返回 404；没有检查点时返回 200 和空图；检查点不兼容或损坏沿用 Phase 6 校验并返回 409。证据尚未产生或判断尚未保存时，前端明确显示空状态或「尚未验证 / 尚未判断」，不自行补充事实、关联或验证结论。

接口只返回公开研究数据，由原有脱敏流程处理；不返回 Settings、数据库路径或原始异常。没有修改 EvidenceStore、SQLite schema、验证规则、Pipeline、CLI 或 Evaluation 逻辑，不调用模型、不重新 Verification。

## 报告与安全

前端只读取已有报告，不调用 Writer 或重新编号 Citation。Markdown 通过 `react-markdown` + `remark-gfm` 渲染，禁用原始 HTML，仅允许文本排版、代码、表格和安全链接；图片、iframe、脚本等不渲染。可点击地址只允许无内嵌用户名/密码的绝对 HTTP(S) URL，外部链接使用 `noopener noreferrer`。来源标题与证据正文按 React 文本节点显示，不使用 `dangerouslySetInnerHTML`。

引用标记 `[1][2]` 保留原文，右侧来源编号、标题和 URL 来自后端 References。不会从 Task 标题生成报告内容，也不会根据文本相似度推断证据链。下载直接使用后端保存的 `markdown`。

POST 创建和恢复不会自动重试；提交超时会提示先查看历史，避免用户在结果不确定时重复启动研究。404、429、检查点错误、报告未就绪、后端断连和空结果均有明确页面状态。

## 构建与测试

```powershell
cd frontend
npm ci
npm run build
npm test
npm run test:e2e
```

浏览器用例默认使用本机 Chrome。测试机需已安装 Chrome，也可用 `npx playwright install chrome` 安装。要使用 Playwright Chromium：

```powershell
npx playwright install chromium
$env:PLAYWRIGHT_CHANNEL = "chromium"
npm run test:e2e
```

`npm run build` 运行严格 TypeScript 检查并生成 `frontend/dist/`。可用 `npm run preview` 在 `http://127.0.0.1:4173` 检查生产构建；preview 也保留 API 代理，后端仍需运行。生产静态托管需要为 `/runs/*` 配置回退到 `index.html`，并将 `/api` 代理到后端；本阶段不新增部署或 Docker 实现。

后端回归从项目根目录执行：

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

验证覆盖创建与跳转、任务依赖、轮询完成、历史查询、报告和引用、证据链、未知验证状态、错误显示、显式恢复、刷新、Markdown 安全，以及桌面和窄窗口布局。单元和浏览器测试均使用 Mock 网络或 Pipeline，不消耗真实研究 Token。只读 Evidence API 测试还验证多次读取后检查点、模型调用计数和 Run 元数据不变。

2026-10-16 验收结果：

- Python 全量：**831 passed**（新增 5 项只读 Evidence API 用例）。
- 前端单元：**13 passed**；Chrome 浏览器工作流：**12 passed**。
- `npm ci`、严格 TypeScript 检查、Vite 生产构建通过；依赖审计无已知漏洞。
- 使用独立 SQLite、离线 Pipeline doubles、实际 FastAPI 与 Vite preview 完成真实 HTTP 验收：历史 → Run → 已保存报告 → 页面刷新 → Evidence → 原始 Source URL 全部通过，浏览器运行错误为 0，没有真实模型调用。
- 检查生产构建，未发现当前已配置的 API Key 或测试 Fixture。现有研究数据库未修改。

真实 HTTP 验收记录位于 `workspace/validations/phase8b/acceptance.json`；浏览器测试截图位于 `frontend/test-results/`，覆盖 Dashboard、Run 详情、报告、证据与窄窗口布局。

## 范围限制

前端沿用 Phase 8A 的单 API 进程、有限后台并发与显式恢复规则；同一数据库执行研究时仍需让 API、CLI、Evaluation 交替使用。前端历史最多展示最近 20 项，不是完整历史分页。没有用户登录/隔离、取消研究、DAG 编辑器、SSE 或 Docker。界面状态的更新受轮询周期和已有检查点粒度影响。

实现参考：[React Effect 清理](https://react.dev/reference/react/useEffect)、[Vite 环境变量](https://vite.dev/guide/env-and-mode.html)、[react-markdown 安全说明](https://github.com/remarkjs/react-markdown#security)与 [Playwright Mock API](https://playwright.dev/docs/mock)。
