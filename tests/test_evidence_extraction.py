"""Phase 3B source selection, cross-task provenance, and parallel integration."""

import json
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from openai import BadRequestError, LengthFinishReasonError
from pydantic import ValidationError

from app.agents.researcher import MAX_RESEARCH_INPUT_CHARS, Researcher
from app.config import Settings
from app.research.evidence_context import EvidenceContext, select_evidence_context
from app.research.evidence_extraction import EvidenceExtractor, prepare_extraction_payload, build_evidence_bundle, model_payload, source_fragments, MAX_EXTRACTION_INPUT_CHARS
from app.research.evidence_store import EvidenceStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import Claim, Evidence, Source, TaskEvidenceBundle, ResearchPlan, ResearchTask, ResearchExecutionResult, CompactResearchResult
from app.schemas.evidence_extraction import ExtractedClaim, ExtractedEvidenceResult


def task(task_id="D1", kind="discovery", deps=None):
    return ResearchTask(id=task_id, title="Research methods", question="Compare methods and reported results",
                        description="Read sources", task_type=kind, depends_on=deps or [])


def page(url="https://example.org/a"):
    return {"url": url, "title": "Paper A", "published_at": "2025",
            "text": "Introduction. Method A uses ordered subgoals. Method A achieves 82% success. Discussion."}


def payload(readings=None, upstream=None):
    return prepare_extraction_payload(task(), "Memo", readings if readings is not None else [page()], upstream or EvidenceContext())


def index_for(data, content):
    return next(item["evidence_index"] for item in data["candidates"] if item["content"] == content)


def extraction(indexes, text="方法 A 使用有序子目标。"):
    return ExtractedEvidenceResult(selected_evidence_indexes=indexes,
                                   claims=[ExtractedClaim(text=text, supporting_evidence_indexes=indexes)])


def test_single_source_evidence_claim_and_program_generated_ids():
    data = payload()
    index = index_for(data, "Method A uses ordered subgoals.")
    bundle = build_evidence_bundle(task(), extraction([index]), data, EvidenceContext())
    assert len(bundle.sources) == len(bundle.evidence) == len(bundle.claims) == 1
    assert bundle.sources[0].source_id.startswith("src_") and bundle.sources[0].published_at == "2025"
    assert bundle.evidence[0].evidence_id.startswith("ev_") and bundle.claims[0].claim_id.startswith("claim_")
    assert bundle.evidence[0].location.startswith("normalized-text chars ")
    store = EvidenceStore()
    store.merge(bundle)
    evidence = store.get_evidence_for_claim(bundle.claims[0].claim_id)[0]
    assert store.get_source_for_evidence(evidence.evidence_id).url == page()["url"]


def test_single_source_multiple_evidence_and_multi_evidence_claim():
    data = payload()
    indexes = [index_for(data, text) for text in ("Method A uses ordered subgoals.", "Method A achieves 82% success.")]
    bundle = build_evidence_bundle(task(), extraction(indexes), data, EvidenceContext())
    assert len(bundle.sources) == 1 and len(bundle.evidence) == 2
    assert set(bundle.claims[0].evidence_ids) == {item.evidence_id for item in bundle.evidence}


def test_one_complete_mechanism_can_include_adjacent_source_sentences():
    data = payload()
    index = index_for(data, "Method A uses ordered subgoals. Method A achieves 82% success.")
    bundle = build_evidence_bundle(task(), extraction([index]), data, EvidenceContext())
    assert bundle.evidence[0].content == "Method A uses ordered subgoals. Method A achieves 82% success."


@pytest.mark.parametrize("references", [None, []])
def test_extracted_claim_requires_evidence(references):
    with pytest.raises(ValidationError):
        if references is None:
            ExtractedClaim(text="Unsupported")
        else:
            ExtractedClaim(text="Unsupported", supporting_evidence_indexes=references)


def test_formal_claim_requires_evidence():
    with pytest.raises(ValidationError):
        Claim(task_id="D1", text="Unsupported", evidence_ids=[])


