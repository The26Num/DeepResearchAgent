"""Phase 7: metric arithmetic, real SQLite harness, callbacks and bounded judge."""

from collections import Counter
import importlib
import json
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agents.relevance import RelevanceAssessor
from app.agents.verifier import ClaimVerifier
from app.agents.writer import WriterUsage
from app.config import Settings
from app.persistence.models import ResearchRunState, EvidenceSnapshot
from app.persistence.store import ResearchRunStore
from app.research.evidence_store import EvidenceStore
from app.research.gap_detection import GapDetector
from app.research.orchestrator import ResearchOrchestrator
from app.research.report_rendering import render_report_with_citations
from app.research.reresearch import GapGroup
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import (Claim, Evidence, Source, ResearchPlan, ResearchTask, ResearchReport,
                         TaskEvidenceBundle, ResearchExecutionResult, CompactResearchResult, VerificationResult)
from app.schemas.gap import ResearchGap
from app.schemas.relevance import ClaimRelevanceResult
from app.schemas.reresearch import ReResearchPlan, ReResearchTask
from evaluation.baselines.simple_search_summary import SimpleSearchSummary
from evaluation.dataset import load_benchmark, select_cases
from evaluation.judge import ReportJudge, full_judge_sources, baseline_judge_sources
from evaluation.metrics import Evaluator, efficiency_metrics, baseline_citation_metrics
from evaluation.models import BenchmarkCase, BenchmarkDataset, EvaluationResult, JudgeResult, MetricVector
from evaluation.output import EvaluationOutput, Redactor, aggregate, compare_results
from evaluation.runner import EvaluationRunner, agent_config
from evaluation.telemetry import TelemetryCollector, observe_execution


BENCHMARK = Path('evaluation/benchmarks/research_benchmark_v2.json')


def case(**kwargs):
    return BenchmarkCase(case_id="test", question="Goal", category="systems", question_type="narrow_focus",
                         expected_dimensions=kwargs.pop("expected_dimensions", ["dimension"]), **kwargs)


def plan(goal="Goal"):
    return ResearchPlan(goal=goal, tasks=[
        ResearchTask(id="D1", title="First", question="First", description="First"),
        ResearchTask(id="D2", title="Second", question="Second", description="Second"),
        ResearchTask(id="A", title="Analysis", question="Compare", description="Compare", task_type="analysis", depends_on=["D1", "D2"]),
        ResearchTask(id="S", title="Synthesis", question="Summarize", description="Summarize", task_type="synthesis", depends_on=["A"]),
    ])


def sample_state(n=10):
    store = EvidenceStore()
    source = store.add_source(Source(source_id="src1", task_id="D1", title="Paper", url="https://example.org/paper", published_at="2024-01-01"))
    fragments = [store.add_evidence(Evidence(evidence_id=f"ev{i}", source_id=source.source_id, task_id="D1", content=f"Fact {i}")) for i in range(5)]
    claims = [store.add_claim(Claim(claim_id=f"c{i}", task_id=("D1", "D2", "A", "S")[i % 4], text=f"Fact {i % 5}",
                                   evidence_ids=[fragments[i % 5].evidence_id])) for i in range(n)]
    verifications = [VerificationResult(claim_id=item.claim_id, task_id=item.task_id, evidence_ids=item.evidence_ids,
                                       status="SUPPORTED" if i < 8 else "PARTIAL" if i == 8 else "UNSUPPORTED", reason="checked")
                     for i, item in enumerate(claims)]
    relevance = {item.claim_id: ClaimRelevanceResult(claim_id=item.claim_id, task_id=item.task_id,
                  status="ALIGNED" if i < 6 else "PARTIAL" if i < 8 else "MISALIGNED", reason="checked") for i, item in enumerate(claims)}
    report = ResearchReport(title="Report", scope="Goal", executive_summary=[{"text":"Fact 0", "claim_ids":["c0"]}],
                            key_findings=[], sections=[], limitations=[], conclusion=[])
    return ResearchRunState(goal="Goal", research_plan=plan(), evidence_store=EvidenceSnapshot.model_validate(store.snapshot()),
        verification_results=verifications, relevance_results=relevance, research_report=report,
        rendered_report=render_report_with_citations(report, store))


