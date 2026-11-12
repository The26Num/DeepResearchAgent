"""Large recent readings, legal context compaction, and synthesis Resume."""

import json

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agents.researcher import Researcher
from app.config import Settings
from app.persistence.store import ResearchRunStore
from app.research.evidence_context import EvidenceContext
from app.research.resource_middleware import ResearchContextMiddleware, message_blocks
from app.research.resource_tools import resource_tools
from app.research.runtime import ResearchModelCallbacks
from app.research.task_resources import RunResources, ResearchContextExceeded, estimated_tokens
from app.research.tool_budget import ToolBudget
from app.schemas import ResearchTask, Source, Evidence, Claim
from archive_support import FACT, URL, OfflineModel, Response, pipeline


def setup(tmp_path, monkeypatch=None, **overrides):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), **overrides)
    if monkeypatch:
        monkeypatch.setattr("app.agents.researcher.get_settings", lambda: cfg)
        monkeypatch.setattr("app.research.evidence_extraction.get_settings", lambda: cfg)
    task = ResearchTask(id="D", title="Study", question="Find the measurement", description="Study")
    item = RunResources(cfg, "context_run").start_task(task)
    return cfg, task, item


def test_recent_large_bodies_become_position_receipts_without_losing_ledger_or_methods(tmp_path):
    cfg, _, item = setup(tmp_path, max_research_context_estimated_tokens=10000, research_model_max_tokens=512)
    item.load_skill("academic_review")
    body = "材料" * 1800 + "\n" + "讨论" * 1800
    ref = item.run.archive.put(requested_url=URL, url=URL, title="Study", raw=body.encode(), text=body, task_id="D")
    item.remember(ref)
    messages = [HumanMessage(content="当前任务与完整上游证据。\n" * 90)]
    for index, (start, end) in enumerate(((0, 3600), (3601, 7201))):
        result = item.read_source(ref.snapshot_id, start_char=start, end_char=end)
        messages += [AIMessage(content="", tool_calls=[{"id": str(index), "name": "read_source", "args": {"snapshot_id": ref.snapshot_id}}]),
                     ToolMessage(content=json.dumps(result, ensure_ascii=False), name="read_source", tool_call_id=str(index))]
    before = item.snapshot()
    request = ModelRequest(model=None, messages=messages, system_message=SystemMessage(content="Base rules"), tools=[])
    compact = ResearchContextMiddleware(ToolBudget(0, 0, resources=item))._request(request)
    assert compact.messages[0].text == messages[0].text
    assert len(message_blocks(compact.messages)) == 3
    receipts = [json.loads(message.text) for message in compact.messages if isinstance(message, ToolMessage)]
    assert len(receipts) == 2 and all(row["content_omitted"] and "text" not in row for row in receipts)
    assert [(row["start_char"], row["end_char"]) for row in receipts] == [(0, 3600), (3601, 7201)]
    assert all(row["snapshot_id"] == ref.snapshot_id for row in receipts)
    assert "对每篇材料区分研究问题" in compact.system_message.text
    assert "CONTEXT NOTICE" in compact.system_message.text
    assert estimated_tokens([compact.system_message, *compact.messages], compact.tools) + 512 <= 10000
    assert item.snapshot().readings == before.readings
    assert item.run.archive.read(ref)[0] == body
    assert "材料" * 100 in messages[2].text  # Original graph/tool content was not mutated.


