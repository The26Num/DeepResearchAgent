"""Official DeepSeek request contracts using the real SDK and offline HTTP."""

from dataclasses import asdict
import json
from types import SimpleNamespace

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from openai import AuthenticationError

import app.llm as llm_module
from app.agents.relevance import RelevanceAssessor
from app.agents.report_grounding import ReportGroundingChecker
from app.agents.researcher import Researcher
from app.agents.reresearch_planner import ReResearchPlanner
from app.agents.supervisor import Supervisor
from app.agents.verifier import ClaimVerifier, VerificationOutputError
from app.agents.writer import ResearchWriter
from app.config import Settings
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import EvidenceExtractor, prepare_extraction_payload
from app.research.evidence_store import EvidenceStore
from app.research.reresearch import select_gap_groups
from app.research.reresearch_budget import ReResearchCallBudget, use_call_budget
from app.research.runtime import ResearchRuntime
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.research.writing_context import WritingClaim, WritingContext
from app.schemas import Claim, Evidence, ResearchGap, ResearchPlan, ResearchTask, Source
from app.structured_output import json_object_options, prefers_json_object


BASE = "https://api.deepseek.com"
FACT = "SQLite stores research checkpoints on disk."


@pytest.fixture
def model_factory(monkeypatch):
    def create(client=None, *, base=BASE, **role):
        settings = Settings(_env_file=None, model_name="deepseek-flash",
                            model_api_key="deepseek-test-key", model_base_url=base,
                            siliconflow_api_key="unused-legacy-key", model_max_retries=0)
        monkeypatch.setattr(llm_module, "get_settings", lambda: settings)
        monkeypatch.setattr(llm_module, "ChatOpenAI", lambda **kw: ChatOpenAI(http_client=client, **kw))
        return llm_module.create_llm(**role)
    return create


