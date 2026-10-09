"""Verify extraction limits reach the real LangChain/OpenAI HTTP client."""

import json

import httpx
import pytest
from langchain_openai import ChatOpenAI
from openai import APITimeoutError

from app.config import Settings
from app.research import evidence_extraction as extraction_module
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import EvidenceExtractor, EvidenceExtractionTimeoutError, prepare_extraction_payload
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.schemas import ResearchTask


def extraction_payload():
    task = ResearchTask(id="A", title="Compare methods", question="What mechanism is used?",
                        description="Analyze sources", task_type="analysis")
    return prepare_extraction_payload(task, "Completed research memo", [{
        "url": "https://example.org/paper", "title": "Paper",
        "text": "Introduction. The method uses subgoals. Discussion.",
    }], EvidenceContext())


def response():
    return httpx.Response(200, json={
        "id": "chatcmpl-test", "object": "chat.completion", "created": 0, "model": "test-model",
        "choices": [{"index": 0, "finish_reason": "stop", "message": {
            "role": "assistant", "content": json.dumps({
                "selected_evidence_indexes": [0],
                "claims": [{"text": "A supported finding", "supporting_evidence_indexes": [0]}],
            }),
        }}],
    })


def use_settings(monkeypatch, **kwargs):
    settings = Settings(_env_file=None, **kwargs)
    monkeypatch.setattr(extraction_module, "get_settings", lambda: settings)
    return settings


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("thinking", [False, True])
def test_qwen_extraction_thinking_override_reaches_http_without_changing_research_model(monkeypatch, native, thinking):
    use_settings(monkeypatch, evidence_extraction_enable_thinking=thinking)
    requests = []

    def transport(request):
        requests.append(request)
        if not native and len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "response_format json_schema unavailable",
                                                      "type": "invalid_request_error"}})
        return response()

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="qwen3.7-flash", api_key="test-key",
                           base_url="https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
                           http_client=client, max_retries=0, extra_body={"enable_thinking": True, "provider_option": "keep"})
        result = EvidenceExtractor(model).extract(extraction_payload(), ResearchRuntime("A", 120, 15))
        assert result.claims and model.extra_body["enable_thinking"] is True
    assert len(requests) == (1 if native else 2)
    for request in requests:
        body = json.loads(request.content)
        assert body["enable_thinking"] is thinking and body["provider_option"] == "keep"


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("base_url", ["https://example.org/v1", "https://api.siliconflow.cn/v1"])
def test_request_limits_reach_native_and_fallback_http_without_mutating_shared_model(monkeypatch, native, base_url, capsys):
    settings = use_settings(monkeypatch, evidence_extraction_timeout_seconds=120,
                            evidence_extraction_max_tokens=3072)
    requests = []

    def transport(request):
        requests.append(request)
        if not native and len(requests) == 1:
            return httpx.Response(400, json={"error": {
                "message": "response_format json_schema is not supported", "type": "invalid_request_error",
            }})
        return response()

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test-model", api_key="test-key", base_url=base_url,
                           http_client=client, timeout=60, max_retries=0, extra_body={"provider_option": "original"})
        result = EvidenceExtractor(model).extract(extraction_payload(), ResearchRuntime("A", 300, 15))
        assert result.claims
        assert model.request_timeout == 60 and model.root_client.timeout == 60
        assert model.extra_body == {"provider_option": "original"}
    assert len(requests) == (1 if native else 2)
    for request in requests:
        assert 119 < request.extensions["timeout"]["read"] <= settings.evidence_extraction_timeout_seconds
        body = json.loads(request.content)
        limit_field = "max_tokens" if "siliconflow" in base_url else "max_completion_tokens"
        assert body[limit_field] == settings.evidence_extraction_max_tokens
        assert body["provider_option"] == "original"
    logs = capsys.readouterr().out
    assert "evidence extraction request: input_chars=" in logs and "max_tokens=3072" in logs


def test_fallback_recomputes_timeout_using_remaining_task_budget(monkeypatch):
    use_settings(monkeypatch)
    runtime = ResearchRuntime("A", 30, 15)
    requests = []

    def transport(request):
        requests.append(request)
        if len(requests) == 1:
            runtime.started -= 20  # Account for time spent on the rejected native request.
            return httpx.Response(400, json={"error": {
                "message": "json_schema unavailable", "type": "invalid_request_error",
            }})
        return response()

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.org/v1",
                           http_client=client, timeout=60, max_retries=0)
        EvidenceExtractor(model).extract(extraction_payload(), runtime)
    assert 29 < requests[0].extensions["timeout"]["read"] <= 30
    assert 9 < requests[1].extensions["timeout"]["read"] <= 10


def test_extraction_timeout_names_stage_preserves_cause_and_does_not_retry(monkeypatch):
    use_settings(monkeypatch)
    requests = []

    def transport(request):
        requests.append(request)
        raise httpx.ReadTimeout("Slow model service", request=request)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.org/v1",
                           http_client=client, timeout=60, max_retries=0)
        with pytest.raises(EvidenceExtractionTimeoutError, match="task A.*research memo was generated") as error:
            EvidenceExtractor(model).extract(extraction_payload(), ResearchRuntime("A", 300, 15))
    assert isinstance(error.value.__cause__, APITimeoutError)
    assert len(requests) == 1


def test_closed_runtime_prevents_extraction_http_call(monkeypatch):
    use_settings(monkeypatch)
    runtime = ResearchRuntime("A", 300, 15)
    runtime.closed.set()
    requests = []

    def transport(request):
        requests.append(request)
        return response()

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        model = ChatOpenAI(model="test-model", api_key="test-key", base_url="https://example.org/v1",
                           http_client=client, timeout=60, max_retries=0)
        with pytest.raises(ResearchTimeoutError):
            EvidenceExtractor(model).extract(extraction_payload(), runtime)
    assert requests == []
