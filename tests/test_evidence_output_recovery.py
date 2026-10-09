"""Exercise truncation and bad references through the installed SDK, not stub parsers."""

import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_openai import ChatOpenAI

from app.config import Settings
from app.research import evidence_extraction as extraction_module
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import (
    EvidenceExtractor, EvidenceExtractionLengthError, build_evidence_bundle,
    extraction_output_schema, model_payload, prepare_extraction_payload,
)
from app.research.evidence_store import EvidenceStore
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.schemas import ResearchTask


def payload():
    task = ResearchTask(id="A", title="Compare", question="Compare methods", description="Read actual sources", task_type="analysis")
    data = prepare_extraction_payload(task, "Completed memo", [{
        "url": "https://example.org/paper", "title": "Paper", "text": "Introduction. The method uses subgoals. Discussion.",
    }], EvidenceContext())
    return task, data


def valid_result():
    return {"selected_evidence_indexes": [0], "claims": [{"text": "A supported finding", "supporting_evidence_indexes": [0]}]}


def response(*, truncated=False, result=None):
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "length" if truncated else "stop", "message": {
            "role": "assistant", "content": '{"claims":[{"text":"unfinished' if truncated else json.dumps(result or valid_result()),
        }}],
        "usage": {"completion_tokens": 3072 if truncated else 100, "prompt_tokens": 500, "total_tokens": 3572 if truncated else 600},
    })


def model(client):
    return ChatOpenAI(model="test-model", api_key="test-key", base_url="https://api.siliconflow.cn/v1",
                      http_client=client, timeout=60, max_retries=0)


@pytest.mark.parametrize("compatible", [True, False])
def test_native_and_compatibility_truncation_retry_once_with_compact_schema_and_more_output_budget(monkeypatch, compatible, capsys):
    monkeypatch.setattr(extraction_module, "get_settings", lambda: Settings(_env_file=None, evidence_extraction_max_tokens=3072))
    requests, task_data = [], payload()
    runtime = ResearchRuntime("A", 100, 15)

    def transport(request):
        requests.append(request)
        if not compatible and len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "json_schema unavailable", "type": "invalid_request_error"}})
        first = len(requests) == (1 if compatible else 2)
        if first:
            runtime.started -= 10
        return response(truncated=first)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        shared_model = model(client)
        result = EvidenceExtractor(shared_model).extract(task_data[1], runtime)
        assert shared_model.request_timeout == 60 and shared_model.max_tokens is None
    assert len(requests) == (2 if compatible else 3)
    first_body, last_body = json.loads(requests[-2].content), json.loads(requests[-1].content)
    assert first_body["max_tokens"] == 3072 and last_body["max_tokens"] == 6144
    assert requests[-1].extensions["timeout"]["read"] < requests[-2].extensions["timeout"]["read"]
    assert first_body["messages"][1] == last_body["messages"][1]  # Same task, sources, indexes; no continued partial JSON.
    assert "at most 6 claims" in last_body["messages"][0]["content"]
    if compatible:
        first_schema = first_body["response_format"]["json_schema"]["schema"]
        last_schema = last_body["response_format"]["json_schema"]["schema"]
        assert first_schema["properties"]["selected_evidence_indexes"]["items"]["enum"] == list(range(len(task_data[1]["candidates"])))
        assert last_schema["properties"]["claims"]["maxItems"] == 6
    store = EvidenceStore()
    bundle = store.merge(build_evidence_bundle(task_data[0], result, task_data[1], EvidenceContext()))
    assert store.get_source_for_evidence(store.get_evidence_for_claim(bundle.claims[0].claim_id)[0].evidence_id)
    assert "compact recovery 1/1" in capsys.readouterr().out


@pytest.mark.parametrize("compatible", [True, False])
def test_repeated_truncation_stops_after_one_recovery_without_accepting_partial_json(monkeypatch, compatible):
    monkeypatch.setattr(extraction_module, "get_settings", lambda: Settings(_env_file=None))
    requests = []

    def transport(request):
        requests.append(request)
        if not compatible and len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format unavailable", "type": "invalid_request_error"}})
        return response(truncated=True)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(EvidenceExtractionLengthError, match="task A after one compact recovery attempt"):
            EvidenceExtractor(model(client)).extract(payload()[1], ResearchRuntime("A", 300, 15))
    assert len(requests) == (2 if compatible else 3)


def test_exhausted_task_deadline_prevents_length_recovery_http_request(monkeypatch):
    monkeypatch.setattr(extraction_module, "get_settings", lambda: Settings(_env_file=None))
    runtime, requests = ResearchRuntime("A", 300, 15), []

    def transport(request):
        requests.append(request)
        runtime.closed.set()
        return response(truncated=True)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ResearchTimeoutError):
            EvidenceExtractor(model(client)).extract(payload()[1], runtime)
    assert len(requests) == 1


