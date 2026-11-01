"""The frontend evidence endpoint is a pure view over the Phase 6 checkpoint."""

from fastapi.testclient import TestClient
import pytest

from app.api.main import create_app
from app.config import Settings
from app.persistence.store import ResearchRunStore
from test_persistence import pipeline


@pytest.fixture
def db(tmp_path):
    return ResearchRunStore(tmp_path / "evidence.db")


def test_missing_and_uncheckpointed_evidence(db):
    run = db.create_run("Goal")
    db.mark_failed(run.run_id, RuntimeError("setup failed"))
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        assert client.get("/api/runs/missing/evidence").status_code == 404
        response = client.get(f"/api/runs/{run.run_id}/evidence")
        assert response.status_code == 200
        body = response.json()
        assert not body["checkpoint_available"]
        assert body["claims"] == body["evidence"] == body["sources"] == []


def test_initial_checkpoint_exposes_empty_graph(db, monkeypatch):
    worker, calls, *_ = pipeline(db, monkeypatch)
    run = worker.prepare_run("Goal")
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        body = client.get(f"/api/runs/{run.run_id}/evidence").json()
        assert body["checkpoint_available"] and body["checkpoint_stage"] == "RUN_CREATED"
        assert body["verification_results"] == body["relevance_results"] == []
        assert not calls


def test_full_evidence_chain_uses_saved_decisions_without_calls_or_writes(db, monkeypatch):
    worker, calls, verification, relevance, writer = pipeline(db, monkeypatch)
    result = worker.run("Goal")
    before = calls.copy(), len(verification.calls), len(relevance.calls), writer.calls
    saved = db.get_run(result.run_id)
    checkpoint = db.load_latest_checkpoint(result.run_id)
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        for _ in range(2):
            response = client.get(f"/api/runs/{result.run_id}/evidence")
            assert response.status_code == 200
            graph = response.json()
            evidence = {item["evidence_id"]: item for item in graph["evidence"]}
            sources = {item["source_id"]: item for item in graph["sources"]}
            verified = {item["claim_id"]: item for item in graph["verification_results"]}
            aligned = {item["claim_id"]: item for item in graph["relevance_results"]}
            assert len(graph["claims"]) == 4
            for claim in graph["claims"]:
                assert verified[claim["claim_id"]]["status"] == "SUPPORTED"
                assert aligned[claim["claim_id"]]["status"] == "ALIGNED"
                for key in claim["evidence_ids"]:
                    assert sources[evidence[key]["source_id"]]["url"] == "https://example.org/paper"
            assert "api_key" not in response.text and "research_db_path" not in response.text
    assert before == (calls.copy(), len(verification.calls), len(relevance.calls), writer.calls)
    assert db.get_run(result.run_id) == saved
    assert db.load_latest_checkpoint(result.run_id).checkpoint_id == checkpoint.checkpoint_id


def test_partial_snapshot_does_not_invent_verification(db, monkeypatch):
    worker, calls, verification, relevance, writer = pipeline(db, monkeypatch, fail_stage="TASK_RESEARCH_COMPLETED:D1")
    with pytest.raises(RuntimeError):
        worker.run("Goal")
    run = db.list_runs()[0]
    before = calls.copy(), len(verification.calls), len(relevance.calls), writer.calls
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        body = client.get(f"/api/runs/{run.run_id}/evidence").json()
        assert len(body["claims"]) == 1 and len(body["evidence"]) == 1
        assert not body["verification_results"] and not body["relevance_results"]
    assert before == (calls.copy(), len(verification.calls), len(relevance.calls), writer.calls)


def test_invalid_checkpoint_is_rejected_without_new_work(db, monkeypatch):
    worker, calls, *_ = pipeline(db, monkeypatch)
    run = worker.prepare_run("Goal")
    db.mark_failed(run.run_id, RuntimeError("stopped"))
    with db.connection() as conn, conn:
        conn.execute("UPDATE checkpoints SET state_json='{}' WHERE run_id=?", (run.run_id,))
    with TestClient(create_app(settings=Settings(_env_file=None), store=db)) as client:
        response = client.get(f"/api/runs/{run.run_id}/evidence")
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "CHECKPOINT_INVALID"
    assert not calls
