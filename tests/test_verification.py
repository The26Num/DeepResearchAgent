"""Phase 4A contracts, complete support chains and compact batch budgets."""

import json
from threading import Barrier, Event
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

import app.llm as llm_module
from app.agents.verifier import ClaimVerifier, VerificationOutputError
from app.config import Settings
from app.research.evidence_store import EvidenceStore
from app.research.orchestrator import ResearchOrchestrator
from app.research.verification_context import (
    VerificationContextLimitError, VerificationIntegrityError, build_verification_context,
)
from app.research.verification_store import VerificationStore
from app.schemas import Claim, CompactResearchResult, Evidence, ResearchExecutionResult, ResearchPlan, ResearchTask, Source, TaskEvidenceBundle
from app.schemas.verification import VerificationResult, VerificationStatus


def fixture_store(content="Method A improves performance on D4RL medium datasets."):
    source = Source(task_id="D1", title="Paper A", url="https://example.org/paper-a", source_type="paper")
    evidence = Evidence(task_id="D1", source_id=source.source_id, content=content, location="RESULTS_NOT_SENT")
    store = EvidenceStore()
    store.merge(TaskEvidenceBundle(task_id="D1", sources=[source], evidence=[evidence]))
    return store, evidence


def claims_for(evidence, count=3, task_id="D1"):
    return [Claim(task_id=task_id, text=f"Method A improves D4RL performance ({index}).", evidence_ids=[evidence.evidence_id])
            for index in range(count)]


class ModelStub:
    def __init__(self, decide=None, mutate=None):
        self.requests = []
        self.decide = decide or (lambda claim: ("SUPPORTED", "The cited result directly supports the specified benchmark improvement."))
        self.mutate = mutate or (lambda value: value)

    def with_structured_output(self, schema, **kwargs):
        self.schema = schema
        assert kwargs == {"method": "json_schema"}
        return self

    def invoke(self, messages, config=None, **kwargs):
        self.requests.append((messages, kwargs))
        payload = json.loads(messages[1].text)
        results = []
        for claim in reversed(payload["claims"]):
            status, reason = self.decide(claim)
            results.append({"claim_id": claim["claim_id"], "status": status, "reason": reason})
        raw = AIMessage(content="", usage_metadata={"input_tokens": 100, "output_tokens": 30, "total_tokens": 130})
        for callback in (config or {}).get("callbacks", []):
            callback.on_llm_end(SimpleNamespace(generations=[[SimpleNamespace(message=raw)]]))
        return self.mutate({"results": results})


def verifier(model=None, **settings):
    return ClaimVerifier(model or ModelStub(), settings=Settings(_env_file=None, **settings))


def test_three_claims_one_call_and_program_generated_fields(capsys):
    store, evidence = fixture_store()
    claims = claims_for(evidence)
    model = ModelStub()
    worker = verifier(model)
    results = worker.verify_batch(claims, store)
    assert len(model.requests) == worker.last_usage.llm_calls == 1
    assert [item["claim_id"] for item in json.loads(model.requests[0][0][1].text)["claims"]] == ["C1", "C2", "C3"]
    assert [item.claim_id for item in results] == [item.claim_id for item in claims]
    assert all(item.status == VerificationStatus.SUPPORTED for item in results)
    assert all(item.evidence_ids == [evidence.evidence_id] and item.task_id == "D1" for item in results)
    assert len({item.verification_id for item in results}) == 3
    assert worker.last_usage.input_tokens == 100 and worker.last_usage.output_tokens == 30
    assert "unique_evidence=1" in capsys.readouterr().out
    assert "verification_status" not in claims[0].model_dump()


def test_context_deduplicates_shared_evidence_and_sends_only_cited_material():
    store, evidence = fixture_store()
    unrelated = Source(task_id="X", title="UNRELATED_TITLE", url="https://example.org/unrelated")
    unused = Evidence(task_id="X", source_id=unrelated.source_id, content="UNRELATED_EVIDENCE")
    store.merge(TaskEvidenceBundle(task_id="X", sources=[unrelated], evidence=[unused]))
    claims = claims_for(evidence)
    context = build_verification_context(claims, store)
    assert list(context.payload["evidence"]) == [evidence.evidence_id]
    assert len(context.payload["sources"]) == 1
    assert context.text.count(evidence.content) == 1
    assert all(word not in context.text for word in ["UNRELATED", "RESULTS_NOT_SENT", "published_at", "memo", "task_id"])
    assert set(context.payload) == {"claims", "evidence", "sources"}


