"""Phase 5A read-only context, strict provenance, one bounded writing pass."""

import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from openai import APITimeoutError
from pydantic import ValidationError

from app.agents.writer import ResearchWriter as ActualResearchWriter, WriterOutputError, validate_report
from app.agents.report_grounding import ReportGroundingChecker
from app.config import Settings
from app.research.evidence_store import EvidenceStore
from app.research.gap_store import GapStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.report_rendering import render_report
from app.research.runtime import ResearchTimeoutError
from app.research.verification_store import VerificationStore
from app.research.writing_context import WritingContextBuilder, WritingContextLimitError, WritingIntegrityError
from app.schemas import (
    Claim, ClaimRelevanceResult, CompactResearchResult, Evidence, ResearchExecutionResult, ResearchGap,
    ResearchPlan, ResearchReport, ResearchTask, Source, TaskEvidenceBundle, VerificationResult,
)


def settings(**kwargs):
    return Settings(_env_file=None, **kwargs)


class ApprovingGroundingModel:
    """Old writer contract tests use a real gate with a deterministic judge."""
    def with_structured_output(self, schema, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        items = json.loads(messages[1].text)["items"]
        return {"results": [{"item_index": item["item_index"], "status": "SUPPORTED", "reason": "Supported"}
                            for item in items]}


def ResearchWriter(*args, **kwargs):
    kwargs.setdefault("grounding_checker", ReportGroundingChecker(ApprovingGroundingModel(), settings=kwargs.get("settings")))
    return ActualResearchWriter(*args, **kwargs)


def fixture(choices=(('SUPPORTED', 'ALIGNED'),), *, evidence_count=1, shared=False, goal="Original research goal"):
    store, verified, gaps, relevance = EvidenceStore(), VerificationStore(), GapStore(), {}
    source = Source(task_id="D1", title="Original paper", url="https://example.org/paper", published_at="2025-01-01")
    store.add_source(source)
    evidence = [Evidence(task_id="D1", source_id=source.source_id,
                         content=f"Direct evidence {i}. " + "Specific original source information. " * 12) for i in range(evidence_count)]
    for item in evidence:
        store.add_evidence(item)
    claims = []
    for index, (support, alignment) in enumerate(choices):
        task_id = "D1" if index == 0 else f"RR_D{index}_1"
        claim = store.add_claim(Claim(task_id=task_id, text=f"Grounded finding {index}",
            evidence_ids=[item.evidence_id for item in evidence] if shared else [evidence[index % evidence_count].evidence_id]))
        verified.add_verification(VerificationResult(claim_id=claim.claim_id, task_id=task_id,
            evidence_ids=claim.evidence_ids, status=support, reason="Support assessment"))
        relevance[claim.claim_id] = ClaimRelevanceResult(claim_id=claim.claim_id, task_id=task_id,
            status=alignment, reason="Goal assessment")
        claims.append(claim)
    context = WritingContextBuilder(settings=settings()).build(goal, store, verified, relevance, gaps)
    return store, verified, relevance, gaps, claims, context


def report_data(context, *, subset=None):
    refs = [item.claim_id for item in context.accepted_claims]
    if subset is not None:
        refs = refs[:subset]
    paragraph = {"text": "The supplied findings address the research goal within the available evidence.", "claim_ids": refs}
    payload = context.payload
    limitations = [{"text": "Current evidence leaves this question unresolved.", "gap_ids": [item["gap_id"]],
                    "partial_claim_ids": []} for item in payload["open_gaps"][:1]]
    return dict(title="Research overview", executive_summary=[paragraph], scope="The original research goal.",
        key_findings=[paragraph], sections=[{"heading": "Evidence and scope", "paragraphs": [paragraph]}],
        limitations=limitations, conclusion=[paragraph])


class ModelStub:
    def __init__(self, context, *, mutate=lambda value: value, native=True):
        self.context, self.mutate, self.native = context, mutate, native
        self.requests = []

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema"}
        self.schema = schema
        if not self.native:
            raise NotImplementedError
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.requests.append((messages, kwargs))
        raw = AIMessage(content="", usage_metadata={"input_tokens": 220, "output_tokens": 100, "total_tokens": 320})
        for callback in (config or {}).get("callbacks", []):
            callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=raw)]]))
        value = self.mutate(report_data(self.context))
        return value if self.native else AIMessage(content=json.dumps(value))


