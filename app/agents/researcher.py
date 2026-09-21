import json
import re
from datetime import date
from pathlib import Path
from typing import Callable

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.middleware.filesystem import FilesystemMiddleware
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, ToolMessage

from app.research.compact import compact_from_memo
from app.research.context import build_dependency_context
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas.research_result import CompactResearchResult, ResearchExecutionResult
from app.schemas.task import ResearchTask
from app.research.source_dedup import deduplicate_memo_sources, retain_memo_sections


_PROMPT = Path(__file__).resolve().parents[1] / "prompts" / "researcher.md"
_URL = re.compile(r"https?://[^\s)>]+")
MAX_RESEARCH_INPUT_CHARS = 20000
MAX_AGENT_STEPS = 40


class Researcher:
    def __init__(self, model: BaseChatModel, agent_factory: Callable[[ToolBudget], object] | None = None) -> None:
        # DeepAgents adds a general-purpose subagent by default. Phase 1 has
        # neither subagents nor filesystem tools.
        register_harness_profile(
            "openai",
            HarnessProfile(
                general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
                excluded_tools={"read_file"},
            ),
        )
        self.model = model
        self.system_prompt = _PROMPT.read_text(encoding="utf-8")
        self._agent_factory = agent_factory or self._create_agent

    def _create_agent(self, budget: ToolBudget):
        return create_deep_agent(
            model=self.model,
            tools=budgeted_tools(budget),
            system_prompt=self.system_prompt,
            middleware=[FilesystemMiddleware(tools=["read_file"])],
            subagents=[],
            checkpointer=None,
            store=None,
        ).with_config({"recursion_limit": MAX_AGENT_STEPS})

    def build_request(
        self, task: ResearchTask, context: dict[str, CompactResearchResult] | None = None, goal: str | None = None,
    ) -> tuple[str, str]:
        """Place bounded dependency context before the complete current task."""
        context = context or {}

        def compose(previous: str) -> str:
            request_parts = []
            if goal:
                request_parts.append("OVERALL RESEARCH GOAL\n---------------------\n" + goal)
            if previous:
                request_parts.append("RELEVANT PREVIOUS RESEARCH\n--------------------------\n" + previous)
            request_parts.append(
                "CURRENT TASK\n============\n"
                f"Current date: {date.today().isoformat()}\nID: {task.id}\nTask type: {task.task_type}\n"
                f"Title: {task.title}\nQuestion: {task.question}\nDescription: {task.description}"
            )
            objectives = self._objectives(task, previous)
            request_parts.append("PRIMARY OBJECTIVE\n=================\n" + "\n".join(objectives))
            return "\n\n".join(request_parts)

        previous = build_dependency_context(context)
        request = compose(previous)
        while previous and len(self.system_prompt) + len(request) > MAX_RESEARCH_INPUT_CHARS:
            overflow = len(self.system_prompt) + len(request) - MAX_RESEARCH_INPUT_CHARS
            previous = build_dependency_context(context, max(0, len(previous) - overflow - 100))
            request = compose(previous)
        if context and not previous:
            raise ValueError("Research input budget cannot retain any dependency context; shorten the current task or goal.")
        if len(self.system_prompt) + len(request) > MAX_RESEARCH_INPUT_CHARS:
            raise ValueError("Current task and system prompt exceed the research input character limit.")
        return request, previous

    @staticmethod
    def _analysis_needs_reading(task: ResearchTask, previous: str) -> bool:
        context_urls = list(dict.fromkeys(_URL.findall(previous)))
        # Compact context deliberately has no page content, so a source-level
        # analysis must re-read its source leads instead of treating a key point
        # as a full paper or abstract.
        return task.task_type == "analysis" and bool(context_urls)

    @staticmethod
    def _objectives(task: ResearchTask, previous: str) -> list[str]:
        objectives = [
            "Answer CURRENT TASK specifically. Previous research is supporting context only, not your current task. "
            "Do not answer a previous task or simply rewrite its memo. Identify the new reading or analysis this task needs."
        ]
        if task.task_type == "discovery":
            objectives.append(
                "Actively call web_search for new sources. For broad discovery, use at least two meaningfully "
                "different search queries within the tool budget; for a narrow original-source lookup, one may "
                "suffice. Seek independent sources and fetch_webpage on important results."
            )
        elif task.task_type == "analysis":
            objectives.append("Reuse relevant previous findings first, then read source pages with fetch_webpage if the detail is insufficient; search only if needed.")
        else:
            objectives.append(
                "Compare previous results, remove repetitions, and extract agreement, differences, trends, and gaps. "
                "在‘主要发现’下使用‘### 共识’和‘### 差异’，比较具名来源。"
                "If evidence cannot support one comparison, say so under that heading. Search only for a clear gap."
            )
        context_urls = list(dict.fromkeys(_URL.findall(previous)))
        if Researcher._analysis_needs_reading(task, previous):
            objectives.append(
                "Previous research gives source URLs without enough source-level detail for this analysis. "
                "Call fetch_webpage on the relevant URLs before making method or contribution claims: "
                + ", ".join(context_urls[:6])
            )
        objectives.append("Preserve the goal's time range. 除非用户另有要求，只用简体中文撰写本任务研究摘要，论文正式标题及专有名词保留原文。")
        return objectives

    def research(
        self, task: ResearchTask, context: dict[str, CompactResearchResult] | None = None, goal: str | None = None,
    ) -> ResearchExecutionResult:
        request, previous = self.build_request(task, context, goal)
        budget = ToolBudget.for_task(task)
        budget.report = lambda progress: print(f"Tool progress: {progress}", flush=True)
        agent = self._agent_factory(budget)
        context_urls = list(dict.fromkeys(_URL.findall(previous)))
        thin_analysis_context = self._analysis_needs_reading(task, previous)
        result = agent.invoke({"messages": [{"role": "user", "content": request}]})
        messages = result.get("messages", [])
        if not messages:
            raise RuntimeError(f"Researcher returned no messages for task {task.id}.")

        observed_messages = list(messages)
        if task.task_type == "discovery" and not any(
            isinstance(message, ToolMessage) and message.name == "web_search" for message in observed_messages
        ):
            reminder = "This is a discovery task. Call web_search for new sources before finalizing the CURRENT TASK memo."
            messages = agent.invoke({"messages": [*messages, HumanMessage(content=reminder)]}).get("messages", [])
            observed_messages.extend(messages)
            if not messages or not any(isinstance(message, ToolMessage) and message.name == "web_search" for message in observed_messages):
                raise RuntimeError(f"Researcher did not search for new sources for discovery task {task.id}.")

        if thin_analysis_context and not any(
            isinstance(message, ToolMessage) and message.name == "fetch_webpage" for message in observed_messages
        ):
            reminder = (
                "The previous compact result only supplies source leads. Call fetch_webpage on relevant source URLs before "
                "finalizing this analysis, and distinguish unreadable pages from evidence. URLs: " + ", ".join(context_urls[:6])
            )
            messages = agent.invoke({"messages": [*messages, HumanMessage(content=reminder)]}).get("messages", [])
            observed_messages.extend(messages)
            if not messages or not any(isinstance(message, ToolMessage) and message.name == "fetch_webpage" for message in observed_messages):
                raise RuntimeError(f"Researcher did not read source URLs for analysis task {task.id}.")

        search_urls: list[str] = []
        fetched = False
        for message in observed_messages:
            if not isinstance(message, ToolMessage):
                continue
            if message.name == "fetch_webpage":
                fetched = True
            elif message.name == "web_search":
                try:
                    search_result = json.loads(message.content)
                    search_urls.extend(item["url"] for item in search_result.get("results", []) if item.get("url"))
                except (TypeError, ValueError, KeyError):
                    pass
        candidate_urls = search_urls or (context_urls if task.task_type == "discovery" else [])
        if candidate_urls and not fetched:
            reminder = (
                "You have candidate URLs but have not read any source page. Before finalizing this memo, "
                "call fetch_webpage on at least one relevant HTML source, then revise the memo. "
                "If the page cannot be read, say so explicitly. Candidate URLs: " + ", ".join(candidate_urls[:5])
            )
            result = agent.invoke({"messages": [*messages, HumanMessage(content=reminder)]})
            messages = result.get("messages", [])
            observed_messages.extend(messages)
            if not messages or not any(isinstance(message, ToolMessage) and message.name == "fetch_webpage" for message in observed_messages):
                raise RuntimeError(f"Researcher did not read a source page for task {task.id} after a reminder.")

        if not context:
            source_found = False
            tool_errors: list[str] = []
            for message in observed_messages:
                if not isinstance(message, ToolMessage) or message.name not in {"web_search", "fetch_webpage"}:
                    continue
                try:
                    tool_result = json.loads(message.content)
                except (TypeError, json.JSONDecodeError):
                    continue
                if "error" in tool_result:
                    tool_errors.append(tool_result["error"])
                elif message.name == "web_search" and tool_result.get("results"):
                    source_found = True
                elif message.name == "fetch_webpage" and tool_result.get("text"):
                    source_found = True
            if not source_found:
                detail = "; ".join(tool_errors) or "the agent did not retrieve any sources"
                raise RuntimeError(f"Researcher found no accessible sources for task {task.id}: {detail}")
        memo = messages[-1].text.strip()
        if not memo:
            raise RuntimeError(f"Researcher returned an empty memo for task {task.id}.")
        if task.task_type == "synthesis" and context and len(context) > 1:
            findings_match = re.search(r"(?is)^## (?:主要发现|Key Findings)\s*(.*?)(?=^## |\Z)", memo, re.MULTILINE)
            findings = findings_match.group(1) if findings_match else ""
            has_commonality = bool(re.search(r"(?im)^###\s*(?:Shared Findings|共识|共同点)\s*$", findings))
            has_difference = bool(re.search(r"(?im)^###\s*(?:Differences|差异|不同点)\s*$", findings))
            if not (has_commonality and has_difference):
                reminder = (
                    "请修改当前综合研究摘要。在‘## 主要发现’下使用‘### 共识’和‘### 差异’，"
                    "分别依据前序摘要比较至少两个具名来源。若证据不足，请在对应小节明确说明，"
                    "不要编造比较。在‘## 不确定性与缺失信息’中指出具体缺口。"
                    "不要把论文逐篇罗列当成趋势，不要加入泛泛的未来工作建议。"
                    "仅保留要求的四个中文一级标题。"
                )
                messages = agent.invoke({"messages": [*messages, HumanMessage(content=reminder)]}).get("messages", [])
                observed_messages.extend(messages)
                if not messages or not messages[-1].text.strip():
                    raise RuntimeError(f"Researcher returned no revised synthesis memo for task {task.id}.")
                memo = messages[-1].text.strip()
        memo = deduplicate_memo_sources(retain_memo_sections(memo))
        return ResearchExecutionResult(memo=memo, compact=compact_from_memo(task.id, memo), tools_used=budget.calls)
