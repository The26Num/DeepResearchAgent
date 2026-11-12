import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config import Settings
from app.research.source_archive import SourceArchive, ArchiveIntegrityError, ArchiveLimitError, text_hash
from app.research.task_resources import RunResources
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.research.resource_tools import resource_tools
from app.schemas import ResearchTask
from app.tools.fetch_webpage import fetch_webpage
from archive_support import HTML, FACT, URL, Response


@pytest.fixture
def resource(tmp_path):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    return RunResources(cfg, "run_one").start_task(ResearchTask(id="D", title="Discovery", question="Find findings", description="Find sources"))


def store(resource, text="Introduction. Original fact. Conclusion.", url=URL):
    ref = resource.run.archive.put(requested_url=url, url=url, title="Source", raw=text.encode(), text=text, task_id=resource.task.id)
    resource.remember(ref)
    return ref


def test_full_raw_and_tail_persist_but_preview_is_bounded(monkeypatch, resource):
    monkeypatch.setattr("app.tools.fetch_webpage.requests.get", lambda *a, **kw: Response())
    with resource.activate():
        page = json.loads(fetch_webpage.invoke({"url": URL, "max_chars": 1000}))
    ref = resource.ref(page["snapshot_id"])
    body, metadata, sections = resource.run.archive.read(ref)
    assert FACT not in page["text"] and len(page["text"]) <= 1000
    assert body.index(FACT) > 12000 and len(body) == page["body_chars"]
    assert (resource.run.archive.root / ref.relative_dir / "raw.bin").read_bytes() == HTML
    assert metadata["complete_response"] and metadata["offset_unit"] == "python_unicode_characters"
    assert "Cookie" not in metadata and "headers" not in metadata
    hit = resource.search_sources("TAIL_METRIC")["hits"][0]
    before = len(resource.snapshot().readings)
    assert hit["start_char"] > 12000 and hit["navigation_only"]
    assert before == 1  # Search did not put its preview into the ledger.
    read = resource.read_source(ref.snapshot_id, section_id=hit["section_id"])
    assert FACT in read["text"] and body[read["start_char"]:read["end_char"]] == read["text"]
    assert len(resource.snapshot().readings) == before + 1


def test_versions_are_immutable_and_identical_content_reuses_snapshot(resource):
    first = store(resource, "Original version. Old conclusion.")
    same = store(resource, "Original version. Old conclusion.")
    second = store(resource, "Updated version. New conclusion.")
    assert first == same and first.snapshot_id != second.snapshot_id
    assert resource.run.archive.read(first)[0] == "Original version. Old conclusion."


@pytest.mark.parametrize("body,content_type,status", [
    (b"PDF fixture", "application/pdf", "unsupported"),
    (b"plain text fixture", "text/plain", "unsupported"),
    (b"<html><body></body></html>", "text/html", "parse_failed"),
])
def test_fetch_statuses_are_honest(monkeypatch, resource, body, content_type, status):
    monkeypatch.setattr("app.tools.fetch_webpage.requests.get", lambda *a, **kw: Response(body, headers={"Content-Type": content_type}))
    with resource.activate():
        result = json.loads(fetch_webpage.invoke({"url": URL}))
    assert "error" in result and result["status"] == status
    assert not resource.snapshot().readings


def test_stream_download_limit_does_not_archive_truncated_body(monkeypatch, resource):
    resource.record.limits["response_bytes"] = 1024
    response = Response(b"x" * 3000)
    monkeypatch.setattr("app.tools.fetch_webpage.requests.get", lambda *a, **kw: response)
    with resource.activate():
        result = json.loads(fetch_webpage.invoke({"url": URL}))
    assert result["status"] == "download_or_storage_limit" and response.closed
    assert not resource.snapshot().snapshots and not resource.snapshot().readings
    assert resource.snapshot().reserved_download_bytes == 1024


def test_cache_reuse_and_explicit_refresh(monkeypatch, resource):
    calls = []
    def response(*a, **kw):
        calls.append(a)
        return Response(HTML if len(calls) == 1 else HTML.replace(b"97", b"98"))
    monkeypatch.setattr("app.tools.fetch_webpage.requests.get", response)
    with resource.activate():
        first = json.loads(fetch_webpage.invoke({"url": URL, "max_chars": 200}))
        reused = json.loads(fetch_webpage.invoke({"url": URL, "max_chars": 200}))
        fresh = json.loads(fetch_webpage.invoke({"url": URL, "max_chars": 200, "refresh": True}))
    assert len(calls) == 2 and reused["reused"]
    assert first["snapshot_id"] == reused["snapshot_id"] != fresh["snapshot_id"]


