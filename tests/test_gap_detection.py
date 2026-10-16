"""Phase 4B: support reuse, independent alignment, strict coverage and bounded cost."""

import json
from collections import Counter
from pathlib import Path
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from app.agents.relevance import RelevanceAssessor, RelevanceOutputError, RelevanceTimeoutError
from app.agents.verifier import VerificationUsage
from app.config import Settings
from app.research.gap_detection import GapDetector, GapIntegrityError, generate_research_gaps
from app.research.gap_store import GapStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.relevance_context import (
    RelevanceContextLimitError, RelevanceIntegrityError, build_relevance_context,
)
from app.research.verification_store import VerificationStore
from app.schemas import Claim, CompactResearchResult, Evidence, ResearchExecutionResult, ResearchPlan, ResearchTask, Source, TaskEvidenceBundle
from app.schemas.gap import GapType, ResearchGap
from app.schemas.relevance import ClaimRelevanceResult
from app.schemas.verification import VerificationResult


def task(key="D1", kind="discovery", dependencies=()):
    return ResearchTask(id=key, title=f"Objective {key}", question=f"Which result addresses {key}?",
                        description=f"Research dimension {key}", task_type=kind, depends_on=list(dependencies))


def claim(key="D1", text="A relevant finding"):
    return Claim(task_id=key, text=text, evidence_ids=["FORMAL_EVIDENCE_ID_NOT_SENT"])


def support(item, status="SUPPORTED", reason="Direct support."):
    return VerificationResult(claim_id=item.claim_id, task_id=item.task_id, evidence_ids=item.evidence_ids,
                              status=status, reason=reason)


def alignment(item, status="ALIGNED", reason="Directly addresses both objectives."):
    return ClaimRelevanceResult(claim_id=item.claim_id, task_id=item.task_id, status=status, reason=reason)


class ModelStub:
    def __init__(self, statuses=None, mutate=None):
        self.statuses = statuses or {}
        self.mutate = mutate or (lambda value: value)
        self.requests = []

    def with_structured_output(self, schema, **kwargs):
        assert kwargs == {"method": "json_schema"}
        self.schema = schema
        return self

    def invoke(self, messages, config=None, **options):
        self.requests.append((messages, options))
        data = json.loads(messages[1].text)
        results = [{"claim_id": item["claim_id"], "status": self.statuses.get(item["text"], "ALIGNED"),
                    "reason": "Checks the full goal and this task objective."} for item in reversed(data["claims"])]
        raw = AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 40, "total_tokens": 140})
        for callback in (config or {}).get("callbacks", []):
            callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=raw)]]))
        return self.mutate({"results": results})


def assessor(model=None, **settings):
    return RelevanceAssessor(model or ModelStub(), settings=Settings(_env_file=None, **settings))


@pytest.mark.parametrize("verification,relevance,expected", [
    ("SUPPORTED", "ALIGNED", set()),
    ("PARTIAL", "ALIGNED", {"SUPPORT_GAP", "COVERAGE_GAP"}),
    ("UNSUPPORTED", "ALIGNED", {"SUPPORT_GAP", "COVERAGE_GAP"}),
    ("SUPPORTED", "MISALIGNED", {"RELEVANCE_GAP", "COVERAGE_GAP"}),
    ("SUPPORTED", "PARTIAL", {"RELEVANCE_GAP", "COVERAGE_GAP"}),
    ("PARTIAL", "MISALIGNED", {"SUPPORT_GAP", "RELEVANCE_GAP", "COVERAGE_GAP"}),
    ("UNSUPPORTED", "PARTIAL", {"SUPPORT_GAP", "RELEVANCE_GAP", "COVERAGE_GAP"}),
])
def test_gap_rules_and_supported_misaligned_case(verification, relevance, expected):
    item = claim()
    store = generate_research_gaps([task()], [item], {item.claim_id: support(item, verification)},
                                  {item.claim_id: alignment(item, relevance)})
    assert {gap.gap_type.value for gap in store.all_gaps()} == expected
    for gap in store.all_gaps():
        assert gap.claim_id == (None if gap.gap_type == GapType.COVERAGE_GAP else item.claim_id)
        assert set(gap.model_dump()) == {"gap_id", "gap_type", "task_id", "claim_id", "description", "missing_information", "status"}


