"""Empty final messages and combined evidence overflow must have bounded recovery."""

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.researcher import Researcher, MAX_RESEARCH_INPUT_CHARS
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import prepare_extraction_payload, build_evidence_bundle
from app.research.evidence_store import EvidenceStore
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.schemas import ResearchTask, Source, Evidence, Claim, CompactResearchResult
from app.schemas.evidence_extraction import ExtractedClaim, ExtractedEvidenceResult


def task(kind="discovery"):
    return ResearchTask(id="D1", title="Research", question="CURRENT_QUESTION", description="Read sources", task_type=kind)


def page():
    return {"url": "https://example.org/paper", "title": "Paper", "text": "Introduction. Source method uses subgoals. Discussion."}


def memo():
    return "## 研究任务\nCURRENT_QUESTION\n## 主要发现\n来源方法使用子目标。\n## 重要来源\nhttps://example.org/paper\n## 不确定性与缺失信息\n尚无性能数据。"


def researcher_with(last, response=None):
    calls, budgets = [], []
    value = page()

    class Agent:
        def invoke(self, request):
            calls.append(("agent", request))
            return {"messages": [
                ToolMessage(content=json.dumps({"results": [{"url": value["url"]}]}), name="web_search", tool_call_id="s"),
                ToolMessage(content=json.dumps(value), name="fetch_webpage", tool_call_id="f"), last,
            ]}

    def factory(budget):
        budgets.append(budget)
        return Agent()

    class Model:
        def invoke(self, messages, **kwargs):
            calls.append(("model", messages, kwargs))
            return response if response is not None else AIMessage(content=memo())

    def extract(payload, runtime):
        index = next(item["evidence_index"] for item in payload["candidates"]
                     if item["content"] == "Source method uses subgoals.")
        return ExtractedEvidenceResult(claims=[ExtractedClaim(text="方法使用子目标", supporting_evidence_indexes=[index])])

    researcher = Researcher(Model(), factory, evidence_extractor=SimpleNamespace(extract=extract))
    return researcher, calls, budgets


@pytest.mark.parametrize("last", [
    AIMessage(content="", response_metadata={"finish_reason": "stop"}),
    AIMessage(content="Planning text", tool_calls=[{"id": "unfinished", "name": "web_search", "args": {}}]),
    ToolMessage(content="This is a tool output, not a memo", tool_call_id="unfinished"),
])
def test_empty_or_nonfinal_message_recovers_from_actual_sources_once(last, capsys):
    researcher, calls, budgets = researcher_with(last)
    result = researcher.research(task())
    assert "- 方法使用子目标" in result.memo
    assert "Source method uses subgoals." in result.memo
    assert "### Paper\n- URL：https://example.org/paper" in result.memo
    assert result.compact.key_findings == ["方法使用子目标"]
    assert [call[0] for call in calls] == ["agent", "model"]
    text = calls[1][1][1].text
    assert "Source method uses subgoals." in text and "CURRENT_QUESTION" in text
    assert "MEMO FINALIZATION" in text
    assert "tools" not in calls[1][2] and 0 < calls[1][2]["timeout"] <= 60
    assert budgets[0].search_calls == budgets[0].fetch_calls == 0  # Factory returns artifacts, no repeated executions.
    bundle = result.evidence_bundle
    assert bundle and len(bundle.claims) == len(bundle.evidence) == len(bundle.sources) == 1
    assert bundle.evidence[0].content == "Source method uses subgoals."
    logs = capsys.readouterr().out
    assert "final memo unavailable:" in logs and "memo recovery 1/1" in logs


def test_valid_memo_never_triggers_extra_model_call():
    researcher, calls, _ = researcher_with(AIMessage(content=memo()))
    researcher.research(task())
    assert [call[0] for call in calls] == ["agent"]


@pytest.mark.parametrize("response", [AIMessage(content=""), AIMessage(content=" "),
    AIMessage(content="Plan", tool_calls=[{"id": "x", "name": "web_search", "args": {}}])])
def test_recovery_stops_after_one_invalid_final_response(response):
    researcher, calls, _ = researcher_with(AIMessage(content=""), response)
    with pytest.raises(RuntimeError, match="after one recovery attempt"):
        researcher.research(task())
    assert [call[0] for call in calls] == ["agent", "model"]


def test_recovery_does_not_retry_model_refusal():
    researcher, calls, _ = researcher_with(AIMessage(content="", additional_kwargs={"refusal": "Refused"}))
    with pytest.raises(RuntimeError, match="refused"):
        researcher.research(task())
    assert [call[0] for call in calls] == ["agent"]


def test_recovery_input_is_bounded_and_keeps_current_task():
    researcher, calls, _ = researcher_with(AIMessage(content=""))
    researcher.system_prompt = "S" * 16000
    from app.research.tool_budget import ToolBudget
    budget = ToolBudget(3, 4)
    for index in range(4):
        budget.record_reading(json.dumps({**page(), "url": f"https://example.org/{index}", "text": "Actual fact. " * 600}))
    researcher._recover_memo(task(), {}, "RESEARCH_GOAL", budget, EvidenceContext(), ResearchRuntime("D1", 300, 15), AIMessage(content=""))
    sent = calls[0][1]
    assert sum(len(message.text) for message in sent) <= MAX_RESEARCH_INPUT_CHARS
    assert "CURRENT_QUESTION" in sent[1].text and "RESEARCH_GOAL" in sent[1].text
    assert all(f"https://example.org/{index}" in sent[1].text for index in range(4))


