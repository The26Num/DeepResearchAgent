"""Phase 6: real SQLite and deterministic models; no provider/network dependency."""

from collections import Counter
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from app.agents.relevance import RelevanceAssessor
from app.agents.verifier import ClaimVerifier
from app.agents.writer import WriterUsage
from app.config import Settings
from app.persistence.models import ResearchRunState, RunStatus, STATE_VERSION
from app.persistence.store import ResearchRunStore, ResearchRunNotFoundError, NoCheckpointError
from app.persistence.serialization import StateIntegrityError, IncompatibleCheckpointError, state_from_json
from app.research.evidence_store import EvidenceStore
from app.research.gap_detection import GapDetector
from app.research.orchestrator import ResearchOrchestrator
from app.research.reresearch_budget import ReResearchCallBudget, active_call_budget
from app.schemas import (Source, Evidence, Claim, ResearchTask, ResearchPlan, TaskEvidenceBundle,
                         ResearchExecutionResult, CompactResearchResult, ResearchReport, VerificationResult,
                         ReResearchPlan, ReResearchTask)


def plan():
    return ResearchPlan(goal="Goal", tasks=[
        ResearchTask(id="D1", title="First", question="Find first", description="First dimension"),
        ResearchTask(id="D2", title="Second", question="Find second", description="Second dimension"),
        ResearchTask(id="A", title="Analysis", question="Compare", description="Compare dimensions",
                     task_type="analysis", depends_on=["D1", "D2"]),
        ResearchTask(id="S", title="Synthesis", question="Summarize", description="Synthesize",
                     task_type="synthesis", depends_on=["A"]),
    ])


def execution(task):
    source = Source(task_id=task.id, url="https://example.org/paper", title="Paper")
    evidence = Evidence(task_id=task.id, source_id=source.source_id, content=f"Fact {task.id}")
    claim = Claim(task_id=task.id, text=f"Fact {task.id}", evidence_ids=[evidence.evidence_id])
    return ResearchExecutionResult(memo=f"Memo {task.id}", tools_used=["web_search"],
        compact=CompactResearchResult(task_id=task.id, summary=f"Fact {task.id}"),
        evidence_bundle=TaskEvidenceBundle(task_id=task.id, sources=[source], evidence=[evidence], claims=[claim]))