def test_all_misaligned_claims_only_one_task_coverage_gap():
    claims = [claim(text=f"Different phenomenon {index}") for index in range(3)]
    store = generate_research_gaps([task()], claims, {item.claim_id: support(item) for item in claims},
                                  {item.claim_id: alignment(item, "MISALIGNED") for item in claims})
    assert len(store.get_gaps_by_type("RELEVANCE_GAP")) == 3
    assert len(store.get_gaps_by_type("COVERAGE_GAP")) == 1


def test_one_supported_aligned_claim_is_sufficient_for_task_coverage():
    first, second = claim(text="Core result"), claim(text="Peripheral result")
    store = generate_research_gaps([task()], [first, second],
        {first.claim_id: support(first), second.claim_id: support(second, "PARTIAL")},
        {first.claim_id: alignment(first), second.claim_id: alignment(second, "MISALIGNED")})
    assert not store.get_gaps_by_type("COVERAGE_GAP")
    assert len(store.all_gaps()) == 2


def test_no_claims_need_no_model_and_produce_one_gap_per_task(monkeypatch):
    monkeypatch.setattr("app.agents.relevance.create_verifier_llm", lambda: pytest.fail("No model is needed"))
    worker = RelevanceAssessor(settings=Settings(_env_file=None))
    result = GapDetector(worker).detect(ResearchPlan(goal="Original goal", tasks=[task(), task("D2")]), [], VerificationStore())
    assert result.relevance_results == {} and result.relevance_usage.llm_calls == 0
    assert worker.model is None
    assert len(result.gap_store.all_gaps()) == 2
    assert all(item.gap_type == GapType.COVERAGE_GAP for item in result.gap_store.all_gaps())


def test_mixed_empty_and_nonempty_tasks_only_send_nonempty_task():
    item = claim()
    verified = VerificationStore()
    verified.add_verification(support(item))
    model = ModelStub()
    result = GapDetector(assessor(model)).detect(ResearchPlan(goal="Goal", tasks=[task(), task("D2")]), [item], verified)
    assert result.relevance_usage.llm_calls == 1
    payload = json.loads(model.requests[0][0][1].text)
    assert set(payload["tasks"]) == {"D1"}
    assert result.gap_store.get_gaps_for_task("D2")[0].gap_type == GapType.COVERAGE_GAP


def test_run_wide_ten_claims_four_tasks_one_relevance_call(capsys):
    tasks = [task(), task("D2"), task("A", "analysis", ["D1", "D2"]), task("S", "synthesis", ["A"])]
    claims = [claim(tasks[index % 4].id, text=f"Finding {index}") for index in range(10)]
    model = ModelStub()
    worker = assessor(model)
    results = worker.assess_batch("Original exact user goal", tasks, claims)
    assert len(results) == 10 and len(model.requests) == worker.last_usage.llm_calls == 1
    assert [item.claim_id for item in results] == [item.claim_id for item in claims]
    assert {item.task_id for item in results} == {"D1", "D2", "A", "S"}
    assert len({item.relevance_id for item in results}) == 10
    assert worker.last_usage.input_tokens == 100 and worker.last_usage.output_tokens == 40
    assert "claims=10 tasks=4" in capsys.readouterr().out


def test_context_contains_only_goal_task_objectives_and_claim_text():
    tasks = [task(), task("UNRELATED")]
    claims = [claim(text="Finding A"), claim(text="Finding B")]
    context = build_relevance_context("Original exact goal", tasks, claims)
    payload = context.payload
    assert set(payload) == {"research_goal", "tasks", "claims"}
    assert set(payload["tasks"]) == {"D1"}
    assert set(payload["tasks"]["D1"]) == {"title", "question", "description"}
    assert all(set(item) == {"claim_id", "task_id", "text"} for item in payload["claims"])
    assert context.text.count(tasks[0].description) == 1
    assert context.text.count("Original exact goal") == 1
    for forbidden in ["FORMAL_EVIDENCE_ID_NOT_SENT", "memo", "evidence", "sources", "UNRELATED"]:
        assert forbidden not in context.text
    assert list(context.claim_ids.values()) == [item.claim_id for item in claims]