def test_recovery_cannot_start_after_task_deadline():
    researcher, calls, _ = researcher_with(AIMessage(content=""))
    runtime = ResearchRuntime("D1", 300, 15)
    runtime.closed.set()
    from app.research.tool_budget import ToolBudget
    budget = ToolBudget(3, 4)
    budget.record_reading(json.dumps(page()))
    with pytest.raises(ResearchTimeoutError):
        researcher._recover_memo(task(), {}, None, budget, EvidenceContext(), runtime, AIMessage(content=""))
    assert calls == []


def test_synthesis_recovery_does_not_request_unlinked_free_form_comparisons():
    calls = []

    class Agent:
        def invoke(self, request):
            calls.append("agent")
            if len(calls) == 1:
                return {"messages": [AIMessage(content="", tool_calls=[
                    {"id": "unfinished", "name": "web_search", "args": {}},
                ])]}
            assert all(not message.tool_calls for message in request["messages"] if isinstance(message, AIMessage))
            return {"messages": [AIMessage(content="## 主要发现\n### 共识\n共同发现。\n### 差异\n数据不同。") ]}

    class Model:
        def invoke(self, messages, **kwargs):
            calls.append("model")
            return AIMessage(content="## 主要发现\n概括已有结果。")

    researcher = Researcher(Model(), lambda budget: Agent())
    result = researcher.research(task("synthesis"), {
        "A": CompactResearchResult(task_id="A", summary="Existing finding A"),
        "B": CompactResearchResult(task_id="B", summary="Existing finding B"),
    })
    assert calls == ["agent", "model"]
    assert result.evidence_bundle.claims == [] and result.compact.key_findings == []


def overflow_payload(upstream=None):
    readings = [{"url": f"https://example.org/{index}", "title": f"Paper {index}",
                 "text": "Introduction. " + " ".join(f"Method {index} fact {number}." for number in range(10)) + " Discussion."}
                for index in range(2)]
    payload = prepare_extraction_payload(task(), "Memo", readings, upstream or EvidenceContext())
    indexes = [item["evidence_index"] for item in payload["candidates"]
               if item["origin"] == "fetched" and item["content"].startswith("Method ") and item["content"].count(".") == 1]
    assert len(indexes) == 20
    return payload, indexes


def test_combined_overflow_prioritizes_complete_claims_and_omits_optional_selections(capsys):
    data, indexes = overflow_payload()
    extracted = ExtractedEvidenceResult(selected_evidence_indexes=indexes[8:20], claims=[
        ExtractedClaim(text=f"Claim {index}", supporting_evidence_indexes=indexes[index * 4:index * 4 + 4])
        for index in range(5)
    ])
    logs = []
    bundle = build_evidence_bundle(task(), extracted, data, EvidenceContext(), report=logs.append)
    assert len(bundle.evidence) == 12 and len(bundle.claims) == 3 and len(bundle.sources) == 2
    assert [item.text for item in bundle.claims] == ["Claim 0", "Claim 1", "Claim 2"]
    assert all(len(item.evidence_ids) == 4 for item in bundle.claims)
    assert "claims_omitted=2" in logs[0] and "candidates_omitted=8" in logs[0]
    assert len(extracted.claims) == 5  # Do not mutate model output.
    store = EvidenceStore()
    store.merge(bundle)
    for claim in bundle.claims:
        assert all(store.get_source_for_evidence(item.evidence_id) for item in store.get_evidence_for_claim(claim.claim_id))


def test_capacity_reuses_existing_support_and_allows_upstream_claims_after_overflow():
    store = EvidenceStore()
    source = store.add_source(Source(task_id="D0", title="Previous", url="https://example.org/previous"))
    prior = store.add_evidence(Evidence(task_id="D0", source_id=source.source_id, content="An upstream source fact."))
    previous_claim = store.add_claim(Claim(task_id="D0", text="Previous finding", evidence_ids=[prior.evidence_id]))
    upstream = EvidenceContext((source,), (prior,), (previous_claim,))
    data, indexes = overflow_payload(upstream)
    upstream_index = next(item["evidence_index"] for item in data["candidates"] if item["origin"] == "upstream")
    extracted = ExtractedEvidenceResult(claims=[
        ExtractedClaim(text="Priority", supporting_evidence_indexes=indexes[:12]),
        ExtractedClaim(text="Overflow", supporting_evidence_indexes=[indexes[12], upstream_index]),
        ExtractedClaim(text="Reusable", supporting_evidence_indexes=[indexes[0], upstream_index]),
        ExtractedClaim(text="Upstream only", supporting_evidence_indexes=[upstream_index]),
    ])
    bundle = store.merge(build_evidence_bundle(task(), extracted, data, upstream))
    assert len(bundle.evidence) == 12 and [claim.text for claim in bundle.claims] == ["Priority", "Reusable", "Upstream only"]
    assert bundle.claims[-1].evidence_ids == [prior.evidence_id]
    assert len(bundle.claims[1].evidence_ids) == 2


@pytest.mark.parametrize("invalid", ["index", "content"])
def test_budget_filter_never_hides_invalid_provenance(invalid):
    data, indexes = overflow_payload()
    last = indexes[12]
    if invalid == "index":
        last = len(data["candidates"]) + 1
    else:
        data["candidates"][last]["content"] = "Invented fragment"
    extracted = ExtractedEvidenceResult(claims=[
        ExtractedClaim(text="First", supporting_evidence_indexes=indexes[:12]),
        ExtractedClaim(text="Omitted", supporting_evidence_indexes=[last]),
    ])
    if invalid == "index":
        bundle = build_evidence_bundle(task(), extracted, data, EvidenceContext())
        assert [claim.text for claim in bundle.claims] == ["First"] and len(bundle.evidence) == 12
    else:
        with pytest.raises(ValueError, match="original source fragment"):
            build_evidence_bundle(task(), extracted, data, EvidenceContext())
