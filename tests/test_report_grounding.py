"""Report semantics cannot inherit a benchmark or a setting from the task goal."""

import json
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from app.agents.report_grounding import ReportGroundingChecker
from app.agents.writer import ResearchWriter
from app.config import Settings
from app.research.writing_context import WritingClaim, WritingContext
from app.schemas.report import ResearchReport


def context_for(*texts, goal="Atari sparse reward offline RL", gaps=None, partial=None):
    claims = tuple(WritingClaim(f"claim_{i}", text, (f"ev_{i}",), (f"src_{i}",), "D1") for i, text in enumerate(texts))
    payload = {"goal": goal, "scope_dimensions": [goal], "accepted_claims": [asdict(c) for c in claims],
               "evidence": {}, "sources": {}, "open_gaps": gaps or [], "partial_claims_for_limitations_only": partial or []}
    return WritingContext(goal, claims, json.dumps(payload, ensure_ascii=False))


def report_for(context, text, *, heading="研究发现", refs=None):
    paragraph = {"text": text, "claim_ids": refs or [context.accepted_claims[0].claim_id]}
    return ResearchReport.model_validate(dict(title="该方法已经解决所有问题", scope="已经证明全部有效",
        executive_summary=[paragraph], key_findings=[paragraph], sections=[{"heading": heading, "paragraphs": [paragraph]}],
        limitations=[], conclusion=[paragraph]))


class Judge:
    def __init__(self, status="SUPPORTED", mutate=lambda value: value, *, native=True, error=None):
        self.status, self.mutate, self.native, self.error = status, mutate, native, error
        self.requests = []

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema"}
        self.schema = schema
        if not self.native:
            raise NotImplementedError
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.requests.append((messages, kwargs))
        if self.error:
            raise self.error
        raw = AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 50, "total_tokens": 150})
        for callback in (config or {}).get("callbacks", []):
            callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=raw)]]))
        items = json.loads(messages[1].text)["items"]
        result = self.mutate({"results": [{"item_index": item["item_index"], "status": self.status, "reason": "test decision"}
                                         for item in items]})
        return result if self.native else AIMessage(content=json.dumps(result))


def checker(model=None, **kwargs):
    return ReportGroundingChecker(model or Judge(), settings=Settings(_env_file=None, **kwargs))


@pytest.mark.parametrize("addition", ["Atari 游戏", "连续控制任务", "offline RL", "离线强化学习", "MuJoCo", "SOTA", "2026 年", "95%"])
def test_missing_qualifiers_blocked_even_if_model_approves(addition):
    context = context_for("DISCOVER 在高维环境中表现出色。")
    bad = f"DISCOVER 在{addition}中表现出色。"
    gate = checker()
    result = gate.check(report_for(context, bad, heading=f"DISCOVER 在{addition}中的应用"), context)
    assert all(p.text == context.accepted_claims[0].text for p in result.factual_paragraphs())
    assert result.sections[0].heading == "研究发现"
    assert gate.last_usage.repaired_items == 5
    assert all(a.repaired for a in gate.last_audit)


def test_explicit_benchmark_and_setting_kept_and_goal_not_used_as_support():
    context = context_for("DISCOVER 在 Atari 离线强化学习实验中表现出色。", goal="SECRET_GOAL benchmark")
    gate = checker()
    result = gate.check(report_for(context, "DISCOVER 在 Atari 离线强化学习实验中表现出色。",
                                   heading="DISCOVER 在 Atari 离线强化学习实验中表现出色"), context)
    assert result.sections[0].heading == "DISCOVER 在 Atari 离线强化学习实验中表现出色"
    assert gate.last_usage.repaired_items == 0
    assert not gate.model.requests  # all statements preserve the cited Claim verbatim
    assert "已经解决" not in result.title and "研究目标" in result.scope


@pytest.mark.parametrize("status", ["UNSUPPORTED", "UNCERTAIN"])
def test_semantic_rejection_repairs_every_occurrence_with_complete_cited_claims(status):
    context = context_for("方法甲改善探索。", "方法乙降低计算开销。")
    gate = checker(Judge(status))
    report = report_for(context, "方法甲降低计算开销，因此一定改善所有环境。", heading="方法甲的计算优势",
                        refs=[item.claim_id for item in context.accepted_claims])
    result = gate.check(report, context)
    assert all(p.text == "方法甲改善探索。\n\n方法乙降低计算开销。" for p in result.factual_paragraphs())
    assert result.used_claim_ids == ["claim_0", "claim_1"]
    assert len(gate.model.requests) == 1  # repeated paragraphs are deduplicated
    assert len(json.loads(gate.model.requests[0][0][1].text)["items"]) == 1


