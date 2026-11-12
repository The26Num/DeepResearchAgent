"""Disabling the cost estimate quota keeps accounting and independent limits."""

import pytest
from langchain_core.messages import HumanMessage

from app.config import Settings
from app.persistence.models import ResearchRunState
from app.persistence.serialization import state_from_json, StateIntegrityError
from app.persistence.store import ResearchRunStore
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import prepare_extraction_payload
from app.research.task_resources import RunResources, ResearchBudgetExceeded, ResearchContextExceeded
from app.schemas import ResearchTask, ResearchPlan


def resources(tmp_path, *, enabled=False, run_id="quota_run", state=None):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"),
                   enforce_task_estimated_token_budget=enabled)
    task = ResearchTask(id="A", title="Analysis", question="Analyze the measurement", description="Analyze", task_type="analysis")
    run = RunResources(cfg, run_id, state)
    return cfg, task, run.start_task(task)


def test_quota_is_on_by_default_and_can_be_configured_without_credentials(tmp_path):
    assert Settings(_env_file=None).enforce_task_estimated_token_budget is True
    environment = tmp_path / "quota.env"
    environment.write_text("ENFORCE_TASK_ESTIMATED_TOKEN_BUDGET=false\n", encoding="utf-8")
    assert Settings(_env_file=environment).enforce_task_estimated_token_budget is False


def test_disabled_quota_records_cost_beyond_the_saved_limit_and_survives_resume(tmp_path):
    cfg, task, item = resources(tmp_path)
    item.record.estimated_tokens = 297525
    item.reserve_model("extraction", [HumanMessage(content="a" * 20000)])
    spent = item.record.estimated_tokens
    assert spent > 300000 and item.record.limits["estimated_tokens"] == 300000
    assert not item.record.estimated_token_budget_enforced
    item.close()
    resumed = RunResources(cfg, "quota_run", item.run.snapshot()).start_task(task)
    assert resumed.record.estimated_tokens == spent
    resumed.reserve_model("extraction", [HumanMessage(content="continued extraction")])
    assert resumed.record.estimated_tokens > spent and resumed.record.model_calls["extraction"] == 2


def test_enabled_quota_still_rejects_the_same_request(tmp_path):
    _, _, item = resources(tmp_path, enabled=True)
    item.record.estimated_tokens = 297525
    with pytest.raises(ResearchBudgetExceeded, match="cumulative estimated-token budget"):
        item.reserve_model("extraction", [HumanMessage(content="a" * 20000)])
    assert item.record.estimated_tokens == 297525 and item.record.model_calls == {}


def test_disabled_quota_keeps_context_and_extraction_call_limits(tmp_path):
    _, _, item = resources(tmp_path)
    with pytest.raises(ResearchContextExceeded):
        item.reserve_model("extraction", [HumanMessage(content="a" * 70000)])
    assert not item.record.model_calls
    for _ in range(3):
        item.reserve_model("extraction", [HumanMessage(content="complete candidate")])
    with pytest.raises(ResearchBudgetExceeded, match="evidence-extraction call budget"):
        item.reserve_model("extraction", [HumanMessage(content="fourth")])
    item.reserve_tool("web_search")
    item.reserve_tool("web_search")
    with pytest.raises(ResearchBudgetExceeded):
        item.reserve_tool("web_search")


def test_disabled_quota_does_not_drop_read_candidates_for_a_spent_estimate(tmp_path):
    _, task, item = resources(tmp_path)
    text = "A measured result is directly supported. Its limitations remain explicit."
    ref = item.run.archive.put(requested_url="https://example.org/study", url="https://example.org/study",
        title="Study", raw=text.encode(), text=text, task_id=task.id)
    item.remember(ref)
    item.read_source(ref.snapshot_id)
    item.record.estimated_tokens = 399000
    payload = prepare_extraction_payload(task, "", [], EvidenceContext(), resources=item)
    assert payload["candidates"] and payload["fetched_pages"][0]["text"] == text
    assert item.record.estimated_tokens == 399000
    item.reserve_model("extraction", [HumanMessage(content="complete selected candidate")])
    assert item.record.estimated_tokens > 399000


