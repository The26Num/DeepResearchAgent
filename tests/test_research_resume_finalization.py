"""Resume finalization without replaying a research loop or resetting budgets."""

import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agents.researcher import Researcher
from app.config import Settings
from app.persistence.store import ResearchRunStore
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import prepare_extraction_payload, model_payload, extraction_output_schema
from app.research.source_archive import text_hash
from app.research.task_resources import RunResources, ResearchBudgetExceeded, estimated_tokens
from app.schemas import ResearchTask, Source, Evidence, Claim
from app.structured_output import json_instance_instructions
from archive_support import FACT, URL, Response, OfflineModel, pipeline


def resumed_resource(tmp_path, monkeypatch, *, extraction=1, memo=1, spent=269310, text=None):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    monkeypatch.setattr("app.agents.researcher.get_settings", lambda: cfg)
    monkeypatch.setattr("app.research.evidence_extraction.get_settings", lambda: cfg)
    task = ResearchTask(id="D", title="TAIL_METRIC study", question="Find TAIL_METRIC", description="Find exact facts")
    run = RunResources(cfg, "resume_run")
    item = run.start_task(task)
    body = text or ("Introduction. " + FACT + " Discussion.")
    ref = run.archive.put(requested_url=URL, url=URL, title="Fixed source", raw=body.encode(), text=body, task_id=task.id)
    item.remember(ref)
    item.reserve_tool("web_search")
    # A bounded tool reading, not the model's draft or a navigation snippet.
    item.read_source(ref.snapshot_id, start_char=0, end_char=len(body), max_chars=4000)
    item.record.model_calls = {"researcher": 4, "extraction": extraction, "memo_recovery": memo}
    item.record.estimated_tokens = spent
    item.close()
    resource = RunResources(cfg, "resume_run", run.snapshot()).start_task(task)
    return cfg, task, resource


@pytest.mark.parametrize("prior_extraction,prior_memo", [(1, 1), (1, 0), (0, 1)])
def test_resume_reuses_readings_without_research_or_a_second_memo(tmp_path, monkeypatch, prior_extraction, prior_memo):
    _, task, resource = resumed_resource(tmp_path, monkeypatch, extraction=prior_extraction, memo=prior_memo)
    before = resource.snapshot()
    def forbidden(*args, **kwargs):
        raise AssertionError("Resume must not reconstruct the agent or repeat downloads.")
    monkeypatch.setattr("requests.get", forbidden)
    model = OfflineModel()
    execution = Researcher(model, agent_factory=forbidden).research(task, goal="Find the numerical fact", resources=resource)
    assert len(model.requests) == 1
    assert "evidence_candidates" in model.requests[0][0][-1].text
    assert resource.record.model_calls["researcher"] == 4
    assert resource.record.model_calls["memo_recovery"] == prior_memo
    assert resource.record.model_calls["extraction"] == prior_extraction + 1
    assert before.estimated_tokens < resource.record.estimated_tokens <= before.limits["estimated_tokens"]
    assert resource.record.calls == before.calls
    evidence = next(item for item in execution.evidence_bundle.evidence if item.content == FACT)
    assert evidence.origin.text_hash == text_hash(FACT)
    assert execution.evidence_bundle.claims and FACT in execution.memo


def test_resume_cannot_bypass_spent_extraction_permits(tmp_path, monkeypatch):
    _, task, resource = resumed_resource(tmp_path, monkeypatch, extraction=3)
    model = OfflineModel()
    with pytest.raises(ResearchBudgetExceeded, match="evidence-extraction call budget exhausted"):
        Researcher(model, agent_factory=lambda budget: pytest.fail("Must not repeat research")).research(task, resources=resource)
    assert not model.requests and resource.record.model_calls["extraction"] == 3


def test_no_room_for_one_complete_candidate_fails_before_a_model_request(tmp_path, monkeypatch):
    _, task, resource = resumed_resource(tmp_path, monkeypatch, spent=299000)
    model = OfflineModel()
    with pytest.raises(ResearchBudgetExceeded, match="complete source candidate"):
        Researcher(model, agent_factory=lambda budget: pytest.fail("Must not repeat research")).research(task, resources=resource)
    assert not model.requests and resource.record.estimated_tokens == 299000