def test_thirteen_claims_split_twelve_and_one_without_claim_loss():
    claims = [claim(text=f"Finding {index}") for index in range(13)]
    model = ModelStub()
    worker = assessor(model)
    assert len(worker.assess_batch("Goal", [task()], claims)) == 13
    assert worker.last_usage.llm_calls == 2
    assert [len(json.loads(messages[1].text)["claims"]) for messages, _ in model.requests] == [12, 1]


def test_context_budget_splits_without_truncating_claim_goal_or_objective():
    claims = [claim(text=str(index) + "x" * 700) for index in range(3)]
    goal = "Original goal " + "g" * 100
    tasks = [task()]
    limit = max(build_relevance_context(goal, tasks, [item]).context_chars for item in claims)
    model = ModelStub()
    worker = assessor(model, max_relevance_context_chars=limit)
    results = worker.assess_batch(goal, tasks, claims)
    assert len(results) == 3 and worker.last_usage.llm_calls == 3
    for item, (messages, _) in zip(claims, model.requests):
        payload = json.loads(messages[1].text)
        assert payload["claims"][0]["text"] == item.text
        assert payload["research_goal"] == goal
        assert payload["tasks"]["D1"]["description"] == tasks[0].description
        assert len(messages[1].text) <= limit


def test_one_oversize_claim_fails_before_all_calls():
    worker = assessor(max_relevance_context_chars=500)
    with pytest.raises(RelevanceContextLimitError, match="not truncated"):
        worker.assess_batch("Goal", [task()], [claim(), claim(text="x" * 1000)])
    assert worker.last_usage.llm_calls == 0


@pytest.mark.parametrize("kind", ["missing", "wrong_task", "wrong_claim", "wrong_evidence"])
def test_incomplete_or_stale_support_results_fail_before_relevance(kind):
    item = claim()
    verified = VerificationStore()
    result = support(item)
    if kind != "missing":
        patch = {"wrong_task": {"task_id": "other"}, "wrong_claim": {"claim_id": "other"},
                 "wrong_evidence": {"evidence_ids": ["other"]}}[kind]
        verified.add_verification(result.model_copy(update=patch))
    worker = assessor()
    with pytest.raises(GapIntegrityError):
        GapDetector(worker).detect(ResearchPlan(goal="Goal", tasks=[task()]), [item], verified)
    assert worker.last_usage.llm_calls == 0


def test_latest_verification_is_consumed_without_reverification():
    item = claim()
    verified = VerificationStore()
    verified.add_verification(support(item, "UNSUPPORTED"))
    verified.add_verification(support(item, "SUPPORTED"))
    before = verified.get_verifications_for_claim(item.claim_id)
    result = GapDetector(assessor()).detect(ResearchPlan(goal="Goal", tasks=[task()]), [item], verified)
    assert result.gap_store.all_gaps() == []
    assert verified.get_verifications_for_claim(item.claim_id) == before
    assert result.relevance_usage.llm_calls == 1


def test_failed_precheck_after_a_prior_run_does_not_report_stale_usage():
    item = claim()
    verified = VerificationStore()
    verified.add_verification(support(item))
    detector = GapDetector(assessor())
    plan = ResearchPlan(goal="Goal", tasks=[task()])
    assert detector.detect(plan, [item], verified).relevance_usage.llm_calls == 1
    with pytest.raises(GapIntegrityError):
        detector.detect(plan, [item], VerificationStore())
    assert detector.assessor.last_usage.llm_calls == 0