def evaluate(state=None):
    return Evaluator().evaluate(state or sample_state(), case())


def test_support_alignment_denominators():
    metrics = evaluate()
    assert metrics.quality["support_rate"] == .8
    assert metrics.quality["partial_rate"] == .1
    assert metrics.quality["unsupported_rate"] == .1
    assert metrics.quality["alignment_rate"] == .6
    assert metrics.quality["partial_alignment_rate"] == .2
    assert metrics.quality["misaligned_rate"] == .2


def test_publishable_requires_both_decisions():
    state = sample_state()
    state.verification_results[0] = VerificationResult.model_validate({**state.verification_results[0].model_dump(), "status": "PARTIAL"})
    assert evaluate(state).quality["publishable_claim_rate"] == .5


def test_assessed_union_does_not_hide_unverified_claims():
    state = sample_state()
    state.verification_results = state.verification_results[:5]
    metrics = evaluate(state)
    assert metrics.research["assessed_claims"] == 10
    assert metrics.quality["publishable_claim_rate"] == .5
    assert metrics.quality["assessment_completion_rate"] == .5
    assert metrics.research["unverified_claims"] == 5


def test_task_coverage_three_of_four():
    state = sample_state()
    for key in ("c3", "c7"):
        state.relevance_results[key] = ClaimRelevanceResult(claim_id=key, task_id="S", status="MISALIGNED", reason="not aligned")
    assert evaluate(state).quality["task_coverage_rate"] == .75


def test_evidence_source_and_claim_usage_metrics():
    metrics = evaluate()
    assert metrics.quality["evidence_coverage"] == 1
    assert metrics.research["unique_evidence"] == 5
    assert metrics.research["total_claim_evidence_links"] == 10
    assert metrics.quality["evidence_reuse_ratio"] == .5
    assert metrics.quality["source_utilization_rate"] == 1
    assert metrics.quality["writer_claim_coverage"] == 1 / 6


def test_valid_citation_and_state_remains_unchanged():
    state = sample_state()
    original = state.model_dump_json()
    metrics = evaluate(state)
    assert metrics.quality["citation_integrity_pass"] is True
    assert metrics.quality["structural_integrity_pass"] is True
    assert state.model_dump_json() == original


def test_cannot_evaluate_a_run_under_a_different_benchmark_question():
    other = case().model_copy(update={"question":"Different question"})
    with pytest.raises(ValueError, match="does not match"):
        Evaluator().evaluate(sample_state(), other)


@pytest.mark.parametrize("tamper", ["marker", "reference", "unknown_source", "association", "unused_reference"])
def test_citation_integrity_detects_structural_errors(tamper):
    state = sample_state()
    rendered = state.rendered_report
    if tamper == "marker":
        state.rendered_report = rendered.model_copy(update={"markdown": rendered.markdown.replace("[1]", "[9]", 1)})
    elif tamper == "reference":
        state.rendered_report = rendered.model_copy(update={"references": []})
    elif tamper == "unknown_source":
        state.rendered_report = rendered.model_copy(update={"references": [rendered.references[0].model_copy(update={"source_id": "unknown"})]})
    elif tamper == "association":
        state.rendered_report = rendered.model_copy(update={"citations": []})
    else:
        state.rendered_report = rendered.model_copy(update={"references": [*rendered.references, rendered.references[0].model_copy(update={"citation_number": 2})]})
    metrics = evaluate(state)
    assert metrics.quality["citation_integrity_pass"] is False
    assert metrics.quality["citation_error_count"] > 0


@pytest.mark.parametrize("broken", ["evidence", "source", "claim_evidence"])
def test_dangling_graph_regression_gate(broken):
    state = sample_state()
    if broken == "evidence":
        state.evidence_store.evidence = state.evidence_store.evidence[1:]
    elif broken == "source":
        state.evidence_store.sources = []
    else:
        state.evidence_store.claims[0] = state.evidence_store.claims[0].model_copy(update={"evidence_ids": []})
    assert evaluate(state).quality["structural_integrity_pass"] is False