@pytest.mark.parametrize("indexes", [[-1], [True], [1.5], ["0"]])
def test_model_reference_indexes_are_strict_nonnegative_integers(indexes):
    with pytest.raises(ValidationError):
        ExtractedClaim(text="Conclusion", supporting_evidence_indexes=indexes)


def test_model_reference_order_and_repetition_do_not_rearrange_source_text():
    data = payload()
    index = index_for(data, "Method A uses ordered subgoals.")
    result = extraction([index, index])
    bundle = build_evidence_bundle(task(), result, data, EvidenceContext())
    assert len(bundle.evidence) == 1 and len(bundle.claims[0].evidence_ids) == 1
    assert ExtractedClaim(text="Conclusion", supporting_evidence_indexes=[2, 1, 2]).supporting_evidence_indexes == [1, 2]


@pytest.mark.parametrize("index", [999, 1000])
def test_unknown_candidate_reference_omits_whole_claim(index):
    bundle = build_evidence_bundle(task(), extraction([index]), payload(), EvidenceContext())
    assert bundle.sources == bundle.evidence == bundle.claims == []


@pytest.mark.parametrize("extra", [{"source_id": "src_model"}, {"evidence_id": "ev_model"},
                                    {"claim_id": "claim_model"}, {"content": "Invented"}, {"verified": True}])
def test_model_cannot_generate_internal_ids_evidence_content_or_verification(extra):
    with pytest.raises(ValidationError):
        ExtractedClaim(text="Conclusion", supporting_evidence_indexes=[0], **extra)


def test_search_results_failed_reads_and_empty_pages_never_become_sources():
    budget = ToolBudget(3, 4)
    for value in [{"results": [{"url": "https://example.org/candidate", "snippet": "Not evidence"}]},
                  {"url": "https://example.org/failed", "text": "Error", "error": "403"},
                  {"url": "https://example.org/empty", "text": " "}]:
        budget.record_reading(json.dumps(value))
    budget.record_reading("invalid JSON")
    budget.record_reading(json.dumps(page()))
    budget.record_reading(json.dumps(page()))
    data = payload(budget.readings)
    assert len(budget.readings) == len(data["fetched_pages"]) == 1
    assert {item["url"] for item in data["candidates"]} == {page()["url"]}


