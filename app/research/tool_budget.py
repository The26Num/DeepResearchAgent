"""Per-task limits for the two public research tools."""

import json
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Callable
from threading import Lock

from langchain_core.tools import BaseTool, tool

from app.schemas.task import ResearchTask
from app.schemas.source import normalize_source_url
from app.research.runtime import ResearchRuntime
from app.tools.fetch_webpage import fetch_webpage
from app.tools.web_search import web_search


_LIMITS = {
    "discovery": (3, 4),
    "analysis": (2, 3),
    "synthesis": (0, 0),
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
    readings: list[dict] = field(default_factory=list)
    attempted_urls: list[str] = field(default_factory=list)
    lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def reserve(self, name: str) -> tuple[int, int] | None:
        with self.lock:
            field_name, maximum = (("search_calls", self.max_search_calls) if name == "web_search"
                                   else ("fetch_calls", self.max_fetch_calls))
            used = getattr(self, field_name)
            if used >= maximum:
                return None
            setattr(self, field_name, used + 1)
            self.calls.append(name)
            return used + 1, maximum

    def record_reading(self, raw: str, requested_url: str | None = None) -> None:
        """Keep successful fetch artifacts local; search candidates never enter here."""
        try:
            page = json.loads(raw)
            if not isinstance(page, dict):
                return
            attempts = page.get("attempted_urls", [])
            attempts = attempts if isinstance(attempts, list) else []
            with self.lock:
                for attempted in [requested_url or page.get("requested_url") or page.get("url"), *attempts]:
                    if isinstance(attempted, str) and attempted not in self.attempted_urls:
                        self.attempted_urls.append(attempted)
            if "error" in page:
                return
            text, url = page.get("text"), page.get("url") or requested_url
            if not isinstance(text, str) or not text.strip() or not isinstance(url, str):
                return
            normalize_source_url(url)
            reading = {"url": url, "title": str(page.get("title") or ""),
                       "published_at": str(page.get("published_at") or ""), "text": text[:8000]}
            with self.lock:
                if reading not in self.readings:
                    self.readings.append(reading)
        except (TypeError, ValueError):
            return

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
            with budget.lock:
                used = budget.search_calls if name == "web_search" else budget.fetch_calls
            payload["tool_budget"] = f"{name}: {used}/{maximum} used, {maximum - used} remaining."
            return json.dumps(payload, ensure_ascii=False)
        return raw

    def execute(selected: BaseTool, arguments: dict, name: str) -> str:
        operation = budget.runtime.operation(name) if budget.runtime else nullcontext()
        with operation as outcome:
            raw = selected.invoke(arguments)
            if name == "fetch_webpage":
                budget.record_reading(raw, arguments.get("url"))
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
        reservation = budget.reserve("web_search")
        if reservation is None:
            if budget.report:
                budget.report("web_search budget exceeded")
            return json.dumps({"error": "WEB_SEARCH BUDGET EXHAUSTED. Tool budget exceeded for this research task. Do NOT call web_search again. Use existing search results and sources to finish the current memo."})
        used, maximum = reservation
        if budget.report:
            budget.report(f"web_search {used}/{maximum} query={query[:160]!r}")
        raw = execute(search_tool, {"query": query, "max_results": max_results}, "web_search")
        return with_remaining(raw, "web_search", used, maximum)

    @tool("fetch_webpage")
    def limited_fetch(url: str, max_chars: int = 5000) -> str:
        """Read one important HTML source page, within this task's fetch budget."""
        if budget.runtime:
            budget.runtime.remaining()
        reservation = budget.reserve("fetch_webpage")
        if reservation is None:
            if budget.report:
                budget.report("fetch_webpage budget exceeded")
            return json.dumps({"url": url, "error": "FETCH_WEBPAGE BUDGET EXHAUSTED. Tool budget exceeded for this research task. Do NOT call fetch_webpage again. Use information already collected; use another tool only if its budget remains, otherwise finish the current memo."})
        used, maximum = reservation
        if budget.report:
            budget.report(f"fetch_webpage {used}/{maximum} url={url}")
        raw = execute(fetch_tool, {"url": url, "max_chars": max_chars}, "fetch_webpage")
        return with_remaining(raw, "fetch_webpage", used, maximum)

    return [limited_search, limited_fetch]
