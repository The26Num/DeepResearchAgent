# DeepResearch Agent

DeepResearch Agent 是一个以证据为中心的多阶段研究系统。本仓库目前只实现 Phase 1：把开放式研究问题拆成带依赖的任务，按依赖顺序搜索与阅读网页，并输出 Research Memo。最终目标是扩展到并行研究、证据收集与验证、补充研究和带引用的报告。

## 当前架构

```text
用户问题 → Supervisor → 带依赖的 ResearchPlan → Orchestrator
                                             └→ 就绪任务（串行）→ 独立 Researcher Agent → Research Memo
                                                      ↑                              └→ CompactResearchResult
                                                      └── 仅接收依赖任务的 compact result
```

- `app/agents/supervisor.py`：生成并校验 3–6 项研究计划，明确标注每项任务的 `task_type`；不搜索网页。
- `app/agents/researcher.py`：每项任务创建独立 DeepAgent；只接收压缩后的依赖结果，返回给人看的 memo 和供后续任务使用的 compact result。
- `app/research/compact.py`、`app/research/context.py`：从最终 memo 提取短结果，并控制每个依赖和总上下文长度；不会传递工具消息或网页正文。
- `app/research/tool_budget.py`：按任务类型创建独立的搜索和网页读取调用预算。
- `app/research/source_dedup.py`：对 Memo 的 Important Sources 标题做大小写、空白和标点规范化，合并明显重复的条目。
- `app/tools/web_search.py`：调用 DDGS，返回去重后的标题、URL 和摘要，不做研究总结。
- `app/tools/fetch_webpage.py`：用 HTTP 请求与 BeautifulSoup 提取 HTML 正文，限制返回长度；本阶段不解析 PDF。
- `app/research/orchestrator.py`：校验依赖、打印计划、按就绪顺序串行执行任务、只传递 compact result 并更新状态。
- `app/schemas/`：任务、计划、来源、证据的数据结构。证据目前只定义结构，不存储。
- `app/config.py` 和 `app/llm.py`：读取配置并集中创建 SiliconFlow ChatOpenAI 模型。
- `app/prompts/`：与 Python 代码分离的 Agent 指令。

## 安装

需要 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

在 macOS/Linux 下，激活命令为 `source .venv/bin/activate`。

## 配置与运行

复制 `.env.example` 为 `.env`，设置：

```dotenv
SILICONFLOW_API_KEY=你的密钥
MODEL_NAME=你在 SiliconFlow 使用的模型 ID
SILICONFLOW_BASE_URL=https://api.siliconflow.cn/v1
```

`.env` 已被 Git 忽略。模型必须支持工具调用；Supervisor 会先尝试 JSON Schema 结构化输出，如果接口不支持，会请求纯 JSON 并用 Pydantic 校验。两种方式都失败时会明确报错。

```powershell
python main.py "调研 sparse reward offline reinforcement learning 的最新研究进展"
```

也可直接运行 `python main.py`，再按提示输入问题。终端依次显示研究计划、每项任务的类型、依赖上下文字符数、工具调用次数、compact result 字符数及其 memo。运行离线测试：`pytest`。

任务类型包括 `discovery`（主动搜索并阅读新来源）、`analysis`（先复用依赖结果，信息不足时重读来源或补充搜索）与 `synthesis`（比较并综合前序结果，通常不重新搜索）。Memo 的 Important Sources 尽量包含标题、URL、可确认的年份或日期、相关性与来源支持的关键点；未知年份标为 `unknown`。

单个 compact result 最多约 3000 字符、6 条 findings、6 个来源和 4 条不确定信息；单个依赖上下文最多 2800 字符，总依赖上下文最多 8000 字符。网页读取默认返回 5000 字符、硬上限 8000；搜索最多返回 8 条、每条摘要最多 500 字符。每个任务独立计数：discovery 最多搜索 3 次并读取 4 页，analysis 为 2 次与 3 页，synthesis 为 1 次与 1 页。超过预算的工具请求会收到明确错误；单任务 Agent 另有 40 步上限，避免反复尝试耗尽上下文。

## 当前限制

Phase 1 仅串行执行任务。搜索结果只是线索，Researcher 可阅读可访问的 HTML 页面；独立任务若没有获得来源会明确失败，搜索到候选来源却未读取页面时会提醒 Agent 补读。压缩采用 Markdown 规则提取，若模型偏离约定格式，可能遗漏部分来源或细节；标题去重不做论文身份解析。仍无完整论文 PDF 解析、正式证据核验、证据库、引用校验或最终报告。部分网站可能拒绝自动读取。小模型可能忽略规划、时间范围或来源约束，Memo 仍需人工检查。真实研究运行需要可用的 SiliconFlow API Key、支持工具调用的模型，以及访问 DDGS 搜索和目标网页的网络。

## Roadmap

| 阶段 | 范围 |
| --- | --- |
| Phase 1 | Supervisor + Researcher + Search Tool |
| Phase 2 | Parallel Research / SubAgents |
| Phase 3 | Source + Evidence + Claim + Evidence Store |
| Phase 4 | Verifier + Research Gap + Re-Research |
| Phase 5 | Writer + Citation |
| Phase 6 | Checkpoint + Research Project Persistence |
| Phase 7 | Evaluation |
| Phase 8 | UI + Docker + README polishing |