def test_duplicate_limitation_and_paragraph_detection():
    state = sample_state()
    raw = state.research_report.model_dump()
    raw["limitations"] = [{"text":"A limitation", "gap_ids":[]}, {"text":"A   limitation", "gap_ids":[]}]
    state.research_report = ResearchReport.model_validate(raw)
    metrics = evaluate(state)
    assert metrics.quality["duplicate_limitation_count"] == 1
    assert metrics.quality["duplicate_paragraph_count"] == 1
    assert metrics.quality["duplicate_paragraph_rate"] == 1 / 3


def test_zero_denominators_are_not_perfect_scores():
    metrics = evaluate(ResearchRunState(goal="Goal"))
    for key in ("support_rate", "alignment_rate", "publishable_claim_rate", "task_coverage_rate", "writer_claim_coverage", "gap_repair_rate"):
        assert metrics.quality[key] is None


def test_gold_sources_and_publication_year_are_optional():
    state = sample_state()
    assert evaluate(state).quality["source_recall"] is None
    c = case(gold_sources=["https://example.org/paper#abstract", "https://example.org/missing"])
    metrics = Evaluator().evaluate(state, c)
    assert metrics.quality["source_recall"] == .5
    assert metrics.research["median_source_year"] == 2024


def test_latest_verification_not_history_count():
    state = sample_state()
    old = state.verification_results[0]
    state.verification_results.append(VerificationResult(claim_id=old.claim_id, task_id=old.task_id, evidence_ids=old.evidence_ids,
                                                        status="UNSUPPORTED", reason="later correction"))
    assert evaluate(state).quality["support_rate"] == .7
    assert evaluate(state).research["verified_claims"] == 10


def test_stale_verification_fails_closed():
    state = sample_state()
    state.verification_results[0] = state.verification_results[0].model_copy(update={"evidence_ids": []})
    metrics = evaluate(state)
    assert metrics.research["publishable_claims"] == 5
    assert not metrics.quality["structural_integrity_pass"]


def test_selected_gap_denominator_excludes_unselected_gaps():
    state = sample_state()
    state.gaps = [ResearchGap(gap_id=f"g{i}", gap_type="COVERAGE_GAP", task_id="D1", description="missing",
                             missing_information="needed", status=("RESOLVED", "RESOLVED", "IMPROVED", "OPEN", "RESOLVED")[i]) for i in range(5)]
    state.selected_gap_groups = [GapGroup("G1", "D1", tuple(f"g{i}" for i in range(4)), (), (), ())]
    metrics = evaluate(state)
    assert metrics.research["selected_gap_count"] == 4
    assert metrics.quality["gap_resolved_rate"] == .5
    assert metrics.quality["gap_improved_rate"] == .25
    assert metrics.quality["gap_repair_rate"] == .75
    assert metrics.research["after_reresearch_open_gaps"] == 2


def test_reresearch_publishable_yield_two_of_five():
    state = sample_state()
    for i in range(5):
        claim = Claim(claim_id=f"rr{i}", task_id="RR1", text="Fact", evidence_ids=["ev0"])
        state.evidence_store.claims.append(claim)
        state.verification_results.append(VerificationResult(claim_id=claim.claim_id, task_id="RR1", evidence_ids=["ev0"], status="SUPPORTED", reason="checked"))
        state.relevance_results[claim.claim_id] = ClaimRelevanceResult(claim_id=claim.claim_id, task_id="RR1", status="ALIGNED" if i < 2 else "MISALIGNED", reason="checked")
    state.reresearch_summary.plans = [ReResearchPlan(round_id=1, selected_gap_ids=["g1"], tasks=[
        ReResearchTask(id="RR1", title="supplement", question="find", description="find", origin_task_id="D1", origin_gap_ids=["g1"])])]
    assert evaluate(state).quality["reresearch_publishable_yield"] == .4


def test_benchmark_version_types_categories_and_counts():
    dataset = load_benchmark(BENCHMARK)
    assert dataset.benchmark_version == 2 and len(dataset.cases) == 2
    assert [item.case_id for item in dataset.cases] == ["offline_rl_001", "china_game_001"]
    assert [item.category for item in dataset.cases] == ["academic_research", "industry_research"]
    assert all(item.question_type == "discovery" and len(item.expected_dimensions) == 4 for item in dataset.cases)


