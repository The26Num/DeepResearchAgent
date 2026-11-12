import json
from pathlib import Path
from time import monotonic

import pytest

from app.api.main import create_app
from fastapi.testclient import TestClient
from app.config import Settings
from app.persistence.models import STATE_VERSION
from app.persistence.store import ResearchRunStore
from app.persistence.serialization import state_from_json
from app.research.evidence_context import EvidenceContext
from app.research.evidence_extraction import prepare_extraction_payload, build_evidence_bundle
from app.research.evidence_store import EvidenceStore
from app.research.source_archive import ArchiveIntegrityError, text_hash
from app.research.task_resources import RunResources
from app.schemas import ResearchTask, TaskEvidenceBundle, Evidence, Claim
from app.schemas.evidence_extraction import ExtractedEvidenceResult, ExtractedClaim
from archive_support import FACT, URL, Response, pipeline


def run_fixture(tmp_path, monkeypatch, failure=None):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), research_db_path=str(tmp_path / "runs.db"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, model = pipeline(db, cfg, monkeypatch, failure=failure)
    return cfg, db, worker, model


def test_tail_read_skill_verification_citation_and_checkpoint_resume(tmp_path, monkeypatch):
    started = monotonic()
    def stop(stage):
        if stage == "CITATION_COMPLETED":
            raise RuntimeError("offline checkpoint boundary stop")
    cfg, db, worker, model = run_fixture(tmp_path, monkeypatch, stop)
    with pytest.raises(RuntimeError, match="checkpoint boundary"):
        worker.run("学术研究：固定资料中的实验测量与限制")
    run = db.list_runs()[0]
    checkpoint = db.load_latest_checkpoint(run.run_id)
    state = checkpoint.state
    assert state.state_version == STATE_VERSION == 2
    assert state.resources.run_id == run.run_id
    assert state.resources.tasks["D"].active_skills.keys() == {"academic_review"}
    stored = EvidenceStore.restore(state.evidence_store.model_dump(mode="json"))
    evidence = next(item for item in stored.get_evidence_for_task("D") if item.content == FACT)
    assert evidence.origin.start_char > 12000
    ref = state.resources.tasks["D"].snapshots[evidence.origin.snapshot_id]
    body, _, _ = worker._resources.archive.read(ref)
    assert body[evidence.origin.start_char:evidence.origin.end_char] == evidence.content
    assert evidence.origin.text_hash == text_hash(FACT)
    assert all(item.status == "SUPPORTED" for item in state.verification_results)
    assert state.rendered_report.references and "[1]" in state.rendered_report.markdown
    assert state.rendered_report.usage.llm_calls == 0
    assert state.resources.tasks["S"].readings  # Internal reads are allowed.
    assert state.task_results["S"].evidence_bundle.evidence == []  # No synthesis bypass.
    assert state.task_results["S"].evidence_bundle.sources == []
    first = model.requests[0][0][0].text
    assert "RESEARCH SKILL CATALOG" in first and "先通过资料目录" not in first
    later = [message.text for request, _ in model.requests for message in request if message.type == "system"]
    assert any("先通过资料目录" in text for text in later)
    previews = [json.loads(message.text) for request, _ in model.requests for message in request
                if message.type == "tool" and message.name == "fetch_webpage"]
    assert previews and all(FACT not in page["text"] for page in previews)
    assert not any("统计口径、时间地域" in text and "对关键数字记录时间段" in text for text in later)
    encoded = state.model_dump_json()
    assert "background material background material" not in encoded
    assert str(tmp_path) not in encoded
    resumed, new_model = pipeline(db, cfg, monkeypatch)
    result = resumed.resume(run.run_id)
    assert not new_model.requests and db.get_run(run.run_id).status == "COMPLETED"
    assert result.rendered_report == state.rendered_report
    persisted = db.load_latest_checkpoint(run.run_id).state
    assert persisted.resources.tasks["D"].calls == state.resources.tasks["D"].calls
    assert persisted.resources.tasks["D"].model_calls == state.resources.tasks["D"].model_calls
    metrics = {"mode": "offline_simulation", "actual_usage": None,
               "estimate_method": state.resources.tasks["D"].token_estimation,
               "estimated_tokens_including_output_reserves": sum(item.estimated_tokens for item in state.resources.tasks.values()),
               "researcher_and_extraction_model_invocations": sum(sum(item.model_calls.values()) for item in state.resources.tasks.values()),
               "offline_workflow_seconds": monotonic() - started,
               "tool_calls": {key: item.calls for key, item in state.resources.tasks.items()},
               "returned_source_characters": sum(item.returned_source_chars for item in state.resources.tasks.values()),
               "formal_evidence": len(stored.snapshot()["evidence"]), "tail_evidence_present": True}
    (tmp_path / "offline-resource-metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")


def test_partial_resume_reuses_committed_snapshots_and_does_not_download_again(tmp_path, monkeypatch):
    def stop(stage):
        if stage == "TASK_COMPLETED:D":
            raise RuntimeError("pause after source commit")
    cfg, db, worker, _ = run_fixture(tmp_path, monkeypatch, stop)
    with pytest.raises(RuntimeError, match="source commit"):
        worker.run("学术研究：固定资料中的测量结果")
    run = db.list_runs()[0]
    before = db.load_latest_checkpoint(run.run_id).state
    assert before.resources.tasks["D"].committed
    def no_network(*args, **kwargs):
        raise AssertionError("A completed source task must not re-download on resume.")
    monkeypatch.setattr("requests.get", no_network)
    resumed, _ = pipeline(db, cfg, monkeypatch)
    result = resumed.resume(run.run_id)
    assert result.rendered_report.references
    after = db.load_latest_checkpoint(run.run_id).state
    assert before.resources.tasks["D"] == after.resources.tasks["D"]


@pytest.mark.parametrize("corruption", ["missing", "body_hash", "skill_hash"])
def test_missing_or_corrupt_pins_fail_before_new_model_calls_but_saved_text_remains(tmp_path, monkeypatch, corruption):
    cfg, db, worker, _ = run_fixture(tmp_path, monkeypatch,
        lambda stage: (_ for _ in ()).throw(RuntimeError("pause")) if stage == "CITATION_COMPLETED" else None)
    with pytest.raises(RuntimeError, match="pause"):
        worker.run("学术研究：固定资料中的测量结果")
    run = db.list_runs()[0]
    state = db.load_latest_checkpoint(run.run_id).state
    ref = next(iter(state.resources.tasks["D"].snapshots.values()))
    target = Path(cfg.source_archive_path) / ref.relative_dir / "body.txt"
    if corruption == "missing":
        target.unlink()
    elif corruption == "body_hash":
        target.write_text("tampered", encoding="utf-8")
    else:
        pin = state.resources.skills["academic_review"]
        (Path(cfg.source_archive_path) / pin.relative_dir / "SKILL.md").write_text("tampered", encoding="utf-8")
    fresh, model = pipeline(db, cfg, monkeypatch)
    with pytest.raises(ArchiveIntegrityError):
        fresh.resume(run.run_id)
    assert not model.requests
    # Plain saved content remains readable without requiring archive files.
    assert db.load_latest_checkpoint(run.run_id).state.rendered_report.markdown == state.rendered_report.markdown


def ledger_resource(tmp_path):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    task = ResearchTask(id="D", title="Find", question="Tail measurement", description="Find evidence")
    resource = RunResources(cfg, "run").start_task(task)
    text = "Intro. " + "background " * 1500 + "\n" + FACT + "\nUnrelated unread conclusion."
    ref = resource.run.archive.put(requested_url=URL, url=URL, title="Source", raw=text.encode(), text=text, task_id="D")
    resource.remember(ref)
    return task, resource, ref, text


def test_only_actual_read_ranges_generate_candidates_and_overlaps_stay_contiguous(tmp_path):
    task, resource, ref, text = ledger_resource(tmp_path)
    start = text.index(FACT)
    resource.read_source(ref.snapshot_id, start_char=start, end_char=start + len(FACT))
    resource.read_source(ref.snapshot_id, start_char=start + 2, end_char=start + len(FACT))
    resource.read_source(ref.snapshot_id, start_char=0, end_char=6)
    pages = resource.reading_pages()
    assert len(pages) == 2 and all("background" not in item["text"] for item in pages)
    payload = prepare_extraction_payload(task, FACT, [{"url": URL, "text": "Untrusted supplied text"}], EvidenceContext(), resources=resource)
    candidate = next(item for item in payload["candidates"] if item["content"] == FACT)
    extracted = ExtractedEvidenceResult(claims=[ExtractedClaim(text=FACT, supporting_evidence_indexes=[candidate["evidence_index"]])])
    bundle = build_evidence_bundle(task, extracted, payload, EvidenceContext())
    assert bundle.evidence[0].origin.start_char == start
    assert all("Untrusted supplied text" not in item["content"] for item in payload["candidates"])


@pytest.mark.parametrize("change", ["hash", "offset", "snapshot", "unread_range", "content"])
def test_forged_candidate_origins_are_rejected(tmp_path, change):
    task, resource, ref, text = ledger_resource(tmp_path)
    start = text.index(FACT)
    resource.read_source(ref.snapshot_id, start_char=start, end_char=start + len(FACT))
    payload = prepare_extraction_payload(task, FACT, [], EvidenceContext(), resources=resource)
    candidate = payload["candidates"][0]
    origin = candidate["_archive_origin"]
    if change == "hash":
        origin["text_hash"] = "0" * 64
    elif change == "offset":
        origin["end_char"] += 100000
    elif change == "snapshot":
        origin["snapshot_id"] = "snap_" + "0" * 64
    elif change == "unread_range":
        candidate["content"] = "Unrelated unread conclusion."
        candidate["_start"], candidate["_end"] = 0, len(candidate["content"])
        payload["fetched_pages"][0]["text"] = candidate["content"]
        origin.update(start_char=text.index(candidate["content"]), end_char=len(text), text_hash=text_hash(candidate["content"]))
    else:
        candidate["content"] = "Invented measurement."
    extracted = ExtractedEvidenceResult(claims=[ExtractedClaim(text=FACT, supporting_evidence_indexes=[0])])
    with pytest.raises((ValueError, PermissionError)):
        build_evidence_bundle(task, extracted, payload, EvidenceContext())


def test_same_url_versions_do_not_merge_evidence_locations(tmp_path):
    task, resource, first, text = ledger_resource(tmp_path)
    second = resource.run.archive.put(requested_url=URL, url=URL, title="Source", raw=(text + "New version.").encode(),
                                      text=text + "New version.", task_id="D")
    resource.remember(second)
    store = EvidenceStore()
    for ref in (first, second):
        resource.read_source(ref.snapshot_id, start_char=text.index(FACT), end_char=text.index(FACT) + len(FACT))
    payload = prepare_extraction_payload(task, FACT, [], EvidenceContext(), resources=resource)
    choices = [item["evidence_index"] for item in payload["candidates"] if item["content"] == FACT]
    bundle = build_evidence_bundle(task, ExtractedEvidenceResult(claims=[ExtractedClaim(text=FACT, supporting_evidence_indexes=choices)]), payload, EvidenceContext())
    store.merge(bundle)
    assert store.counts == {"sources": 1, "evidence": 2, "claims": 1}
    assert len({item.origin.snapshot_id for item in EvidenceStore.restore(store.snapshot()).get_evidence_for_task("D")}) == 2


def test_historical_v1_run_record_checkpoint_and_origin_none_remain_compatible(tmp_path, monkeypatch):
    cfg, db, worker, _ = run_fixture(tmp_path, monkeypatch)
    result = worker.run("学术研究：固定资料中的测量结果")
    current = db.load_latest_checkpoint(result.run_id).state
    raw = current.model_dump(mode="json")
    raw.pop("resources")
    raw["state_version"] = 1
    for evidence in raw["evidence_store"]["evidence"]:
        evidence.pop("origin", None)
    for execution in raw["task_results"].values():
        for evidence in (execution.get("evidence_bundle") or {}).get("evidence", []):
            evidence.pop("origin", None)
    # Historical citation debug is disabled; IDs, locations and rendered text stay unchanged.
    migrated = state_from_json(json.dumps(raw))
    assert migrated.state_version == 2 and not migrated.resources.tasks
    assert all(item.origin is None for item in migrated.evidence_store.evidence)
    with db.connection() as connection, connection:
        connection.execute("UPDATE checkpoints SET state_json=? WHERE checkpoint_id=?", (json.dumps(raw), db.get_run(result.run_id).last_checkpoint_id))
        connection.execute("UPDATE research_runs SET state_version=1 WHERE run_id=?", (result.run_id,))
    assert db.load_latest_checkpoint(result.run_id).state.rendered_report == current.rendered_report
    db.save_checkpoint(result.run_id, "RUN_COMPLETED", migrated)
    assert db.get_run(result.run_id).state_version == 2
    with TestClient(create_app(settings=cfg, store=db)) as client:
        graph = client.get(f"/api/runs/{result.run_id}/evidence")
        assert graph.status_code == 200 and graph.json()["evidence"][0]["origin"] is None
        assert client.get(f"/api/runs/{result.run_id}/report").status_code == 200


def test_completed_reports_and_optional_origin_api_survive_archive_loss(tmp_path, monkeypatch):
    cfg, db, worker, _ = run_fixture(tmp_path, monkeypatch)
    result = worker.run("学术研究：固定资料中的测量结果")
    state = db.load_latest_checkpoint(result.run_id).state
    ref = next(iter(state.resources.tasks["D"].snapshots.values()))
    (Path(cfg.source_archive_path) / ref.relative_dir / "body.txt").unlink()
    with TestClient(create_app(settings=cfg, store=db)) as client:
        report = client.get(f"/api/runs/{result.run_id}/report")
        graph = client.get(f"/api/runs/{result.run_id}/evidence")
        assert report.status_code == graph.status_code == 200
        assert graph.json()["evidence"][0]["origin"]["snapshot_id"] == ref.snapshot_id
        assert str(tmp_path) not in graph.text and "relative_dir" not in graph.text
    completed, model = pipeline(db, cfg, monkeypatch)
    assert completed.resume(result.run_id).rendered_report == state.rendered_report
    assert not model.requests