@pytest.mark.parametrize("support,alignment,accepted,partial", [
    ("SUPPORTED", "ALIGNED", 1, 0), ("SUPPORTED", "MISALIGNED", 0, 0),
    ("PARTIAL", "ALIGNED", 0, 1), ("SUPPORTED", "PARTIAL", 0, 1),
    ("PARTIAL", "PARTIAL", 0, 1), ("UNSUPPORTED", "ALIGNED", 0, 0),
    ("UNSUPPORTED", "PARTIAL", 0, 0), ("PARTIAL", "MISALIGNED", 0, 0),
])
def test_program_selects_only_supported_aligned_and_separates_partial(support, alignment, accepted, partial):
    *_, context = fixture([(support, alignment)])
    assert len(context.accepted_claims) == accepted
    assert len(context.payload["partial_claims_for_limitations_only"]) == partial
    if not accepted:
        assert context.payload["evidence"] == context.payload["sources"] == {}
        if not partial:
            assert "Grounded finding" not in context.text


def test_new_reresearch_claims_use_same_filter_and_shared_evidence_and_sources_are_deduped():
    store, _, _, _, claims, context = fixture([("SUPPORTED", "ALIGNED")] * 3, evidence_count=4, shared=True)
    assert len(context.accepted_claims) == 3
    assert len(context.payload["evidence"]) == 3 and len(context.payload["sources"]) == 1
    assert context.accepted_claims[1].origin_task_id == "RR_D1_1"
    assert len(context.accepted_claims[0].evidence_ids) == 4
    assert len(context.payload["accepted_claims"][0]["evidence_ids"]) == 3
    for item in claims:
        assert len(store.get_claim(item.claim_id).evidence_ids) == 4
    assert all(len(item["content"]) <= 400 for item in context.payload["evidence"].values())


def test_missing_decisions_excluded_and_latest_supported_decision_controls_acceptance():
    store, verified, relevance, gaps, claims, _ = fixture()
    claim = claims[0]
    verified.add_verification(VerificationResult(claim_id=claim.claim_id, task_id=claim.task_id,
        evidence_ids=claim.evidence_ids, status="PARTIAL", reason="Latest state has insufficient support"))
    builder = WritingContextBuilder(settings=settings())
    assert not builder.build("Goal", store, verified, relevance, gaps).accepted_claims
    assert not builder.build("Goal", store, verified, {}, gaps).accepted_claims
    assert not builder.build("Goal", store, VerificationStore(), relevance, gaps).accepted_claims


@pytest.mark.parametrize("kind", ["verification_task", "verification_evidence", "relevance_task", "relevance_claim"])
def test_stale_or_mismatched_decisions_fail_before_writing(kind):
    store, verified, relevance, gaps, claims, _ = fixture()
    claim = claims[0]
    if kind.startswith("verification"):
        result = verified.get_latest_verification(claim.claim_id)
        update = {"task_id": "wrong"} if kind == "verification_task" else {"evidence_ids": ["missing"]}
        verified = VerificationStore()
        verified.add_verification(result.model_copy(update=update))
    else:
        relevance[claim.claim_id] = relevance[claim.claim_id].model_copy(update={
            "task_id" if kind == "relevance_task" else "claim_id": "wrong"})
    with pytest.raises(WritingIntegrityError):
        WritingContextBuilder(settings=settings()).build("Goal", store, verified, relevance, gaps)