def test_duplicate_case_id_rejected():
    with pytest.raises(ValidationError, match="Duplicate case_id"):
        BenchmarkDataset(benchmark_version=1, cases=[case(), case()])


def test_case_filter_limit_and_invalid_selection():
    dataset = load_benchmark(BENCHMARK)
    assert len(select_cases(dataset, limit=2)) == 2
    assert select_cases(dataset, case_id="china_game_001")[0].case_id == "china_game_001"
    with pytest.raises(ValueError, match="Unknown"):
        select_cases(dataset, case_id="missing")
    with pytest.raises(ValueError, match="positive"):
        select_cases(dataset, limit=0)


def completion(content="done", *, usage=True):
    payload = {"id":"chatcmpl-test", "object":"chat.completion", "created":0, "model":"test-model",
        "choices":[{"index":0,"finish_reason":"stop","message":{"role":"assistant","content":content}}]}
    if usage:
        payload["usage"] = {"prompt_tokens":10,"completion_tokens":4,"total_tokens":14}
    return httpx.Response(200, json=payload)


def test_callback_observes_real_sdk_calls_across_worker_threads():
    with httpx.Client(transport=httpx.MockTransport(lambda request: completion())) as client:
        model = ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.org/v1", http_client=client, max_retries=0)
        with observe_execution() as collector:
            workers = [Thread(target=lambda: model.invoke([SystemMessage(content="EVALUATION_BASELINE_SUMMARY"), HumanMessage(content="test")])) for _ in range(3)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            telemetry = collector.snapshot()
    metrics = efficiency_metrics(telemetry)
    assert metrics["total_llm_calls"] == 3
    assert metrics["total_tokens"] == 42
    assert metrics["usage_coverage"] == 1
    assert metrics["calls_by_stage"]["baseline_summary"]["llm_calls"] == 3


def test_tool_callbacks_do_not_double_count_budget_wrappers():
    @tool("web_search")
    def search(query: str, max_results: int = 5):
        """Test search."""
        return json.dumps({"results":[]})
    @tool("fetch_webpage")
    def fetch(url: str, max_chars: int = 5000):
        """Test fetch."""
        return json.dumps({"url":url,"text":"Page"})
    with observe_execution() as collector:
        tools = budgeted_tools(ToolBudget(1,1), search, fetch)
        tools[0].invoke({"query":"q"})
        tools[0].invoke({"query":"denied"})
        tools[1].invoke({"url":"https://example.org"})
        data = collector.snapshot()
    assert data["tools"]["web_search_calls"] == 1
    assert data["tools"]["fetch_webpage_calls"] == 1
    assert data["tools"]["successful_fetches"] == 1


def test_missing_usage_retains_subtotal_not_fake_total():
    collector = TelemetryCollector()
    messages = [[SystemMessage(content="EVALUATION_BASELINE_SUMMARY")]]
    for i in range(2):
        collector.model_start(i, messages)
        message = AIMessage(content="x", usage_metadata={"input_tokens":10,"output_tokens":4,"total_tokens":14}) if i == 0 else AIMessage(content="x")
        collector.model_end(i, LLMResult(generations=[[ChatGeneration(message=message)]]))
    metrics = efficiency_metrics(collector.snapshot())
    assert metrics["total_llm_calls"] == 2 and metrics["usage_coverage"] == .5
    assert metrics["total_tokens"] is None
    assert metrics["observed_total_tokens"] == 14


def test_failed_model_request_counts_call_without_fabricating_usage():
    collector = TelemetryCollector()
    collector.model_start("failed", [[SystemMessage(content="EVALUATION_BASELINE_SUMMARY")]])
    metrics = efficiency_metrics(collector.snapshot())
    assert metrics["total_llm_calls"] == 1 and metrics["total_tokens"] is None
    assert metrics["model_responses"] == 0 and metrics["usage_coverage"] is None
    assert metrics["model_no_response_count"] == 1 and metrics["model_response_rate"] == 0


def test_closed_collector_ignores_late_callbacks_and_sessions_isolate():
    with observe_execution() as first:
        first.model_start("one", [[SystemMessage(content="EVALUATION_BASELINE_SUMMARY")]])
        first_data = first.snapshot()
    with observe_execution() as second:
        first.model_start("late", [[SystemMessage(content="EVALUATION_BASELINE_SUMMARY")]])
        second_data = second.snapshot()
    assert efficiency_metrics(first_data)["total_llm_calls"] == 1
    assert efficiency_metrics(second_data)["total_llm_calls"] == 0


def test_pdf_failure_and_fetch_efficiency():
    collector = TelemetryCollector()
    for i, output in enumerate(({"text":"body"}, {"error":"PDF parsing not supported"}, {"error":"timeout"})):
        collector.tool_start(i, None, {"name":"fetch_webpage"})
        collector.tool_end(i, json.dumps(output))
    metrics = efficiency_metrics(collector.snapshot())
    assert metrics["successful_fetches"] == 1 and metrics["failed_fetches"] == 2
    assert metrics["pdf_failure_count"] == 1 and metrics["fetch_success_rate"] == 1 / 3


def test_persisted_state_does_not_pretend_to_have_all_usage():
    metrics = efficiency_metrics(state=sample_state())
    assert metrics["total_llm_calls"] is None
    assert metrics["total_tokens"] is None
    assert metrics["researcher_calls"] is None
    assert metrics["successful_fetches"] is None


class ScriptedModel:
    def __init__(self, texts):
        self.texts, self.calls = iter(texts), []

    def invoke(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        return AIMessage(content=next(self.texts), usage_metadata={"input_tokens":10,"output_tokens":5,"total_tokens":15})


def baseline_model():
    return ScriptedModel(['{"queries":["q1","q2","q3"]}', 'Finding supported by material. [S1]'])


def baseline_tools():
    calls = Counter()
    def search(arguments):
        calls["search"] += 1
        return json.dumps({"results":[{"title":"Source", "url":f"https://example.org/{arguments['query']}/{i}","snippet":"Finding"} for i in range(3)]})
    def fetch(arguments):
        calls["fetch"] += 1
        return json.dumps({"url":arguments["url"], "text":"Finding supported by material", "title":"Source"})
    return SimpleNamespace(invoke=search), SimpleNamespace(invoke=fetch), calls


def test_baseline_search_read_summary_without_research_components(monkeypatch):
    for module, name in (("app.research.evidence_store", "EvidenceStore"), ("app.agents.verifier", "ClaimVerifier"), ("app.research.gap_store", "GapStore")):
        monkeypatch.setattr(importlib.import_module(module), name, lambda *a, **k: pytest.fail("Baseline accessed a research component"))
    search, fetch, calls = baseline_tools()
    model = baseline_model()
    result = SimpleSearchSummary(model=model, search_tool=search, fetch_tool=fetch).run("Goal")
    assert calls == {"search":3,"fetch":4}
    assert result.llm_calls == 2 and len(model.calls) == 2
    assert result.tokens["observed_input_tokens"] == 20
    assert len(result.sources) == 4 and result.sources[0].basis == "page_excerpt"
    assert baseline_citation_metrics(result.report, result.sources)["citation_integrity_pass"]


def test_baseline_uses_same_default_tools_and_main_model_factory():
    from app.tools.web_search import web_search
    from app.tools.fetch_webpage import fetch_webpage
    pipeline = SimpleSearchSummary()
    assert pipeline.search_tool is web_search and pipeline.fetch_tool is fetch_webpage
    assert pipeline.max_search_calls == 3 and pipeline.max_fetch_calls == 4


def test_baseline_unknown_citation_is_reported_not_repaired():
    assert baseline_citation_metrics("Unsupported [S9]", [])["citation_integrity_pass"] is False


def test_baseline_fetch_failure_falls_back_to_labeled_snippet():
    search, _, _ = baseline_tools()
    fetch = SimpleNamespace(invoke=lambda args: json.dumps({"error":"PDF parsing unsupported"}))
    result = SimpleSearchSummary(model=baseline_model(), search_tool=search, fetch_tool=fetch).run("Goal")
    assert all(item.basis == "search_snippet" for item in result.sources)
    assert result.tool_usage["pdf_failure_count"] == 4


def judge_json(**kwargs):
    raw = {"goal_relevance":3,"coverage":3,"groundedness":2.5,"clarity":4,
           "dimensions":[{"dimension":"dimension","status":"PARTIAL"}],"missing_dimensions":[],
           "unsupported_statements":["Unsupported sentence"],"assessed_statement_count":4,"reason":"Bounded assessment"}
    raw.update(kwargs)
    return json.dumps(raw)


def test_one_structured_judge_call_and_programmatic_scores():
    model = ScriptedModel([judge_json()])
    judge = ReportJudge(model=model)
    result = judge.evaluate(case(), "Report", [])
    assert result.dimension_coverage_score == .5
    assert result.unsupported_statement_rate == .25
    assert judge.calls == 1 and len(model.calls) == 1
    assert judge.last_usage["total_tokens"] == 15
    with pytest.raises(RuntimeError, match="budget"):
        judge.evaluate(case(), "Report", [])
    assert len(model.calls) == 1


@pytest.mark.parametrize("raw", [judge_json(dimensions=[{"dimension":"dimension","status":"UNKNOWN"}]),
                                  judge_json(goal_relevance=5), judge_json(missing_dimensions=["dimension"]),
                                  judge_json(assessed_statement_count=0), "invalid JSON"])
def test_judge_invalid_response_never_retries(raw):
    model = ScriptedModel([raw])
    judge = ReportJudge(model=model)
    with pytest.raises((ValidationError, ValueError)):
        judge.evaluate(case(), "Report", [])
    assert judge.calls == 1 and len(model.calls) == 1


def test_empty_dimensions_skip_coverage_judgment():
    model = ScriptedModel([judge_json(dimensions=[], missing_dimensions=[], coverage=None)])
    result = ReportJudge(model=model).evaluate(case(expected_dimensions=[]), "Report", [])
    assert result.dimension_coverage_score is None and result.coverage is None


def test_judge_bounded_input_and_no_full_store_or_memo():
    state = sample_state()
    sources = full_judge_sources(state)
    assert all(len(excerpt) <= 400 for item in sources for excerpt in item["excerpts"])
    model = ScriptedModel([judge_json()])
    ReportJudge(model=model).evaluate(case(), "Report", sources)
    text = model.calls[0][0][1].content
    assert "task_results" not in text and "evidence_store" not in text and "memo" not in json.loads(text)
    with pytest.raises(ValueError, match="limit"):
        ReportJudge(model=ScriptedModel([]), max_context_chars=1).evaluate(case(), "Report", [])


def test_judge_unknown_dimension_is_schema_failure_without_research_changes():
    model = ScriptedModel([judge_json(dimensions=[{"dimension":"wrong","status":"COVERED"}])])
    with pytest.raises(ValueError, match="dimensions"):
        ReportJudge(model=model).evaluate(case(), "Report", [])
    assert len(model.calls) == 1


def test_optional_judge_model_name_reaches_real_http_without_extra_calls():
    requests = []
    def transport(request):
        requests.append(json.loads(request.content))
        return completion(judge_json())
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="research-model", api_key="test-key", base_url="https://example.org/v1", http_client=client, max_retries=0)
        ReportJudge(model=model, model_name="judge-model").evaluate(case(), "Report", [])
        assert model.model_name == "research-model"
    assert len(requests) == 1 and requests[0]["model"] == "judge-model"
    assert requests[0]["response_format"] == {"type":"json_object"}


def test_missing_values_are_excluded_from_averages():
    items = [EvaluationResult(case_id="a", benchmark_version=1, variant="full", status="COMPLETED",
                              metrics=MetricVector(efficiency={"total_tokens":None})),
             EvaluationResult(case_id="b", benchmark_version=1, variant="full", status="COMPLETED",
                              metrics=MetricVector(efficiency={"total_tokens":100})),
             EvaluationResult(case_id="c", benchmark_version=1, variant="full", status="FAILED",
                              metrics=MetricVector(efficiency={"total_tokens":1000}))]
    summary = aggregate(items)["full"]
    assert summary["means"]["efficiency.total_tokens"] == 100
    assert summary["metric_sample_count"]["efficiency.total_tokens"] == 1


def test_comparison_preserves_na_and_only_computes_shared_numeric_delta():
    full = EvaluationResult(case_id="a", benchmark_version=1, variant="full", status="COMPLETED",
                            metrics=MetricVector(quality={"support_rate":.8},efficiency={"total_tokens":100}))
    baseline = EvaluationResult(case_id="a", benchmark_version=1, variant="baseline", status="COMPLETED",
                                metrics=MetricVector(efficiency={"total_tokens":60}))
    comparison = compare_results("a", full, baseline)
    assert comparison.full_status == "COMPLETED" and comparison.baseline_status == "COMPLETED"
    assert comparison.baseline["quality.support_rate"] is None
    assert comparison.delta["quality.support_rate"] is None
    assert comparison.delta["efficiency.total_tokens"] == 40


def test_secret_scrubbing_in_all_output_files(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "private-key")
    output = EvaluationOutput(tmp_path)
    result = EvaluationResult(case_id="test", benchmark_version=1, variant="full", status="FAILED",
                              metrics=MetricVector(quality={"message":"private-key Authorization: Bearer secret-token"}))
    output.save_json("config.json", {"diagnostic":"private-key"})
    output.save_report("test", "full", "private-key https://example.org?api_key=another-secret")
    output.append_case(case(), [result])
    for path in output.path.iterdir():
        text = path.read_text(encoding="utf-8-sig")
        assert "private-key" not in text and "secret-token" not in text and "another-secret" not in text
    assert (output.path / "summary.json").exists() and (output.path / "cases.jsonl").exists()
    assert (output.path / "summary.csv").exists()


class Decisions:
    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[1].text)
        relevance = "research_goal" in payload
        return {"results":[{"claim_id": item["claim_id"], "status":"ALIGNED" if relevance else "SUPPORTED", "reason":"checked"} for item in payload["claims"]]}


