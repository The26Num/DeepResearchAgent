"""Phase 4C: bounded consolidation, shared provenance, incremental checking, stop."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agents.relevance import RelevanceAssessor
from app.agents.reresearch_planner import ReResearchPlanner
from app.agents.verifier import ClaimVerifier
from app.config import Settings
from app.research.gap_detection import GapDetector
from app.research.gap_store import GapStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.reresearch import select_gap_groups, update_group_status
from app.research.reresearch_budget import (
    ReResearchBudgetExhausted, ReResearchCallBudget, active_call_budget, use_call_budget,
)
from app.research.runtime import ResearchModelCallbacks, ResearchRuntime
from app.research.tool_budget import ToolBudget
from app.schemas import (
    Claim, ClaimRelevanceResult, CompactResearchResult, Evidence, GapStatus, ResearchExecutionResult,
    ResearchGap, ResearchPlan, ResearchTask, ReResearchPlan, ReResearchTask, Source, TaskEvidenceBundle,
    VerificationResult,
)


def task(key="D1", **kwargs):
    return ResearchTask(id=key, title=f"Dimension {key}", question=f"Research {key}", description=f"Find direct {key} evidence", **kwargs)


def gap(key="D1", kind="RELEVANCE_GAP", **kwargs):
    return ResearchGap(task_id=key, gap_type=kind, claim_id=None if kind == "COVERAGE_GAP" else uuid4().hex,
                       description="Missing answer", missing_information="Find missing scope and limits", **kwargs)


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


class PlannerModel:
    def __init__(self, mutate=lambda value: value):
        self.requests = []
        self.mutate = mutate

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema"}
        self.schema = schema
        return self

    def invoke(self, messages, **kwargs):
        self.requests.append((messages, kwargs))
        payload = json.loads(messages[1].text)
        return self.mutate({"results": [dict(group_id=item["group_id"], title="Focused discovery",
            description="Find direct new evidence addressing the missing dimensions.") for item in reversed(payload["groups"])]})


def test_eleven_gaps_consolidated_and_selected_with_hard_two_task_limit():
    gaps = [gap("D1") for _ in range(4)] + [gap("D1", "COVERAGE_GAP")]
    gaps += [gap("D2", "SUPPORT_GAP") for _ in range(3)]
    gaps += [gap("D3", "SUPPORT_GAP") for _ in range(3)]
    groups = select_gap_groups(gaps, settings())
    assert len(gaps) == 11 and len(groups) == 2
    assert [item.task_id for item in groups] == ["D1", "D2"]
    assert len(groups[0].gap_ids) == 4
    assert set(groups[0].gap_types) == {"COVERAGE_GAP", "RELEVANCE_GAP"}


def test_priority_coverage_then_relevance_then_support_and_skip_resolved():
    gaps = [gap("SUP", "SUPPORT_GAP"), gap("REL"), gap("COV", "COVERAGE_GAP"),
            gap("DONE", "COVERAGE_GAP", status="RESOLVED")]
    assert [item.task_id for item in select_gap_groups(gaps, settings())] == ["COV", "REL"]
    assert select_gap_groups([gaps[-1]], settings()) == []


def test_batch_planner_two_groups_one_call_origins_compact_context_no_internal_ids():
    gaps = [gap(), gap("D2", "SUPPORT_GAP")]
    groups = select_gap_groups(gaps, settings())
    model = PlannerModel()
    planner = ReResearchPlanner(model, settings=settings())
    original = ResearchPlan(goal="Old goal", tasks=[task(), task("D2")])
    budget = ReResearchCallBudget(1)
    with use_call_budget(budget):
        result = planner.create_plan("Full original goal", original, groups, [])
    assert len(model.requests) == planner.last_calls == budget.calls == 1
    assert budget.by_stage == {"planner": 1}
    assert len(result.tasks) == 2 and result.round_id == 1
    assert {item.id for item in result.tasks} == {"RR_D1_1", "RR_D2_1"}
    assert result.selected_gap_ids == [key for group in groups for key in group.gap_ids]
    for item, group in zip(result.tasks, groups):
        assert item.origin_task_id == group.task_id and item.origin_gap_ids == list(group.gap_ids)
        assert item.task_type == "discovery" and item.depends_on == []
    payload = json.loads(model.requests[0][0][1].text)
    assert payload["research_goal"] == "Full original goal"
    assert len(payload["groups"]) == 2
    assert all(key not in model.requests[0][0][1].text for key in [gaps[0].gap_id, "evidence_ids", "memo", "sources"])
    assert model.requests[0][1]["max_tokens"] == 3072
    assert model.requests[0][1]["timeout"] <= 60
    assert len(original.tasks) == 2


@pytest.mark.parametrize("mutate", [
    lambda value: {"results": []},
    lambda value: {"results": [value["results"][0]] * 2},
    lambda value: {"results": [{**value["results"][0], "group_id": "UNKNOWN"}]},
    lambda value: {"results": [{**value["results"][0], "task_type": "synthesis"}]},
    lambda value: {"results": [{**value["results"][0], "title": ""}]},
])
def test_planner_rejects_invalid_or_untrusted_output_without_retry(mutate):
    model = PlannerModel(mutate)
    worker = ReResearchPlanner(model, settings=settings())
    with pytest.raises(ValueError):
        worker.create_plan("Goal", ResearchPlan(goal="Goal", tasks=[task()]), select_gap_groups([gap()], settings()), [])
    assert len(model.requests) == 1


def test_empty_groups_and_oversized_context_make_no_model_call():
    model = PlannerModel()
    worker = ReResearchPlanner(model, settings=settings(max_reresearch_planning_context_chars=500))
    original = ResearchPlan(goal="Goal", tasks=[task()])
    assert worker.create_plan("Goal", original, [], []).tasks == []
    with pytest.raises(ValueError, match="context exceeds"):
        worker.create_plan("G" * 501, original, select_gap_groups([gap()], settings()), [])
    assert not model.requests


def test_planner_rejects_more_groups_than_configured():
    groups = select_gap_groups([gap("D1"), gap("D2"), gap("D3")], settings(max_reresearch_tasks_per_round=3))
    worker = ReResearchPlanner(PlannerModel(), settings=settings())
    with pytest.raises(ValueError, match="exceed"):
        worker.create_plan("Goal", ResearchPlan(goal="Goal", tasks=[task("D1"), task("D2"), task("D3")]), groups, [])


def test_reresearch_tasks_reject_analysis_dependencies_or_ambiguous_origins():
    base = dict(id="RR1", title="Title", description="Description", question="Question", origin_task_id="D1", origin_gap_ids=["gap1"])
    for override in [{"task_type": "analysis"}, {"depends_on": ["D1"]}, {"origin_gap_ids": ["gap1", "gap1"]}]:
        with pytest.raises(ValidationError):
            ReResearchTask(**{**base, **override})
    rr = ReResearchTask(**base)
    with pytest.raises(ValidationError):
        ReResearchPlan(round_id=1, selected_gap_ids=["different"], tasks=[rr])


@pytest.mark.parametrize("override", [{"max_reresearch_rounds": 2}, {"reresearch_max_search_calls": 3},
                                    {"reresearch_max_fetch_calls": 4}, {"max_reresearch_llm_calls": -1}])
def test_config_protects_current_version_round_and_tools(override):
    with pytest.raises(ValidationError):
        settings(**override)


def test_smaller_tool_budget_config_does_not_change_original_discovery(monkeypatch):
    monkeypatch.setattr("app.research.tool_budget.get_settings", lambda: settings(reresearch_max_fetch_calls=3))
    rr = ReResearchTask(**task().model_dump(), origin_task_id="OLD", origin_gap_ids=["gap1"])
    initial, supplemental = ToolBudget.for_task(task()), ToolBudget.for_task(rr)
    assert (initial.max_search_calls, initial.max_fetch_calls) == (3, 4)
    assert (supplemental.max_search_calls, supplemental.max_fetch_calls) == (2, 3)
    assert [supplemental.reserve("web_search") for _ in range(3)] == [(1, 2), (2, 2), None]


@pytest.mark.parametrize("support,relevance,expected", [
    ("SUPPORTED", "ALIGNED", "RESOLVED"), ("PARTIAL", "ALIGNED", "IMPROVED"),
    ("SUPPORTED", "PARTIAL", "IMPROVED"), ("PARTIAL", "PARTIAL", "IMPROVED"),
    ("UNSUPPORTED", "ALIGNED", "OPEN"), ("SUPPORTED", "MISALIGNED", "OPEN"),
])
def test_gap_resolution_rules_keep_ids_and_unselected_gaps(support, relevance, expected):
    store = GapStore()
    first, other = store.add_gap(gap()), store.add_gap(gap("D2"))
    claim = Claim(task_id="RR1", text="New finding", evidence_ids=["ev1"])
    v = VerificationResult(claim_id=claim.claim_id, task_id="RR1", evidence_ids=claim.evidence_ids, status=support, reason="Checked")
    r = ClaimRelevanceResult(claim_id=claim.claim_id, task_id="RR1", status=relevance, reason="Checked")
    assert update_group_status(store, [first.gap_id], [claim], {claim.claim_id: v}, {claim.claim_id: r}) == expected
    assert store.get_gap(first.gap_id).status == expected
    assert store.get_gap(other.gap_id).status == "OPEN" and len(store.all_gaps()) == 2


def test_no_claims_or_missing_decisions_leave_gap_open():
    store = GapStore()
    item = store.add_gap(gap())
    assert update_group_status(store, [item.gap_id], [], {}, {}) == "OPEN"
    assert store.get_gap(item.gap_id).gap_id == item.gap_id


def test_global_budget_is_atomic_and_context_restored_and_closed_workers_blocked():
    budget = ReResearchCallBudget(5)
    def attempt(_):
        try:
            budget.reserve("researcher")
            return True
        except ReResearchBudgetExhausted:
            return False
    with ThreadPoolExecutor(10) as pool:
        assert sum(pool.map(attempt, range(40))) == 5
    assert budget.calls == 5
    assert active_call_budget() is None
    with use_call_budget(budget):
        assert active_call_budget() is budget
    assert active_call_budget() is None
    late = ReResearchCallBudget(10)
    with use_call_budget(late):
        runtime = ResearchRuntime("RR", 5, 1)
    late.close()
    with pytest.raises(ReResearchBudgetExhausted):
        runtime.run(lambda: ResearchModelCallbacks(runtime).on_chat_model_start({}, [], run_id=uuid4()))
    assert late.calls == 0


def test_real_langchain_callback_stops_request_before_model_generation():
    model = FakeListChatModel(responses=["One", "Two"])
    budget = ReResearchCallBudget(1)
    with use_call_budget(budget):
        runtime = ResearchRuntime("RR", 5, 1)
    callback = ResearchModelCallbacks(runtime)
    def work():
        assert model.invoke("First", config={"callbacks": [callback]}).text == "One"
        model.invoke("Second", config={"callbacks": [callback]})
    with pytest.raises(ReResearchBudgetExhausted):
        runtime.run(work)
    assert model.i == 1 and budget.calls == 1


class DecisionModel(PlannerModel):
    def __init__(self, kind, rr_support="SUPPORTED", rr_relevance="ALIGNED"):
        super().__init__()
        self.kind, self.rr_support, self.rr_relevance = kind, rr_support, rr_relevance

    def invoke(self, messages, config=None, **kwargs):
        self.requests.append((messages, kwargs))
        payload = json.loads(messages[1].text)
        decisions = []
        for item in payload["claims"]:
            new = item["text"].startswith("NEW")
            status = (self.rr_support if new else "SUPPORTED") if self.kind == "verify" else (self.rr_relevance if new else "MISALIGNED")
            decisions.append(dict(claim_id=item["claim_id"], status=status, reason="Independent check"))
        return {"results": decisions}


def pipeline(monkeypatch, *, rr_support="SUPPORTED", rr_relevance="ALIGNED", fail=False, empty=False,
             rounds=1, calls=12, concurrency=3, malicious=False):
    cfg = settings(max_reresearch_rounds=rounds, max_reresearch_llm_calls=calls)
    monkeypatch.setattr("app.research.orchestrator.get_settings", lambda: cfg)
    tasks = [task(), task("D2"), task("A", task_type="analysis", depends_on=["D1", "D2"]),
             task("S", task_type="synthesis", depends_on=["A"])]
    original = ResearchPlan(goal="Goal", tasks=tasks)
    barrier = Barrier(2, timeout=3) if concurrency >= 2 and not fail else None
    research_calls = []
    class ResearcherStub:
        def research(self, task, **kwargs):
            research_calls.append(task.id)
            new = isinstance(task, ReResearchTask)
            origin = task.origin_task_id if new else task.id
            budget = active_call_budget()
            if new:
                assert budget is not None
                budget.reserve("researcher")
                if fail:
                    raise RuntimeError("Search failed")
                if barrier:
                    barrier.wait()
                budget.reserve("extraction")
            else:
                assert budget is None
            # Repeated URLs and exact fragments must reuse their canonical records.
            source = Source(task_id=task.id, url="https://example.org/paper", title="Original paper")
            evidence = Evidence(task_id=task.id, source_id=source.source_id, content=f"Original fact {origin}")
            fragments = [evidence]
            if new:
                fragments.append(Evidence(task_id=task.id, source_id=source.source_id, content=f"New fact {origin}"))
            claims = [] if new and empty else [Claim(task_id=task.id, text=("NEW " if new else "OLD ") + origin,
                                                      evidence_ids=[item.evidence_id for item in fragments])]
            bundle = TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=fragments, claims=claims)
            return ResearchExecutionResult(memo="ORIGINAL_MEMO_" + task.id, tools_used=[], evidence_bundle=bundle,
                compact=CompactResearchResult(task_id=task.id, summary="ORIGINAL_COMPACT_" + task.id))
    verifier_model = DecisionModel("verify", rr_support, rr_relevance)
    relevance_model = DecisionModel("relevance", rr_support, rr_relevance)
    planner_model = PlannerModel()
    planner = ReResearchPlanner(planner_model, settings=cfg)
    if malicious:
        real = planner.create_plan
        def corrupt(*args, **kwargs):
            result = real(*args, **kwargs)
            result.tasks[0].id = "D1"
            return result
        planner.create_plan = corrupt
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: original), ResearcherStub(),
        verifier=ClaimVerifier(verifier_model, settings=cfg),
        gap_detector=GapDetector(RelevanceAssessor(relevance_model, settings=cfg)),
        reresearch_planner=planner, max_concurrency=concurrency, write_report=False).run("Goal")
    return result, research_calls, verifier_model, relevance_model, planner_model


def test_parallel_incremental_pipeline_dedup_provenance_original_dag_memos_unchanged(monkeypatch, capsys):
    result, calls, verifier, relevance, planner = pipeline(monkeypatch)
    summary = result.reresearch_summary
    assert calls[:2] == ["D1", "D2"] and calls[2:4] == ["A", "S"]
    assert set(calls[4:]) == {"RR_D1_1", "RR_D2_1"} and len(calls) == 6
    assert len(result.plan.tasks) == 4
    assert summary.rounds == 1 and summary.tasks == summary.selected_groups == 2
    assert summary.llm_calls == 8  # planner=1, research=2, extraction=2, verify=2, relevance=1
    assert summary.calls_by_stage == dict(planner=1, researcher=2, extraction=2, verification=2, relevance=1)
    assert summary.new_sources == 0 and summary.new_evidence == summary.new_claims == 2
    assert summary.verification_calls == 2 and summary.relevance_calls == 1
    assert summary.stop_reason == "round limit reached" and not summary.failures
    assert result.evidence_store.counts == dict(sources=1, evidence=6, claims=6)
    assert len(planner.requests) == 1 and len(verifier.requests) == 6 and len(relevance.requests) == 2
    assert all(json.loads(request[0][1].text)["claims"][0]["text"].startswith("NEW") for request in verifier.requests[4:])
    rr_payload = json.loads(relevance.requests[1][0][1].text)
    assert len(rr_payload["claims"]) == 2 and all(item["text"].startswith("NEW") for item in rr_payload["claims"])
    assert set(rr_payload["tasks"]) == {"RR_D1_1", "RR_D2_1"}
    gaps = result.gap_store.all_gaps()
    assert len(gaps) == summary.initial_gaps == 8
    assert sum(item.status == "RESOLVED" for item in gaps) == 4
    assert sum(item.status == "OPEN" for item in gaps) == 4
    for current in result.plan.tasks:
        assert result.memos[current.id] == "ORIGINAL_MEMO_" + current.id
        assert result.compact_results[current.id].summary == "ORIGINAL_COMPACT_" + current.id
        for claim in result.evidence_store.get_claims_for_task(current.id):
            assert len(claim.evidence_ids) == 1
            assert len(result.verification_store.get_verifications_for_claim(claim.claim_id)) == 1
    for key, execution in result.supplemental_results.items():
        for claim in execution.evidence_bundle.claims:
            for fragment in result.evidence_store.get_evidence_for_claim(claim.claim_id):
                assert result.evidence_store.get_source_for_evidence(fragment.evidence_id).url == "https://example.org/paper"
            old = result.evidence_store.get_evidence(claim.evidence_ids[0])
            assert old.task_id in {"D1", "D2"}
            assert key in result.evidence_store.get_evidence_provenance(old.evidence_id).used_by_tasks
    output = capsys.readouterr().out
    assert "Gap Summary Before Re-Research: open=8" in output
    assert "Gap Summary After Re-Research: resolved=4 improved=0 open=4" in output
    assert "origin_gap_ids=" not in output


@pytest.mark.parametrize("support,relevance,expected", [("PARTIAL", "ALIGNED", "IMPROVED"),
    ("SUPPORTED", "MISALIGNED", "OPEN"), ("UNSUPPORTED", "ALIGNED", "OPEN")])
def test_pipeline_improvement_and_open_statuses(monkeypatch, support, relevance, expected):
    result, *_ = pipeline(monkeypatch, rr_support=support, rr_relevance=relevance)
    for gap_id in result.reresearch_summary.plans[0].selected_gap_ids:
        assert result.gap_store.get_gap(gap_id).status == expected
    assert result.reresearch_summary.rounds == 1


@pytest.mark.parametrize("kwargs", [{"fail": True}, {"empty": True}])
def test_failed_or_empty_supplement_never_retries_or_invalidates_original_run(monkeypatch, kwargs):
    result, calls, verifier, relevance, planner = pipeline(monkeypatch, **kwargs)
    assert len(calls) == 6 and len(planner.requests) == 1
    assert len(verifier.requests) == 4 and len(relevance.requests) == 1
    assert all(item.status == "OPEN" for item in result.gap_store.all_gaps())
    assert result.reresearch_summary.failures
    assert set(result.memos) == {"D1", "D2", "A", "S"}
    if kwargs.get("empty"):
        assert result.reresearch_summary.new_evidence == 2  # Evidence survives even with no claim.


@pytest.mark.parametrize("limit", [0, 1, 3, 5, 8])
def test_hard_call_budget_enforced_across_all_stages_and_no_second_round(monkeypatch, limit):
    result, calls, *_ = pipeline(monkeypatch, calls=limit, concurrency=1)
    summary = result.reresearch_summary
    assert summary.llm_calls <= limit
    assert summary.rounds <= 1 and len(calls) <= 6
    assert summary.stop_reason == "budget exhausted"
    if limit < 8:
        assert all(item.status == "OPEN" for item in result.gap_store.all_gaps())


def test_disabled_round_makes_no_extra_calls_and_original_results_survive(monkeypatch):
    result, calls, verifier, relevance, planner = pipeline(monkeypatch, rounds=0)
    assert len(calls) == 4 and len(verifier.requests) == 4 and len(relevance.requests) == 1
    assert not planner.requests and result.reresearch_summary.rounds == 0


def test_untrusted_planner_cannot_overwrite_original_task_or_schedule_new_dag(monkeypatch):
    result, calls, *_ = pipeline(monkeypatch, malicious=True)
    assert len(calls) == 4 and result.reresearch_summary.stop_reason == "supplemental stage failed"
    assert result.reresearch_summary.rounds == 0


def test_planner_http_native_fallback_both_counted_and_limits_forwarded():
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported", "type": "invalid_request_error"}})
        data = {"results": [{"group_id": "G1", "title": "Targeted research", "description": "Find direct evidence"}]}
        return httpx.Response(200, json={"id": "result", "object": "chat.completion", "created": 0, "model": "stub",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": json.dumps(data)}, "finish_reason": "stop"}]})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = ChatOpenAI(model="stub", api_key="test", base_url="https://example.org/v1", http_client=client, max_retries=0)
        budget = ReResearchCallBudget(2)
        with use_call_budget(budget):
            result = ReResearchPlanner(model, settings=settings()).create_plan("Goal", ResearchPlan(goal="Goal", tasks=[task()]),
                select_gap_groups([gap()], settings()), [])
    assert len(result.tasks) == 1 and budget.calls == 2
    assert len(requests) == 2
    assert all(body["max_completion_tokens"] == 3072 and not body.get("tools") for body in requests)


def test_reresearch_prompt_does_not_hardcode_topic_or_force_proof():
    from pathlib import Path
    prompt = Path("app/prompts/reresearch_planner.md").read_text(encoding="utf-8").lower()
    assert all(topic not in prompt for topic in ["atari", "offline rl", "sparse reward"])
    assert "contradict" in prompt and "missing_information" in prompt


@pytest.mark.parametrize("limit", [3, 4])
def test_existing_deepagent_researcher_and_extractor_share_the_call_budget(monkeypatch, limit):
    from app.agents.researcher import Researcher
    from app.research.tool_budget import budgeted_tools

    @tool
    def search(query: str, max_results: int = 5) -> str:
        """Return a research lead."""
        return json.dumps({"results": [{"url": "https://example.org/paper", "title": "Paper"}]})

    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Read source text."""
        return json.dumps({"url": url, "title": "Paper", "text": "The method addresses the missing research dimension with direct evidence. Experiments describe its limitations."})

    class ToolModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def with_structured_output(self, *args, **kwargs):
            raise NotImplementedError("This fixture supports plain JSON extraction.")

    responses = [
        AIMessage(content="", tool_calls=[{"id": "search1", "name": "web_search", "args": {"query": "missing dimension"}}]),
        AIMessage(content="", tool_calls=[{"id": "fetch1", "name": "fetch_webpage", "args": {"url": "https://example.org/paper"}}]),
        AIMessage(content="## 研究任务\n补充缺失维度\n## 主要发现\n该方法处理缺失维度。\n## 重要来源\nPaper\n## 不确定性与缺失信息\n实验范围待确认。"),
        AIMessage(content=json.dumps({"selected_evidence_indexes": [0], "claims": [
            {"text": "The method addresses the missing dimension.", "supporting_evidence_indexes": [0]}]})),
    ]
    monkeypatch.setattr("app.agents.researcher.budgeted_tools", lambda budget: budgeted_tools(budget, search, fetch))
    monkeypatch.setattr("app.agents.researcher.get_settings", lambda: settings())
    worker = Researcher(ToolModel(responses=responses))
    supplemental = ReResearchTask(**task("RR1").model_dump(), origin_task_id="D1", origin_gap_ids=["gap1"])
    budget = ReResearchCallBudget(limit)
    with use_call_budget(budget):
        if limit == 3:
            with pytest.raises(ReResearchBudgetExhausted):
                worker.research(supplemental, goal="Full goal")
        else:
            result = worker.research(supplemental, goal="Full goal")
            assert len(result.evidence_bundle.claims) == 1
            assert result.tools_used == ["web_search", "fetch_webpage"]
    assert budget.calls == limit
    assert budget.by_stage["researcher"] == 3
    assert budget.by_stage["extraction"] == limit - 3


