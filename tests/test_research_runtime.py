import importlib
import json
from threading import Event
from time import monotonic
from types import SimpleNamespace
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.agents.researcher import MAX_RESEARCH_INPUT_CHARS, Researcher
from app.config import Settings
from app.research.orchestrator import ResearchOrchestrator
from app.research.runtime import ResearchModelCallbacks, ResearchRuntime, ResearchTimeoutError, bounded_request_timeout
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import CompactResearchResult, ResearchPlan, ResearchTask, SourceSummary


researcher_module = importlib.import_module("app.agents.researcher")


@pytest.fixture(autouse=True)
def offline_settings(monkeypatch):
    monkeypatch.setattr(researcher_module, "get_settings", lambda: Settings(_env_file=None))


def analysis_task():
    return ResearchTask(id="A", title="Methods", question="Compare methods", description="Analyze the source evidence", task_type="analysis")


def context(*groups):
    return {
        f"D{index}": CompactResearchResult(task_id=f"D{index}", summary="Earlier finding", sources=[
            SourceSummary(title=f"Paper {i}", url=url) for i, url in enumerate(group)
        ]) for index, group in enumerate(groups, 1)
    }


def researcher_with(reader, factory):
    researcher = Researcher.__new__(Researcher)
    researcher.system_prompt = "System instructions"
    researcher._source_fetch_tool = reader
    researcher._agent_factory = factory
    return researcher


def test_source_preparation_interleaves_dependencies_and_shares_remaining_budget():
    reads, requests, budgets = [], [], []

    @tool
    def reader(url: str, max_chars: int = 5000) -> str:
        """Return current-task source evidence."""
        reads.append(url)
        return json.dumps({"url": url, "text": "Fresh evidence " + url})

    def factory(budget):
        budgets.append(budget)
        fetch = budgeted_tools(budget, fetch_tool=reader)[1]

        class Agent:
            def invoke(self, request):
                requests.append(request)
                assert budget.fetch_calls == 2
                assert "1 remaining" in request["messages"][0]["content"]
                assert "Fresh evidence https://example.org/b" in request["messages"][0]["content"]
                fetch.invoke({"url": "https://example.org/missing"})
                assert "BUDGET EXHAUSTED" in fetch.invoke({"url": "https://example.org/never"})
                return {"messages": [AIMessage(content="Source-grounded analysis")]}
        return Agent()

    researcher = researcher_with(reader, factory)
    result = researcher.research(analysis_task(), context(
        ["https://example.org/a", "https://example.org/a2"], ["https://example.org/b"],
    ))
    assert reads == ["https://example.org/a", "https://example.org/b", "https://example.org/missing"]
    assert result.tools_used == ["fetch_webpage"] * 3
    assert len(requests) == 1


def test_failed_read_falls_back_and_duplicate_urls_do_not_spend_budget():
    reads, requests = [], []

    @tool
    def reader(url: str, max_chars: int = 5000) -> str:
        """Read a page or return a fetch failure."""
        reads.append(url)
        if url.endswith("blocked"):
            return json.dumps({"url": url, "error": "HTTP 403"})
        return json.dumps({"url": url, "text": "Readable evidence"})

    class Agent:
        def invoke(self, request):
            requests.append(request)
            return {"messages": [AIMessage(content="Analysis with an explicit evidence gap")]}

    researcher = researcher_with(reader, lambda budget: Agent())
    result = researcher.research(analysis_task(), context([
        "https://example.org/blocked", "https://example.org/read", "https://example.org/read#section", "https://example.org/other",
    ]))
    assert reads == ["https://example.org/blocked", "https://example.org/read", "https://example.org/other"]
    assert result.tools_used == ["fetch_webpage"] * 3
    request = requests[0]["messages"][0]["content"]
    assert "failed_readings" in request and "HTTP 403" in request
    assert "Readable evidence" in request


@pytest.mark.parametrize("response", [
    {"error": "PDF content is not supported"}, {"error": "HTTP 403"},
    {"text": "  "}, {"text": "Looks valid", "error": "not accessible"}, [],
])
def test_unreadable_sources_fail_before_model_generation(response):
    calls = []

    @tool
    def reader(url: str, max_chars: int = 5000) -> str:
        """Return an unreadable source result."""
        calls.append(url)
        return json.dumps(response)

    def factory(budget):
        pytest.fail("Unreadable pages must not be passed to a model as source evidence")

    researcher = researcher_with(reader, factory)
    with pytest.raises(RuntimeError, match="No readable source pages for analysis task A"):
        researcher.research(analysis_task(), context([f"https://example.org/{i}" for i in range(6)]))
    assert len(calls) == 3