@pytest.mark.parametrize("text,status,reason", [
    ("Method A improves performance on D4RL medium datasets.", "SUPPORTED", "Evidence directly states improvement under these conditions."),
    ("Method A improves all offline RL tasks.", "PARTIAL", "Evidence supports D4RL medium datasets, not all tasks."),
    ("Method A uses diffusion models.", "UNSUPPORTED", "Evidence provides no diffusion mechanism."),
    ("Method A outperforms every baseline.", "UNSUPPORTED", "Evidence reports improvement but no baseline comparison."),
])
def test_support_scope_relevance_and_comparison_contract(text, status, reason):
    # Offline tests validate transport/mapping and the required judging rules;
    # the semantic accuracy of an actual model is measured separately in live runs.
    store, evidence = fixture_store()
    worker = verifier(ModelStub(decide=lambda claim: (status, reason)))
    result = worker.verify_claim(Claim(task_id="D1", text=text, evidence_ids=[evidence.evidence_id]), store)
    assert result.status.value == status and result.reason == reason
    for rule in ("scope", "strength", "comparisons", "conditions", "prior knowledge", "absolute", "UNSUPPORTED", "PARTIAL"):
        assert rule in worker.prompt


def test_empty_evidence_precheck_needs_no_model_or_credentials(monkeypatch):
    def forbidden():
        raise AssertionError("Model factory must not be called")
    monkeypatch.setattr("app.agents.verifier.create_verifier_llm", forbidden)
    worker = ClaimVerifier(settings=Settings(_env_file=None))
    # Claim's Phase 3 public schema still rejects empty refs. This tests the
    # defensive boundary for legacy/debug/unvalidated objects without weakening it.
    claim = Claim.model_construct(claim_id="legacy", task_id="D1", text="Unlinked statement", evidence_ids=[])
    result = worker.verify_claim(claim, EvidenceStore())
    assert result.status == VerificationStatus.UNSUPPORTED and result.evidence_ids == []
    assert worker.last_usage.llm_calls == 0 and worker.model is None
    with pytest.raises(ValidationError):
        Claim(task_id="D1", text="Invalid formal Claim", evidence_ids=[])


def test_mixed_precheck_and_valid_claims_preserve_order():
    store, evidence = fixture_store()
    empty = Claim.model_construct(claim_id="empty", task_id="D1", text="Empty", evidence_ids=[])
    valid = claims_for(evidence, 1)[0]
    worker = verifier()
    result = worker.verify_batch([empty, valid], store)
    assert [item.claim_id for item in result] == ["empty", valid.claim_id]
    assert worker.last_usage.llm_calls == 1


def test_unknown_evidence_fails_before_any_calls_even_after_a_valid_batch():
    store, evidence = fixture_store()
    worker = verifier(max_verify_claims_per_batch=1)
    missing = Claim(task_id="D1", text="Missing evidence", evidence_ids=["not-in-store"])
    with pytest.raises(VerificationIntegrityError, match="unresolved.*not-in-store"):
        worker.verify_batch([*claims_for(evidence, 2), missing], store)
    assert worker.last_usage.llm_calls == 0


def test_empty_batch_is_free():
    worker = verifier()
    assert worker.verify_batch([], EvidenceStore()) == []
    assert worker.last_usage.llm_calls == 0


def test_unknown_source_is_a_precheck_integrity_error():
    store, evidence = fixture_store()
    class BrokenSourceStore:
        get_evidence = store.get_evidence
        def get_source(self, source_id):
            raise KeyError(source_id)
    worker = verifier()
    with pytest.raises(VerificationIntegrityError, match="unresolved Evidence/Source"):
        worker.verify_batch(claims_for(evidence), BrokenSourceStore())
    assert worker.last_usage.llm_calls == 0


