"""Phase 8A: real SQLite, controlled background workers, no network/model calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event, Lock, get_ident
from time import monotonic, sleep
from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.api.main import create_app
from app.config import Settings
from app.persistence.models import RunStatus
from app.persistence.store import ResearchRunStore
from app.research.orchestrator import ResearchOrchestrator
from app.schemas.plan import ResearchPlan

from test_persistence import pipeline


def eventually(predicate, timeout=5):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if predicate():
            return
        sleep(0.01)
    raise AssertionError("Background worker did not reach the expected state.")


def control():
    return SimpleNamespace(started=Event(), release=Event(), calls=[], threads=[],
                           failure=None, lock=Lock(), active=0, peak=0)


class ControlledPipeline(ResearchOrchestrator):
    def __init__(self, *, run_store, ctl):
        super().__init__(run_store=run_store, verify_claims=False, detect_gaps=False,
                         reresearch=False, write_report=False)
        self.ctl = ctl

    def _work(self, run_id, mode):
        ctl = self.ctl
        with ctl.lock:
            ctl.calls.append((mode, run_id))
            ctl.threads.append(get_ident())
            ctl.active += 1
            ctl.peak = max(ctl.peak, ctl.active)
        ctl.started.set()
        try:
            if not ctl.release.wait(10):
                raise TimeoutError("Test did not release the controlled worker.")
            if ctl.failure:
                raise ctl.failure
            state = self.run_store.load_latest_checkpoint(run_id).state
            state.research_plan = ResearchPlan(goal=state.goal, tasks=[])
            state.completed_stages.update({"PLAN_CREATED", "INITIAL_RESEARCH_COMPLETED", "RUN_COMPLETED"})
            self.run_store.save_checkpoint(run_id, "RUN_COMPLETED", state)
            self.run_store.mark_completed(run_id)
        finally:
            with ctl.lock:
                ctl.active -= 1

    def run(self, query, *, prepared_run_id=None):
        assert self.run_store.get_run(prepared_run_id).goal == query
        self._work(prepared_run_id, "run")

    def resume(self, run_id):
        self.run_store.mark_running(run_id)
        self._work(run_id, "resume")


@pytest.fixture
def api(tmp_path, monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "test-api-key-never-expose")
    cfg = Settings(_env_file=None, research_db_path=str(tmp_path / "api.db"))
    db = ResearchRunStore(cfg.research_db_path)
    ctl = control()
    app = create_app(settings=cfg, store=db,
                     pipeline_factory=lambda **kw: ControlledPipeline(**kw, ctl=ctl))
    with TestClient(app, raise_server_exceptions=False) as client:
        try:
            yield SimpleNamespace(client=client, db=db, ctl=ctl, app=app, cfg=cfg)
        finally:
            ctl.release.set()


def create(api):
    response = api.client.post("/api/runs", json={"question": "Goal"})
    assert response.status_code == 202, response.text
    return response.json()["run_id"]


def wait_complete(api, run_id):
    api.ctl.release.set()
    eventually(lambda: api.db.get_run(run_id).status == RunStatus.COMPLETED)
    eventually(lambda: not api.app.state.run_service._active)


def test_health_and_swagger_and_openapi(api):
    assert api.client.get("/api/health").json() == {"status": "ok"}
    assert api.client.get("/docs").status_code == 200
    schema = api.client.get("/openapi.json").json()
    assert "/api/runs/{run_id}/resume" in schema["paths"]
    assert "202" in schema["paths"]["/api/runs"]["post"]["responses"]
    assert schema["paths"]["/api/runs"]["post"]["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorResponse"}


def test_create_returns_persisted_id_without_waiting_and_loop_stays_responsive(api):
    request_thread = get_ident()
    with ThreadPoolExecutor(max_workers=1) as requester:
        pending = requester.submit(api.client.post, "/api/runs", json={"question": "  Goal  "})
        try:
            response = pending.result(timeout=2)
            assert response.status_code == 202
            run_id = response.json()["run_id"]
            assert len(run_id) == 32
            assert api.ctl.started.wait(1)
            assert api.db.get_run(run_id).goal == "Goal"
            assert api.db.load_latest_checkpoint(run_id).stage == "RUN_CREATED"
            assert not api.ctl.release.is_set()
            assert api.client.get("/api/health").status_code == 200
            status = api.client.get(f"/api/runs/{run_id}").json()
            assert status["status"] == "RUNNING" and status["plan"] is None
            assert status["checkpoint_available"] and not status["resumable"]
            assert datetime.fromisoformat(status["created_at"].replace("Z", "+00:00")).tzinfo
            assert api.ctl.threads[0] != request_thread
            assert api.ctl.threads[0] != api.client.portal.call(get_ident)
        finally:
            api.ctl.release.set()


@pytest.mark.parametrize("body", [{}, {"question": "   "}, {"question": None},
                                  {"question": 17}, {"question": "x" * 10001},
                                  {"question": "Goal", "api_key": "test-api-key-never-expose"}])
def test_invalid_create_is_rejected_without_creating_runs_or_echoing_inputs(api, body):
    response = api.client.post("/api/runs", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_REQUEST"
    assert "test-api-key-never-expose" not in response.text
    assert not api.db.list_runs() and not api.ctl.calls


@pytest.mark.parametrize("method,path", [("get", "/api/runs/missing"),
    ("get", "/api/runs/missing/report"), ("post", "/api/runs/missing/resume")])
def test_nonexistent_run_returns_uniform_404(api, method, path):
    response = getattr(api.client, method)(path)
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_running_resume_and_unfinished_report_are_conflicts(api):
    run_id = create(api)
    assert api.client.post(f"/api/runs/{run_id}/resume").json()["error"]["code"] == "RUN_ALREADY_RUNNING"
    assert api.client.post(f"/api/runs/{run_id}/resume").status_code == 409
    report = api.client.get(f"/api/runs/{run_id}/report")
    assert report.status_code == 409 and report.json()["error"]["code"] == "REPORT_NOT_READY"


def test_capacity_rejects_without_orphan_runs_and_releases_after_completion(api):
    run_id = create(api)
    response = api.client.post("/api/runs", json={"question": "Second"})
    assert response.status_code == 429
    assert len(api.db.list_runs()) == 1
    wait_complete(api, run_id)
    second = create(api)
    eventually(lambda: api.db.get_run(second).status == RunStatus.COMPLETED)
    assert api.ctl.peak == 1


def test_simultaneous_create_respects_global_limit(api):
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _: api.client.post("/api/runs", json={"question": "Goal"}), range(6)))
    assert sorted(r.status_code for r in responses) == [202, 429, 429, 429, 429, 429]
    assert len(api.db.list_runs()) == 1


def test_completed_run_is_not_reexecuted(api):
    run_id = create(api)
    wait_complete(api, run_id)
    before = list(api.ctl.calls)
    response = api.client.post(f"/api/runs/{run_id}/resume")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_ALREADY_COMPLETED"
    assert api.ctl.calls == before
    assert api.db.get_run(run_id).resume_count == 0
    # Runs created with report writing disabled have no fabricated report.
    assert api.client.get(f"/api/runs/{run_id}/report").json()["error"]["code"] == "REPORT_UNAVAILABLE"


def test_failure_is_persisted_and_private_errors_are_not_exposed(api):
    api.ctl.failure = RuntimeError(r"provider test-api-key-never-expose at C:\private\config.env")
    run_id = create(api)
    api.ctl.release.set()
    eventually(lambda: api.db.get_run(run_id).status == RunStatus.FAILED)
    saved = api.db.get_run(run_id)
    assert "[REDACTED]" in saved.error_message
    response = api.client.get(f"/api/runs/{run_id}")
    assert response.json()["error"]["code"] == "RESEARCH_FAILED"
    assert response.json()["resumable"]
    assert "test-api-key-never-expose" not in response.text
    assert "private" not in response.text and "config.env" not in response.text
    assert "research_db_path" not in response.text
    history = api.client.get("/api/runs").text
    assert "test-api-key-never-expose" not in history and "private" not in history


def test_resume_uses_existing_entry_and_rejects_simultaneous_duplicate(api):
    worker = ControlledPipeline(run_store=api.db, ctl=api.ctl)
    run = worker.prepare_run("Goal")
    api.db.mark_failed(run.run_id, TimeoutError("timeout"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        responses = list(pool.map(lambda _: api.client.post(f"/api/runs/{run.run_id}/resume"), range(4)))
    assert sorted(r.status_code for r in responses) == [202, 409, 409, 409]
    assert api.ctl.started.wait(1)
    assert api.ctl.calls == [("resume", run.run_id)]
    wait_complete(api, run.run_id)
    assert api.db.get_run(run.run_id).resume_count == 1


def test_history_limit_and_no_checkpoint_resume(api):
    run = api.db.create_run("Uncheckpointed")
    api.db.mark_failed(run.run_id, RuntimeError("setup failed"))
    status = api.client.get(f"/api/runs/{run.run_id}").json()
    assert not status["resumable"] and not status["checkpoint_available"]
    response = api.client.post(f"/api/runs/{run.run_id}/resume")
    assert response.status_code == 409 and response.json()["error"]["code"] == "CHECKPOINT_UNAVAILABLE"
    assert len(api.client.get("/api/runs?limit=1").json()["runs"]) == 1
    assert api.client.get("/api/runs?limit=0").status_code == 422
    assert api.client.get("/api/runs?limit=101").status_code == 422


def test_local_cors_is_explicit_and_external_origins_are_rejected(api):
    headers = {"Origin": "http://localhost:5173", "Access-Control-Request-Method": "POST",
               "Access-Control-Request-Headers": "content-type"}
    good = api.client.options("/api/runs", headers=headers)
    assert good.status_code == 200 and good.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in good.headers
    bad = api.client.options("/api/runs", headers={**headers, "Origin": "https://outside.example"})
    assert bad.status_code == 400 and "access-control-allow-origin" not in bad.headers


@pytest.mark.parametrize("origin", ["*", "https://outside.example", "http://localhost:5173/path",
                                    "http://user:password@localhost:5173", "http://localhost:99999"])
def test_cors_config_cannot_allow_arbitrary_origins(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_cors_origins=[origin])


def test_api_parameters_are_read_from_environment(monkeypatch):
    monkeypatch.setenv("API_PORT", "8123")
    monkeypatch.setenv("API_MAX_CONCURRENT_RUNS", "2")
    monkeypatch.setenv("API_CORS_ORIGINS", '["http://localhost:3000"]')
    cfg = Settings(_env_file=None)
    assert cfg.api_port == 8123 and cfg.api_max_concurrent_runs == 2
    assert cfg.api_cors_origins == ["http://localhost:3000"]


def test_restarting_recovers_unfinished_run_without_starting_models(tmp_path):
    db = ResearchRunStore(tmp_path / "restart.db")
    ctl = control()
    prepared = ControlledPipeline(run_store=db, ctl=ctl).prepare_run("Goal")
    checkpoint = db.load_latest_checkpoint(prepared.run_id)
    # Simulate a process that died with a persisted RUNNING run.
    app = create_app(settings=Settings(_env_file=None), store=ResearchRunStore(db.path),
                     pipeline_factory=lambda **kw: ControlledPipeline(**kw, ctl=ctl))
    with TestClient(app) as client:
        try:
            status = client.get(f"/api/runs/{prepared.run_id}").json()
            assert status["status"] == "INTERRUPTED" and status["resumable"]
            assert not ctl.calls
            assert db.load_latest_checkpoint(prepared.run_id).checkpoint_id == checkpoint.checkpoint_id
            assert client.post(f"/api/runs/{prepared.run_id}/resume").status_code == 202
            assert ctl.started.wait(1)
        finally:
            ctl.release.set()
    assert db.get_run(prepared.run_id).status == RunStatus.COMPLETED


def test_completed_report_read_is_exact_and_invokes_no_writer_or_models(tmp_path, monkeypatch):
    db = ResearchRunStore(tmp_path / "report.db")
    workers = []
    def factory(**kw):
        parts = pipeline(kw["run_store"], monkeypatch)
        workers.append(parts)
        return parts[0]
    app = create_app(settings=Settings(_env_file=None), store=db, pipeline_factory=factory)
    with TestClient(app) as client:
        run_id = client.post("/api/runs", json={"question": "Goal"}).json()["run_id"]
        eventually(lambda: db.get_run(run_id).status == RunStatus.COMPLETED)
        eventually(lambda: not app.state.run_service._active)
        state = db.load_latest_checkpoint(run_id).state
        worker, calls, verify, relevance, writer = workers[0]
        before = calls.copy(), len(verify.calls), len(relevance.calls), writer.calls
        for _ in range(2):
            report = client.get(f"/api/runs/{run_id}/report")
            assert report.status_code == 200
            assert report.json()["markdown"] == state.rendered_report.markdown
            assert "[1]" in report.json()["markdown"]
            assert report.json()["references"][0]["url"] == "https://example.org/paper"
            assert report.json()["report"] == state.research_report.model_dump(mode="json")
        status = client.get(f"/api/runs/{run_id}").json()
        assert all(t["status"] == "completed" for t in status["plan"]["tasks"])
        assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
        assert len(workers) == 1
        assert before == (calls.copy(), len(verify.calls), len(relevance.calls), writer.calls)
        assert db.get_run(run_id).resume_count == 0


def test_api_resume_reuses_phase6_and_skips_cli_completed_stages(tmp_path, monkeypatch):
    db = ResearchRunStore(tmp_path / "shared.db")
    original, *_ = pipeline(db, monkeypatch, fail_stage="TASK_COMPLETED:D1")
    with pytest.raises(RuntimeError):
        original.run("Goal")
    run = db.list_runs()[0]
    resumed = []
    def factory(**kw):
        parts = pipeline(kw["run_store"], monkeypatch)
        resumed.append(parts)
        return parts[0]
    with TestClient(create_app(settings=Settings(_env_file=None), store=db, pipeline_factory=factory)) as client:
        assert client.post(f"/api/runs/{run.run_id}/resume").status_code == 202
        eventually(lambda: db.get_run(run.run_id).status == RunStatus.COMPLETED)
        assert client.get(f"/api/runs/{run.run_id}/report").status_code == 200
    assert resumed[0][1] == {"D2": 1, "A": 1, "S": 1}
    assert db.get_run(run.run_id).resume_count == 1


def test_cli_can_resume_api_created_run(api, monkeypatch):
    import main as cli
    api.app.state.run_service.pipeline_factory = lambda **kw: pipeline(
        kw["run_store"], monkeypatch, fail_stage="TASK_COMPLETED:D1")[0]
    run_id = create(api)
    eventually(lambda: api.db.get_run(run_id).status == RunStatus.FAILED)
    eventually(lambda: not api.app.state.run_service._active)
    monkeypatch.setattr(cli, "ResearchOrchestrator", lambda **kw: pipeline(kw["run_store"], monkeypatch)[0])
    assert cli.main(["--resume", run_id, "--db-path", str(api.db.path)]) == 0
    assert api.client.get(f"/api/runs/{run_id}/report").status_code == 200


def test_configured_two_run_limit_and_failure_releases_slot(tmp_path):
    db = ResearchRunStore(tmp_path / "two.db")
    ctl = control()
    ctl.failure = TimeoutError("injected timeout")
    app = create_app(settings=Settings(_env_file=None, api_max_concurrent_runs=2), store=db,
                     pipeline_factory=lambda **kw: ControlledPipeline(**kw, ctl=ctl))
    with TestClient(app) as client:
        try:
            ids = [client.post("/api/runs", json={"question": "Goal"}).json()["run_id"] for _ in range(2)]
            eventually(lambda: ctl.active == 2)
            assert client.post("/api/runs", json={"question": "Goal"}).status_code == 429
            ctl.release.set()
            eventually(lambda: all(db.get_run(key).status == RunStatus.FAILED for key in ids))
            eventually(lambda: not app.state.run_service._active)
            ctl.failure = None
            response = client.post("/api/runs", json={"question": "Goal"})
            assert response.status_code == 202
            eventually(lambda: db.get_run(response.json()["run_id"]).status == RunStatus.COMPLETED)
            assert ctl.peak == 2
        finally:
            ctl.release.set()


def test_invalid_checkpoint_fails_closed_before_starting_a_worker(api):
    run = ControlledPipeline(run_store=api.db, ctl=api.ctl).prepare_run("Goal")
    api.db.mark_failed(run.run_id, RuntimeError("failure"))
    with api.db.connection() as conn, conn:
        conn.execute("UPDATE checkpoints SET state_json='{}' WHERE run_id=?", (run.run_id,))
    response = api.client.post(f"/api/runs/{run.run_id}/resume")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHECKPOINT_INVALID"
    assert not api.ctl.calls


def test_prepared_entry_does_not_create_second_run_and_validates_identity(tmp_path, monkeypatch):
    db = ResearchRunStore(tmp_path / "prepared.db")
    worker, calls, *_ = pipeline(db, monkeypatch)
    run = worker.prepare_run("Goal")
    assert not calls  # Preparing never plans or researches.
    assert worker.run("Goal", prepared_run_id=run.run_id).run_id == run.run_id
    assert len(db.list_runs()) == 1 and db.get_run(run.run_id).resume_count == 0
    with pytest.raises(ValueError, match="unstarted"):
        worker.run("Goal", prepared_run_id=run.run_id)
    fresh = worker.prepare_run("Goal")
    with pytest.raises(ValueError, match="unstarted"):
        worker.run("Different goal", prepared_run_id=fresh.run_id)


def test_unhandled_errors_have_uniform_safe_response(api, monkeypatch):
    def fail():
        raise RuntimeError(r"test-api-key-never-expose C:\private\config.env")
    monkeypatch.setattr(api.app.state.run_service, "health", fail)
    response = api.client.get("/api/health")
    assert response.status_code == 500 and response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private" not in response.text and "test-api-key-never-expose" not in response.text


def test_environment_key_is_redacted_from_returned_question(api):
    response = api.client.post("/api/runs", json={"question": "Find test-api-key-never-expose"})
    assert response.status_code == 202
    status = api.client.get("/api/runs/" + response.json()["run_id"])
    assert "test-api-key-never-expose" not in status.text
    assert "[REDACTED]" in status.json()["question"]


def test_second_api_instance_cannot_interrupt_or_execute_live_runs(api):
    run_id = create(api)
    second = create_app(settings=api.cfg, store=ResearchRunStore(api.db.path))
    with pytest.raises(RuntimeError, match="Only one API process"):
        with TestClient(second):
            pass
    assert api.db.get_run(run_id).status == RunStatus.RUNNING
    assert api.client.get("/api/health").status_code == 200


def test_executor_submission_failure_is_persisted_and_slot_is_released(api, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError(r"test-api-key-never-expose C:\private\config.env")
    monkeypatch.setattr(api.app.state.run_service._executor, "submit", fail)
    response = api.client.post("/api/runs", json={"question": "Goal"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "BACKGROUND_START_FAILED"
    run = api.db.list_runs()[0]
    assert run.status == RunStatus.FAILED and run.last_checkpoint_id is not None
    assert not api.app.state.run_service._active and not api.ctl.calls
    assert "private" not in response.text and "test-api-key-never-expose" not in response.text


def test_unexpected_pipeline_return_is_failed_instead_of_stuck_running(api, monkeypatch):
    monkeypatch.setattr(ControlledPipeline, "run", lambda *args, **kwargs: None)
    run_id = create(api)
    eventually(lambda: api.db.get_run(run_id).status == RunStatus.FAILED)
    assert api.client.get(f"/api/runs/{run_id}").json()["resumable"]


def test_process_lock_is_released_on_clean_shutdown(tmp_path):
    from app.api.services.process_lock import ApiProcessLock
    db = ResearchRunStore(tmp_path / "lock.db")
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)):
        with pytest.raises(RuntimeError, match="Only one API process"):
            ApiProcessLock(db.path)
    lease = ApiProcessLock(db.path)
    lease.close()