def test_source_readings_respect_input_limit_and_preserve_current_question():
    requests = []

    @tool
    def reader(url: str, max_chars: int = 5000) -> str:
        """Return an oversized source to exercise the input bound."""
        return json.dumps({"url": url, "text": 'Quoted "evidence" ' * 10000})

    class Agent:
        def invoke(self, request):
            requests.append(request)
            return {"messages": [AIMessage(content="Bounded analysis")]}

    researcher = researcher_with(reader, lambda budget: Agent())
    researcher.system_prompt = "S" * 14500
    task = analysis_task()
    task.question = "UNTOUCHED_CURRENT_QUESTION"
    researcher.research(task, context(["https://example.org/a"], ["https://example.org/b"]))
    request = requests[0]["messages"][0]["content"]
    assert len(request) + len(researcher.system_prompt) <= MAX_RESEARCH_INPUT_CHARS
    assert "UNTOUCHED_CURRENT_QUESTION" in request
    assert "https://example.org/a" in request and "https://example.org/b" in request
    assert "successful_readings" in request


def test_task_deadline_returns_without_joining_blocked_worker_and_prevents_late_tools(capsys):
    release, stopped, called = Event(), Event(), Event()
    runtime = ResearchRuntime("D1", timeout=0.1, progress_interval=0.02)
    budget = ToolBudget(3, 4, runtime=runtime)

    @tool
    def forbidden_search(query: str, max_results: int = 5) -> str:
        """Record any prohibited search after a deadline."""
        called.set()
        return '{"results": []}'

    search = budgeted_tools(budget, search_tool=forbidden_search)[0]

    def work():
        try:
            with runtime.operation("model request"):
                release.wait(3)
                search.invoke({"query": "late call"})
            return "late memo"
        finally:
            stopped.set()

    started = monotonic()
    try:
        with pytest.raises(ResearchTimeoutError, match="D1 timed out.*model request"):
            runtime.run(work)
        assert monotonic() - started < 1
        assert runtime.closed.is_set()
        assert "waiting stage=model request" in capsys.readouterr().out
    finally:
        release.set()
        assert stopped.wait(1)
    assert not called.is_set()
    assert budget.search_calls == 0
    assert "late" not in capsys.readouterr().out


def test_callbacks_report_actual_model_requests_and_reject_requests_after_close(capsys):
    runtime = ResearchRuntime("A", timeout=5, progress_interval=1)
    callbacks = ResearchModelCallbacks(runtime)
    run_id = uuid4()
    callbacks.on_chat_model_start({}, [], run_id=run_id)
    callbacks.on_llm_end({}, run_id=run_id)
    error_run_id = uuid4()
    callbacks.on_chat_model_start({}, [], run_id=error_run_id)
    callbacks.on_llm_error(RuntimeError("do not print provider secrets"), run_id=error_run_id)
    logs = capsys.readouterr().out
    assert "[A] Progress: model request started" in logs
    assert "model request completed elapsed=" in logs
    assert "model request failed (RuntimeError) elapsed=" in logs
    assert "provider secrets" not in logs
    runtime.closed.set()
    with pytest.raises(ResearchTimeoutError):
        callbacks.on_chat_model_start({}, [], run_id=uuid4())


def test_http_timeouts_are_capped_by_task_time_remaining():
    runtime = ResearchRuntime("D1", timeout=0.5, progress_interval=0.1)
    assert bounded_request_timeout((5, 30)) == (5, 30)
    with runtime.activate():
        connect, read = bounded_request_timeout((5, 30))
        assert 0 < connect <= 0.5 and 0 < read <= 0.5
        assert 0 < bounded_request_timeout(15) <= 0.5
    runtime.closed.set()
    with runtime.activate(), pytest.raises(ResearchTimeoutError):
        bounded_request_timeout(15)