class Decisions:
    def __init__(self, status):
        self.status, self.calls, self.fail = status, [], False

    def with_structured_output(self, *args, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        payload = json.loads(messages[1].text)
        self.calls.append([item["text"] for item in payload["claims"]])
        if self.fail:
            raise RuntimeError("model failure")
        return {"results": [dict(claim_id=item["claim_id"], status=self.status(item) if callable(self.status) else self.status, reason="Checked directly")
                            for item in payload["claims"]]}


class Writer:
    def __init__(self):
        self.calls, self.last_usage, self.last_grounding = 0, WriterUsage(), ()

    def write(self, context):
        self.calls += 1
        self.last_usage = WriterUsage(accepted_claims=len(context.accepted_claims), llm_calls=1)
        paragraphs = [{"text": item.text.replace("_", " "), "claim_ids": [item.claim_id]} for item in context.accepted_claims]
        if not paragraphs:
            return ResearchReport(title="Insufficient findings", scope="Goal", executive_summary=[], key_findings=[],
                                  sections=[], conclusion=[], limitations=[])
        return ResearchReport(title="Research findings", scope="Goal", executive_summary=paragraphs[:3],
                              key_findings=paragraphs, sections=[{"heading": "Findings", "paragraphs": paragraphs[:4]}],
                              conclusion=paragraphs[:2], limitations=[])


def pipeline(db, monkeypatch, *, fail_stage=None, interrupt=False, failures=None, relevance="ALIGNED", rr=False,
             concurrency=1, cfg=None):
    cfg = cfg or Settings(_env_file=None, max_relevance_claims_per_batch=1)
    monkeypatch.setattr("app.research.orchestrator.get_settings", lambda: cfg)
    calls = Counter()
    failures = failures if failures is not None else set()
    def research(task, **kwargs):
        calls[task.id] += 1
        if task.id in failures:
            raise RuntimeError("injected research failure")
        budget = active_call_budget()
        if budget:
            budget.reserve("researcher")
            budget.reserve("extraction")
        return execution(task)
    def create_plan(query):
        calls["supervisor"] += 1
        return plan()
    def stop(stage):
        if stage == fail_stage:
            if interrupt:
                raise KeyboardInterrupt()
            raise RuntimeError("injected crash")
    verify, alignment, writer = Decisions("SUPPORTED"), Decisions(relevance), Writer()
    orchestrator = ResearchOrchestrator(SimpleNamespace(create_plan=create_plan), SimpleNamespace(research=research),
        verifier=ClaimVerifier(verify, settings=cfg), gap_detector=GapDetector(RelevanceAssessor(alignment, settings=cfg)),
        reresearch=rr, writer=writer, max_concurrency=concurrency, run_store=db, failure_injector=stop)
    if rr:
        def supplemental_plan(query, original, groups, claims):
            calls["planner"] += 1
            active_call_budget().reserve("planner")
            return ReResearchPlan(round_id=1, selected_gap_ids=[key for group in groups for key in group.gap_ids],
                tasks=[ReResearchTask(id=f"RR_{group.task_id}_1", title="Supplemental", question="Find missing evidence",
                    description="Find missing dimension", origin_task_id=group.task_id, origin_gap_ids=list(group.gap_ids))
                    for group in groups])
        orchestrator.reresearch_planner = SimpleNamespace(create_plan=supplemental_plan)
    return orchestrator, calls, verify, alignment, writer


@pytest.fixture
def db(tmp_path):
    return ResearchRunStore(tmp_path / "research.db")


def test_create_runs_and_metadata_and_no_checkpoint(db):
    first, second = db.create_run("Goal"), db.create_run("Goal")
    assert first.run_id != second.run_id
    assert db.get_run(first.run_id).status == RunStatus.RUNNING
    assert db.list_runs()[0].goal == "Goal"
    with pytest.raises(NoCheckpointError, match="No checkpoint available"):
        db.load_latest_checkpoint(first.run_id)
    with pytest.raises(ResearchRunNotFoundError, match="Research run not found"):
        db.get_run("unknown")
    with db.connection() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_checkpoint_sequences_latest_and_atomic_rollback(db):
    run = db.create_run("Goal")
    state = ResearchRunState(goal="Goal", research_plan=plan())
    one = db.save_checkpoint(run.run_id, "PLAN_CREATED", state)
    two = db.save_checkpoint(run.run_id, "SECOND", state)
    assert (one.sequence, two.sequence) == (1, 2)
    assert db.get_run(run.run_id).last_checkpoint_id == two.checkpoint_id
    assert db.load_latest_checkpoint(run.run_id).checkpoint_id == two.checkpoint_id
    with db.connection() as conn, conn:
        conn.execute("CREATE TRIGGER reject_checkpoint BEFORE UPDATE OF last_checkpoint_id ON research_runs "
                     "BEGIN SELECT RAISE(ABORT,'injected database error'); END")
    with pytest.raises(sqlite3.IntegrityError):
        db.save_checkpoint(run.run_id, "THIRD", state)
    assert db.load_latest_checkpoint(run.run_id).checkpoint_id == two.checkpoint_id
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0] == 2


def test_ids_aliases_dedup_and_provenance_survive_json_and_sqlite(db):
    store = EvidenceStore()
    first = execution(plan().tasks[0]).evidence_bundle
    second = execution(plan().tasks[1]).evidence_bundle
    second.evidence[0] = second.evidence[0].model_copy(update={"content": first.evidence[0].content})
    store.merge(first)
    store.merge(second)
    state = ResearchRunState(goal="Goal", research_plan=plan(), evidence_store=store.snapshot())
    run = db.create_run("Goal")
    db.save_checkpoint(run.run_id, "PLAN_CREATED", state)
    loaded = db.load_latest_checkpoint(run.run_id).state
    restored = EvidenceStore.restore(loaded.evidence_store.model_dump(mode="json"))
    assert restored.snapshot() == store.snapshot()
    assert restored.reuse_counts == store.reuse_counts
    assert restored.get_evidence(second.evidence[0].evidence_id).evidence_id == first.evidence[0].evidence_id
    restored.merge(second)
    assert restored.snapshot() == store.snapshot()
    third = execution(plan().tasks[2]).evidence_bundle
    restored.merge(third)
    assert restored.counts == dict(sources=1, evidence=2, claims=3)


@pytest.mark.parametrize("stage", ["PLAN_CREATED", "TASK_COMPLETED:D1", "TASK_COMPLETED:D2",
    "INITIAL_RESEARCH_COMPLETED", "GAP_DETECTION_COMPLETED", "WRITER_COMPLETED", "CITATION_COMPLETED", "RUN_COMPLETED"])