def test_successful_fetch_wrapper_records_final_url_without_agent_messages():
    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Return a successful redirect."""
        return json.dumps(page("https://example.org/final"))
    budget = ToolBudget(0, 1)
    reader = budgeted_tools(budget, fetch_tool=fetch)[1]
    reader.invoke({"url": "https://example.org/redirect"})
    reader.invoke({"url": "https://example.org/not-executed"})
    assert len(budget.readings) == 1 and budget.readings[0]["url"] == "https://example.org/final"


def test_unused_pages_are_not_published_and_url_aliases_are_canonical_locally():
    data = payload([page(), page("https://example.org/a/#method"), page("https://example.org/not-used")])
    assert len(data["fetched_pages"]) == 2
    bundle = build_evidence_bundle(task(), extraction([index_for(data, "Method A uses ordered subgoals.")]), data, EvidenceContext())
    assert [item.url for item in bundle.sources] == [page()["url"]]


def test_candidate_content_is_exact_whitespace_normalized_source_text():
    value = page()
    value["text"] = "Introduction. Method A\nuses ordered\tsubgoals. Discussion."
    data = payload([value])
    index = index_for(data, "Method A uses ordered subgoals.")
    bundle = build_evidence_bundle(task(), extraction([index]), data, EvidenceContext())
    assert bundle.evidence[0].content == "Method A uses ordered subgoals."


def test_whole_page_and_overlong_fragments_are_not_offered():
    assert all(item["content"] != page()["text"] for item in source_fragments(page()["text"]))
    assert source_fragments("x" * 5000) == []
    assert source_fragments("A single short sentence.") == []


def test_mutated_candidate_cannot_invent_source_content():
    data = payload()
    data["candidates"][0]["content"] = "Invented 99% accuracy"
    with pytest.raises(ValueError, match="original source fragment"):
        build_evidence_bundle(task(), extraction([0]), data, EvidenceContext())


class ModelStub:
    def __init__(self, response=None, mode="native"):
        self.response = response or extraction([0])
        self.mode = mode
        self.calls = []

    def with_structured_output(self, schema, method):
        assert method == "json_schema"
        assert schema["properties"]["selected_evidence_indexes"]["items"]["enum"]
        if self.mode == "unsupported":
            raise NotImplementedError("Unsupported native schema")
        return SimpleNamespace(invoke=self.structured_invoke)

    def structured_invoke(self, messages, config, **kwargs):
        self.calls.append((messages, config))
        return self.response

    def invoke(self, messages, config, **kwargs):
        self.calls.append((messages, config))
        return AIMessage(content="```json\n" + self.response.model_dump_json() + "\n```")


@pytest.mark.parametrize("mode", ["native", "unsupported"])
def test_native_structured_output_and_single_json_compatibility_fallback(mode):
    model = ModelStub(mode=mode)
    result = EvidenceExtractor(model).extract(payload(), ResearchRuntime("D1", 5, 1))
    assert len(result.claims) == len(model.calls) == 1
    assert len(model.calls[0][1]["callbacks"]) == 1
    sent = json.loads(model.calls[0][0][1].text)
    assert "evidence_candidates" in sent and "fetched_pages" not in sent
    assert all(not key.startswith("_") for item in sent["evidence_candidates"] for key in item)


def test_invalid_output_fails_without_repair_loop():
    model = ModelStub(response={"claims": [{"text": "No evidence"}]})
    with pytest.raises(ValidationError):
        EvidenceExtractor(model).extract(payload(), ResearchRuntime("D1", 5, 1))
    assert len(model.calls) == 1


def test_provider_account_error_does_not_trigger_schema_fallback():
    model = ModelStub()
    error = BadRequestError("Invalid account", response=httpx.Response(400, request=httpx.Request("POST", "https://example.org")), body={})
    model.with_structured_output = lambda *args, **kwargs: (_ for _ in ()).throw(error)
    with pytest.raises(BadRequestError):
        EvidenceExtractor(model).extract(payload(), ResearchRuntime("D1", 5, 1))
    assert model.calls == []


def offline_run(shared_url=False, recover_and_limit=False, output_errors=False):
    plan = ResearchPlan(goal="Research", tasks=[task("D1"), task("D2"), task("A", "analysis", ["D1", "D2"]), task("S", "synthesis", ["A"])])
    barrier = Barrier(2, timeout=5)
    requests, budgets, attempts = {}, [], {}

    @tool
    def search(query: str, max_results: int = 5) -> str:
        """Return selected and unselected candidates."""
        url = "https://example.org/a" if shared_url or query == "D1" else "https://example.org/b"
        return json.dumps({"results": [{"url": url}, {"url": "https://example.org/not-fetched"}]})

    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Return an actual local fetch artifact."""
        value = page(url)
        if url.endswith("/b"):
            value.update(title="Paper B", text="Introduction. Method B uses density constraints. Method B achieves 74% success. Discussion.")
            if recover_and_limit:
                value["text"] = "Introduction. " + " ".join(f"Method B fact {index}." for index in range(20)) + " Discussion."
        return json.dumps(value)

    def factory(budget):
        budgets.append(budget)
        tools = budgeted_tools(budget, search, fetch)
        class Agent:
            def invoke(self, request):
                text = request["messages"][0]["content"]
                task_id = next(key for key in ("D1", "D2", "A", "S") if f"\nID: {key}\n" in text)
                messages = []
                if task_id.startswith("D"):
                    barrier.wait()
                    raw = tools[0].invoke({"query": task_id})
                    url = json.loads(raw)["results"][0]["url"]
                    messages = [ToolMessage(content=raw, name="web_search", tool_call_id="s"),
                                ToolMessage(content=tools[1].invoke({"url": url}), name="fetch_webpage", tool_call_id="f")]
                else:
                    assert "UPSTREAM EVIDENCE AND CLAIMS" in text
                messages.append(AIMessage(content=f"## 研究任务\n{task_id}\n## 主要发现\n{task_id} 的发现。\n## 重要来源\n### Paper\n- URL：https://example.org/a\n## 不确定性与缺失信息\nNone"))
                if recover_and_limit and task_id == "D1":
                    messages[-1] = AIMessage(content="", response_metadata={"finish_reason": "stop"})
                return {"messages": messages}
        return Agent()

    class Model(ModelStub):
        def invoke(self, messages, config, **kwargs):
            assert recover_and_limit and "MEMO FINALIZATION" in messages[1].text
            assert "Method A uses ordered subgoals." in messages[1].text
            requests["memo_recovery"] = messages[1].text
            return AIMessage(content="## 研究任务\nD1\n## 主要发现\n方法 A 使用子目标。\n## 重要来源\nhttps://example.org/a\n## 不确定性与缺失信息\n缺少对比实验。")

        def structured_invoke(self, messages, config, **kwargs):
            data = json.loads(messages[1].text)
            task_id = data["task"]["task_id"]
            requests[task_id] = data
            attempts[task_id] = attempts.get(task_id, 0) + 1
            if output_errors and task_id == "D1" and attempts[task_id] == 1:
                raise LengthFinishReasonError(completion=SimpleNamespace(usage=None))
            if recover_and_limit and task_id == "D2":
                indexes = [item["evidence_index"] for item in data["evidence_candidates"]
                           if item["content"].startswith("Method B fact ") and item["content"].count(".") == 1]
                return ExtractedEvidenceResult(claims=[
                    ExtractedClaim(text=f"B finding {index}", supporting_evidence_indexes=indexes[index * 4:index * 4 + 4])
                    for index in range(5)
                ])
            if task_id.startswith("D"):
                chosen = next(item for item in data["evidence_candidates"] if item["content"] in {
                    "Method A uses ordered subgoals.", "Method B uses density constraints."})
                if output_errors and task_id == "D2":
                    valid = extraction([chosen["evidence_index"]])
                    valid.selected_evidence_indexes.append(999)
                    valid.claims.append(ExtractedClaim(text="Unknown support", supporting_evidence_indexes=[chosen["evidence_index"], 999]))
                    return valid
                return extraction([chosen["evidence_index"]])
            indexes = [item["evidence_index"] for item in data["evidence_candidates"] if item["origin"] == "upstream"]
            return extraction(indexes[:12], "这些方法采用不同机制。")

    researcher = Researcher(Model(), factory)
    researcher._source_fetch_tool = fetch
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), researcher).run("Research")
    if output_errors:
        assert attempts == {"D1": 2, "D2": 1, "A": 1, "S": 1}
    return result, requests, budgets