def test_tool_failure_and_completion_logs_include_outcome_and_duration(capsys):
    runtime = ResearchRuntime("D1", timeout=5, progress_interval=1)
    budget = ToolBudget(3, 4, runtime=runtime, report=lambda message: runtime.log(message, "Tool progress"))

    @tool
    def failed_search(query: str, max_results: int = 5) -> str:
        """Return an explicit provider failure."""
        return '{"error": "Tavily HTTP 429: rate limit exceeded"}'

    search = budgeted_tools(budget, search_tool=failed_search)[0]
    assert "429" in search.invoke({"query": "topic"})
    logs = capsys.readouterr().out
    assert "web_search started" in logs
    assert "web_search returned failed: Tavily HTTP 429" in logs
    assert "web_search failed (tool error) elapsed=" in logs
    assert "web_search completed" not in logs


def test_parallel_timeout_retains_completed_memo_and_stops_pending_tasks(monkeypatch, capsys):
    monkeypatch.setattr(researcher_module, "get_settings", lambda: SimpleNamespace(
        research_task_timeout_seconds=0.15, research_progress_interval_seconds=0.03,
    ))
    release, stopped = Event(), Event()
    plan = ResearchPlan(goal="Research", tasks=[
        ResearchTask(id="D1", title="One", question="One?", description="Find"),
        ResearchTask(id="D2", title="Two", question="Two?", description="Find"),
        ResearchTask(id="S", title="Summary", question="Compare?", description="Synthesize", task_type="synthesis", depends_on=["D1", "D2"]),
    ])

    class Supervisor:
        def create_plan(self, query):
            return plan

    def factory(budget):
        class Agent:
            def invoke(self, request):
                if "\nID: D1\n" in request["messages"][0]["content"]:
                    try:
                        release.wait(3)
                        budget.runtime.remaining()
                    finally:
                        stopped.set()
                return {"messages": [
                    ToolMessage(content='{"results": [{"url": "https://example.org"}]}', name="web_search", tool_call_id="s"),
                    ToolMessage(content='{"text": "Source text"}', name="fetch_webpage", tool_call_id="f"),
                    AIMessage(content="Completed sibling memo"),
                ]}
        return Agent()

    researcher = researcher_with(None, factory)
    try:
        with pytest.raises(RuntimeError, match="D1: ResearchTimeoutError.*Pending tasks not executed: S"):
            ResearchOrchestrator(Supervisor(), researcher).run("Research")
        assert [task.status for task in plan.tasks] == ["failed", "completed", "pending"]
        output = capsys.readouterr().out
        assert "Completed sibling memo" in output
        assert "[S] 正在执行" not in output
    finally:
        release.set()
        assert stopped.wait(1)


def test_model_has_explicit_timeout_and_retry_configuration(monkeypatch):
    llm_module = importlib.import_module("app.llm")
    settings = Settings(_env_file=None, siliconflow_api_key="test-key", model_name="test-model",
                        model_request_timeout_seconds=45, model_max_retries=1)
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    model = llm_module.create_llm()
    assert model.request_timeout == 45
    assert model.client._client.timeout == 45
    assert model.client._client.max_retries == 1


def test_native_deep_agent_uses_prefetched_evidence_and_model_callbacks(capsys):
    observed = []

    class AnalysisModel(BaseChatModel):
        @property
        def _llm_type(self):
            return "offline-analysis-test"

        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            observed.extend(messages)
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="## 主要发现\n基于新读取正文的分析。"))])

    @tool
    def reader(url: str, max_chars: int = 5000) -> str:
        """Read the native agent's evidence offline."""
        return json.dumps({"url": url, "text": "FRESH_NATIVE_SOURCE_EVIDENCE"})

    researcher = Researcher(AnalysisModel())
    researcher._source_fetch_tool = reader
    result = researcher.research(analysis_task(), context(["https://example.org/a"]))
    assert result.tools_used == ["fetch_webpage"]
    assert any("FRESH_NATIVE_SOURCE_EVIDENCE" in message.text for message in observed)
    logs = capsys.readouterr().out
    assert "[A] Progress: model request started" in logs
    assert "[A] Progress: model request completed elapsed=" in logs


@pytest.mark.parametrize("field,value", [
    ("model_request_timeout_seconds", 0), ("model_max_retries", -1),
    ("research_task_timeout_seconds", -1), ("research_progress_interval_seconds", 0),
    ("research_task_timeout_seconds", float("inf")),
])
def test_invalid_timeout_settings_are_rejected(field, value):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **{field: value})