def test_mixed_tasks_are_not_accidentally_combined():
    store, evidence = fixture_store()
    worker = verifier()
    with pytest.raises(ValueError, match="one task"):
        worker.verify_batch([*claims_for(evidence, 1), *claims_for(evidence, 1, task_id="S")], store)
    assert worker.last_usage.llm_calls == 0


def test_fourteen_claims_split_into_six_six_two():
    store, evidence = fixture_store()
    model = ModelStub()
    worker = verifier(model)
    result = worker.verify_batch(claims_for(evidence, 14), store)
    assert len(result) == 14 and worker.last_usage.llm_calls == 3
    assert [len(json.loads(messages[1].text)["claims"]) for messages, _ in model.requests] == [6, 6, 2]


def test_context_budget_splits_without_truncating_evidence():
    store, first = fixture_store("First evidence " + "x" * 500)
    second = Evidence(task_id="D1", source_id=first.source_id, content="Second evidence " + "y" * 500)
    store.add_evidence(second)
    claims = [Claim(task_id="D1", text="Finding one", evidence_ids=[first.evidence_id]),
              Claim(task_id="D1", text="Finding two", evidence_ids=[second.evidence_id])]
    budget = max(build_verification_context([item], store).context_chars for item in claims)
    model = ModelStub()
    worker = verifier(model, max_verification_context_chars=budget)
    assert len(worker.verify_batch(claims, store)) == 2 and worker.last_usage.llm_calls == 2
    for index, (messages, _) in enumerate(model.requests):
        data = json.loads(messages[1].text)
        original = [first, second][index]
        assert data["evidence"][original.evidence_id]["content"] == original.content
        assert len(messages[1].text) <= budget


def test_one_oversize_chain_fails_without_truncation_or_calls():
    store, evidence = fixture_store("x" * 1800)
    worker = verifier(max_verification_context_chars=500)
    with pytest.raises(VerificationContextLimitError, match="Evidence was not truncated"):
        worker.verify_batch(claims_for(evidence), store)
    assert worker.last_usage.llm_calls == 0


def test_synthesis_resolves_evidence_from_two_discovery_tasks():
    store, first = fixture_store()
    source = Source(task_id="D2", title="Paper B", url="https://example.org/b")
    second = Evidence(task_id="D2", source_id=source.source_id, content="Method B uses ordered subgoals.")
    store.merge(TaskEvidenceBundle(task_id="D2", sources=[source], evidence=[second]))
    claim = Claim(task_id="S", text="The mechanisms differ.", evidence_ids=[first.evidence_id, second.evidence_id])
    store.add_claim(claim)
    model = ModelStub()
    result = verifier(model).verify_claim(claim, store)
    payload = json.loads(model.requests[0][0][1].text)
    assert set(payload["evidence"]) == set(claim.evidence_ids)
    assert result.task_id == "S" and result.evidence_ids == claim.evidence_ids


@pytest.mark.parametrize("mutate", [
    lambda value: {"results": value["results"][:-1]},
    lambda value: {"results": value["results"] + value["results"][:1]},
    lambda value: {"results": [{**value["results"][0], "claim_id": "invented"}, *value["results"][1:]]},
])
def test_missing_duplicate_and_unknown_output_ids_fail_closed(mutate):
    store, evidence = fixture_store()
    worker = verifier(ModelStub(mutate=mutate))
    with pytest.raises(VerificationOutputError, match="exactly one decision"):
        worker.verify_batch(claims_for(evidence), store)
    assert worker.last_usage.llm_calls == 1


@pytest.mark.parametrize("patch", [
    {"status": "TRUE"}, {"status": "UNKNOWN"}, {"confidence": 0.8}, {"reason": "x" * 401}, {"reason": " "},
    {"evidence_ids": ["model-invented-id"]}, {"gap": "Search again"},
])
def test_invalid_status_long_reason_and_extra_fields_rejected(patch):
    store, evidence = fixture_store()
    worker = verifier(ModelStub(mutate=lambda value: {"results": [{**value["results"][0], **patch}]}))
    with pytest.raises(ValidationError):
        worker.verify_batch(claims_for(evidence, 1), store)