def test_resume_skips_every_completed_stage_and_preserves_final_report(db, monkeypatch, stage):
    worker, calls, verify, relevance, writer = pipeline(db, monkeypatch, fail_stage=stage)
    with pytest.raises(RuntimeError, match="injected crash"):
        worker.run("Goal")
    run = db.list_runs()[0]
    assert run.status == RunStatus.FAILED
    checkpoint = db.load_latest_checkpoint(run.run_id)
    assert checkpoint.stage == stage
    before = calls.copy(), len(verify.calls), len(relevance.calls), writer.calls
    # Fresh runtime and fresh model instances, as in a new process.
    resumed, new_calls, new_verify, new_relevance, new_writer = pipeline(db, monkeypatch)
    result = resumed.resume(run.run_id)
    assert result.run_id == run.run_id
    assert db.get_run(run.run_id).status == RunStatus.COMPLETED
    assert calls["supervisor"] == 1 and new_calls["supervisor"] == 0
    for task in checkpoint.state.research_plan.tasks:
        if task.status == "completed":
            assert new_calls[task.id] == 0
    if stage in {"GAP_DETECTION_COMPLETED", "WRITER_COMPLETED", "CITATION_COMPLETED", "RUN_COMPLETED"}:
        assert not new_verify.calls and not new_relevance.calls
    if stage in {"WRITER_COMPLETED", "CITATION_COMPLETED", "RUN_COMPLETED"}:
        assert new_writer.calls == 0
        assert result.report == checkpoint.state.research_report
    if stage in {"CITATION_COMPLETED", "RUN_COMPLETED"}:
        assert result.rendered_report == checkpoint.state.rendered_report
    assert result.evidence_store.counts == dict(sources=1, evidence=4, claims=4)
    assert len(result.verification_store.snapshot()) == 4
    assert before == (calls.copy(), len(verify.calls), len(relevance.calls), writer.calls)


def test_parallel_failure_saves_successful_sibling_and_twice_resume_never_duplicates(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch, failures={"D2"}, concurrency=2)
    with pytest.raises(RuntimeError, match="injected research failure"):
        worker.run("Goal")
    run = db.list_runs()[0]
    first = db.load_latest_checkpoint(run.run_id)
    assert first.stage == "TASK_COMPLETED:D1"
    assert first.state.research_plan.tasks[0].status == "completed"
    ids = first.state.evidence_store.model_dump(mode="json")
    again, calls, *_ = pipeline(db, monkeypatch, failures={"D2"}, concurrency=2)
    with pytest.raises(RuntimeError):
        again.resume(run.run_id)
    assert calls == {"D2": 1}
    final, calls, *_ = pipeline(db, monkeypatch)
    result = final.resume(run.run_id)
    assert calls == {"D2": 1, "A": 1, "S": 1}
    assert db.get_run(run.run_id).resume_count == 2
    assert result.evidence_store.get_source(ids["sources"][0]["source_id"]).model_dump(mode="json") == ids["sources"][0]
    assert result.evidence_store.counts == dict(sources=1, evidence=4, claims=4)


def test_running_task_with_saved_research_reuses_result_and_only_verifies(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch, fail_stage="TASK_RESEARCH_COMPLETED:D1")
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    checkpoint = db.load_latest_checkpoint(run.run_id)
    assert checkpoint.state.research_plan.tasks[0].status == "running"
    assert not checkpoint.state.verification_results
    resumed, calls, verify, *_ = pipeline(db, monkeypatch)
    resumed.resume(run.run_id)
    assert calls["D1"] == 0  # Result was already safely stored, no need to pay for research again.
    assert verify.calls[0] == ["Fact D1"]


def test_running_task_without_result_becomes_pending_and_runs(db, monkeypatch):
    run = db.create_run("Goal")
    pending = plan()
    pending.tasks[0].status = "running"
    state = ResearchRunState(goal="Goal", research_plan=pending, completed_stages={"PLAN_CREATED"},
                             phase_options=dict(verify_claims=True, detect_gaps=True, reresearch=False, write_report=True))
    db.save_checkpoint(run.run_id, "PLAN_CREATED", state)
    resumed, calls, *_ = pipeline(db, monkeypatch)
    resumed.resume(run.run_id)
    assert calls["D1"] == 1


