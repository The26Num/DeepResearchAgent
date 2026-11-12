from concurrent.futures import ThreadPoolExecutor
import json

import pytest
from langchain.agents.middleware.types import ToolCallRequest
from langchain_core.messages import HumanMessage

from app.config import Settings
from app.research.budget_middleware import ResearchBudgetMiddleware
from app.research.resource_tools import resource_tools
from app.research.task_resources import RunResources, ResearchBudgetExceeded, estimated_tokens
from app.research.reresearch_budget import ReResearchCallBudget, use_call_budget
from app.research.runtime import ResearchRuntime, ResearchModelCallbacks
from app.research.tool_budget import ToolBudget
from app.schemas import ResearchTask


def resource(tmp_path, **values):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), **values)
    task = ResearchTask(id="D", title="D", question="D", description="D")
    return cfg, task, RunResources(cfg, "run").start_task(task)


def test_resume_preserves_tool_model_text_budgets_and_pins_original_limits(tmp_path):
    cfg, task, item = resource(tmp_path, max_research_model_calls=5, max_source_read_calls=1)
    item.reserve_tool("read_source")
    item.reserve_chars(100)
    item.reserve_model("researcher", [HumanMessage(content="task")])
    item.load_skill("academic_review")
    item.close()
    snapshot = item.run.snapshot()
    enlarged = cfg.model_copy(update={"max_research_model_calls": 60, "max_source_read_calls": 50})
    resumed = RunResources(enlarged, "run", snapshot).start_task(task)
    assert resumed.record.attempts == 2 and resumed.record.reserved_source_chars == 100
    assert "academic_review" in resumed.record.active_skills
    with pytest.raises(ResearchBudgetExceeded):
        resumed.reserve_tool("read_source")
    with pytest.raises(ResearchBudgetExceeded):
        resumed.reserve_model("researcher", [HumanMessage(content="another normal request")])
    resumed.reserve_model("memo_recovery", [HumanMessage(content="memo")])
    resumed.reserve_model("extraction", [HumanMessage(content="extract")])
    resumed.reserve_model("extraction", [HumanMessage(content="compact retry")])
    resumed.reserve_model("extraction", [HumanMessage(content="reserved compatibility request")])
    with pytest.raises(ResearchBudgetExceeded):
        resumed.reserve_model("extraction", [HumanMessage(content="fourth attempt")])
    assert sum(resumed.record.model_calls.values()) == 5


def test_failed_and_repeated_tool_attempts_consume_budget(tmp_path):
    _, _, item = resource(tmp_path, max_source_read_calls=2)
    reader = next(tool for tool in resource_tools(ToolBudget(0, 0, resources=item)) if tool.name == "read_source")
    for _ in range(3):
        assert "error" in json.loads(reader.invoke({"snapshot_id": "not-authorized"}))
    assert item.record.calls["read_source"] == 2


def test_restarting_a_closed_attempt_in_the_same_coordinator_keeps_permits(tmp_path):
    _, task, item = resource(tmp_path, max_source_read_calls=1)
    item.reserve_tool("read_source")
    item.close()
    retried = item.run.start_task(task)
    assert retried.record.attempts == 2 and retried.record.attempt_id != item.record.attempt_id
    with pytest.raises(ResearchBudgetExceeded):
        retried.reserve_tool("read_source")


def test_parallel_task_skills_ledgers_and_counters_do_not_leak(tmp_path):
    _, task, first = resource(tmp_path)
    other = first.run.start_task(task.model_copy(update={"id": "D2"}))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(item.load_skill, skill) for item, skill in ((first, "academic_review"), (other, "industry_analysis"))]
        [future.result() for future in futures]
    first.reserve_tool("list_sources")
    assert set(first.record.active_skills) == {"academic_review"}
    assert set(other.record.active_skills) == {"industry_analysis"}
    assert other.record.calls == {} and first.record.calls == {"list_sources": 1}


def test_shared_reresearch_budget_and_task_permits_count_once_each(tmp_path):
    _, _, item = resource(tmp_path)
    shared = ReResearchCallBudget(2)
    with use_call_budget(shared):
        runtime = ResearchRuntime("D", 30, 1)
    runtime.task_resources = item
    callback = ResearchModelCallbacks(runtime)
    for index in range(2):
        callback.on_chat_model_start({}, [[HumanMessage(content="task")]], run_id=str(index))
        callback.on_llm_error(RuntimeError("offline failure"), run_id=str(index))
    assert shared.calls == 2 and item.record.model_calls["researcher"] == 2
    assert shared.by_stage == {"researcher": 2}


def test_unregistered_and_invalid_arguments_cannot_bypass_permits(tmp_path):
    _, _, item = resource(tmp_path)
    budget = ToolBudget(0, 0, resources=item)
    middleware = ResearchBudgetMiddleware(budget)
    def forbidden(request):
        raise AssertionError("Unknown tool must not execute.")
    for index in range(2):
        request = ToolCallRequest(tool_call={"id": str(index), "name": "arbitrary_shell", "args": {}}, tool=None, state={}, runtime=None)
        assert "forbidden" in middleware.wrap_tool_call(request, forbidden).text
    assert item.record.calls["invalid_tool"] == 2
    reader = next(tool for tool in resource_tools(budget) if tool.name == "read_source")
    invalid = ToolCallRequest(tool_call={"id": "bad", "name": "read_source", "args": {}}, tool=reader, state={}, runtime=None)
    assert "Invalid tool arguments" in middleware.wrap_tool_call(invalid, forbidden).text
    assert item.record.calls["read_source"] == 1