def test_duplicate_input_claim_id_is_verified_once():
    store, evidence = fixture_store()
    claim = claims_for(evidence, 1)[0]
    worker = verifier()
    assert len(worker.verify_batch([claim, claim], store)) == 1
    with pytest.raises(ValueError, match="Conflicting claim_id"):
        worker.verify_batch([claim, claim.model_copy(update={"text": "Different"})], store)


def test_multiple_results_are_historical_and_store_copies_are_isolated():
    store = VerificationStore()
    first = VerificationResult(claim_id="c", task_id="S", evidence_ids=["e"], status="PARTIAL", reason="Missing scope.")
    second = VerificationResult(claim_id="c", task_id="S", evidence_ids=["e", "e2"], status="SUPPORTED", reason="Scope now supported.")
    store.add_verification(first)
    store.add_verification(second)
    assert store.get_verifications_for_claim("c") == [first, second]
    assert store.get_latest_verification("c") == second
    assert store.get_verifications_for_task("S") == [first, second]
    assert store.get_latest_verification("missing") is None
    store.get_latest_verification("c").evidence_ids.append("mutated")
    first.evidence_ids.append("also-mutated")
    assert store.get_verifications_for_claim("c")[0].evidence_ids == ["e"]
    assert store.latest_results[0].evidence_ids == ["e", "e2"]
    with pytest.raises(ValueError, match="Conflicting verification_id"):
        store.add_verification(second.model_copy(update={"reason": "Changed"}))


def http_response(body):
    data = json.loads(body["messages"][1]["content"])
    result = {"results": [{"claim_id": item["claim_id"], "status": "SUPPORTED", "reason": "Directly supported."}
                          for item in data["claims"]]}
    return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0, "model": "test",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": json.dumps(result)}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 60, "total_tokens": 560}})


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("base_url", ["https://api.siliconflow.cn/v1", "https://provider.example/v1"])
def test_real_http_transport_native_fallback_usage_no_tools_and_output_budget(native, base_url):
    requests = []
    def transport(request):
        body = json.loads(request.content)
        requests.append((request, body))
        if not native and body.get("response_format", {}).get("type") == "json_schema":
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unavailable", "type": "invalid_request_error"}})
        return http_response(body)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url=base_url, http_client=client,
                           max_retries=0, extra_body={"keep": "original"})
        worker = verifier(model)
        store, evidence = fixture_store()
        assert len(worker.verify_batch(claims_for(evidence, 7), store)) == 7
        assert model.extra_body == {"keep": "original"}
    assert len(requests) == worker.last_usage.llm_calls == (2 if native else 3)
    assert worker.last_usage.usage_responses == 2
    assert worker.last_usage.input_tokens == 1000 and worker.last_usage.output_tokens == 120
    actual_chars = sum(len(body["messages"][1]["content"]) for _, body in requests)
    assert worker.last_usage.context_chars == actual_chars
    for request, body in requests:
        assert "tools" not in body and "tool_choice" not in body
        assert body["keep"] == "original"
        assert body["max_tokens" if "siliconflow" in base_url else "max_completion_tokens"] == 3072
        assert 0 < request.extensions["timeout"]["read"] <= 60


def test_timeout_is_not_retried_or_treated_as_unsupported():
    requests = []
    def transport(request):
        requests.append(request)
        raise httpx.ReadTimeout("Slow verification", request=request)
    from openai import APITimeoutError
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        store, evidence = fixture_store()
        worker = verifier(model)
        with pytest.raises(APITimeoutError):
            worker.verify_batch(claims_for(evidence), store)
    assert len(requests) == worker.last_usage.llm_calls == 1


def test_total_verification_deadline_stops_late_batches():
    from app.research.runtime import ResearchTimeoutError
    release, done = Event(), Event()
    class SlowModel(ModelStub):
        def invoke(self, *args, **kwargs):
            try:
                release.wait(2)
                return super().invoke(*args, **kwargs)
            finally:
                done.set()
    store, evidence = fixture_store()
    model = SlowModel()
    worker = verifier(model, verification_timeout_seconds=0.06,
                      research_progress_interval_seconds=0.02, max_verify_claims_per_batch=1)
    try:
        with pytest.raises(ResearchTimeoutError, match="claim verification"):
            worker.verify_batch(claims_for(evidence, 3), store)
    finally:
        release.set()
        assert done.wait(2)
    assert len(model.requests) == worker.last_usage.llm_calls == 1