def test_only_cited_claims_enter_each_units_support_indexes():
    context = context_for("方法甲改善探索。", "方法乙在 Atari 中表现出色。")
    gate = checker()
    result = gate.check(report_for(context, "方法甲在 Atari 中表现出色。"), context)
    assert result.executive_summary[0].text == "方法甲改善探索。"
    assert not gate.model.requests  # lexical scope guard rejects before an API request


@pytest.mark.parametrize("mode", ["missing", "duplicate", "unknown", "invalid_status", "bad_json", "timeout"])
def test_invalid_or_unavailable_judge_fails_back_without_retry(mode):
    def mutate(value):
        if mode == "missing":
            value["results"] = []
        if mode == "duplicate":
            value["results"].append(value["results"][0])
        if mode == "unknown":
            value["results"][0]["item_index"] = 999
        if mode == "invalid_status":
            value["results"][0]["status"] = "YES"
        if mode == "bad_json":
            return {"invalid": True}
        return value
    model = Judge(mutate=mutate, error=TimeoutError("test timeout") if mode == "timeout" else None)
    gate = checker(model)
    context = context_for("方法改善探索。")
    result = gate.check(report_for(context, "该方法已经彻底解决探索问题。"), context)
    assert result.executive_summary[0].text == "方法改善探索。"
    assert gate.last_usage.fallback_reason
    assert gate.last_usage.llm_calls == len(model.requests) == 1


@pytest.mark.parametrize("native", [True, False])
def test_one_batch_schema_timeout_usage_and_no_tools(native):
    model = Judge(native=native)
    gate = checker(model, model_request_timeout_seconds=23, report_grounding_max_tokens=1800)
    context = context_for("方法改善探索。")
    original = report_for(context, "该方法有助于探索。")
    result = gate.check(original, context)
    assert result.executive_summary == original.executive_summary
    assert gate.last_usage.llm_calls == 1
    assert gate.last_usage.input_tokens == 100 and gate.last_usage.output_tokens == 50
    assert gate.last_usage.context_chars < 1000
    options = model.requests[0][1]
    assert options["max_tokens"] == 1800 and options["timeout"] <= 23
    assert "tools" not in options


def test_valid_combination_keeps_separate_method_attributes():
    context = context_for("方法甲改善探索。", "方法乙降低计算开销。")
    text = "方法甲改善探索，方法乙降低计算开销。"
    gate = checker()
    result = gate.check(report_for(context, text, refs=["claim_0", "claim_1"]), context)
    assert result.executive_summary[0].text == text and gate.last_usage.repaired_items == 0
    sent = json.loads(gate.model.requests[0][0][1].text)
    assert len(sent["supports"]) == 2 and sent["items"][0]["support_indexes"] == [0, 1]
    assert "Atari" not in gate.model.requests[0][0][1].text  # goal metadata never becomes support


def test_assertive_heading_is_neutralized_even_if_judge_would_approve_it():
    context = context_for("方法甲改善探索。", "方法乙降低计算开销。")
    gate = checker(Judge("SUPPORTED"))
    result = gate.check(report_for(context, "方法甲改善探索。", heading="方法甲的计算效果", refs=["claim_0", "claim_1"]), context)
    assert result.sections[0].heading == "研究发现"
    assert not gate.model.requests
    assert next(a for a in gate.last_audit if a.path.endswith("heading")).repaired


def test_fallback_does_not_truncate_an_oversized_claim_or_publish_empty_fact():
    context = context_for("完整论述。" * 250)
    gate = checker(Judge("UNSUPPORTED"))
    with pytest.raises(ValueError, match="complete cited Claim"):
        gate.check(report_for(context, "结论有效。"), context)


def test_zero_accepted_claims_never_initialize_either_model(monkeypatch):
    context = context_for()
    monkeypatch.setattr("app.agents.writer.create_writer_llm", lambda: pytest.fail("unexpected writer request"))
    monkeypatch.setattr("app.agents.report_grounding.create_verifier_llm", lambda: pytest.fail("unexpected judge request"))
    writer = ResearchWriter(settings=Settings(_env_file=None))
    result = writer.write(context)
    assert not result.used_claim_ids and writer.last_usage.llm_calls == writer.last_usage.grounding.llm_calls == 0


@pytest.mark.parametrize("native", [True, False])
def test_truncated_judge_response_cannot_publish_unsupported_prose(native):
    class Truncated(Judge):
        def invoke(self, messages, config=None, **kwargs):
            self.requests.append(messages)
            raw = AIMessage(content='{"results":', response_metadata={"finish_reason": "length"})
            for callback in config["callbacks"]:
                callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=raw)]]))
            return {} if native else raw
    gate = checker(Truncated(native=native))
    context = context_for("方法改善探索。")
    result = gate.check(report_for(context, "方法解决全部探索问题。"), context)
    assert result.executive_summary[0].text == context.accepted_claims[0].text
    assert "truncated" in gate.last_usage.fallback_reason and gate.last_usage.llm_calls == 1


