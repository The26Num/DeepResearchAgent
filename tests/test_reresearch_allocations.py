"""Finite supplemental budgets must leave room to publish and audit evidence."""

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from app.agents.researcher import Researcher
from app.config import Settings
from app.persistence.store import ResearchRunStore
from app.research.reresearch_budget import (ReResearchCallBudget, ReResearchBudgetExhausted,
    ReResearchTaskBudgetExhausted, active_call_budget, use_call_budget)
from app.research.runtime import ResearchRuntime, ResearchModelCallbacks
from app.research.task_resources import RunResources
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import ResearchTask
from app.schemas.reresearch import ReResearchPlan, ReResearchTask
from archive_support import FACT, URL, OfflineModel, Response, pipeline


def allocated():
    budget = ReResearchCallBudget(12)
    budget.reserve("planner")
    budget.configure_tasks(["RR1", "RR2"])
    return budget


def test_each_worker_stops_research_with_extraction_and_audits_still_available():
    budget = allocated()
    for key in ("RR1", "RR2"):
        for _ in range(3):
            budget.reserve("researcher", task_id=key)
        with pytest.raises(ReResearchTaskBudgetExhausted):
            budget.reserve("researcher", task_id=key)
    assert budget.calls == 7 and not budget.exhausted
    for key in ("RR1", "RR2"):
        budget.reserve("extraction", task_id=key)
        budget.reserve("verification", task_id=key)
    budget.reserve("relevance")
    assert budget.calls == 12 and budget.exhausted
    assert budget.by_stage == {"planner": 1, "researcher": 6, "extraction": 2, "verification": 2, "relevance": 1}


def test_parallel_workers_cannot_steal_another_tasks_stage_permits():
    budget = allocated()
    def attempt(key):
        try:
            budget.reserve("researcher", task_id=key)
            return True
        except ReResearchTaskBudgetExhausted:
            return False
    with ThreadPoolExecutor(max_workers=10) as workers:
        outcomes = list(workers.map(attempt, ["RR1", "RR2"] * 12))
    assert sum(outcomes) == 6 and budget.calls == 7
    budget.reserve("extraction", task_id="RR1")
    with pytest.raises(ReResearchTaskBudgetExhausted):
        budget.reserve("extraction", task_id="RR1")
    budget.reserve("extraction", task_id="RR2")


def test_allocation_and_usage_restore_without_resetting_or_adding_budget():
    budget = allocated()
    budget.reserve("researcher", task_id="RR1")
    restored = ReResearchCallBudget.restore(budget.snapshot())
    restored.configure_tasks(["RR1", "RR2"])
    assert restored.snapshot() == budget.snapshot()
    for _ in range(2):
        restored.reserve("researcher", task_id="RR1")
    with pytest.raises(ReResearchTaskBudgetExhausted):
        restored.reserve("researcher", task_id="RR1")
    malformed = restored.snapshot()
    malformed["allocation"]["task_calls"]["RR1"]["researcher"] += 1
    with pytest.raises(ValueError):
        ReResearchCallBudget.restore(malformed)


def test_shared_denial_does_not_charge_a_task_for_a_request_never_started(tmp_path):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    item = RunResources(cfg, "run").start_task(ResearchTask(id="RR1", title="Find", question="Find", description="Find"))
    shared = allocated()
    with use_call_budget(shared):
        runtime = ResearchRuntime("RR1", 30, 1)
    runtime.task_resources = item
    callback = ResearchModelCallbacks(runtime)
    for index in range(3):
        callback.on_chat_model_start({}, [[HumanMessage(content="task")]], run_id=str(index))
        callback.on_llm_error(RuntimeError("offline response failure"), run_id=str(index))
    with pytest.raises(ReResearchTaskBudgetExhausted):
        callback.on_chat_model_start({}, [[HumanMessage(content="fourth")]], run_id="rejected")
    assert item.record.model_calls == {"researcher": 3} and shared.by_stage["researcher"] == 3


def test_unfunded_round_does_not_launch_workers_or_enlarge_the_limit():
    shared = ReResearchCallBudget(3)
    shared.reserve("planner")
    with pytest.raises(ReResearchBudgetExhausted, match="cannot cover"):
        shared.configure_tasks(["RR1", "RR2"])
    assert shared.calls == 1 and shared.maximum == 3


def test_expected_research_stop_occurs_before_model_callback_dispatch():
    from langchain.agents.middleware import ModelRequest
    from app.research.budget_middleware import ResearchBudgetMiddleware
    shared = allocated()
    for _ in range(3):
        shared.reserve("researcher", task_id="RR1")
    with use_call_budget(shared):
        runtime = ResearchRuntime("RR1", 30, 1)
    middleware = ResearchBudgetMiddleware(ToolBudget(2, 2, runtime=runtime))
    with pytest.raises(ReResearchTaskBudgetExhausted, match="continue with reserved"):
        middleware.wrap_model_call(ModelRequest(model=None, messages=[], tools=[]), lambda request: pytest.fail("No provider/callback dispatch"))
    assert shared.calls == 4 and shared.remaining_for("extraction", "RR1") == 1


def test_navigation_leads_survive_resume_but_are_not_readable_evidence(tmp_path):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    task = ResearchTask(id="D", title="Find", question="Find", description="Find")
    item = RunResources(cfg, "run").start_task(task)
    budget = ToolBudget(2, 2, resources=item)
    budget.record_search(json.dumps({"results": [{"url": URL, "snippet": "Unverified navigation"},
        {"url": "https://example.org/private?token=secret"}, {"url": "file:///private"}]}))
    item.close()
    resumed = RunResources(cfg, "run", item.run.snapshot()).start_task(task)
    assert resumed.record.search_urls == [URL]
    assert not resumed.record.snapshots and not resumed.record.readings
    assert "Unverified navigation" not in resumed.run.snapshot().model_dump_json()