def test_completed_relevance_batch_skipped_after_later_failure(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch, fail_stage="RELEVANCE_BATCH_COMPLETED")
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    saved = db.load_latest_checkpoint(run.run_id).state
    assert len(saved.relevance_results) == 1
    resumed, calls, verify, relevance, _ = pipeline(db, monkeypatch)
    result = resumed.resume(run.run_id)
    assert not calls and not verify.calls
    assert len(relevance.calls) == 3 and ["Fact D1"] not in relevance.calls
    assert len(result.relevance_results) == 4
    assert result.relevance_usage.llm_calls == 4


def test_keyboard_interrupt_preserves_safe_checkpoint_and_marks_interrupted(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch, fail_stage="GAP_DETECTION_COMPLETED", interrupt=True)
    with pytest.raises(KeyboardInterrupt):
        worker.run("Goal")
    run = db.list_runs()[0]
    assert run.status == RunStatus.INTERRUPTED
    assert db.load_latest_checkpoint(run.run_id).stage == "GAP_DETECTION_COMPLETED"
    resumed, calls, verify, relevance, _ = pipeline(db, monkeypatch)
    resumed.resume(run.run_id)
    assert not calls and not verify.calls and not relevance.calls


def test_completed_run_requires_no_models_or_citation_work(db, monkeypatch, capsys):
    worker, *_ = pipeline(db, monkeypatch)
    expected = worker.run("Goal")
    def forbidden(*args, **kwargs):
        raise AssertionError("Completed run performed work")
    monkeypatch.setattr("app.research.orchestrator.create_llm", forbidden)
    monkeypatch.setattr("app.research.orchestrator.render_report_with_citations", forbidden)
    actual = ResearchOrchestrator(run_store=db).resume(expected.run_id)
    assert actual.rendered_report == expected.rendered_report
    assert "already completed" in capsys.readouterr().out
    assert db.get_run(expected.run_id).resume_count == 0


def test_zero_accepted_report_persists_with_zero_references(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch, relevance="MISALIGNED")
    from app.agents.writer import ResearchWriter
    worker.writer = ResearchWriter(settings=Settings(_env_file=None))
    first = worker.run("Goal")
    assert first.writer_usage.llm_calls == 0
    assert first.rendered_report.references == []
    second = ResearchOrchestrator(run_store=db).resume(first.run_id)
    assert second.rendered_report.markdown == first.rendered_report.markdown


@pytest.mark.parametrize("mutation", ["claim_evidence", "evidence_source", "verification_claim", "relevance_claim",
    "completed_result", "gap_task", "citation_stage", "writer_stage"])
def test_corrupt_graph_fails_before_any_model_call(db, monkeypatch, mutation):
    worker, *_ = pipeline(db, monkeypatch, fail_stage="GAP_DETECTION_COMPLETED", relevance="PARTIAL")
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    with db.connection() as conn, conn:
        raw = json.loads(conn.execute("SELECT state_json FROM checkpoints WHERE checkpoint_id=?", (run.last_checkpoint_id,)).fetchone()[0])
        if mutation == "claim_evidence": raw["evidence_store"]["claims"][0]["evidence_ids"] = ["missing"]
        elif mutation == "evidence_source": raw["evidence_store"]["evidence"][0]["source_id"] = "missing"
        elif mutation == "verification_claim": raw["verification_results"][0]["claim_id"] = "missing"
        elif mutation == "relevance_claim": raw["relevance_results"]["missing"] = next(iter(raw["relevance_results"].values()))
        elif mutation == "completed_result": del raw["task_results"]["D1"]
        elif mutation == "gap_task": raw["gaps"][0]["task_id"] = "missing"
        elif mutation == "citation_stage": raw["completed_stages"].append("CITATION_COMPLETED")
        elif mutation == "writer_stage": raw["completed_stages"].append("WRITER_COMPLETED")
        conn.execute("UPDATE checkpoints SET state_json=? WHERE checkpoint_id=?", (json.dumps(raw), run.last_checkpoint_id))
    resumed, calls, verify, relevance, writer = pipeline(db, monkeypatch)
    with pytest.raises(StateIntegrityError):
        resumed.resume(run.run_id)
    assert not calls and not verify.calls and not relevance.calls and writer.calls == 0


@pytest.mark.parametrize("location", ["run", "state"])
def test_incompatible_version_is_explicit(db, location):
    run = db.create_run("Goal")
    db.save_checkpoint(run.run_id, "PLAN_CREATED", ResearchRunState(goal="Goal", research_plan=plan()))
    with db.connection() as conn, conn:
        if location == "run":
            conn.execute("UPDATE research_runs SET state_version=99 WHERE run_id=?", (run.run_id,))
        else:
            raw = db.load_latest_checkpoint(run.run_id).state.model_dump(mode="json")
            raw["state_version"] = 99
            conn.execute("UPDATE checkpoints SET state_json=? WHERE run_id=?", (json.dumps(raw), run.run_id))
    with pytest.raises(IncompatibleCheckpointError):
        db.load_latest_checkpoint(run.run_id)