def test_oversized_context_sends_no_request_and_preserves_complete_claim():
    context = context_for("方法改善探索。" * 30)
    model = Judge()
    gate = checker(model, max_report_grounding_context_chars=500)
    report = report_for(context, "方法对研究有所帮助。" * 30)
    result = gate.check(report, context)
    assert not model.requests
    assert result.executive_summary[0].text == context.accepted_claims[0].text
    assert "limit" in gate.last_usage.fallback_reason


def test_limitations_cannot_turn_gap_into_positive_or_field_wide_fact():
    context = context_for("方法改善探索。", gaps=[{"gap_id": "gap_1", "status": "OPEN", "gap_type": "COVERAGE_GAP",
        "description": "当前材料未覆盖实验环境", "missing_information": "直接实验依据"}])
    report = report_for(context, "方法改善探索。")
    data = report.model_dump()
    data["limitations"] = [{"text": "整个领域没有该方向的研究。", "gap_ids": ["gap_1"], "partial_claim_ids": []}]
    gate = checker(Judge("UNSUPPORTED"))
    result = gate.check(ResearchReport.model_validate(data), context)
    assert "整个领域" not in result.limitations[0].text
    assert result.limitations[0].gap_ids == ["gap_1"]
    assert "当前收集材料" in result.limitations[0].text


def test_grounding_deadline_returns_fallback_and_no_followup_calls():
    release, done = Event(), Event()
    class Slow(Judge):
        def invoke(self, *args, **kwargs):
            try:
                release.wait(2)
                return super().invoke(*args, **kwargs)
            finally:
                done.set()
    gate = checker(Slow(), report_grounding_timeout_seconds=0.04, research_progress_interval_seconds=0.01)
    context = context_for("方法改善探索。")
    try:
        result = gate.check(report_for(context, "方法完全解决探索。"), context)
        assert result.executive_summary[0].text == "方法改善探索。"
        assert "ResearchTimeoutError" in gate.last_usage.fallback_reason
        assert gate.last_usage.llm_calls == 1
    finally:
        release.set()
        assert done.wait(2)


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("host", ["https://api.siliconflow.cn/v1", "https://example.org/v1"])
def test_real_http_boundary_has_no_tools_and_correct_output_limit(native, host):
    requests = []
    def handler(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if not native and body.get("response_format"):
            return httpx.Response(400, json={"error": {"message": "json_schema unsupported", "type": "invalid_request_error"}})
        items = json.loads(body["messages"][1]["content"])["items"]
        result = {"results": [{"item_index": i["item_index"], "status": "SUPPORTED", "reason": "支持"} for i in items]}
        return httpx.Response(200, json={"id": "x", "object": "chat.completion", "created": 0, "model": "test",
            "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}})
    context = context_for("方法改善探索。")
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = ChatOpenAI(model="test", api_key="test", base_url=host, http_client=client, max_retries=0)
        gate = checker(model, model_request_timeout_seconds=23, report_grounding_max_tokens=1800)
        gate.check(report_for(context, "该方法有助于探索。"), context)
    assert len(requests) == gate.last_usage.llm_calls == (1 if native else 2)
    assert gate.last_usage.input_tokens == 100 and gate.last_usage.output_tokens == 50
    for request, body in requests:
        assert not body.get("tools")
        key = "max_tokens" if "siliconflow" in host else "max_completion_tokens"
        assert body[key] == 1800 and request.extensions["timeout"]["read"] <= 23


def test_default_writer_gate_repairs_and_recomputes_coverage_without_research(monkeypatch):
    context = context_for("方法甲改善探索。", "方法乙降低开销。" + "完整论述。" * 240)
    generated = report_for(context, "方法甲在 Atari 中降低开销。", heading="方法甲的 Atari 表现", refs=["claim_0", "claim_1"])
    class WriterModel:
        def with_structured_output(self, *args, **kwargs):
            return self
        def invoke(self, *args, **kwargs):
            return generated.model_dump()
    monkeypatch.setattr("app.agents.report_grounding.create_verifier_llm", lambda: Judge())
    writer = ResearchWriter(WriterModel(), settings=Settings(_env_file=None))
    result = writer.write(context)
    assert result.used_claim_ids == ["claim_0"]  # long Claim is omitted whole, never cut mid-statement
    assert writer.last_usage.used_claims == 1 and writer.last_usage.coverage == 0.5
    assert writer.last_usage.llm_calls == 1
    assert writer.last_grounding and writer.last_usage.grounding.repaired_items == 5
    assert context.accepted_claims[1].text.endswith("完整论述。")