def test_parallel_empty_memo_and_evidence_overflow_recover_without_blocking_analysis_and_synthesis(capsys):
    result, requests, budgets = offline_run(recover_and_limit=True)
    assert [item.status for item in result.plan.tasks] == ["completed"] * 4
    assert "memo_recovery" in requests
    assert len(result.evidence_bundles["D2"].evidence) == 12
    assert len(result.evidence_bundles["D2"].claims) == 3
    assert result.evidence_bundles["A"].claims and result.evidence_bundles["S"].claims
    assert budgets[-1].calls == []
    for bundle in result.evidence_bundles.values():
        for claim in bundle.claims:
            for item in result.evidence_store.get_evidence_for_claim(claim.claim_id):
                assert result.evidence_store.get_source_for_evidence(item.evidence_id)
    logs = capsys.readouterr().out
    assert "memo recovery 1/1" in logs and "claims_omitted=2" in logs


def test_parallel_length_recovery_and_unknown_reference_filter_allow_analysis_and_synthesis(capsys):
    result, requests, budgets = offline_run(output_errors=True)
    assert [item.status for item in result.plan.tasks] == ["completed"] * 4
    assert result.evidence_store.counts == {"sources": 2, "evidence": 2, "claims": 4}
    assert [claim.text for claim in result.evidence_bundles["D2"].claims] == ["方法 A 使用有序子目标。"]
    for bundle in result.evidence_bundles.values():
        for claim in bundle.claims:
            assert all(result.evidence_store.get_source_for_evidence(item.evidence_id)
                       for item in result.evidence_store.get_evidence_for_claim(claim.claim_id))
    assert budgets[-1].calls == []
    logs = capsys.readouterr().out
    assert "compact recovery 1/1" in logs and "invalid_indexes=[999]" in logs