def test_scope_is_committed_task_manifest_not_disk_or_evidence_context(resource):
    first = store(resource)
    sibling = resource.run.start_task(ResearchTask(id="D2", title="Other", question="Other", description="Other"))
    child = resource.run.start_task(ResearchTask(id="A", title="Analysis", question="Analyze", description="Analyze", task_type="analysis"), ["D"])
    for task in (sibling, child):
        assert task.list_sources()["total"] == 0
        with pytest.raises(PermissionError):
            task.read_source(first.snapshot_id)
    resource.run.commit("D")
    assert child.list_sources()["total"] == 1 and not sibling.list_sources()["sources"]
    assert child.read_source(first.snapshot_id)["text"]
    other_run = RunResources(resource.run.settings, "another_run").start_task(ResearchTask(id="A", title="A", question="A", description="A"))
    with pytest.raises(PermissionError):
        other_run.read_source(first.snapshot_id)
    with pytest.raises(PermissionError):
        child.list_sources(task_id="D2")


@pytest.mark.parametrize("snapshot_id", ["../body.txt", "C:\\Users\\private", "/etc/passwd", "snap_unknown"])
def test_path_inputs_are_never_read(resource, snapshot_id):
    with pytest.raises(PermissionError):
        resource.read_source(snapshot_id)


def test_unicode_offsets_read_caps_pagination_and_fulltext_chinese_search(resource):
    ref = store(resource, "前言\n" + "背景说明" * 4000 + "\n关键结论：测量值为九十七。\n最后一段。")
    hits = resource.search_sources("关键结论")["hits"]
    assert hits and hits[0]["start_char"] > 12000
    result = resource.read_source(ref.snapshot_id, start_char=hits[0]["start_char"], end_char=hits[0]["end_char"], max_chars=5)
    assert len(result["text"]) == 5 and not result["range_complete"] and result["next_start_char"]
    for index in range(4):
        store(resource, f"Source {index}. Fact.", f"https://example.org/{index}")
    page = resource.list_sources(limit=2)
    assert len(page["sources"]) == 2 and page["next_offset"] == 2
    assert len(resource.list_sources(offset=2, limit=2)["sources"]) == 2


def test_corruption_and_storage_limit_are_rejected(resource, tmp_path):
    ref = store(resource)
    (resource.run.archive.root / ref.relative_dir / "body.txt").write_text("tampered", encoding="utf-8")
    with pytest.raises(ArchiveIntegrityError, match="hash"):
        resource.read_source(ref.snapshot_id)
    archive = SourceArchive(tmp_path / "tiny", "run", max_bytes=100)
    with pytest.raises(ArchiveLimitError):
        archive.put(requested_url=URL, url=URL, title="Title", raw=b"x" * 200, text="body", task_id="D")
    assert not list((tmp_path / "tiny").rglob("metadata.json"))


def test_parallel_reads_reserve_budget_before_execution(resource):
    ref = store(resource)
    resource.record.limits["read_source"] = 2
    budget = ToolBudget(0, 0, resources=resource)
    reader = next(tool for tool in resource_tools(budget) if tool.name == "read_source")
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda _: json.loads(reader.invoke({"snapshot_id": ref.snapshot_id})), range(10)))
    assert sum("text" in value for value in results) == 2
    assert resource.snapshot().calls["read_source"] == 2 and len(resource.snapshot().readings) == 2


@pytest.mark.parametrize("filename", ["raw.bin", "metadata.json", "sections.json"])
def test_raw_metadata_and_index_integrity_cannot_be_bypassed(resource, filename):
    ref = store(resource)
    target = resource.run.archive.root / ref.relative_dir / filename
    target.write_bytes(target.read_bytes() + b" ")
    # Whitespace is insignificant JSON, so index verification also compares the
    # parsed index to the complete canonical text, not a stale pointer.
    if filename == "sections.json":
        target.write_text("[]", encoding="utf-8")
    with pytest.raises(ArchiveIntegrityError):
        resource.read_source(ref.snapshot_id)
    assert not resource.snapshot().readings


def test_late_http_completion_cannot_register_a_source(monkeypatch, resource):
    from threading import Event, Thread
    started, release = Event(), Event()
    result = []
    def get(*args, **kwargs):
        started.set()
        assert release.wait(3)
        return Response()
    monkeypatch.setattr("requests.get", get)
    def fetch():
        with resource.activate():
            result.append(json.loads(fetch_webpage.invoke({"url": URL})))
    worker = Thread(target=fetch)
    worker.start()
    assert started.wait(3)
    resource.close()
    release.set()
    worker.join(3)
    assert not worker.is_alive() and "error" in result[0]
    assert not resource.snapshot().snapshots and not resource.snapshot().readings


def test_abstract_is_labeled_and_unicode_offsets_are_not_byte_offsets(monkeypatch, resource):
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response(url="https://arxiv.org/abs/fixture"))
    with resource.activate():
        page = json.loads(fetch_webpage.invoke({"url": "https://arxiv.org/abs/fixture", "max_chars": 100}))
    assert page["representation"] == "abstract"
    ref = store(resource, "A🙂中文。\nNext sentence.")
    read = resource.read_source(ref.snapshot_id, start_char=1, end_char=3)
    assert read["text"] == "🙂中" and read["end_char"] - read["start_char"] == 2