class TestWriter:
    __test__ = False

    def __init__(self):
        self.last_usage, self.last_grounding = WriterUsage(), ()

    def write(self, context):
        self.last_usage = WriterUsage(accepted_claims=len(context.accepted_claims))
        paragraphs = [{"text":item.text,"claim_ids":[item.claim_id]} for item in context.accepted_claims]
        return ResearchReport(title="Report", scope="Goal", executive_summary=paragraphs[:3], key_findings=paragraphs,
                              sections=[{"heading":"Findings","paragraphs":paragraphs[:4]}], limitations=[], conclusion=paragraphs[:2])


def persisted_factory(monkeypatch, calls, *, fail_first=False):
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.research.orchestrator.get_settings", lambda: settings)
    def factory(store, concurrency):
        def create_plan(goal):
            calls["supervisor"] += 1
            if fail_first and calls["supervisor"] == 1:
                raise TimeoutError("test timeout")
            return plan(goal)
        def research(task, **kwargs):
            calls["researcher"] += 1
            source = Source(task_id=task.id, title="Source", url="https://example.org/paper")
            evidence = Evidence(task_id=task.id, source_id=source.source_id, content="Supported finding")
            claim = Claim(task_id=task.id, text="Supported finding", evidence_ids=[evidence.evidence_id])
            return ResearchExecutionResult(memo="Memo", compact=CompactResearchResult(task_id=task.id, summary="Finding"), tools_used=[],
                evidence_bundle=TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=[evidence], claims=[claim]))
        return ResearchOrchestrator(supervisor=SimpleNamespace(create_plan=create_plan), researcher=SimpleNamespace(research=research),
            verifier=ClaimVerifier(model=Decisions(), settings=settings), gap_detector=GapDetector(RelevanceAssessor(model=Decisions(), settings=settings)),
            writer=TestWriter(), run_store=store, max_concurrency=concurrency, reresearch=False)
    return factory