def test_gap_text_uses_existing_reasons_and_task_objectives_without_queries():
    item = claim()
    store = generate_research_gaps([task()], [item],
        {item.claim_id: support(item, "PARTIAL", "Only one benchmark and no baseline comparison.")},
        {item.claim_id: alignment(item, "MISALIGNED", "Different population from the requested task.")})
    support_gap = store.get_gaps_by_type("SUPPORT_GAP")[0]
    relevance_gap = store.get_gaps_by_type("RELEVANCE_GAP")[0]
    assert "no baseline comparison" in support_gap.missing_information
    assert "Different population" in relevance_gap.description
    assert task().question in relevance_gap.missing_information
    assert all("search_query" not in item.model_dump() and "research_task" not in item.model_dump() for item in store.all_gaps())


def test_gap_store_dedup_queries_and_no_history_lifecycle():
    store = GapStore()
    original = ResearchGap(gap_type="RELEVANCE_GAP", task_id="D1", claim_id="c", description="Different topic.", missing_information="Core task findings.")
    saved = store.add_gap(original)
    duplicate = ResearchGap(gap_type="RELEVANCE_GAP", task_id="D1", claim_id="c", description="Repeated topic gap.", missing_information="Core task findings.")
    assert store.add_gap(duplicate) == saved
    coverage = ResearchGap(gap_type="COVERAGE_GAP", task_id="D1", description="No effective claim.", missing_information="Task objective.")
    store.add_gap(coverage)
    store.add_gap(coverage.model_copy(update={"gap_id": "new-coverage-id"}))
    assert len(store.all_gaps()) == len(store.get_gaps_for_task("D1")) == 2
    assert store.get_gaps_for_claim("c") == [saved]
    assert store.get_gap(saved.gap_id) == saved and store.get_gap(saved.gap_id) is not saved
    assert store.get_gaps_by_type(GapType.COVERAGE_GAP) == [coverage]
    with pytest.raises(KeyError, match="Unknown gap_id"):
        store.get_gap("missing")
    with pytest.raises(ValueError, match="Conflicting gap_id"):
        store.add_gap(original.model_copy(update={"description": "Changed"}))


@pytest.mark.parametrize("patch", [
    {"claim_id": "c", "gap_type": "COVERAGE_GAP"}, {"claim_id": None, "gap_type": "SUPPORT_GAP"},
    {"search_query": "query"}, {"resolved": True},
])
def test_gap_schema_rejects_invalid_scope_or_query_lifecycle(patch):
    with pytest.raises(ValidationError):
        ResearchGap.model_validate({"gap_type": "RELEVANCE_GAP", "task_id": "D1", "claim_id": "c",
                                    "description": "Gap", "missing_information": "Need direct findings", **patch})


@pytest.mark.parametrize("mutate", [
    lambda value: {"results": value["results"][:-1]},
    lambda value: {"results": value["results"] + value["results"][:1]},
    lambda value: {"results": [{**value["results"][0], "claim_id": "invented"}, *value["results"][1:]]},
])
def test_incomplete_duplicate_or_unknown_relevance_labels_fail_closed(mutate):
    worker = assessor(ModelStub(mutate=mutate))
    with pytest.raises(RelevanceOutputError, match="exactly one decision"):
        worker.assess_batch("Goal", [task()], [claim(), claim(text="Second")])
    assert worker.last_usage.llm_calls == 1


@pytest.mark.parametrize("patch", [{"status": "SUPPORTED"}, {"status": "TRUE"}, {"reason": "x" * 321},
                                   {"reason": " "}, {"search_query": "query"}, {"confidence": 0.9}])
def test_relevance_schema_rejects_support_status_long_reason_and_extra_fields(patch):
    worker = assessor(ModelStub(mutate=lambda value: {"results": [{**value["results"][0], **patch}]}))
    with pytest.raises(ValidationError):
        worker.assess_batch("Goal", [task()], [claim()])


def test_input_duplicate_claims_are_free_but_conflicts_and_unknown_tasks_fail():
    item = claim()
    worker = assessor()
    assert len(worker.assess_batch("Goal", [task()], [item, item])) == 1
    for bad in [item.model_copy(update={"text": "Changed"}), item.model_copy(update={"task_id": "unknown"})]:
        with pytest.raises(RelevanceIntegrityError):
            worker.assess_batch("Goal", [task()], [item, bad])
        assert worker.last_usage.llm_calls == 0