def test_open_and_improved_gaps_enter_limitations_but_resolved_excluded():
    store, verified, relevance, gaps, _, _ = fixture()
    entries = [ResearchGap(task_id="D1", gap_type="COVERAGE_GAP", description="Limited coverage",
                missing_information="More direct results", status=state) for state in ["OPEN", "IMPROVED", "RESOLVED"]]
    # Different original task IDs avoid task/type deduplication.
    for index, item in enumerate(entries):
        gaps.add_gap(item.model_copy(update={"task_id": f"D{index}"}))
    context = WritingContextBuilder(settings=settings()).build("Goal", store, verified, relevance, gaps)
    assert {item["status"] for item in context.payload["open_gaps"]} == {"OPEN", "IMPROVED"}
    assert entries[2].gap_id not in context.text


def test_context_budget_reduces_evidence_before_claims_and_never_mutates_store():
    store, verified, relevance, gaps, claims, full = fixture([("SUPPORTED", "ALIGNED")] * 2, evidence_count=6, shared=True)
    original = [item.model_dump() for item in store.all_claims()]
    max_chars = full.context_chars - 700
    compact = WritingContextBuilder(settings=settings(max_writer_context_chars=max_chars)).build("Goal", store, verified, relevance, gaps)
    assert compact.context_chars <= max_chars
    assert len(compact.accepted_claims) == len(claims)
    assert len(compact.payload["evidence"]) < len(full.payload["evidence"])
    assert original == [item.model_dump() for item in store.all_claims()]
    assert all(item.text == full.accepted_claims[index].text for index, item in enumerate(compact.accepted_claims))
    payload = compact.payload
    payload["accepted_claims"].clear()
    assert len(compact.payload["accepted_claims"]) == 2


def test_context_cannot_fit_complete_claims_fails_without_silent_omissions():
    store, verified, relevance, gaps, _, _ = fixture()
    with pytest.raises(WritingContextLimitError, match="not dropped or truncated"):
        WritingContextBuilder(settings=settings(max_writer_context_chars=500)).build("Goal", store, verified, relevance, gaps)
    with pytest.raises(WritingIntegrityError, match="goal"):
        WritingContextBuilder(settings=settings()).build(" ", store, verified, relevance, gaps)


def test_empty_accepted_claims_do_not_create_model_and_return_minimal_report(monkeypatch):
    store, verified, relevance, gaps, _, _ = fixture([("SUPPORTED", "MISALIGNED")])
    item = gaps.add_gap(ResearchGap(task_id="D1", gap_type="COVERAGE_GAP", description="No answer", missing_information="Direct evidence needed"))
    context = WritingContextBuilder(settings=settings()).build("Goal", store, verified, relevance, gaps)
    monkeypatch.setattr("app.agents.writer.create_writer_llm", lambda: pytest.fail("No LLM needed"))
    worker = ResearchWriter(settings=settings())
    report = worker.write(context)
    assert worker.model is None and worker.last_usage.llm_calls == 0
    assert report.used_claim_ids == [] and not report.key_findings
    assert report.limitations[0].gap_ids == [item.gap_id]
    assert "当前研究没有获得足够支持且与目标对齐的结论" in render_report(report)
    assert "Grounded finding" not in render_report(report)


def test_minimal_report_many_gaps_consolidates_without_losing_gap_ids():
    store, verified, relevance, gaps, _, _ = fixture([("PARTIAL", "ALIGNED")])
    for index in range(24):
        gaps.add_gap(ResearchGap(task_id=f"D{index}", gap_type="COVERAGE_GAP", description="No direct answer", missing_information="More direct evidence"))
    context = WritingContextBuilder(settings=settings()).build("Goal", store, verified, relevance, gaps)
    report = ResearchWriter(settings=settings()).write(context)
    assert {key for item in report.limitations for key in item.gap_ids} == {item.gap_id for item in gaps.all_gaps()}
    assert len(report.limitations) <= 20


