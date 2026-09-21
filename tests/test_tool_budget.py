import json

from langchain_core.tools import tool

from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import ResearchTask


@tool
def fake_search(query: str, max_results: int = 5) -> str:
    """Return one fake search result."""
    return json.dumps({"results": [{"url": "https://example.org"}]})


@tool
def fake_fetch(url: str, max_chars: int = 5000) -> str:
    """Return one fake page."""
    return json.dumps({"url": url, "text": "page"})


def task(task_type: str) -> ResearchTask:
    return ResearchTask(id="T1", title="Research", question="What?", description="Investigate", task_type=task_type)


def test_discovery_budget_rejects_excess_calls() -> None:
    budget = ToolBudget.for_task(task("discovery"))
    search, fetch = budgeted_tools(budget, fake_search, fake_fetch)
    for _ in range(3):
        assert "results" in search.invoke({"query": "topic"})
    search_error = search.invoke({"query": "topic"})
    assert "Tool budget exceeded" in search_error
    assert "Do NOT call web_search again" in search_error
    for _ in range(4):
        assert "page" in fetch.invoke({"url": "https://example.org"})
    fetch_error = fetch.invoke({"url": "https://example.org"})
    assert "Tool budget exceeded" in fetch_error
    assert "Do NOT call fetch_webpage again" in fetch_error
    assert budget.search_calls == 3 and budget.fetch_calls == 4


def test_analysis_budget_is_independent_and_resets_for_new_task() -> None:
    first = ToolBudget.for_task(task("analysis"))
    search, fetch = budgeted_tools(first, fake_search, fake_fetch)
    search.invoke({"query": "topic"})
    for _ in range(3):
        fetch.invoke({"url": "https://example.org"})
    assert "Tool budget exceeded" in fetch.invoke({"url": "https://example.org"})
    second = ToolBudget.for_task(task("analysis"))
    _, fresh_fetch = budgeted_tools(second, fake_search, fake_fetch)
    assert "page" in fresh_fetch.invoke({"url": "https://example.org"})
    assert first.fetch_calls == 3 and second.fetch_calls == 1


def test_successful_tool_results_show_remaining_budget() -> None:
    budget = ToolBudget.for_task(task("discovery"))
    search, fetch = budgeted_tools(budget, fake_search, fake_fetch)
    assert json.loads(search.invoke({"query": "topic"}))["tool_budget"] == "web_search: 1/3 used, 2 remaining."
    assert json.loads(fetch.invoke({"url": "https://example.org"}))["tool_budget"] == "fetch_webpage: 1/4 used, 3 remaining."