def test_harness_case_failure_continues_and_saves_recoverable_run(tmp_path, monkeypatch):
    calls = Counter()
    runner = EvaluationRunner(settings=Settings(_env_file=None), output_root=tmp_path / "evals", db_path=tmp_path / "runs.db",
        orchestrator_factory=persisted_factory(monkeypatch, calls, fail_first=True))
    summary = runner.run(BENCHMARK, limit=2)
    assert summary["cases_total"] == 2 and summary["cases_completed"] == 1 and summary["cases_failed"] == 1
    entries = [json.loads(line) for line in (runner.output.path / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(entries) == 2 and entries[0]["run_id"] and entries[1]["run_id"]
    store = ResearchRunStore(tmp_path / "runs.db")
    assert store.get_run(entries[0]["run_id"]).status == "FAILED"
    assert store.load_latest_checkpoint(entries[0]["run_id"]).stage == "RUN_CREATED"
    assert store.get_run(entries[1]["run_id"]).status == "COMPLETED"


def test_compare_mode_same_question_independent_baseline_and_judge_disabled(tmp_path, monkeypatch):
    calls, baseline_questions = Counter(), []
    search, fetch, _ = baseline_tools()
    class Baseline:
        def run(self, question):
            baseline_questions.append(question)
            return SimpleSearchSummary(model=baseline_model(), search_tool=search, fetch_tool=fetch).run(question)
    runner = EvaluationRunner(settings=Settings(_env_file=None), output_root=tmp_path / "evals", db_path=tmp_path / "runs.db",
        orchestrator_factory=persisted_factory(monkeypatch,calls), baseline_factory=Baseline,
        judge_factory=lambda: pytest.fail("Judge disabled but constructed"))
    summary = runner.run(BENCHMARK, mode="compare", limit=1)
    assert summary["cases_completed"] == 1
    entries = [json.loads(line) for line in (runner.output.path / "cases.jsonl").read_text(encoding="utf-8").splitlines()]
    assert baseline_questions == [entries[0]["question"]]
    assert set(entries[0]["results"]) == {"full","baseline"}
    assert (runner.output.path / "comparison.json").exists()
    assert len(ResearchRunStore(tmp_path / "runs.db").list_runs()) == 1


def test_judge_failure_is_diagnostic_and_never_repeats_research(tmp_path, monkeypatch):
    calls = Counter()
    model = ScriptedModel(["invalid JSON"])
    runner = EvaluationRunner(settings=Settings(_env_file=None), output_root=tmp_path / "evals", db_path=tmp_path / "runs.db",
        orchestrator_factory=persisted_factory(monkeypatch,calls), judge_enabled=True,
        judge_factory=lambda: ReportJudge(model=model))
    summary = runner.run(BENCHMARK, limit=1)
    assert summary["cases_completed"] == 1
    assert calls["researcher"] == 4 and len(model.calls) == 1
    assert runner.output.results[0].errors[0].stage == "judge"
    assert runner.output.results[0].judge_usage["llm_calls"] == 1


def test_config_allowlist_contains_budgets_but_no_credentials():
    cfg = agent_config(Settings(_env_file=None, model_api_key="do-not-save", model_base_url="https://example.org/v1"), 3)
    text = json.dumps(cfg)
    assert "do-not-save" not in text and "api_key" not in text and "base_url" not in text
    assert cfg["original_task_tool_budgets"]["discovery"] == {"search":3,"fetch":4}
    assert cfg["temperature"] == 0 and cfg["max_concurrency"] == 3


def test_cli_selection_forwarding_and_exit_codes(monkeypatch):
    import evaluate as cli
    received = {}
    class Runner:
        def __init__(self, **kwargs):
            received.update(kwargs)
        def run(self, path, **kwargs):
            received.update(kwargs)
            return {"cases_failed":1}
    monkeypatch.setattr(cli, "EvaluationRunner", Runner)
    assert cli.main([str(BENCHMARK), "--mode", "compare", "--limit", "2", "--case", "china_game_001", "--judge"]) == 1
    assert received["judge_enabled"] and received["limit"] == 2 and received["mode"] == "compare"