def test_zero_accepted_gaps_do_not_fail_an_unused_llm_context_budget():
    store, verified, relevant, gaps, _, _ = fixture([("UNSUPPORTED", "ALIGNED")])
    for index in range(30):
        gaps.add_gap(ResearchGap(task_id=f"D{index}", gap_type="COVERAGE_GAP", description="Limited coverage",
            missing_information="Additional direct evidence is needed. " * 5))
    cfg = settings(max_writer_context_chars=500)
    context = WritingContextBuilder(settings=cfg).build("Goal", store, verified, relevant, gaps)
    assert context.context_chars > cfg.max_writer_context_chars
    worker = ResearchWriter(settings=cfg)
    report = worker.write(context)
    assert worker.last_usage.llm_calls == 0 and worker.model is None
    assert {key for item in report.limitations for key in item.gap_ids} == {item.gap_id for item in gaps.all_gaps()}


@pytest.mark.parametrize("native", [True, False])
def test_one_primary_call_whole_report_traces_and_usage(native):
    store, _, _, _, _, context = fixture([("SUPPORTED", "ALIGNED")] * 3)
    model = ModelStub(context, native=native)
    worker = ResearchWriter(model, settings=settings())
    report = worker.write(context)
    assert len(model.requests) == worker.last_usage.llm_calls == 1
    assert worker.last_usage.input_tokens == 220 and worker.last_usage.output_tokens == 100
    assert worker.last_usage.used_claims == 3 and worker.last_usage.coverage == 1
    assert worker.last_usage.unused_claims == 0
    for key in report.used_claim_ids:
        assert store.get_evidence_for_claim(key)
        assert store.get_sources_for_claim(key)
    assert model.requests[0][1]["max_tokens"] == 4096 and model.requests[0][1]["timeout"] <= 60
    assert all("tools" not in options for _, options in model.requests)


def test_low_claim_coverage_warns_without_another_call(capsys):
    *_, context = fixture([("SUPPORTED", "ALIGNED")] * 5)
    model = ModelStub(context, mutate=lambda value: report_data(context, subset=2))
    worker = ResearchWriter(model, settings=settings())
    worker.write(context)
    assert worker.last_usage.coverage == 0.4 and worker.last_usage.unused_claims == 3
    assert "warning: Claim coverage" in capsys.readouterr().out
    assert len(model.requests) == 1


def test_schema_requires_all_seven_top_level_fields():
    *_, context = fixture()
    schema = ResearchReport.model_json_schema()
    assert set(schema["required"]) == {"title", "executive_summary", "scope", "key_findings", "sections", "limitations", "conclusion"}
    data = report_data(context)
    del data["sections"]
    with pytest.raises(ValidationError, match="sections"):
        ResearchWriter(ModelStub(context, mutate=lambda value: data), settings=settings()).write(context)


def test_model_omitting_limitations_retains_current_gaps_without_retry_or_research():
    store, verified, relevant, gaps, _, _ = fixture([("SUPPORTED", "ALIGNED"), ("PARTIAL", "ALIGNED")])
    for index, state in enumerate(["OPEN", "IMPROVED", "RESOLVED"]):
        gaps.add_gap(ResearchGap(task_id=f"D{index}", gap_type="COVERAGE_GAP", status=state,
                                description="Limited coverage", missing_information="Direct evidence needed"))
    context = WritingContextBuilder(settings=settings()).build("Goal", store, verified, relevant, gaps)
    model = ModelStub(context, mutate=lambda value: {**value, "limitations": []})
    worker = ResearchWriter(model, settings=settings())
    report = worker.write(context)
    assert {key for item in report.limitations for key in item.gap_ids} == {item["gap_id"] for item in context.payload["open_gaps"]}
    assert {key for item in report.limitations for key in item.partial_claim_ids} == {item["claim_id"] for item in context.payload["partial_claims_for_limitations_only"]}
    assert worker.last_usage.llm_calls == len(model.requests) == 1
    assert worker.last_usage.used_claims == 1
    assert [item.status.value for item in gaps.all_gaps()] == ["OPEN", "IMPROVED", "RESOLVED"]
    assert "补充材料" in " ".join(item.text for item in report.limitations)