def test_secret_never_enters_database_or_error_metadata(tmp_path, monkeypatch):
    secret = "super-secret-test-key"
    monkeypatch.setenv("MODEL_API_KEY", secret)
    db = ResearchRunStore(tmp_path / "secrets.db")
    run = db.create_run("Goal")
    db.save_checkpoint(run.run_id, "PLAN_CREATED", ResearchRunState(goal="Goal", research_plan=plan()))
    db.mark_failed(run.run_id, RuntimeError(f"Authorization: Bearer {secret}"))
    with db.connection() as conn:
        assert secret not in "\n".join(conn.iterdump())
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert secret.encode() not in db.path.read_bytes()
    assert "REDACTED" in db.get_run(run.run_id).error_message


def test_cli_modes_unknown_id_and_list_runs(tmp_path, monkeypatch, capsys):
    from main import main
    path = str(tmp_path / "cli.db")
    for args in [["Goal", "--resume", "id"], ["Goal", "--list-runs"], ["--resume", "id", "--list-runs"]]:
        with pytest.raises(SystemExit) as error:
            main([*args, "--db-path", path])
        assert error.value.code == 2
    assert main(["--resume", "unknown", "--db-path", path]) == 1
    assert "Research run not found" in capsys.readouterr().err
    db = ResearchRunStore(path)
    run = db.create_run("Goal")
    assert main(["--list-runs", "--db-path", path]) == 0
    assert run.run_id in capsys.readouterr().out


def test_budget_snapshot_restore_rejects_tampering_and_preserves_limit():
    budget = ReResearchCallBudget(3)
    budget.reserve("planner")
    restored = ReResearchCallBudget.restore(budget.snapshot())
    assert restored.snapshot() == budget.snapshot()
    for raw in [dict(maximum=3, calls=4, by_stage={"planner": 4}), dict(maximum=3, calls=2, by_stage={"planner": 1})]:
        with pytest.raises(ValueError):
            ReResearchCallBudget.restore(raw)


@pytest.mark.parametrize("stage", ["RERESEARCH_PLAN_CREATED", "RERESEARCH_TASK_RESEARCH_COMPLETED:RR_D1_1",
    "RERESEARCH_TASK_COMPLETED:RR_D1_1", "RERESEARCH_RELEVANCE_BATCH_COMPLETED", "RERESEARCH_COMPLETED"])
def test_supplemental_resume_preserves_round_tasks_budget_and_gap_ids(db, monkeypatch, stage):
    status = lambda item: "ALIGNED" if "RR_" in item["text"] else "PARTIAL"
    worker, calls, *_ = pipeline(db, monkeypatch, fail_stage=stage, rr=True, relevance=status)
    with pytest.raises(RuntimeError, match="injected crash"):
        worker.run("Goal")
    run = db.list_runs()[0]
    saved = db.load_latest_checkpoint(run.run_id).state
    gap_ids = [item.gap_id for item in saved.gaps]
    budget_before = saved.reresearch_budget["calls"]
    assert budget_before > 0
    restored, new_calls, new_verify, new_relevance, _ = pipeline(db, monkeypatch, rr=True, relevance=status)
    result = restored.resume(run.run_id)
    assert not set(new_calls) & {"D1", "D2", "A", "S", "supervisor", "planner"}
    if "RR_D1_1" in saved.supplemental_results:
        assert new_calls["RR_D1_1"] == 0
    if stage == "RERESEARCH_COMPLETED":
        assert not new_calls and not new_verify.calls and not new_relevance.calls
    assert result.reresearch_summary.rounds == 1
    assert result.reresearch_summary.llm_calls == 9
    assert result.reresearch_summary.tasks == 2
    assert result.evidence_store.counts == dict(sources=1, evidence=6, claims=6)
    assert [item.gap_id for item in result.gap_store.all_gaps()] == gap_ids
    assert len(result.verification_store.snapshot()) == 6