def test_no_unresolved_gaps_skips_the_entire_supplemental_stage(monkeypatch):
    model = DecisionModel("relevance")
    # Treat the initial claim as fully aligned so there is nothing to supplement.
    model.rr_relevance = "ALIGNED"
    current = task()
    source = Source(task_id=current.id, url="https://example.org/paper", title="Paper")
    evidence = Evidence(task_id=current.id, source_id=source.source_id, content="Direct evidence")
    item = Claim(task_id=current.id, text="NEW initial relevant finding", evidence_ids=[evidence.evidence_id])
    execution = ResearchExecutionResult(memo="Original memo", tools_used=[],
        compact=CompactResearchResult(task_id=current.id, summary="Original"),
        evidence_bundle=TaskEvidenceBundle(task_id=current.id, sources=[source], evidence=[evidence], claims=[item]))
    planner_model = PlannerModel()
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: ResearchPlan(goal="Goal", tasks=[current])),
        SimpleNamespace(research=lambda **kwargs: execution), verifier=ClaimVerifier(DecisionModel("verify"), settings=settings()),
        gap_detector=GapDetector(RelevanceAssessor(model, settings=settings())),
        reresearch_planner=ReResearchPlanner(planner_model, settings=settings()), write_report=False).run("Goal")
    assert result.reresearch_summary.stop_reason == "no unresolved gaps"
    assert result.reresearch_summary.llm_calls == result.reresearch_summary.rounds == 0
    assert not planner_model.requests