def test_direct_supplemental_finalization_keeps_the_existing_refusal_guard(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    monkeypatch.setattr("app.agents.researcher.get_settings", lambda: cfg)
    task = ReResearchTask(id="RR1", title="Find evidence", question="Find the measurement", description="Find", origin_task_id="D", origin_gap_ids=["gap_fixture"])
    item = RunResources(cfg, "run").start_task(task)
    ref = item.run.archive.put(requested_url=URL, url=URL, title="Study", raw=FACT.encode(), text=FACT, task_id=task.id)
    item.remember(ref)
    item.read_source(ref.snapshot_id)
    item.reserve_tool("web_search")
    class RefusedAgent:
        def invoke(self, request):
            return {"messages": [AIMessage(content="", response_metadata={"finish_reason": "content_filter"})]}
    shared = allocated()
    model = OfflineModel()
    with use_call_budget(shared), pytest.raises(RuntimeError, match="memo was refused"):
        Researcher(model, agent_factory=lambda budget: RefusedAgent()).research(task, resources=item)
    assert not model.requests and shared.calls == 1


@pytest.mark.parametrize("json_provider", [True, False])
def test_two_real_researcher_loops_complete_supplemental_extraction_and_audits_within_12(tmp_path, monkeypatch, json_provider):
    rr_url = "https://example.org/supplement"
    cfg = Settings(_env_file=None, research_db_path=str(tmp_path / "runs.db"), source_archive_path=str(tmp_path / "archive"),
                   enforce_task_estimated_token_budget=False)
    monkeypatch.setattr("requests.get", lambda url, **kwargs: Response(
        ("<main>Introduction. " + FACT + " Study limitations remain explicit.</main>").encode(), url=rr_url) if url == rr_url else Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, _ = pipeline(db, cfg, monkeypatch)
    @tool
    def search(query: str, max_results: int = 5) -> str:
        """Return a fixed source lead."""
        return json.dumps({"results": [{"url": rr_url if "supplement" in query else URL, "title": "Original source"}]})
    from app.tools.fetch_webpage import fetch_webpage
    monkeypatch.setattr("app.agents.researcher.budgeted_tools", lambda budget, **kwargs:
                        budgeted_tools(budget, search, kwargs.get("fetch_tool", fetch_webpage)))
    class LoopModel(OfflineModel):
        openai_api_base: str = "https://api.deepseek.com"
        model_name: str = "deepseek-flash"
        def _generate(self, messages, **kwargs):
            task_id = next((match.group(1) for message in messages if (match := re.search(r"\nID: ([^\n]+)", message.text))), "")
            if not task_id.startswith("RR_"):
                return super()._generate(messages, **kwargs)
            self._requests.append((list(messages), kwargs))
            seen = {message.name for message in messages if isinstance(message, ToolMessage)}
            if "load_research_skill" not in seen:
                name, args = "load_research_skill", {"skill_id": "academic_review"}
            elif "web_search" not in seen:
                name, args = "web_search", {"query": "targeted supplement"}
            elif "fetch_webpage" not in seen:
                name, args = "fetch_webpage", {"url": rr_url}
            else:
                name, args = "list_sources", {}  # Must be denied before a fourth model invocation.
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="", tool_calls=[{"id": str(len(self._requests)), "name": name, "args": args}]))])
    class GoalModel(OfflineModel):
        def _generate(self, messages, **kwargs):
            data = json.loads(messages[-1].text)
            status = "MISALIGNED" if not self._requests else "ALIGNED"
            self._requests.append((list(messages), kwargs))
            result = {"results": [{"claim_id": item["claim_id"], "status": status, "reason": "Fixed offline gap scenario."} for item in data["claims"]]}
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content=json.dumps(result)))])
    worker.researcher = Researcher(LoopModel(openai_api_base="https://api.deepseek.com" if json_provider else "https://example.org/v1",
                                            model_name="deepseek-flash" if json_provider else "offline-native"))
    worker.gap_detector.assessor.model = GoalModel(role="relevance")
    class Planner:
        def create_plan(self, query, plan, groups, claims):
            active_call_budget().reserve("planner")
            return ReResearchPlan(round_id=1, selected_gap_ids=[key for group in groups for key in group.gap_ids], tasks=[
                ReResearchTask(id=f"RR_{group.task_id}_1", title="Find TAIL_METRIC", question="Find the measurement",
                    description="Retrieve direct original evidence", origin_task_id=group.task_id, origin_gap_ids=list(group.gap_ids))
                for group in groups])
    worker.reresearch_planner = Planner()
    result = worker.run("Locate numerical evidence and its limitations")
    summary = result.reresearch_summary
    assert summary.tasks == 2 and not summary.failures
    normal = 3 if json_provider else 2
    assert summary.llm_calls == 6 + 2 * normal
    assert summary.calls_by_stage == {"planner": 1, "researcher": 2 * normal, "extraction": 2, "verification": 2, "relevance": 1}
    assert summary.new_claims == 2 and summary.verification_calls == 2 and summary.relevance_calls == 1
    state = db.load_latest_checkpoint(result.run_id).state
    for task in summary.plans[0].tasks:
        assert task.status == "completed"
        assert state.resources.tasks[task.id].model_calls == {"researcher": normal, "extraction": 1}
    assert db.get_run(result.run_id).status == "COMPLETED" and result.rendered_report.references