@pytest.mark.parametrize("shared_url", [False, True])
def test_parallel_bundles_analysis_multi_source_and_synthesis_reuse(shared_url, monkeypatch, capsys):
    monkeypatch.setenv("SHOW_EVIDENCE_DEBUG", "false")
    result, requests, budgets = offline_run(shared_url)
    assert all(item.status == "completed" for item in result.plan.tasks)
    assert result.evidence_store.counts == {"sources": 1 if shared_url else 2, "evidence": 1 if shared_url else 2, "claims": 4}
    assert set(result.evidence_bundles) == {"D1", "D2", "A", "S"}
    assert len(requests["A"]["upstream_claims"]) == 2
    assert requests["S"]["upstream_claims"][0]["task_id"] == "A"
    assert all(item["origin"] == "upstream" for item in requests["S"]["evidence_candidates"])
    assert budgets[-1].calls == [] and len({id(budget) for budget in budgets}) == 4
    assert result.evidence_bundles["S"].evidence == [] and result.evidence_bundles["S"].sources == []
    for bundle in result.evidence_bundles.values():
        for claim in bundle.claims:
            assert claim.evidence_ids
            for item in result.evidence_store.get_evidence_for_claim(claim.claim_id):
                assert result.evidence_store.get_source_for_evidence(item.evidence_id).url != "https://example.org/not-fetched"
    analysis = result.evidence_store.get_claims_for_task("A")[0]
    linked = result.evidence_store.get_evidence_for_claim(analysis.claim_id)
    assert {item.task_id for item in linked} == ({"D1"} if shared_url else {"D1", "D2"})
    if shared_url:
        assert set(result.evidence_store.get_evidence_provenance(linked[0].evidence_id).used_by_tasks) == {"D1", "D2", "A", "S"}
    assert budgets[-2].calls == []  # Analysis uses the existing source fragments.
    assert result.memos["D1"] and result.evidence_bundles["D1"].claims
    logs = capsys.readouterr().out
    assert "正在执行批次：2 项任务" in logs and "Evidence Store:" in logs
    assert "src_" not in logs and "ev_" not in logs and "claim_" not in logs


def test_debug_output_defaults_off_and_can_show_complete_chains(monkeypatch, capsys):
    assert Settings(_env_file=None).show_evidence_debug is False
    monkeypatch.setenv("SHOW_EVIDENCE_DEBUG", "true")
    offline_run()
    logs = capsys.readouterr().out
    assert "[D1] Claim:" in logs and "  Evidence:" in logs and "  Source:" in logs
    assert "claim_id: claim_" in logs and "evidence_ids: ['ev_" in logs
    assert "evidence_id: ev_" in logs and "→ source_id: src_" in logs


def test_bounded_upstream_context_has_complete_chains_and_isolated_snapshots():
    store = EvidenceStore()
    for index in range(2):
        source = store.add_source(Source(task_id=f"D{index}", title="Paper", url=f"https://example.org/{index}"))
        for number in range(8):
            item = store.add_evidence(Evidence(task_id=f"D{index}", source_id=source.source_id, content="fact " * 60))
            store.add_claim(Claim(task_id=f"D{index}", text=f"Claim {number}", evidence_ids=[item.evidence_id]))
    context = select_evidence_context(store, ["D0", "D1"], max_chars=1800)
    assert len(context.to_json()) <= 1800 and {item.task_id for item in context.claims} == {"D0", "D1"}
    assert all(set(claim.evidence_ids) <= {item.evidence_id for item in context.evidence} for claim in context.claims)
    context.claims[0].evidence_ids.append("mutated")
    assert "mutated" not in store.get_claim(context.claims[0].claim_id).evidence_ids


