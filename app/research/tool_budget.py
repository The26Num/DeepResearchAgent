"""Per-task limits for the two public research tools."""

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable

from langchain_core.tools import BaseTool, tool

from app.schemas.task import ResearchTask
from app.research.runtime import ResearchRuntime
from app.tools.fetch_webpage import fetch_webpage
from app.tools.web_search import web_search


_LIMITS = {
    "discovery": (3, 4),
    "analysis": (2, 3),
    "synthesis": (1, 1),
}


@dataclass
class ToolBudget:
    max_search_calls: int
    max_fetch_calls: int
    search_calls: int = 0
    fetch_calls: int = 0
    calls: list[str] = field(default_factory=list)
    report: Callable[[str], None] | None = None
    runtime: ResearchRuntime | None = None

    @classmethod
    def for_task(cls, task: ResearchTask) -> "ToolBudget":
        return cls(*_LIMITS[task.task_type])


def budgeted_tools(
    budget: ToolBudget,
    search_tool: BaseTool = web_search,
    fetch_tool: BaseTool = fetch_webpage,
) -> list[BaseTool]:
    """Wrap tools in closures scoped to one research invocation."""

    def with_remaining(raw: str, name: str, used: int, maximum: int) -> str:
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return raw
        if isinstance(payload, dict):
            payload["tool_budget"] = f"{name}: {used}/{maximum} used, {maximum - used} remaining."
            return json.dumps(payload, ensure_ascii=False)
        return raw

    def execute(selected: BaseTool, arguments: dict, name: str) -> str:
        operation = budget.runtime.operation(name) if budget.runtime else nullcontext()
        with operation as outcome:
            raw = selected.invoke(arguments)
            if budget.report:
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        summary = "invalid result format"
                        if outcome is not None:
                            outcome["status"] = "failed (invalid result format)"
                    elif "error" in payload:
                        summary = "failed: " + str(payload["error"])[:300]
                        if outcome is not None:
                            outcome["status"] = "failed (tool error)"
                    elif name == "web_search":
                        summary = f"results={len(payload.get('results', []))}"
                    else:
                        summary = f"chars={len(payload.get('text', ''))}"
                except (TypeError, ValueError):
                    summary = "invalid result format"
                    if outcome is not None:
                        outcome["status"] = "failed (invalid result format)"
                budget.report(f"{name} returned {summary}")
            return raw

    @tool("web_search")
    def limited_search(query: str, max_results: int = 5) -> str:
        """Find current external research sources, titles, URLs and short snippets."""
        if budget.runtime:
            budget.runtime.remaining()
        if budget.search_calls >= budget.max_search_calls:
            if budget.report:
                budget.report("web_search budget exceeded")
            return json.dumps({"error": "WEB_SEARCH BUDGET EXHAUSTED. Tool budget exceeded for this research task. Do NOT call web_search again. Use existing search results and sources to finish the current memo."})
        budget.search_calls += 1
        budget.calls.append("web_search")
        if budget.report:
            budget.report(f"web_search {budget.search_calls}/{budget.max_search_calls} query={query[:160]!r}")
        raw = execute(search_tool, {"query": query, "max_results": max_results}, "web_search")
        return with_remaining(raw, "web_search", budget.search_calls, budget.max_search_calls)

    @tool("fetch_webpage")
    def limited_fetch(url: str, max_chars: int = 5000) -> str:
        """Read one important HTML source page, within this task's fetch budget."""
        if budget.runtime:
            budget.runtime.remaining()
        if budget.fetch_calls >= budget.max_fetch_calls:
            if budget.report:
                budget.report("fetch_webpage budget exceeded")
            return json.dumps({"url": url, "error": "FETCH_WEBPAGE BUDGET EXHAUSTED. Tool budget exceeded for this research task. Do NOT call fetch_webpage again. Use information already collected; use another tool only if its budget remains, otherwise finish the current memo."})
        budget.fetch_calls += 1
        budget.calls.append("fetch_webpage")
        if budget.report:
            budget.report(f"fetch_webpage {budget.fetch_calls}/{budget.max_fetch_calls} url={url}")
        raw = execute(fetch_tool, {"url": url, "max_chars": max_chars}, "fetch_webpage")
        return with_remaining(raw, "fetch_webpage", budget.fetch_calls, budget.max_fetch_calls)

    return [limited_search, limited_fetch]