def completion(data=None, *, message=None, finish="stop"):
    return httpx.Response(200, json={
        "id": "offline", "object": "chat.completion", "created": 0, "model": "deepseek-flash",
        "choices": [{"index": 0, "finish_reason": finish, "message": message or {
            "role": "assistant", "content": data if isinstance(data, str) else json.dumps(data)}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130},
    })


def assert_request(request, *, structured=True, limit=None):
    body = json.loads(request.content)
    assert body["model"] == "deepseek-flash"
    assert body["thinking"] == {"type": "disabled"}
    assert "enable_thinking" not in body
    assert "max_completion_tokens" not in body
    if structured:
        assert body["response_format"] == {"type": "json_object"}
        assert "JSON DATA INSTANCE" in body["messages"][0]["content"]
        assert not body.get("tools")
    if limit is not None:
        assert body["max_tokens"] == limit
    return body


def artifacts():
    store = EvidenceStore()
    source = store.add_source(Source(source_id="src1", task_id="D", url="https://example.org/source", title="Source"))
    evidence = store.add_evidence(Evidence(evidence_id="ev1", source_id=source.source_id, task_id="D", content=FACT))
    claim = store.add_claim(Claim(claim_id="claim1", task_id="D", text=FACT, evidence_ids=[evidence.evidence_id]))
    task = ResearchTask(id="D", title="Checkpoint persistence", question="How are checkpoints persisted?",
                        description="Checkpoint persistence")
    return store, claim, task


@pytest.mark.parametrize("base", [BASE, BASE + "/v1"])
@pytest.mark.parametrize("role", [{}, {"writer": True}, {"verifier": True}])
def test_factories_disable_thinking_and_keep_credentials_paired(model_factory, base, role):
    model = model_factory(base=base, **role)
    assert model.extra_body == {"thinking": {"type": "disabled"}}
    assert model.openai_api_key.get_secret_value() == "deepseek-test-key"
    assert str(model.openai_api_base) == base
    assert model.max_retries == 0


@pytest.mark.parametrize("base,name,expected", [
    (BASE, "deepseek-flash", True),
    (BASE + "/v1", "deepseek-v4-pro", True),
    ("https://api.deepseek.com.evil.example/v1", "deepseek-flash", False),
    ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V3", False),
    ("https://example.org/v1", "deepseek-flash", False),
])
def test_capabilities_are_scoped_to_official_endpoint(base, name, expected):
    model = SimpleNamespace(openai_api_base=base, model_name=name, extra_body={"keep": "original"})
    assert prefers_json_object(model) is expected
    assert json_object_options(model) == ({"response_format": {"type": "json_object"}} if expected else {})
    assert model.extra_body == {"keep": "original"}


@pytest.mark.parametrize("stage", ["supervisor", "extraction", "verification", "relevance", "reresearch", "writer_grounding"])
def test_all_seven_structured_stages_use_documented_transport_without_extra_calls(model_factory, monkeypatch, stage):
    store, claim, task = artifacts()
    settings = Settings(_env_file=None)
    monkeypatch.setattr("app.research.evidence_extraction.get_settings", lambda: settings)
    requests = []

    def handler(request):
        if stage == "supervisor" and not requests:
            requests.append(assert_request(request, structured=False))
            return completion("Find checkpoint persistence; summarize the findings.")
        limits = {"extraction": 8192, "verification": 3072, "relevance": 3072, "reresearch": 3072}
        limit = (6144 if len(requests) == 0 else 4096) if stage == "writer_grounding" else limits.get(stage)
        body = assert_request(request, limit=limit)
        requests.append(body)
        if stage == "supervisor":
            return completion({"goal": task.question, "tasks": [task.model_dump(), {
                "id": "S", "title": "Summary", "description": "Summarize", "question": "Summarize findings",
                "task_type": "synthesis", "depends_on": ["D"], "status": "pending"}]})
        if stage == "extraction":
            return completion({"selected_evidence_indexes": [0], "claims": [{"text": FACT, "supporting_evidence_indexes": [0]}]})
        if stage in {"verification", "relevance"}:
            return completion({"results": [{"claim_id": "C1", "status": "SUPPORTED" if stage == "verification" else "ALIGNED",
                                            "reason": "Direct evidence."}]})
        if stage == "reresearch":
            return completion({"results": [{"group_id": "G1", "title": "Direct details", "description": "Find direct persistence evidence"}]})
        payload = json.loads(body["messages"][1]["content"])
        if "items" in payload:
            return completion({"results": [{"item_index": item["item_index"], "status": "SUPPORTED", "reason": "Supported paraphrase."}
                                            for item in payload["items"]]})
        paragraph = {"text": "SQLite saves research checkpoints on disk.", "claim_ids": ["C1"]}
        return completion({"title": "Research report", "scope": "Checkpoint persistence", "executive_summary": [paragraph],
                           "key_findings": [paragraph], "sections": [{"heading": "Research findings", "paragraphs": [paragraph]}],
                           "limitations": [], "conclusion": [paragraph]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        model = model_factory(client)
        original_options = dict(model.extra_body)
        budget = ReResearchCallBudget(2 if stage == "writer_grounding" else 1)
        if stage == "supervisor":
            result = Supervisor(model).create_plan(task.question)
            assert len(result.tasks) == 2
        else:
            with use_call_budget(budget):
                if stage == "extraction":
                    payload = prepare_extraction_payload(task, FACT, [{"url": "https://example.org/source", "title": "Source",
                                                                     "text": FACT + " Further discussion of persistence."}], EvidenceContext())
                    result = EvidenceExtractor(model).extract(payload, ResearchRuntime("D", 120, 15))
                    assert result.claims
                elif stage == "verification":
                    worker = ClaimVerifier(model, settings=settings)
                    assert worker.verify_batch([claim], store)[0].status == "SUPPORTED"
                elif stage == "relevance":
                    worker = RelevanceAssessor(model, settings=settings)
                    assert worker.assess_batch(task.question, [task], [claim])[0].status == "ALIGNED"
                elif stage == "reresearch":
                    gap = ResearchGap(gap_type="COVERAGE_GAP", task_id="D", description="Missing details", missing_information="Direct evidence")
                    worker = ReResearchPlanner(model, settings=settings)
                    assert len(worker.create_plan(task.question, ResearchPlan(goal=task.question, tasks=[task]), select_gap_groups([gap], settings), []).tasks) == 1
                else:
                    item = WritingClaim(claim.claim_id, claim.text, ("ev1",), ("src1",), "D")
                    payload = {"goal": task.question, "scope_dimensions": ["persistence"], "accepted_claims": [asdict(item)],
                               "evidence": {"ev1": {"source_id": "src1", "content": FACT}},
                               "sources": {"src1": {"title": "Source", "url": "https://example.org/source"}},
                               "open_gaps": [], "partial_claims_for_limitations_only": []}
                    gate = ReportGroundingChecker(model, settings=settings)
                    worker = ResearchWriter(model, settings=settings, grounding_checker=gate)
                    result = worker.write(WritingContext(task.question, (item,), json.dumps(payload)))
                    assert result.used_claim_ids == [claim.claim_id]
                    assert worker.last_usage.llm_calls == gate.last_usage.llm_calls == 1
                    assert not gate.last_usage.fallback_reason
            # Writer and Grounding have their own usage budgets, outside the
            # supplemental-research permit counter.
            assert budget.calls == (0 if stage == "writer_grounding" else len(requests))
        assert model.extra_body == original_options
    assert len(requests) == (2 if stage in {"supervisor", "writer_grounding"} else 1)


def test_researcher_actual_deepagents_tool_round_trip(model_factory, monkeypatch):
    requests = []

    @tool
    def local_search(query: str, max_results: int = 5) -> str:
        """Return offline search results for a transport test."""
        return json.dumps({"results": [{"title": "Source", "url": "https://example.org/source", "snippet": FACT}]})

    budget = ToolBudget(1, 0)
    monkeypatch.setattr("app.agents.researcher.budgeted_tools", lambda current: budgeted_tools(current, search_tool=local_search))

    def handler(request):
        body = assert_request(request, structured=False)
        assert "response_format" not in body
        requests.append(body)
        if len(requests) == 1:
            return completion(message={"role": "assistant", "content": "", "tool_calls": [{
                "id": "call_search", "type": "function", "function": {"name": "web_search", "arguments": '{"query":"checkpoint persistence"}'}}]}, finish="tool_calls")
        assert any(message["role"] == "tool" and message["tool_call_id"] == "call_search" for message in body["messages"])
        return completion(FACT)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = Researcher(model_factory(client))._create_agent(budget).invoke({"messages": [HumanMessage(content="Find checkpoint persistence.")]})
    assert result["messages"][-1].text == FACT
    assert len(requests) == 2 and budget.search_calls == 1


@pytest.mark.parametrize("error", ["invalid_reference", "authentication"])
def test_invalid_results_are_not_published_and_authentication_is_not_retried(model_factory, error):
    store, claim, _ = artifacts()
    requests = []

    def handler(request):
        requests.append(assert_request(request, limit=3072))
        if error == "authentication":
            return httpx.Response(401, json={"error": {"message": "Invalid API key", "type": "authentication_error", "code": "invalid_api_key"}})
        return completion({"results": [{"claim_id": "unknown", "status": "SUPPORTED", "reason": "Untrusted reference"}]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        worker = ClaimVerifier(model_factory(client), settings=Settings(_env_file=None))
        with pytest.raises(AuthenticationError if error == "authentication" else VerificationOutputError):
            worker.verify_batch([claim], store)
    # Unknown references fail closed immediately; they are not format repairs.
    assert len(requests) == 1
