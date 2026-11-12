import json
from pathlib import Path
import shutil

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.config import Settings
from app.research.resource_middleware import ResearchContextMiddleware, message_blocks
from app.research.resource_tools import resource_tools
from app.research.source_archive import ArchiveIntegrityError
from app.research.task_resources import RunResources
from app.research.tool_budget import ToolBudget
from app.schemas import ResearchTask
from archive_support import pipeline, Response
from app.persistence.store import ResearchRunStore


def setup_resource(tmp_path, **values):
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"), **values)
    task = ResearchTask(id="D", title="Review", question="Find findings", description="Review")
    return cfg, RunResources(cfg, "run").start_task(task)


def test_catalog_is_short_and_load_changes_next_model_request_without_duplicate_text(tmp_path):
    _, resource = setup_resource(tmp_path)
    budget = ToolBudget(0, 0, resources=resource)
    request = ModelRequest(model=None, messages=[HumanMessage(content="Review the study.")], system_message=SystemMessage(content="Base rules"), tools=[])
    middleware = ResearchContextMiddleware(budget)
    initial = middleware._request(request).system_message.text
    assert "academic_review" in initial and "对每篇材料区分研究问题" not in initial
    loader = next(item for item in resource_tools(budget) if item.name == "load_research_skill")
    loaded = json.loads(loader.invoke({"skill_id": "academic_review"}))
    duplicate = json.loads(loader.invoke({"skill_id": "academic_review"}))
    assert "对每篇材料区分研究问题" in loaded["text"] and duplicate["already_active"] and "text" not in duplicate
    active = middleware._request(request).system_message.text
    assert active.count("对每篇材料区分研究问题") == 1
    assert "对关键数字记录时间段" not in active  # The unselected industry body is absent.
    assert resource.snapshot().calls["load_research_skill"] == 2
    assert not resource.snapshot().readings and not resource.snapshot().snapshots


def test_active_method_survives_legal_tool_history_compaction(tmp_path):
    _, resource = setup_resource(tmp_path, max_research_context_estimated_tokens=10000)
    resource.load_skill("academic_review")
    messages = [HumanMessage(content="Current task: compare study findings.")]
    for index, body in enumerate(("x" * 20000, "search navigation", "recent original excerpt")):
        messages += [AIMessage(content="", tool_calls=[{"id": str(index), "name": "read_source", "args": {}}]),
                     ToolMessage(content=json.dumps({"snapshot_id": "snap_" + "a" * 64, "text": body,
                                                     "start_char": 0, "end_char": len(body)}), tool_call_id=str(index), name="read_source")]
    request = ModelRequest(model=None, messages=messages, system_message=SystemMessage(content="Base rules"), tools=[])
    compact = ResearchContextMiddleware(ToolBudget(0, 0, resources=resource))._request(request)
    assert "对每篇材料区分研究问题" in compact.system_message.text
    assert "x" * 1000 not in " ".join(item.text for item in compact.messages)
    assert "recent original excerpt" in " ".join(item.text for item in compact.messages)
    assert "content_omitted" in " ".join(item.text for item in compact.messages)
    assert len(message_blocks(compact.messages)) == 4
    assert resource.snapshot().context_compactions == 1
    assert messages[2].text != compact.messages[2].text  # Original graph history was not mutated.


def test_skill_and_reference_limits_and_paths(tmp_path):
    _, resource = setup_resource(tmp_path)
    budget = ToolBudget(0, 0, resources=resource)
    tools = {item.name: item for item in resource_tools(budget)}
    for skill_id in ("academic_review", "industry_analysis"):
        assert "text" in json.loads(tools["load_research_skill"].invoke({"skill_id": skill_id}))
    assert "error" in json.loads(tools["load_research_skill"].invoke({"skill_id": "technical_comparison"}))
    reference = json.loads(tools["read_skill_reference"].invoke({"skill_id": "academic_review", "reference": "references/checklist.md"}))
    assert "论文比较检查表" in reference["text"] and reference["not_evidence"]
    for name in ("../../.env", "/etc/passwd", "C:\\private", "references/../SKILL.md"):
        with pytest.raises(PermissionError):
            resource.skill_reference("academic_review", name)
    with pytest.raises(PermissionError):
        resource.skill_reference("technical_comparison", "references/comparison.md")
    assert not resource.snapshot().readings


def test_whole_skill_over_100_lines_is_loaded_and_run_pin_survives_product_update(tmp_path):
    product = tmp_path / "product"
    source = Path(__file__).resolve().parents[1] / "app/research_skills"
    shutil.copytree(source, product)
    file = product / "academic_review/SKILL.md"
    file.write_text("method\n" * 150 + "TAIL_METHOD_RULE", encoding="utf-8")
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    run = RunResources(cfg, "old_run", product_root=product)
    task = ResearchTask(id="D", title="D", question="D", description="D")
    resource = run.start_task(task)
    assert resource.load_skill("academic_review")["text"].endswith("TAIL_METHOD_RULE")
    resource.close()
    saved = run.snapshot()
    file.write_text("UPDATED_METHOD", encoding="utf-8")
    restored = RunResources(cfg, "old_run", saved, product_root=product)
    restored.validate()
    assert restored.start_task(task).method_text().endswith("TAIL_METHOD_RULE")
    newer = RunResources(cfg, "new_run", product_root=product)
    assert newer.state.skills["academic_review"].version != saved.skills["academic_review"].version


def test_skill_file_size_is_rejected_instead_of_truncated(tmp_path):
    product = tmp_path / "product"
    shutil.copytree(Path(__file__).resolve().parents[1] / "app/research_skills", product)
    (product / "academic_review/SKILL.md").write_text("x" * 13000, encoding="utf-8")
    cfg = Settings(_env_file=None, source_archive_path=str(tmp_path / "archive"))
    with pytest.raises(ValueError, match="size limit"):
        RunResources(cfg, "run", product_root=product)


def test_no_skill_selection_still_completes_normal_pipeline(tmp_path, monkeypatch):
    cfg = Settings(_env_file=None, research_db_path=str(tmp_path / "runs.db"), source_archive_path=str(tmp_path / "archive"))
    monkeypatch.setattr("requests.get", lambda *a, **kw: Response())
    db = ResearchRunStore(cfg.research_db_path)
    worker, _ = pipeline(db, cfg, monkeypatch, use_skills=False)
    result = worker.run("固定资料研究")
    assert result.rendered_report.references
    state = db.load_latest_checkpoint(result.run_id).state
    assert all(not record.active_skills for record in state.resources.tasks.values())