@pytest.mark.parametrize("field", ["executive_summary", "key_findings", "conclusion", "sections"])
def test_unknown_claim_ids_detected_in_all_factual_sections(field):
    *_, context = fixture()
    data = report_data(context)
    if field == "sections":
        data[field][0]["paragraphs"] = [{"text": "Invented fact", "claim_ids": ["claim_999"]}]
    else:
        data[field] = [{"text": "Invented fact", "claim_ids": ["claim_999"]}]
    worker = ResearchWriter(ModelStub(context, mutate=lambda value: data), settings=settings())
    with pytest.raises(WriterOutputError, match="unknown or non-accepted"):
        worker.write(context)
    assert worker.last_usage.llm_calls == 1


@pytest.mark.parametrize("text", ["A fact [1]", "A fact [1, 2]", "A fact [^1]", "A fact (Author, 2025)",
    "Internal claim_999", "Internal RR_D1_1", "https://example.org/paper", "References"])
def test_no_citation_number_bibliography_or_internal_id_leak_in_prose(text):
    *_, context = fixture()
    data = report_data(context)
    data["scope"] = text
    with pytest.raises(WriterOutputError, match="formatting reserved"):
        validate_report(ResearchReport.model_validate(data), context)


@pytest.mark.parametrize("patch", [{"gap_ids": ["resolved-or-invented"]}, {"partial_claim_ids": ["invented"]},
                                  {"gap_ids": [], "partial_claim_ids": []}])
def test_limitations_require_current_supplied_references(patch):
    *_, context = fixture()
    data = report_data(context)
    data["limitations"] = [{"text": "Insufficient coverage", **patch}]
    with pytest.raises(WriterOutputError, match="limitation must reference"):
        validate_report(ResearchReport.model_validate(data), context)


def test_partial_claim_ids_are_only_allowed_in_limitations():
    *_, context = fixture([("SUPPORTED", "ALIGNED"), ("PARTIAL", "ALIGNED")])
    partial_id = context.payload["partial_claims_for_limitations_only"][0]["claim_id"]
    data = report_data(context)
    data["limitations"] = [{"text": "Current evidence is insufficient to establish this result.", "partial_claim_ids": [partial_id]}]
    validate_report(ResearchReport.model_validate(data), context)
    data["key_findings"] = [{"text": "Definite result", "claim_ids": [partial_id]}]
    with pytest.raises(WriterOutputError, match="non-accepted"):
        validate_report(ResearchReport.model_validate(data), context)


@pytest.mark.parametrize("refs", [[], ["a", "a"], [""]])
def test_schema_rejects_missing_or_duplicate_paragraph_references(refs):
    from app.schemas.report import ReportParagraph
    with pytest.raises(ValidationError):
        ReportParagraph(text="Fact", claim_ids=refs)