def test_length_finished_json_is_rejected_without_retry():
    requests = []
    def transport(request):
        requests.append(request)
        body = json.loads(request.content)
        response = http_response(body)
        payload = response.json()
        payload["choices"][0]["finish_reason"] = "length"
        return httpx.Response(200, json=payload)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test", api_key="test-key", base_url="https://provider.example/v1",
                           http_client=client, max_retries=0)
        store, evidence = fixture_store()
        worker = verifier(model)
        with pytest.raises(VerificationOutputError, match="truncated"):
            worker.verify_batch(claims_for(evidence), store)
    assert len(requests) == worker.last_usage.llm_calls == 1


def test_parallel_research_then_verification_and_unchanged_reports(capsys):
    plan = ResearchPlan(goal="topic", tasks=[
        ResearchTask(id="D1", title="D1", question="Methods?", description="Research"),
        ResearchTask(id="D2", title="D2", question="Benchmarks?", description="Research"),
        ResearchTask(id="A", title="A", question="Compare?", description="Analyze", task_type="analysis", depends_on=["D1", "D2"]),
        ResearchTask(id="S", title="S", question="Summarize?", description="Summarize", task_type="synthesis", depends_on=["A"]),
    ])
    barrier = Barrier(2, timeout=5)
    discovery = {}
    class ResearcherStub:
        def research(self, task, context, goal):
            if task.task_type == "discovery":
                source = Source(task_id=task.id, title=task.id, url=f"https://example.org/{task.id}")
                item = Evidence(task_id=task.id, source_id=source.source_id, content=f"Result for {task.id}.")
                discovery[task.id] = item.evidence_id
                claims = [Claim(task_id=task.id, text=f"Finding {index}", evidence_ids=[item.evidence_id]) for index in range(3)]
                bundle = TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=[item], claims=claims)
                barrier.wait()
            else:
                claims = [Claim(task_id=task.id, text=f"Comparison {index}", evidence_ids=list(discovery.values())) for index in range(3)]
                bundle = TaskEvidenceBundle(task_id=task.id, claims=claims)
            return ResearchExecutionResult(memo=f"MEMO_UNCHANGED_{task.id}", tools_used=[], evidence_bundle=bundle,
                    compact=CompactResearchResult(task_id=task.id, summary=f"COMPACT_UNCHANGED_{task.id}"))
    model = ModelStub(decide=lambda claim: ("UNSUPPORTED", "The cited passages do not support this statement."))
    worker = verifier(model)
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), ResearcherStub(), verifier=worker, detect_gaps=False, reresearch=False, write_report=False).run("topic")
    assert all(item.status == "completed" for item in plan.tasks)
    assert len(result.verification_store.latest_results) == 12
    assert result.verification_usage.llm_calls == 4 < 12
    for task in plan.tasks:
        assert result.memos[task.id] == f"MEMO_UNCHANGED_{task.id}"
        assert result.compact_results[task.id].summary == f"COMPACT_UNCHANGED_{task.id}"
        assert len(result.verification_store.get_verifications_for_task(task.id)) == 3
    assert all("MEMO_UNCHANGED" not in messages[1].text for messages, _ in model.requests)
    output = capsys.readouterr().out
    assert "Verification Summary: claims=12 supported=0 partial=0 unsupported=12 llm_calls=4" in output
    assert "The cited passages do not support this statement." not in output