@pytest.mark.parametrize("name", ["list_sources", "search_sources"])
def test_recent_navigation_receipt_retains_whole_ids_and_ranges(tmp_path, name):
    _, _, item = setup(tmp_path, max_research_context_estimated_tokens=10000, research_model_max_tokens=512)
    ids = ["snap_" + str(index) * 64 for index in range(1, 6)]
    if name == "list_sources":
        payload = {"sources": [{"snapshot_id": key, "title": "页面标题" * 100, "body_chars": 4000} for key in ids]}
        container = "sources"
    else:
        payload = {"hits": [{"snapshot_id": key, "section_id": "p_8", "start_char": 12000, "end_char": 12280,
                             "preview": "导航内容" * 70} for key in ids]}
        container = "hits"
    messages = [HumanMessage(content="当前目标与完整必要证据。" * 140),
                AIMessage(content="", tool_calls=[{"id": "nav", "name": name, "args": {}}]),
                ToolMessage(content=json.dumps(payload, ensure_ascii=False), name=name, tool_call_id="nav")]
    result = ResearchContextMiddleware(ToolBudget(0, 0, resources=item))._request(ModelRequest(
        model=None, messages=messages, system_message=SystemMessage(content="Keep source identities"), tools=[]))
    compact = json.loads(result.messages[-1].text)
    assert compact["content_omitted"] and [row["snapshot_id"] for row in compact[container]] == ids
    if name == "search_sources":
        assert all(row["start_char"] == 12000 and row["end_char"] == 12280 for row in compact[container])
    assert not item.record.readings  # Compaction/navigation itself cannot fabricate source reads.
    assert result.messages[0] == messages[0] and len(message_blocks(result.messages)) == 2


def test_large_recent_tool_planning_can_shrink_without_cutting_calls_or_results(tmp_path):
    _, _, item = setup(tmp_path, max_research_context_estimated_tokens=10000, research_model_max_tokens=512)
    messages = [HumanMessage(content="Formal complete upstream support chains.\n" * 110)]
    for index in range(3):
        messages += [AIMessage(content="Model planning prose. " * 160,
                              tool_calls=[{"id": str(index), "name": "read_source", "args": {"snapshot_id": "snap_" + "a" * 64}}]),
                     ToolMessage(content=json.dumps({"snapshot_id": "snap_" + "a" * 64, "start_char": index * 20,
                                                     "end_char": index * 20 + 20, "text": "Exact source preview."}),
                                 name="read_source", tool_call_id=str(index))]
    request = ModelRequest(model=None, messages=messages, tools=[])
    compact = ResearchContextMiddleware(ToolBudget(0, 0, resources=item))._request(request)
    assert compact.messages[0] == messages[0] and len(message_blocks(compact.messages)) == 4
    assert [m.tool_calls for m in compact.messages if isinstance(m, AIMessage)] == [m.tool_calls for m in messages if isinstance(m, AIMessage)]
    assert [m.text for m in compact.messages if isinstance(m, ToolMessage)] == [m.text for m in messages if isinstance(m, ToolMessage)]
    assert any("planning omitted" in m.text for m in compact.messages if isinstance(m, AIMessage))
    assert estimated_tokens([compact.system_message, *compact.messages], compact.tools) + 512 <= 10000


def test_oversized_mandatory_input_fails_before_model_without_cutting_support_chains(tmp_path):
    _, _, item = setup(tmp_path, max_research_context_estimated_tokens=10000, research_model_max_tokens=512)
    request = ModelRequest(model=None, messages=[HumanMessage(content="完整上游支持链" * 2000)], tools=[])
    def forbidden(request):
        pytest.fail("Oversized mandatory context must not be sent to a provider.")
    with pytest.raises(ResearchContextExceeded, match="input_estimate=.*output_reserve=.*limit="):
        ResearchContextMiddleware(ToolBudget(0, 0, resources=item)).wrap_model_call(request, forbidden)
    assert request.messages[0].text == "完整上游支持链" * 2000