def test_renderer_hides_ids_and_no_references_but_debug_retains_trace():
    *_, context = fixture()
    report = ResearchReport.model_validate(report_data(context))
    ordinary, debug = render_report(report), render_report(report, debug=True)
    assert all(key not in ordinary for key in report.used_claim_ids)
    assert all(key in debug for key in report.used_claim_ids)
    assert all(heading in ordinary for heading in ["执行摘要", "研究范围", "主要发现", "详细分析", "局限性", "结论"])
    assert "References" not in ordinary and "[1]" not in ordinary


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("base_url", ["https://api.siliconflow.cn/v1", "https://example.org/v1"])
def test_http_structured_output_compatibility_limits_and_usage(native, base_url):
    *_, context = fixture()
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if not native and body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unsupported", "type": "invalid_request_error"}})
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(report_data(context))}}],
            "usage": {"prompt_tokens": 500, "completion_tokens": 300, "total_tokens": 800}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = ChatOpenAI(model="test", api_key="test", base_url=base_url, http_client=client, max_retries=0)
        worker = ResearchWriter(model, settings=settings(model_request_timeout_seconds=23, max_report_output_tokens=2500))
        worker.write(context)
    assert worker.last_usage.llm_calls == len(requests) == (1 if native else 2)
    assert worker.last_usage.input_tokens == 500 and worker.last_usage.output_tokens == 300
    for request, body in requests:
        assert not body.get("tools")
        field = "max_tokens" if "siliconflow" in base_url else "max_completion_tokens"
        assert body[field] == 2500
        assert request.extensions["timeout"]["read"] <= 23


def test_timeout_or_parse_failure_does_not_retry_or_publish_report():
    *_, context = fixture()
    class TimeoutModel(ModelStub):
        def invoke(self, *args, **kwargs):
            self.requests.append(args)
            raise APITimeoutError(request=httpx.Request("POST", "https://example.org/v1"))
    model = TimeoutModel(context)
    worker = ResearchWriter(model, settings=settings())
    with pytest.raises(APITimeoutError):
        worker.write(context)
    assert len(model.requests) == 1
    worker = ResearchWriter(ModelStub(context, mutate=lambda value: {"nonsense": "output"}), settings=settings())
    with pytest.raises(ValidationError):
        worker.write(context)
    assert worker.last_usage.llm_calls == 1


def test_writer_deadline_blocks_wait_and_discards_late_report():
    *_, context = fixture()
    release, done = Event(), Event()
    class SlowModel(ModelStub):
        def invoke(self, *args, **kwargs):
            try:
                release.wait(2)
                return super().invoke(*args, **kwargs)
            finally:
                done.set()
    worker = ResearchWriter(SlowModel(context), settings=settings(writer_timeout_seconds=0.04, research_progress_interval_seconds=0.01))
    try:
        with pytest.raises(ResearchTimeoutError):
            worker.write(context)
    finally:
        release.set()
        assert done.wait(2)
    assert worker.last_usage.llm_calls == 1