def test_closed_attempt_rejects_late_publication_and_further_spending(tmp_path):
    _, _, item = resource(tmp_path)
    item.close()
    for action in (lambda: item.reserve_tool("list_sources"), lambda: item.load_skill("academic_review"),
                   lambda: item.read_source("snap_" + "a" * 64)):
        with pytest.raises(ResearchBudgetExceeded):
            action()
    assert not item.record.snapshots and not item.record.readings and not item.record.calls


def test_source_text_and_model_token_estimates_have_explicit_separate_units(tmp_path):
    _, _, item = resource(tmp_path, max_task_source_chars=100)
    assert item.reserve_chars(80) == 80
    assert item.reserve_chars(80) == 20
    with pytest.raises(ResearchBudgetExceeded):
        item.reserve_chars(1)
    chinese = estimated_tokens([HumanMessage(content="中文")])
    english = estimated_tokens([HumanMessage(content="ab")])
    assert chinese > english
    assert "conservative" in item.record.token_estimation


def test_source_text_exhaustion_hides_fetch_and_refuses_hidden_execution(tmp_path):
    from langchain.agents.middleware import ModelRequest
    _, _, item = resource(tmp_path, max_task_source_chars=100)
    item.reserve_chars(100)
    budget = ToolBudget(3, 4, resources=item)
    request = ModelRequest(model=None, messages=[], tools=[{"name": "fetch_webpage"},
        {"name": "search_sources"}, {"name": "read_source"}, {"name": "list_sources"}, {"name": "web_search"}])
    seen = []
    ResearchBudgetMiddleware(budget).wrap_model_call(request, lambda value: seen.append(value))
    assert {tool["name"] for tool in seen[0].tools} == {"list_sources", "web_search"}
    assert budget.reserve("fetch_webpage") is None
    assert not item.record.calls  # A denied invocation cannot execute or return additional text.


def test_out_of_order_private_snapshots_cannot_roll_back_durable_permits(tmp_path):
    import asyncio
    from app.persistence.models import ResearchRunState
    from app.persistence.manager import CheckpointManager
    from app.persistence.store import ResearchRunStore
    from app.schemas import ResearchPlan
    async def check():
        cfg, task, original = resource(tmp_path)
        db = ResearchRunStore(tmp_path / "runs.db")
        run = db.create_run("Goal")
        manager = CheckpointManager(db, run.run_id)
        state = ResearchRunState(goal="Goal", research_plan=ResearchPlan(goal="Goal", tasks=[task]))
        manager.save("PLAN_CREATED", state)
        coordinator = RunResources(cfg, run.run_id)
        item = coordinator.start_task(task)
        item.reserve_tool("list_sources")
        older = item.snapshot()
        item.reserve_tool("list_sources")
        newer = item.snapshot()
        manager.reserve_resources(run.run_id, coordinator.state.skills, newer)
        manager.reserve_resources(run.run_id, coordinator.state.skills, older)
        restored = db.load_latest_checkpoint(run.run_id).state.resources.tasks[task.id]
        assert restored.calls["list_sources"] == 2 and restored.revision == newer.revision
    asyncio.run(check())


def test_actual_agent_budget_stop_uses_one_reserved_memo_and_bounded_extraction(tmp_path, monkeypatch):
    import re
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from archive_support import OfflineModel, Response, FACT, URL, search
    from app.agents.researcher import Researcher
    from app.research.tool_budget import budgeted_tools
    from app.tools.fetch_webpage import fetch_webpage
    cfg, task, item = resource(tmp_path, max_research_model_calls=6)
    monkeypatch.setattr("app.agents.researcher.get_settings", lambda: cfg)
    monkeypatch.setattr("app.research.evidence_extraction.get_settings", lambda: cfg)
    monkeypatch.setattr("app.agents.researcher.budgeted_tools", lambda budget: budgeted_tools(budget, search, fetch_webpage))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(("<main>Introduction. " + FACT + " Discussion.</main>").encode()))
    class Loop(OfflineModel):
        def _generate(self, messages, **kwargs):
            if "evidence_candidates" in messages[-1].text:
                return super()._generate(messages, **kwargs)
            self._requests.append((list(messages), kwargs))
            if "MEMO FINALIZATION" in messages[-1].text:
                reply = AIMessage(content="## 主要发现\n" + FACT)
            else:
                count = len(self._requests)
                name, arguments = (("web_search", {"query": "fixture"}) if count == 1 else
                                   ("fetch_webpage", {"url": URL}) if count == 2 else ("list_sources", {}))
                reply = AIMessage(content="", tool_calls=[{"id": str(count), "name": name, "args": arguments}])
            return ChatResult(generations=[ChatGeneration(message=reply)])
    model = Loop()
    execution = Researcher(model).research(task, goal="Find the measurement", resources=item)
    assert execution.evidence_bundle.claims
    assert item.record.model_calls == {"researcher": 2, "memo_recovery": 1, "extraction": 1}
    assert len(model.requests) == 4