def response_for(body, finish_reason="stop"):
    data = json.loads(body["messages"][1]["content"])
    result = {"results": [{"claim_id": item["claim_id"], "status": "ALIGNED", "reason": "Directly addresses both objectives."}
                          for item in data["claims"]]}
    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": {"role": "assistant", "content": json.dumps(result)}}],
        "usage": {"prompt_tokens": 400, "completion_tokens": 80, "total_tokens": 480}})


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("url", ["https://api.siliconflow.cn/v1", "https://provider.example/v1"])
def test_http_output_limit_timeout_no_tools_and_compatibility_once(native, url):
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if not native and body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unavailable", "type": "invalid_request_error"}})
        return response_for(body)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url=url, http_client=client,
                           max_retries=0, extra_body={"preserved": True})
        worker = assessor(model)
        assert len(worker.assess_batch("Goal", [task()], [claim(text=f"Finding {i}") for i in range(13)])) == 13
        assert model.extra_body == {"preserved": True}
    assert worker.last_usage.llm_calls == len(requests) == (2 if native else 3)
    assert worker.last_usage.input_tokens == 800 and worker.last_usage.output_tokens == 160
    assert worker.last_usage.context_chars == sum(len(body["messages"][1]["content"]) for _, body in requests)
    for request, body in requests:
        assert "tools" not in body and "tool_choice" not in body
        assert body["max_tokens" if "siliconflow" in url else "max_completion_tokens"] == 3072
        assert body["preserved"] is True
        assert 0 < request.extensions["timeout"]["read"] <= 60
        assert "FORMAL_EVIDENCE_ID_NOT_SENT" not in body["messages"][1]["content"]


@pytest.mark.parametrize("failure", ["timeout", "length", "auth", "rate_limit", "connection"])
def test_only_api_timeouts_retry_once_without_generating_fake_alignment(failure):
    from openai import APIConnectionError, AuthenticationError, RateLimitError
    requests = []
    def transport(request):
        requests.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("Slow relevance", request=request)
        if failure == "auth":
            return httpx.Response(401, json={"error": {"message": "Unauthorized", "type": "authentication_error"}})
        if failure == "rate_limit":
            return httpx.Response(429, json={"error": {"message": "Rate limit", "type": "rate_limit_error"}})
        if failure == "connection":
            raise httpx.ConnectError("Unavailable", request=request)
        return response_for(json.loads(request.content), "length")
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        worker = assessor(model)
        with pytest.raises({"timeout": RelevanceTimeoutError, "length": RelevanceOutputError,
                            "auth": AuthenticationError, "rate_limit": RateLimitError,
                            "connection": APIConnectionError}[failure]):
            worker.assess_batch("Goal", [task()], [claim()])
    assert len(requests) == worker.last_usage.llm_calls == (2 if failure == "timeout" else 1)


@pytest.mark.parametrize("outcome", ["recovered", "timeout", "invalid"])
def test_second_batch_timeout_only_retries_its_three_claims(outcome, capsys):
    claims = [claim(text=f"Finding {index}") for index in range(15)]
    verified = VerificationStore()
    for item in claims:
        verified.add_verification(support(item))
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 2 or (len(requests) == 3 and outcome == "timeout"):
            raise httpx.ReadTimeout("Transient relevance timeout", request=request)
        response = response_for(body)
        if len(requests) == 3 and outcome == "invalid":
            payload = response.json()
            message = payload["choices"][0]["message"]
            data = json.loads(message["content"])
            data["results"] = data["results"][:1]
            message["content"] = json.dumps(data)
            return httpx.Response(200, json=payload)
        return response

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        worker = assessor(model)
        detector = GapDetector(worker)
        plan = ResearchPlan(goal="Goal", tasks=[task()])
        if outcome == "recovered":
            result = detector.detect(plan, claims, verified)
            assert set(result.relevance_results) == {item.claim_id for item in claims}
            assert result.gap_store.all_gaps() == []
            assert worker.last_usage.input_tokens == 800
            assert worker.last_usage.output_tokens == 160
            assert worker.last_usage.usage_responses == 2
        else:
            error_type = RelevanceTimeoutError if outcome == "timeout" else RelevanceOutputError
            with pytest.raises(error_type) as caught:
                detector.detect(plan, claims, verified)
            if outcome == "timeout":
                assert "batch 2/2" in str(caught.value) and "retry=1/1" in str(caught.value)
    assert len(requests) == worker.last_usage.llm_calls == 3
    assert worker.last_usage.batches == 2
    data = [json.loads(body["messages"][1]["content"]) for body in requests]
    assert [len(item["claims"]) for item in data] == [12, 3, 3]
    assert requests[1] == requests[2]
    assert worker._native_available is True  # A timeout does not downgrade the schema interface.
    assert worker.last_usage.context_chars == sum(len(body["messages"][1]["content"]) for body in requests)
    output = capsys.readouterr().out
    assert "relevance batch 1/2: claims=12" in output
    assert "relevance batch 2/2: claims=3" in output
    assert "retry=0/1" in output and "retry=1/1" in output and "remaining=" in output
    if outcome != "recovered":
        assert "[Gap Detection] result:" not in output