@pytest.mark.parametrize("writer_fails", [False, True])
def test_default_pipeline_writer_consumes_no_memo_and_leaves_every_research_record_unchanged(writer_fails, monkeypatch, capsys):
    from app.agents.relevance import RelevanceAssessor
    from app.agents.verifier import ClaimVerifier
    from app.research.gap_detection import GapDetector
    cfg = settings(max_reresearch_rounds=0)
    monkeypatch.setattr("app.research.orchestrator.get_settings", lambda: cfg)
    source_store, _, _, _, claims, context = fixture()
    claim = claims[0]
    source = source_store.get_sources_for_claim(claim.claim_id)[0]
    evidence = source_store.get_evidence_for_claim(claim.claim_id)
    task = ResearchTask(id="D1", title="Dimension", question="Original research goal", description="Research dimension")
    bundle = TaskEvidenceBundle(task_id="D1", sources=[source], evidence=evidence, claims=[claim])
    memo = "UNVERIFIED_MEMO_CONTAINS_REJECTED_FACTS"
    execution = ResearchExecutionResult(memo=memo, tools_used=[], evidence_bundle=bundle,
        compact=CompactResearchResult(task_id="D1", summary="COMPACT_NOT_ALLOWED"))
    class SupportModel:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, messages, **kwargs):
            items = json.loads(messages[1].text)["claims"]
            return {"results": [{"claim_id": item["claim_id"], "status": "SUPPORTED", "reason": "Direct support"} for item in items]}
    class AlignmentModel(SupportModel):
        def invoke(self, messages, **kwargs):
            items = json.loads(messages[1].text)["claims"]
            return {"results": [{"claim_id": item["claim_id"], "status": "ALIGNED", "reason": "Goal matches"} for item in items]}
    events = []
    original_reresearch = ResearchOrchestrator._run_reresearch
    async def observed_reresearch(self, *args, **kwargs):
        events.append("reresearch")
        return await original_reresearch(self, *args, **kwargs)
    monkeypatch.setattr(ResearchOrchestrator, "_run_reresearch", observed_reresearch)
    def research(**kwargs):
        events.append("research")
        return execution
    def returned_report(value):
        if writer_fails:
            value["conclusion"][0]["claim_ids"] = ["claim_invented"]
        return value
    writer_model = ModelStub(context, mutate=returned_report)
    worker = ResearchWriter(writer_model, settings=cfg)
    orchestrator = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: ResearchPlan(goal="Goal", tasks=[task])),
        SimpleNamespace(research=research), verifier=ClaimVerifier(SupportModel(), settings=cfg),
        gap_detector=GapDetector(RelevanceAssessor(AlignmentModel(), settings=cfg)), writer=worker,
        writing_context_builder=WritingContextBuilder(settings=cfg))
    if writer_fails:
        with pytest.raises(RuntimeError, match="Writer failed.*non-accepted"):
            orchestrator.run("Original research goal")
        assert events == ["research", "reresearch"]  # No restart after Writer rejects the report.
        assert worker.last_usage.llm_calls == 1
        output = capsys.readouterr().out
        assert "Re-Research Summary:" in output and "[Writer] failed" in output
        assert "FINAL RESEARCH REPORT" not in output
        return
    result = orchestrator.run("Original research goal")
    assert result.report is not None and result.report.used_claim_ids == [claim.claim_id]
    assert result.memos["D1"] == memo and result.compact_results["D1"].summary == "COMPACT_NOT_ALLOWED"
    assert result.evidence_store.get_claim(claim.claim_id) == claim
    assert result.evidence_store.get_evidence(evidence[0].evidence_id) == evidence[0]
    assert len(result.verification_store.get_verifications_for_claim(claim.claim_id)) == 1
    assert len(result.relevance_results) == 1 and not result.gap_store.all_gaps()
    assert memo not in result.writing_context.text and "COMPACT_NOT_ALLOWED" not in result.writing_context.text
    assert "Original research goal" in result.writing_context.text
    output = capsys.readouterr().out
    assert output.index("Re-Research Summary:") < output.index("[Writer] context:") < output.index("FINAL RESEARCH REPORT")
    assert claim.claim_id not in output.split("FINAL RESEARCH REPORT", 1)[1]


def test_writer_model_factory_uses_research_provider_without_sdk_retries(monkeypatch):
    import app.llm as module
    monkeypatch.setattr(module, "get_settings", lambda: settings(model_name="qwen3-flash", model_api_key="test",
        model_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1", model_max_retries=2,
        verifier_model_name="other", verifier_model_api_key="verifier-test", verifier_model_base_url="https://verifier.example/v1"))
    model = module.create_writer_llm()
    assert model.model_name == "qwen3-flash"
    assert model.openai_api_base == "https://dashscope.aliyuncs.com/compatible-mode/v1"
    assert model.max_retries == 0 and model.extra_body == {"enable_thinking": False}


def test_no_topics_or_tools_hardcoded_and_no_memo_builder_input():
    import inspect
    assert "memo" not in inspect.signature(WritingContextBuilder.build).parameters
    for path in ["app/prompts/writer.md", "app/agents/writer.py", "app/research/writing_context.py"]:
        text = Path(path).read_text(encoding="utf-8").lower()
        assert all(word not in text for word in ["atari", "offline rl", "sparse neural"])
    text = Path("app/agents/writer.py").read_text(encoding="utf-8")
    assert "app.tools" not in text and "create_deep_agent" not in text