def test_explicit_disable_applies_to_old_attempt_without_resetting_limits(tmp_path):
    cfg, task, old = resources(tmp_path, enabled=True)
    old.record.estimated_tokens = 297525
    old.record.model_calls = {"researcher": 5, "memo_recovery": 1}
    old.close()
    new_cfg = cfg.model_copy(update={"enforce_task_estimated_token_budget": False})
    continued = RunResources(new_cfg, "quota_run", old.run.snapshot()).start_task(task)
    assert not continued.record.estimated_token_budget_enforced
    assert continued.record.estimated_tokens == 297525 and continued.record.model_calls == old.record.model_calls
    assert continued.record.limits == old.record.limits
    continued.reserve_model("extraction", [HumanMessage(content="complete upstream support chain" * 300)])
    assert continued.record.estimated_tokens > 300000


def test_sqlite_checkpoint_accepts_explicit_disabled_policy_and_preserves_usage(tmp_path):
    db = ResearchRunStore(tmp_path / "runs.db")
    run = db.create_run("Goal")
    _, task, item = resources(tmp_path, run_id=run.run_id)
    item.record.estimated_tokens = 297525
    item.reserve_model("extraction", [HumanMessage(content="a" * 20000)])
    state = ResearchRunState(goal="Goal", research_plan=ResearchPlan(goal="Goal", tasks=[task]),
                             resources=item.run.snapshot())
    db.save_checkpoint(run.run_id, "TASK_RESOURCE_RESERVED:A", state)
    restored = db.load_latest_checkpoint(run.run_id).state.resources.tasks[task.id]
    assert restored.estimated_tokens == item.record.estimated_tokens > restored.limits["estimated_tokens"]
    assert not restored.estimated_token_budget_enforced
    # An over-limit enabled record must still be rejected as inconsistent.
    state.resources.tasks[task.id].estimated_token_budget_enforced = True
    with pytest.raises(StateIntegrityError, match="budget"):
        state_from_json(state.model_dump_json())


def test_reenabling_after_over_quota_usage_fails_without_resetting_or_corrupting_history(tmp_path):
    cfg, task, item = resources(tmp_path)
    item.record.estimated_tokens = 310000
    item.close()
    snapshot = item.run.snapshot()
    enabled = cfg.model_copy(update={"enforce_task_estimated_token_budget": True})
    coordinator = RunResources(enabled, "quota_run", snapshot)
    with pytest.raises(ResearchBudgetExceeded, match="Cannot re-enable"):
        coordinator.start_task(task)
    retained = coordinator.snapshot().tasks[task.id]
    assert retained.estimated_tokens == 310000 and not retained.estimated_token_budget_enforced


def test_disabled_policy_resumes_old_analysis_checkpoint_through_citation(tmp_path, monkeypatch):
    from archive_support import pipeline, Response
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), research_db_path=str(tmp_path / "runs.db"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, _ = pipeline(db, cfg, monkeypatch)
    original = worker.researcher._evidence_extractor.extract
    def fail_analysis(payload, runtime):
        result = original(payload, runtime)
        if payload["task"]["task_id"] == "A":
            raise ValueError("offline interrupted analysis finalization")
        return result
    worker.researcher._evidence_extractor.extract = fail_analysis
    with pytest.raises(RuntimeError, match="interrupted analysis finalization"):
        worker.run("Locate the fixed numerical evidence")
    run = db.list_runs()[0]
    state = db.load_latest_checkpoint(run.run_id).state
    # Historical fixture with an almost exhausted, enabled cost quota.
    prior = state.resources.tasks["A"]
    prior.estimated_tokens = 297525
    assert prior.estimated_token_budget_enforced and state.resources.tasks["D"].committed
    db.save_checkpoint(run.run_id, "TASK_RESOURCE_RESERVED:A", state)
    disabled = cfg.model_copy(update={"enforce_task_estimated_token_budget": False})
    continued, _ = pipeline(db, disabled, monkeypatch)
    factory = continued.researcher._agent_factory
    def no_analysis_research(budget):
        assert budget.resources.task.id != "A", "Saved analysis must continue extraction only."
        return factory(budget)
    continued.researcher._agent_factory = no_analysis_research
    monkeypatch.setattr("requests.get", lambda *a, **kw: pytest.fail("Completed discovery must not re-download."))
    result = continued.resume(run.run_id)
    final = db.load_latest_checkpoint(run.run_id).state
    analysis = final.resources.tasks["A"]
    assert db.get_run(run.run_id).status == "COMPLETED"
    assert analysis.estimated_tokens > 300000 and not analysis.estimated_token_budget_enforced
    assert analysis.model_calls["researcher"] == prior.model_calls["researcher"]
    assert final.resources.tasks["D"] == state.resources.tasks["D"]
    assert result.rendered_report.references and "[1]" in result.rendered_report.markdown