def test_worker_budget_permits_are_durable_before_request_on_main_thread(db, monkeypatch):
    main_thread = threading.get_ident()
    writes = []
    original = db.save_checkpoint
    def save(*args, **kwargs):
        writes.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "save_checkpoint", save)
    worker, calls, *_ = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL",
                                 fail_stage="RERESEARCH_CALL_RESERVED", concurrency=2)
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    state = db.load_latest_checkpoint(run.run_id).state
    # The durable permit survives even when its model request never starts.
    assert state.reresearch_budget == dict(maximum=12, calls=1, by_stage={"planner": 1})
    assert all(key == main_thread for key in writes)
    resumed, *_ = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL",
        cfg=Settings(_env_file=None, max_reresearch_llm_calls=100, max_relevance_claims_per_batch=1))
    result = resumed.resume(run.run_id)
    latest = db.load_latest_checkpoint(run.run_id).state
    assert latest.reresearch_budget["maximum"] == 12  # .env changes cannot reset/enlarge a saved budget.
    assert result.reresearch_summary.llm_calls == 10  # One interrupted permit, then nine actual requests.


def test_failure_during_verification_retains_research_and_previous_verifications(db, monkeypatch):
    worker, calls, verify, *_ = pipeline(db, monkeypatch)
    invoke = verify.invoke
    def fail_d2(messages, **kwargs):
        if json.loads(messages[1].text)["claims"][0]["text"] == "Fact D2":
            raise RuntimeError("verification unavailable")
        return invoke(messages, **kwargs)
    verify.invoke = fail_d2
    with pytest.raises(RuntimeError, match="verification unavailable"):
        worker.run("Goal")
    run = db.list_runs()[0]
    saved = db.load_latest_checkpoint(run.run_id).state
    assert set(saved.task_results) == {"D1", "D2"}
    assert len(saved.verification_results) == 1
    resumed, new_calls, new_verify, *_ = pipeline(db, monkeypatch)
    resumed.resume(run.run_id)
    assert not set(new_calls) & {"D1", "D2"}
    assert new_verify.calls == [["Fact D2"], ["Fact A"], ["Fact S"]]


def test_resume_after_initial_research_recreates_missing_researcher_for_supplement(db, monkeypatch):
    status = lambda item: "ALIGNED" if "RR_" in item["text"] else "PARTIAL"
    worker, *_ = pipeline(db, monkeypatch, fail_stage="GAP_DETECTION_COMPLETED", rr=True, relevance=status)
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    resumed, calls, *_ = pipeline(db, monkeypatch, rr=True, relevance=status)
    saved_researcher = resumed.researcher
    resumed.researcher = None
    def recreate():
        calls["recreate_runtime"] += 1
        resumed.researcher = saved_researcher
    resumed._ensure_research_agents = recreate
    result = resumed.resume(run.run_id)
    assert calls["recreate_runtime"] == 1
    assert calls["RR_D1_1"] == calls["RR_D2_1"] == 1
    assert not result.reresearch_summary.failures


def test_planning_failure_can_resume_from_run_created(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch)
    worker.supervisor.create_plan = lambda query: (_ for _ in ()).throw(TimeoutError("planning timed out"))
    with pytest.raises(TimeoutError):
        worker.run("Goal")
    run = db.list_runs()[0]
    assert run.status == RunStatus.FAILED
    assert db.load_latest_checkpoint(run.run_id).stage == "RUN_CREATED"
    resumed, calls, *_ = pipeline(db, monkeypatch)
    resumed.resume(run.run_id)
    assert calls["supervisor"] == 1


def test_concurrent_supplemental_workers_never_write_sqlite_directly(db, monkeypatch):
    owner = threading.get_ident()
    writes = []
    original = db.save_checkpoint
    def save(*args, **kwargs):
        writes.append(threading.get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(db, "save_checkpoint", save)
    worker, *_ = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL", concurrency=2)
    result = worker.run("Goal")
    assert result.reresearch_summary.llm_calls == 9
    assert len(writes) > 20 and set(writes) == {owner}


def test_exhausted_budget_is_not_reset_when_resuming_unfinished_supplement(db, monkeypatch):
    cfg = Settings(_env_file=None, max_reresearch_llm_calls=3, max_relevance_claims_per_batch=1)
    worker, *_ = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL", cfg=cfg,
                         fail_stage="RERESEARCH_TASK_RESEARCH_COMPLETED:RR_D1_1")
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    resumed, calls, verify, alignment, _ = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL")
    result = resumed.resume(run.run_id)
    assert not calls and not verify.calls and not alignment.calls
    assert result.reresearch_summary.llm_calls == 3
    assert result.reresearch_summary.stop_reason == "budget exhausted"
    assert result.evidence_store.counts["claims"] == 5
    assert len(result.verification_store.snapshot()) == 4