def test_verifier_failure_preserves_memo_and_prints_failure_not_fake_decisions(capsys):
    store, evidence = fixture_store()
    claims = claims_for(evidence)
    task = ResearchTask(id="D1", title="D1", question="Question?", description="Research")
    bundle = TaskEvidenceBundle(task_id="D1", sources=store.get_sources_for_task("D1"), evidence=[evidence], claims=claims)
    outcome = ResearchExecutionResult(memo="Original Memo", tools_used=[], evidence_bundle=bundle,
                                     compact=CompactResearchResult(task_id="D1", summary="Original"))
    worker = verifier(ModelStub(mutate=lambda value: {"results": value["results"][:-1]}))
    with pytest.raises(RuntimeError, match="exactly one decision"):
        ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: ResearchPlan(goal="topic", tasks=[task])),
                             SimpleNamespace(research=lambda **kwargs: outcome), verifier=worker, reresearch=False, write_report=False).run("topic")
    output = capsys.readouterr().out
    assert "Original Memo" in output and "Verification Summary: claims=0" in output
    assert task.status == "completed"  # Research was successful; verification failed separately.


def test_verification_uses_canonical_ids_after_cross_task_deduplication():
    tasks = [ResearchTask(id=key, title=key, question="Question?", description="Research") for key in ["D1", "D2"]]
    bundles = {}
    for task in tasks:
        source = Source(task_id=task.id, title=f"Title {task.id}", url="https://example.org/shared")
        evidence = Evidence(task_id=task.id, source_id=source.source_id, content="The same source fragment.")
        claim = Claim(task_id=task.id, text="A shared result.", evidence_ids=[evidence.evidence_id])
        bundles[task.id] = TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=[evidence], claims=[claim])
    def research(task, **kwargs):
        return ResearchExecutionResult(memo="Unchanged", tools_used=[], evidence_bundle=bundles[task.id],
                                       compact=CompactResearchResult(task_id=task.id, summary="Unchanged"))
    model = ModelStub()
    result = ResearchOrchestrator(
        SimpleNamespace(create_plan=lambda query: ResearchPlan(goal="topic", tasks=tasks)),
        SimpleNamespace(research=research), verifier=verifier(model), detect_gaps=False, reresearch=False, write_report=False).run("topic")
    canonical = bundles["D1"].evidence[0].evidence_id
    assert result.evidence_store.counts == {"sources": 1, "evidence": 1, "claims": 2}
    for task in tasks:
        decision = result.verification_store.get_verifications_for_task(task.id)[0]
        assert decision.evidence_ids == [canonical]
    for messages, _ in model.requests:
        payload = json.loads(messages[1].text)
        assert list(payload["evidence"]) == [canonical]
        assert next(iter(payload["sources"].values()))["title"] == "Title D1"


def test_verifier_factory_fallback_independent_provider_and_thinking(monkeypatch):
    settings = Settings(_env_file=None, model_name="Qwen/Qwen2.5-7B-Instruct", siliconflow_api_key="research-key")
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    assert llm_module.create_verifier_llm().model_name == "Qwen/Qwen2.5-7B-Instruct"
    settings.verifier_model_name = "qwen3.7-flash"
    settings.verifier_model_api_key = "verifier-key"
    settings.verifier_model_base_url = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    settings.model_max_retries = 2
    model = llm_module.create_verifier_llm()
    assert model.model_name == "qwen3.7-flash" and model.openai_api_key.get_secret_value() == "verifier-key"
    assert model.extra_body == {"enable_thinking": False} and model.max_retries == 0
    assert llm_module.create_llm().extra_body is None
    settings.verifier_enable_thinking = True
    assert llm_module.create_verifier_llm().extra_body == {"enable_thinking": True}


@pytest.mark.parametrize("kwargs", [
    {"verifier_model_api_key": "test-key"}, {"verifier_model_base_url": "https://provider.example/v1"},
])
def test_partial_verifier_credentials_are_rejected_without_leaking_keys(monkeypatch, kwargs):
    settings = Settings(_env_file=None, model_name="test", siliconflow_api_key="legacy-key", **kwargs)
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="both VERIFIER_MODEL_API_KEY and VERIFIER_MODEL_BASE_URL") as error:
        llm_module.create_verifier_llm()
    assert "test-key" not in str(error.value) and "legacy-key" not in str(error.value)


def test_independent_verifier_provider_needs_no_research_credentials(monkeypatch):
    settings = Settings(_env_file=None, verifier_model_name="test", verifier_model_api_key="verifier-key",
                        verifier_model_base_url="https://provider.example/v1")
    monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
    assert llm_module.create_verifier_llm().openai_api_key.get_secret_value() == "verifier-key"