def test_timeout_retry_reserves_time_without_resetting_the_shared_deadline(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("app.research.runtime.monotonic", lambda: clock[0])
    timeouts = []

    class SlowFirstRequest(ModelStub):
        def invoke(self, *args, **kwargs):
            timeouts.append(kwargs["timeout"])
            if len(timeouts) == 1:
                clock[0] += kwargs["timeout"] + 1  # Timeout plus response/exception overhead.
                raise httpx.ReadTimeout("Slow first attempt")
            return super().invoke(*args, **kwargs)

    worker = assessor(SlowFirstRequest(), model_request_timeout_seconds=120, relevance_timeout_seconds=180)
    assert len(worker.assess_batch("Goal", [task()], [claim()])) == 1
    assert timeouts == [90, 89]
    assert worker.last_usage.batches == 1 and worker.last_usage.llm_calls == 2


def test_no_timeout_retry_is_sent_after_the_shared_deadline(monkeypatch):
    from app.research.runtime import ResearchTimeoutError

    clock, attempts = [0.0], []
    monkeypatch.setattr("app.research.runtime.monotonic", lambda: clock[0])

    class ExhaustedRequest(ModelStub):
        def invoke(self, *args, **kwargs):
            attempts.append(kwargs["timeout"])
            clock[0] = 181
            raise httpx.ReadTimeout("Transport ignored its timeout")

    worker = assessor(ExhaustedRequest(), relevance_timeout_seconds=180)
    with pytest.raises(ResearchTimeoutError):
        worker.assess_batch("Goal", [task()], [claim()])
    assert len(attempts) == worker.last_usage.llm_calls == 1


@pytest.mark.parametrize("maximum", [1, 2])
def test_relevance_timeout_retry_respects_supplemental_call_budget(maximum):
    from app.research.reresearch_budget import ReResearchBudgetExhausted, ReResearchCallBudget, use_call_budget

    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ReadTimeout("Transient timeout", request=request)
        return response_for(json.loads(request.content))

    budget = ReResearchCallBudget(maximum)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        worker = assessor(model)
        with use_call_budget(budget):
            if maximum == 1:
                with pytest.raises(ReResearchBudgetExhausted):
                    worker.assess_batch("Goal", [task()], [claim()])
            else:
                assert len(worker.assess_batch("Goal", [task()], [claim()])) == 1
    assert len(requests) == worker.last_usage.llm_calls == budget.calls == maximum
    assert budget.by_stage == {"relevance": maximum}


@pytest.mark.parametrize("first_native_timeout", [False, True])
def test_native_and_json_fallback_share_one_timeout_retry(first_native_timeout):
    requests = []

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        if first_native_timeout and len(requests) == 1:
            raise httpx.ReadTimeout("Native timeout", request=request)
        if body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unavailable",
                                                       "type": "invalid_request_error"}})
        if first_native_timeout or len(requests) == 2:
            raise httpx.ReadTimeout("JSON timeout", request=request)
        return response_for(body)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        worker = assessor(model)
        if first_native_timeout:
            with pytest.raises(RelevanceTimeoutError, match="batch 1/1.*retry=1/1"):
                worker.assess_batch("Goal", [task()], [claim()])
        else:
            assert len(worker.assess_batch("Goal", [task()], [claim()])) == 1
    assert len(requests) == worker.last_usage.llm_calls == 3
    assert worker.last_usage.batches == 1