def test_candidate_namespace_maps_upstream_claims_after_fresh_page_candidates():
    source = Source(task_id="D1", title="Upstream paper", url="https://example.org/upstream")
    item = Evidence(source_id=source.source_id, task_id="D1", content="A genuine upstream source fact.")
    claim = Claim(task_id="D1", text="A supported finding", evidence_ids=[item.evidence_id])
    context = EvidenceContext((source,), (item,), (claim,))
    data = payload([page()], context)
    candidate = next(item for item in data["candidates"] if item["origin"] == "upstream")
    assert candidate["evidence_index"] > 0
    assert data["upstream_claims"][0]["supporting_evidence_indexes"] == [candidate["evidence_index"]]
    bundle = build_evidence_bundle(task("A", "analysis"), extraction([candidate["evidence_index"]]), data, context)
    assert bundle.claims[0].evidence_ids == [item.evidence_id]
    assert bundle.evidence == [] and bundle.sources == []


def test_analysis_claim_can_mix_new_and_upstream_evidence_from_multiple_sources():
    store = EvidenceStore()
    source = store.add_source(Source(task_id="D1", title="Upstream paper", url="https://example.org/upstream"))
    previous = store.add_evidence(Evidence(task_id="D1", source_id=source.source_id, content="A genuine upstream fact."))
    store.add_claim(Claim(task_id="D1", text="Previous finding", evidence_ids=[previous.evidence_id]))
    context = select_evidence_context(store, ["D1"])
    data = payload([page()], context)
    fresh = index_for(data, "Method A uses ordered subgoals.")
    prior = next(item["evidence_index"] for item in data["candidates"] if item["origin"] == "upstream")
    local = build_evidence_bundle(task("A", "analysis"), extraction([fresh, prior]), data, context)
    merged = store.merge(local)
    evidence = store.get_evidence_for_claim(merged.claims[0].claim_id)
    assert {item.task_id for item in evidence} == {"D1", "A"}
    assert len({store.get_source_for_evidence(item.evidence_id).url for item in evidence}) == 2
    assert len(local.evidence) == 1 and previous.evidence_id in local.claims[0].evidence_ids


def test_extraction_input_and_initial_research_input_are_bounded():
    data = payload([dict(page(), text="Data. " * 20000)])
    assert len(json.dumps(model_payload(data), ensure_ascii=False)) < MAX_EXTRACTION_INPUT_CHARS
    source = Source(task_id="D1", title="Paper", url="https://example.org/a")
    item = Evidence(task_id="D1", source_id=source.source_id, content="Actual source evidence.")
    claim = Claim(task_id="D1", text="Supported finding.", evidence_ids=[item.evidence_id])
    context = EvidenceContext((source,), (item,), (claim,))
    researcher = Researcher.__new__(Researcher)
    researcher.system_prompt = "S" * 14000
    request, _ = researcher.build_request(task("S", "synthesis"), {"D1": CompactResearchResult(task_id="D1", summary="m" * 500)}, evidence_context=context)
    assert len(request) + len(researcher.system_prompt) <= MAX_RESEARCH_INPUT_CHARS
    assert "Actual source evidence." in request


def test_extraction_cannot_start_after_runtime_is_closed():
    runtime = ResearchRuntime("D1", 5, 1)
    runtime.closed.set()
    with pytest.raises(ResearchTimeoutError):
        with runtime.operation("evidence extraction"):
            pytest.fail("No extraction after deadline")


def test_merge_failure_stops_downstream_without_partial_publication(capsys):
    plan = ResearchPlan(goal="Research", tasks=[task("D1"), task("S", "synthesis", ["D1"])])
    class Worker:
        def research(self, task, context, goal):
            assert task.id == "D1"
            return ResearchExecutionResult(memo="Memo", compact=CompactResearchResult(task_id="D1", summary="Result"), tools_used=[],
                evidence_bundle=TaskEvidenceBundle(task_id="D1", evidence=[Evidence(task_id="D1", source_id="src_missing", content="A source fact.")]))
    with pytest.raises(RuntimeError, match="unknown source_id.*Pending tasks not executed: S"):
        ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), Worker()).run("Research")
    assert [item.status for item in plan.tasks] == ["failed", "pending"]
    assert "Evidence Store: sources=0 evidence=0 claims=0" in capsys.readouterr().out