def test_context_stop_uses_one_reserved_memo_then_exact_extraction(tmp_path, monkeypatch):
    _, task, item = setup(tmp_path, monkeypatch)
    text = "Introduction. " + FACT + " Discussion."
    ref = item.run.archive.put(requested_url=URL, url=URL, title="Study", raw=text.encode(), text=text, task_id=task.id)
    item.remember(ref)
    item.reserve_tool("web_search")
    item.read_source(ref.snapshot_id)
    class RecoveryModel(OfflineModel):
        def _generate(self, messages, **kwargs):
            if "MEMO FINALIZATION" in messages[-1].text:
                self._requests.append((list(messages), kwargs))
                return ChatResult(generations=[ChatGeneration(message=AIMessage(content="## 主要发现\n" + FACT))])
            return super()._generate(messages, **kwargs)
    class ContextStoppedAgent:
        def invoke(self, request):
            raise ResearchContextExceeded("offline context compaction stop")
    model = RecoveryModel()
    result = Researcher(model, agent_factory=lambda budget: ContextStoppedAgent()).research(task, resources=item)
    assert item.record.model_calls == {"memo_recovery": 1, "extraction": 1}
    assert len(model.requests) == 2 and result.evidence_bundle.claims
    assert any(row.content == FACT and row.origin for row in result.evidence_bundle.evidence)


def test_synthesis_with_prior_research_resumes_only_upstream_extraction(tmp_path, monkeypatch):
    cfg, _, first = setup(tmp_path, monkeypatch, enforce_task_estimated_token_budget=False)
    task = ResearchTask(id="S", title="Synthesis", question="Summarize measurements", description="Synthesize", task_type="synthesis", depends_on=["A"])
    initial = first.run.start_task(task, ["A"])
    initial.record.model_calls = {"researcher": 9}
    initial.record.estimated_tokens = 479212
    initial.close()
    resumed = RunResources(cfg, "context_run", first.run.snapshot()).start_task(task, ["A"])
    source = Source(task_id="A", url=URL, title="Study")
    evidence = Evidence(task_id="A", source_id=source.source_id, content=FACT)
    claim = Claim(task_id="A", text=FACT, evidence_ids=[evidence.evidence_id])
    upstream = EvidenceContext((source,), (evidence,), (claim,))
    model = OfflineModel()
    result = Researcher(model, agent_factory=lambda budget: pytest.fail("Do not restart synthesis source reading")).research(
        task, evidence_context=upstream, resources=resumed)
    assert resumed.record.model_calls == {"researcher": 9, "extraction": 1}
    assert len(model.requests) == 1 and not result.evidence_bundle.evidence and not result.evidence_bundle.sources
    assert result.evidence_bundle.claims[0].evidence_ids == [evidence.evidence_id]
    assert not resumed.record.calls and resumed.record.estimated_tokens > 479212


def test_historical_synthesis_stop_resumes_checkpoint_through_cited_report(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), research_db_path=str(tmp_path / "runs.db"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, model = pipeline(db, cfg, monkeypatch)
    factory = worker.researcher._agent_factory
    def history_failure(budget):
        if budget.resources.task.id != "S":
            return factory(budget)
        class HistoricalSynthesisAgent:
            def invoke(self, request):
                model.bind_tools(resource_tools(budget)).invoke(
                    [HumanMessage(content=request["messages"][0]["content"])],
                    config={"callbacks": [ResearchModelCallbacks(budget.runtime)]})
                raise RuntimeError("offline historical synthesis context failure")
        return HistoricalSynthesisAgent()
    worker.researcher._agent_factory = history_failure
    with pytest.raises(RuntimeError, match="historical synthesis context failure"):
        worker.run("Locate the original measurement")
    run = db.list_runs()[0]
    before = db.load_latest_checkpoint(run.run_id).state
    assert before.resources.tasks["S"].model_calls == {"researcher": 1}
    fresh, new_model = pipeline(db, cfg, monkeypatch)
    fresh.researcher._agent_factory = lambda budget: pytest.fail("Completed research and failed synthesis must not restart agents")
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("Completed sources must not re-download"))
    result = fresh.resume(run.run_id)
    after = db.load_latest_checkpoint(run.run_id).state
    assert db.get_run(run.run_id).status == "COMPLETED"
    assert after.resources.tasks["S"].model_calls == {"researcher": 1, "extraction": 1}
    assert len(new_model.requests) == 1 and result.rendered_report.references and "[1]" in result.rendered_report.markdown
    assert all(after.resources.tasks[key] == before.resources.tasks[key] for key in ("D", "A"))