def test_recovery_output_budget_remains_bounded_even_with_largest_configured_limit(monkeypatch):
    monkeypatch.setattr(extraction_module, "get_settings", lambda: Settings(_env_file=None, evidence_extraction_max_tokens=8192))
    requests = []

    def transport(request):
        requests.append(request)
        return response(truncated=len(requests) == 1)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        EvidenceExtractor(model(client)).extract(payload()[1], ResearchRuntime("A", 300, 15))
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [8192, 8192]


def test_default_initial_budget_is_6144_and_recovery_is_capped_at_8192(monkeypatch):
    settings = Settings(_env_file=None)
    assert settings.evidence_extraction_max_tokens == 6144
    monkeypatch.setattr(extraction_module, "get_settings", lambda: settings)
    requests = []

    def transport(request):
        requests.append(request)
        return response(truncated=len(requests) == 1)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        EvidenceExtractor(model(client)).extract(payload()[1], ResearchRuntime("A", 300, 15))
    assert [json.loads(request.content)["max_tokens"] for request in requests] == [6144, 8192]


def test_schema_candidate_ranges_are_request_local_and_recovery_has_smaller_output_limits():
    large, small = extraction_output_schema(40), extraction_output_schema(2, compact=True)
    assert large["properties"]["selected_evidence_indexes"]["items"]["enum"] == list(range(40))
    assert small["$defs"]["ExtractedClaim"]["properties"]["supporting_evidence_indexes"]["items"]["enum"] == [0, 1]
    assert small["properties"]["claims"]["maxItems"] == 6
    assert small["$defs"]["ExtractedClaim"]["properties"]["text"]["maxLength"] == 90
    assert large["properties"]["claims"]["maxItems"] == 6


def test_unknown_references_omit_entire_affected_claim_while_preserving_other_chains(monkeypatch, capsys):
    monkeypatch.setattr(extraction_module, "get_settings", lambda: Settings(_env_file=None))
    task, data = payload()
    unknown = len(data["candidates"])
    raw = {"selected_evidence_indexes": [0, unknown], "claims": [
        {"text": "Valid", "supporting_evidence_indexes": [0]},
        {"text": "Invalid mixed support", "supporting_evidence_indexes": [0, unknown]},
        {"text": "Also valid", "supporting_evidence_indexes": [1]},
    ]}
    requests = []

    def transport(request):
        requests.append(request)
        return response(result=raw)  # Simulate a provider ignoring the enum constraint.

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        extracted = EvidenceExtractor(model(client)).extract(data, ResearchRuntime("A", 300, 15))
    assert len(requests) == 1
    assert [claim.text for claim in extracted.claims] == ["Valid", "Also valid"]
    store = EvidenceStore()
    bundle = store.merge(build_evidence_bundle(task, extracted, data, EvidenceContext()))
    assert len(bundle.claims) == 2
    for claim in bundle.claims:
        assert all(store.get_source_for_evidence(item.evidence_id) for item in store.get_evidence_for_claim(claim.claim_id))
    assert "claims_omitted=1" in capsys.readouterr().out


def test_all_unknown_references_produce_no_formal_claim_or_invented_source():
    from app.schemas.evidence_extraction import ExtractedClaim, ExtractedEvidenceResult
    task, data = payload()
    output = ExtractedEvidenceResult(selected_evidence_indexes=[999], claims=[
        ExtractedClaim(text="Unsupported", supporting_evidence_indexes=[0, 999]),
    ])
    logs = []
    bundle = build_evidence_bundle(task, output, data, EvidenceContext(), report=logs.append)
    assert bundle.sources == bundle.evidence == bundle.claims == []
    assert "invalid_indexes=[999]" in logs[0]


def test_empty_candidates_skip_the_model_and_never_send_an_empty_enum_schema():
    task, data = payload()
    data["candidates"] = []
    extractor = EvidenceExtractor(SimpleNamespace())
    result = extractor.extract(data, ResearchRuntime("A", 300, 15))
    assert result.claims == result.selected_evidence_indexes == []


def test_compacted_metadata_preserves_every_candidate_and_its_original_source():
    _, data = payload()
    sent = model_payload(data)
    sources = {item["label"]: item for item in sent["sources"]}
    assert len(sent["sources"]) == 1 and len(sent["evidence_candidates"]) == len(data["candidates"])
    for original, offered in zip(data["candidates"], sent["evidence_candidates"]):
        assert original["evidence_index"] == offered["evidence_index"]
        assert original["content"] == offered["content"]
        assert sources[offered["source"]]["url"] == original["url"]
