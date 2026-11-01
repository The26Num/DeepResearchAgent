"""Phase 8C: progress observers and real SSE, using offline pipeline doubles."""

import asyncio
import json
import socket
from threading import Event, Thread

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest
import uvicorn

from app.api.main import create_app
from app.api.services.events import EventHub
from app.api.services.runs import RunService
from app.api.services.streaming import RunEventResponse, encode_event, event_stream
from app.config import Settings
from app.persistence.models import RunStatus
from app.persistence.store import ResearchRunStore
from app.research.events import ProgressData, ProgressUpdate, ResearchEvent, make_event
from test_api import ControlledPipeline, control, eventually
from test_persistence import pipeline


def frames(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


@pytest.fixture
def db(tmp_path):
    return ResearchRunStore(tmp_path / "stream.db", secrets=["test-secret-never-stream"])


def test_completed_and_missing_run_streams_have_correct_protocol(db, monkeypatch):
    worker, *_ = pipeline(db, monkeypatch)
    result = worker.run("Goal")
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        missing = client.get("/api/runs/missing/events")
        assert missing.status_code == 404 and missing.json()["error"]["code"] == "RUN_NOT_FOUND"
        response = client.get(f"/api/runs/{result.run_id}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]
        assert "retry: 2000" in response.text
        events = frames(response.text)
        assert [event["event_type"] for event in events] == ["run_snapshot", "run_completed"]
        assert all(ResearchEvent.model_validate(event).run_id == result.run_id for event in events)
        assert client.get("/api/health").status_code == 200
        assert client.app.state.run_service.events._connections == 0


@pytest.mark.parametrize("status,kind", [("FAILED", "run_failed"), ("INTERRUPTED", "run_interrupted")])
def test_terminal_errors_are_saved_state_notifications_without_exception_text(db, status, kind):
    run = db.create_run("Goal")
    if status == "FAILED":
        db.mark_failed(run.run_id, RuntimeError(r"test-secret-never-stream C:\private\credentials.env"))
    else:
        db.mark_interrupted(run.run_id)
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        response = client.get(f"/api/runs/{run.run_id}/events")
        assert frames(response.text)[-1]["event_type"] == kind
        assert "credentials.env" not in response.text and "test-secret-never-stream" not in response.text
        assert frames(response.text)[-1]["progress_data"]["run_status"] == status


def test_observer_tracks_actual_order_and_only_committed_task_completions(db, monkeypatch):
    worker, calls, verifier, relevance, writer = pipeline(db, monkeypatch)
    observed = []
    def sink(update):
        state = db.load_latest_checkpoint(db.list_runs()[0].run_id).state
        observed.append((update, state))
    worker.event_sink = sink
    result = worker.run("Goal")
    events = [item for item, _ in observed]
    for task in ("D1", "D2", "A", "S"):
        order = [event.event_type for event in events if event.task_id == task]
        assert order == ["task_started", "task_research_completed", "verification_started", "verification_completed", "task_completed"]
    for event, state in observed:
        if event.event_type == "task_completed":
            assert next(task for task in state.research_plan.tasks if task.id == event.task_id).status == "completed"
    kinds = [event.event_type for event in events]
    assert kinds.index("plan_created") < kinds.index("task_started")
    assert kinds.index("gap_detection_completed") < kinds.index("writer_started")
    assert kinds.index("writer_completed") < kinds.index("report_completed")
    assert calls == {"supervisor": 1, "D1": 1, "D2": 1, "A": 1, "S": 1}
    assert len(verifier.calls) == 4 and len(relevance.calls) == 4 and writer.calls == 1
    with db.connection() as conn:
        stages = [r[0] for r in conn.execute("SELECT stage FROM checkpoints WHERE run_id=? ORDER BY sequence", (result.run_id,))]
    baseline, *_ = pipeline(db, monkeypatch)
    other = baseline.run("Goal")
    with db.connection() as conn:
        baseline_stages = [r[0] for r in conn.execute("SELECT stage FROM checkpoints WHERE run_id=? ORDER BY sequence", (other.run_id,))]
    assert stages == baseline_stages


def test_observer_exceptions_never_retry_or_fail_research(db, monkeypatch):
    worker, calls, verifier, relevance, writer = pipeline(db, monkeypatch)
    def unavailable(update):
        raise RuntimeError("broken observer")
    worker.event_sink = unavailable
    result = worker.run("Goal")
    assert db.get_run(result.run_id).status == RunStatus.COMPLETED
    assert calls == {"supervisor": 1, "D1": 1, "D2": 1, "A": 1, "S": 1}
    assert len(verifier.calls) == len(relevance.calls) == 4 and writer.calls == 1


def test_raw_research_failure_and_verification_failure_have_task_events(db, monkeypatch):
    first, *_ = pipeline(db, monkeypatch, failures={"D2"})
    observed = []
    first.event_sink = observed.append
    with pytest.raises(RuntimeError):
        first.run("Goal")
    assert any(e.event_type == "task_failed" and e.task_id == "D2" for e in observed)
    assert not any(e.event_type == "task_completed" and e.task_id == "D2" for e in observed)
    second, _, verifier, *_ = pipeline(db, monkeypatch)
    verifier.fail = True
    observed.clear()
    second.event_sink = observed.append
    with pytest.raises(RuntimeError):
        second.run("Goal")
    assert any(e.event_type == "verification_failed" for e in observed)


def test_reresearch_events_reuse_existing_budget_and_safe_checkpoints(db, monkeypatch):
    worker, calls, verify, relevance, writer = pipeline(db, monkeypatch, rr=True, relevance="PARTIAL")
    observed = []
    worker.event_sink = observed.append
    result = worker.run("Goal")
    kinds = [e.event_type for e in observed]
    assert "reresearch_started" in kinds and "reresearch_plan_created" in kinds and "reresearch_completed" in kinds
    ids = {task.id for plan in result.reresearch_summary.plans for task in plan.tasks}
    assert ids and ids <= {e.task_id for e in observed if e.event_type == "task_started"}
    assert ids <= {e.task_id for e in observed if e.event_type == "task_completed"}
    assert calls["planner"] == 1 and all(calls[key] == 1 for key in ids)


def test_progress_contract_rejects_arbitrary_content_and_redacts_known_secrets():
    with pytest.raises(ValidationError):
        ProgressData(webpage="full source text")
    with pytest.raises(ValidationError):
        ProgressUpdate(event_type="task_started", stage="X", message="raw LLM output")
    hub = EventHub(redact=lambda value: {**value, "stage": value["stage"].replace("secret", "[REDACTED]")})
    event = hub.event("run", ProgressUpdate(event_type="task_failed", stage="secret"))
    assert event.stage == "[REDACTED]"
    assert event.message == "任务执行失败，已保留安全检查点"
    assert "\nevent:" in encode_event(event) and "\ndata:" in encode_event(event)


class Request:
    disconnected = False
    async def is_disconnected(self):
        return self.disconnected


def test_heartbeat_and_disconnect_cleanup_do_not_change_run(db):
    async def scenario():
        run = db.create_run("Goal")
        service = RunService(db, sse_heartbeat_seconds=.02, sse_state_check_seconds=.03)
        subscription = service.events.subscribe(run.run_id)
        request = Request()
        stream = event_stream(request, service, subscription)
        assert "retry:" in await anext(stream)
        assert frames(await anext(stream))[0]["event_type"] == "run_snapshot"
        assert "heartbeat" in await asyncio.wait_for(anext(stream), .5)
        request.disconnected = True
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert service.events._connections == 0
        assert db.get_run(run.run_id).status == RunStatus.RUNNING
        service.close()
    asyncio.run(scenario())


def test_broadcast_overflow_and_reconnect_snapshot_without_history(db):
    async def scenario():
        run = db.create_run("Goal")
        service = RunService(db)
        service.events = EventHub(buffer_size=2, max_connections=2)
        one, two = service.events.subscribe(run.run_id), service.events.subscribe(run.run_id)
        with pytest.raises(OverflowError):
            service.events.subscribe(run.run_id)
        for _ in range(4):
            service.events.publish(run.run_id, ProgressUpdate(event_type="task_started", stage="INITIAL_RESEARCH_BATCH",
                task_id="D1", progress_data=ProgressData(task_status="running")))
        items, missed = service.events.read(one)
        assert missed and len(items) == 2
        assert service.events.read(two)[0] == items
        service.events.unsubscribe(one)
        fresh = service.events.subscribe(run.run_id)
        assert service.events.read(fresh) == ([], False)  # No history replay.
        assert service.events.live(fresh).tasks[0].status == "running"
        request = Request()
        stream = event_stream(request, service, fresh)
        await anext(stream)
        assert frames(await anext(stream))[0]["progress_data"]["tasks"][0]["task_id"] == "D1"
        for _ in range(3):
            service.events.publish(run.run_id, ProgressUpdate(event_type="writer_started", stage="WRITER"))
        assert frames(await anext(stream))[0]["event_type"] == "stream_reset"
        await stream.aclose()
        service.events.unsubscribe(two)
        assert not service.events._channels and service.events._connections == 0
        service.close()
    asyncio.run(scenario())


def test_sqlite_terminal_state_heals_lost_callback(db):
    async def scenario():
        run = db.create_run("Goal")
        service = RunService(db, sse_state_check_seconds=.01)
        stream = event_stream(Request(), service, service.events.subscribe(run.run_id))
        await anext(stream)
        await anext(stream)
        db.mark_failed(run.run_id, RuntimeError("not sent to observer"))
        assert frames(await asyncio.wait_for(anext(stream), .5))[0]["event_type"] == "run_snapshot"
        assert frames(await anext(stream))[0]["event_type"] == "run_failed"
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
        assert service.events._connections == 0
        service.close()
    asyncio.run(scenario())


def test_client_disconnecting_before_headers_releases_subscription(db):
    async def scenario():
        hub = EventHub()
        subscription = hub.subscribe("run")
        async def content():
            yield "never sent"
        async def send(message):
            raise OSError("client closed before headers")
        async def receive():
            return {"type": "http.disconnect"}
        response = RunEventResponse(content(), hub=hub, subscription=subscription)
        with pytest.raises(Exception):
            await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        assert subscription.closed and hub._connections == 0
    asyncio.run(scenario())


def test_no_subscriber_or_finished_run_history_is_retained():
    hub = EventHub()
    hub.start("run")
    for _ in range(200):
        hub.publish("run", ProgressUpdate(event_type="stage_changed", stage="PLANNING"))
    assert len(hub._channels["run"].buffer) == 128
    hub.finish("run")
    assert not hub._channels
    hub.publish("missing", ProgressUpdate(event_type="stage_changed", stage="PLANNING"))
    assert not hub._channels


def test_sse_capacity_is_a_readable_http_error(db):
    run = db.create_run("Goal")
    db.mark_failed(run.run_id, RuntimeError("failed"))
    app = create_app(settings=Settings(_env_file=None, api_sse_max_connections=1), store=db)
    with TestClient(app) as client:
        hub = app.state.run_service.events
        existing = client.portal.call(hub.subscribe, run.run_id)
        try:
            response = client.get(f"/api/runs/{run.run_id}/events")
            assert response.status_code == 429
            assert response.json()["error"]["code"] == "STREAM_CAPACITY_EXCEEDED"
        finally:
            hub.unsubscribe(existing)


def test_background_failure_publishes_terminal_only_after_sqlite_is_failed(db):
    ctl = control()
    ctl.release.set()
    ctl.failure = RuntimeError(r"test-secret-never-stream C:\private\key.env")
    service = RunService(db, pipeline_factory=lambda **kw: ControlledPipeline(**kw, ctl=ctl))
    observed = []
    publish = service.events.publish
    def inspect(run_id, update):
        observed.append((update.event_type, db.get_run(run_id).status))
        publish(run_id, update)
    service.events.publish = inspect
    try:
        run = service.create("Goal")
        eventually(lambda: not service._active)
        assert observed[0] == ("run_started", RunStatus.RUNNING)
        assert observed[-1] == ("run_failed", RunStatus.FAILED)
        assert db.get_run(run.run_id).status == RunStatus.FAILED
        assert not service.events._channels
    finally:
        service.close()


def test_live_http_sse_disconnect_does_not_stop_worker_and_reconnect_finishes(db, monkeypatch):
    planning, research = Event(), Event()
    parts = []
    def factory(**kw):
        item = pipeline(kw["run_store"], monkeypatch)
        worker = item[0]
        create_plan, run_task = worker.supervisor.create_plan, worker.researcher.research
        def plan(query):
            assert planning.wait(10)
            return create_plan(query)
        def task(**kwargs):
            assert research.wait(10)
            return run_task(**kwargs)
        worker.supervisor.create_plan, worker.researcher.research = plan, task
        parts.append(item)
        return worker
    app = create_app(settings=Settings(_env_file=None, api_sse_heartbeat_seconds=.1, api_sse_state_check_seconds=.1),
                     store=db, pipeline_factory=factory)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    thread = Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        eventually(lambda: server.started)
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
            run_id = client.post("/api/runs", json={"question": "Goal"}).json()["run_id"]
            with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
                assert stream.status_code == 200
                observed = []
                for line in stream.iter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        observed.append(event)
                        if event["event_type"] == "run_snapshot":
                            planning.set()
                        if event["event_type"] == "task_started":
                            break
                assert observed[-1]["task_id"] == "D1"
            assert db.get_run(run_id).status == RunStatus.RUNNING
            research.set()
            eventually(lambda: db.get_run(run_id).status == RunStatus.COMPLETED)
            final = client.get(f"/api/runs/{run_id}/events")
            assert frames(final.text)[-1]["event_type"] == "run_completed"
            assert client.get(f"/api/runs/{run_id}/report").status_code == 200
            assert parts[0][1] == {"supervisor": 1, "D1": 1, "D2": 1, "A": 1, "S": 1}
    finally:
        planning.set(); research.set()
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
    assert not thread.is_alive()