def test_selection_uses_remaining_cumulative_allowance_without_clipping_fragments(tmp_path, monkeypatch):
    from app.research.evidence_extraction import _PROMPT
    body = "\n".join([f"Observation {i}: " + "测量条件与产品分类明确" * 12 + "。" for i in range(18)]) + "\n" + FACT
    _, task, resource = resumed_resource(tmp_path, monkeypatch, text=body)
    # Confirm the tail too, even if the first bounded reading did not include it.
    ref = next(iter(resource.record.snapshots.values()))
    resource.read_source(ref.snapshot_id, start_char=max(0, len(body) - 100), end_char=len(body))
    small = prepare_extraction_payload(task, "", [], EvidenceContext(), resources=resource)
    before = resource.record.estimated_tokens
    resource.record.estimated_tokens = 0
    spacious = prepare_extraction_payload(task, "", [], EvidenceContext(), resources=resource)
    resource.record.estimated_tokens = before
    assert 0 < len(small["candidates"]) < len(spacious["candidates"])
    assert any(FACT == candidate["content"] for candidate in small["candidates"])
    assert "omitted" in small["selection_note"]
    for candidate in small["candidates"]:
        origin = candidate["_archive_origin"]
        assert body[origin["start_char"]:origin["end_char"]] == candidate["content"]
    instructions = _PROMPT.read_text(encoding="utf-8") + json_instance_instructions(extraction_output_schema(len(small["candidates"])))
    required = estimated_tokens([SystemMessage(content=instructions), HumanMessage(content=json.dumps(model_payload(small), ensure_ascii=False))])
    required += resource.run.settings.evidence_extraction_max_tokens + 1500
    assert required <= resource.record.limits["estimated_tokens"] - before


def test_complete_upstream_chains_cannot_be_removed_to_hide_a_spent_budget(tmp_path, monkeypatch):
    _, task, resource = resumed_resource(tmp_path, monkeypatch, spent=288000)
    source = Source(task_id="UP", url="https://example.org/upstream", title="Original")
    evidence = Evidence(task_id="UP", source_id=source.source_id, content="上游完整实验条件和限制。" * 180)
    claim = Claim(task_id="UP", text="Upstream result", evidence_ids=[evidence.evidence_id])
    upstream = EvidenceContext((source,), (evidence,), (claim,))
    with pytest.raises(ResearchBudgetExceeded, match="complete upstream support chains"):
        prepare_extraction_payload(task, "", [], upstream, resources=resource)
    assert resource.record.estimated_tokens == 288000


def test_real_checkpoint_resume_continues_failed_extraction_and_keeps_completed_research(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), research_db_path=str(tmp_path / "runs.db"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, first_model = pipeline(db, cfg, monkeypatch)
    original = worker.researcher._evidence_extractor.extract
    def failure_after_model_response(payload, runtime):
        original(payload, runtime)
        raise ValueError("offline simulated extraction validation failure")
    worker.researcher._evidence_extractor.extract = failure_after_model_response
    with pytest.raises(RuntimeError, match="simulated extraction validation failure"):
        worker.run("Locate the fixed TAIL_METRIC result")
    run = db.list_runs()[0]
    before = db.load_latest_checkpoint(run.run_id).state.resources.tasks["D"]
    assert before.model_calls["extraction"] == 1 and not before.committed
    resumed, second_model = pipeline(db, cfg, monkeypatch)
    create_agent = resumed.researcher._agent_factory
    def forbid_discovery(budget):
        if budget.resources.task.id == "D":
            pytest.fail("D research was already performed; resume extraction only.")
        return create_agent(budget)
    resumed.researcher._agent_factory = forbid_discovery
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("Resume must reuse the committed private source snapshot."))
    result = resumed.resume(run.run_id)
    after = db.load_latest_checkpoint(run.run_id).state.resources.tasks["D"]
    assert db.get_run(run.run_id).status == "COMPLETED"
    assert after.model_calls["researcher"] == before.model_calls["researcher"]
    assert after.model_calls.get("memo_recovery", 0) == before.model_calls.get("memo_recovery", 0)
    assert after.model_calls["extraction"] == 2 and after.calls == before.calls
    assert after.attempts == 2 and after.committed
    assert after.estimated_tokens <= after.limits["estimated_tokens"]
    assert result.rendered_report.references and "[1]" in result.rendered_report.markdown