def test_each_batch_has_its_own_single_timeout_retry():
    requests, attempts = [], Counter()

    def transport(request):
        body = json.loads(request.content)
        requests.append(body)
        key = body["messages"][1]["content"]
        attempts[key] += 1
        if attempts[key] == 1:
            raise httpx.ReadTimeout("First attempt timeout", request=request)
        return response_for(body)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        worker = assessor(model, max_relevance_claims_per_batch=1)
        assert len(worker.assess_batch("Goal", [task()], [claim(text="First"), claim(text="Second")])) == 2
    assert len(requests) == worker.last_usage.llm_calls == 4
    assert worker.last_usage.batches == 2 and sorted(attempts.values()) == [2, 2]


def test_total_relevance_deadline_prevents_late_batches():
    from app.research.runtime import ResearchTimeoutError
    release, done = Event(), Event()
    class SlowModel(ModelStub):
        def invoke(self, *args, **kwargs):
            try:
                release.wait(2)
                return super().invoke(*args, **kwargs)
            finally:
                done.set()
    model = SlowModel()
    worker = assessor(model, relevance_timeout_seconds=0.06, research_progress_interval_seconds=0.02,
                      max_relevance_claims_per_batch=1)
    try:
        with pytest.raises(ResearchTimeoutError, match="claim relevance"):
            worker.assess_batch("Goal", [task()], [claim(), claim(text="Second")])
    finally:
        release.set()
        assert done.wait(2)
    assert len(model.requests) == worker.last_usage.llm_calls == 1


def test_relevance_and_gap_code_has_no_current_topic_rules():
    for name in ["app/prompts/relevance.md", "app/agents/relevance.py", "app/research/relevance_context.py", "app/research/gap_detection.py"]:
        text = Path(name).read_text(encoding="utf-8").lower()
        assert all(topic not in text for topic in ["atari", "sparse network", "offline rl"])


@pytest.mark.parametrize("timeout_once", [False, True])
def test_default_pipeline_parallel_research_then_one_global_relevance_and_no_memo_edits(capsys, timeout_once):
    tasks = [task(), task("D2"), task("A", "analysis", ["D1", "D2"]), task("S", "synthesis", ["A"])]
    plan = ResearchPlan(goal="Supervisor paraphrase", tasks=tasks)
    barrier = Barrier(2, timeout=5)
    evidence_ids = {}
    research_calls, verified_tasks = [], []
    class ResearcherStub:
        def research(self, task, **kwargs):
            research_calls.append(task.id)
            if task.task_type == "discovery":
                source = Source(task_id=task.id, title="Paper", url=f"https://example.org/{task.id}")
                evidence = Evidence(task_id=task.id, source_id=source.source_id, content="Evidence only; must not enter relevance.")
                evidence_ids[task.id] = evidence.evidence_id
                refs = [evidence.evidence_id]
                sources, fragments = [source], [evidence]
                barrier.wait()
            else:
                refs = list(evidence_ids.values())
                sources, fragments = [], []
            claims = [Claim(task_id=task.id, text=f"Result {task.id} {index}", evidence_ids=refs) for index in range(2)]
            return ResearchExecutionResult(memo=f"ORIGINAL_MEMO_{task.id}", tools_used=[],
                compact=CompactResearchResult(task_id=task.id, summary=f"ORIGINAL_COMPACT_{task.id}"),
                evidence_bundle=TaskEvidenceBundle(task_id=task.id, sources=sources, evidence=fragments, claims=claims))
    class VerifierStub:
        last_usage = VerificationUsage()
        def verify_batch(self, claims, store):
            self.last_usage = VerificationUsage(llm_calls=1)
            verified_tasks.append(claims[0].task_id)
            return [support(item, "PARTIAL" if item.task_id == "A" else "SUPPORTED") for item in claims]
    class PipelineModel(ModelStub):
        timed_out = False

        def invoke(self, *args, **kwargs):
            if timeout_once and not self.timed_out:
                self.timed_out = True
                raise httpx.ReadTimeout("Transient relevance timeout")
            return super().invoke(*args, **kwargs)

    model = PipelineModel({"Result D1 0": "MISALIGNED", "Result D1 1": "MISALIGNED"})
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), ResearcherStub(),
                                 verifier=VerifierStub(), gap_detector=GapDetector(assessor(model)), reresearch=False, write_report=False).run("Original exact goal")
    assert len(research_calls) == len(verified_tasks) == len(plan.tasks) == 4
    expected_calls = 2 if timeout_once else 1
    assert result.verification_usage.llm_calls == 4 and result.relevance_usage.llm_calls == expected_calls
    assert len(result.relevance_results) == 8
    assert {item.task_id for item in result.gap_store.get_gaps_by_type("COVERAGE_GAP")} == {"D1", "A"}
    assert len(result.gap_store.get_gaps_by_type("SUPPORT_GAP")) == 2
    assert len(result.gap_store.get_gaps_by_type("RELEVANCE_GAP")) == 2
    for item in tasks:
        assert item.status == "completed"
        assert result.memos[item.id] == f"ORIGINAL_MEMO_{item.id}"
        assert result.compact_results[item.id].summary == f"ORIGINAL_COMPACT_{item.id}"
    payload = json.loads(model.requests[0][0][1].text)
    assert payload["research_goal"] == "Original exact goal"
    assert set(payload["tasks"]) == {item.id for item in tasks}
    assert "ORIGINAL_MEMO" not in model.requests[0][0][1].text
    output = capsys.readouterr().out
    assert "Verification Summary: claims=8" in output
    assert f"Relevance Summary: claims=8 aligned=6 partial=0 misaligned=2 llm_calls={expected_calls}" in output
    assert "Gap Summary: support=2 relevance=2 coverage=2 total=6" in output
    assert "missing_information:" not in output


def test_relevance_failure_preserves_research_and_verification_without_false_gap_summary(capsys):
    current = task()
    source = Source(task_id=current.id, title="Paper", url="https://example.org/a")
    evidence = Evidence(task_id=current.id, source_id=source.source_id, content="Original evidence.")
    item = Claim(task_id=current.id, text="Finding", evidence_ids=[evidence.evidence_id])
    execution = ResearchExecutionResult(memo="Original Memo", tools_used=[], compact=CompactResearchResult(task_id="D1", summary="Original"),
        evidence_bundle=TaskEvidenceBundle(task_id="D1", sources=[source], evidence=[evidence], claims=[item]))
    class VerifierStub:
        last_usage = VerificationUsage(llm_calls=1)
        def verify_batch(self, claims, store):
            return [support(item)]
    detector = GapDetector(assessor(ModelStub(mutate=lambda value: {"results": []})))
    with pytest.raises(RuntimeError, match="Gap Detection failed"):
        ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: ResearchPlan(goal="Goal", tasks=[current])),
            SimpleNamespace(research=lambda **kwargs: execution), verifier=VerifierStub(), gap_detector=detector, reresearch=False, write_report=False).run("Goal")
    output = capsys.readouterr().out
    assert "Original Memo" in output and "Verification Summary: claims=1 supported=1" in output
    assert "Gap Summary:" not in output and "Relevance Summary:" not in output
    assert current.status == "completed"


def test_gap_detection_requires_verification_but_can_be_disabled_for_isolated_tests():
    supervisor, researcher = SimpleNamespace(), SimpleNamespace()
    with pytest.raises(ValueError, match="requires Phase 4A"):
        ResearchOrchestrator(supervisor, researcher, verify_claims=False, detect_gaps=True, reresearch=False, write_report=False)
    assert ResearchOrchestrator(supervisor, researcher, verify_claims=False, reresearch=False, write_report=False).detect_gaps is False
