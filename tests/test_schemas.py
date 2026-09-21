import pytest
from pydantic import ValidationError

from app.schemas import Evidence, ResearchPlan, ResearchTask, Source


def test_research_task_defaults_and_status_validation() -> None:
    task = ResearchTask(id="T1", title="Background", question="What is known?", description="Survey background")
    assert task.status == "pending"
    assert task.task_type == "discovery"
    assert task.depends_on == []
    other = ResearchTask(id="T2", title="Methods", question="Which methods?", description="Survey methods", depends_on=["T1"])
    assert other.depends_on == ["T1"]
    task.depends_on.append("T2")
    assert other.depends_on == ["T1"]
    task.status = "running"
    with pytest.raises(ValidationError):
        ResearchTask(id="T1", title="Background", question="What is known?", description="Survey background", status="unknown")


@pytest.mark.parametrize("task_type", ["discovery", "analysis", "synthesis"])
def test_research_task_accepts_valid_types(task_type: str) -> None:
    task = ResearchTask(id="T1", title="Topic", question="What?", description="Research", task_type=task_type)
    assert task.task_type == task_type


def test_research_task_rejects_invalid_type() -> None:
    with pytest.raises(ValidationError):
        ResearchTask(id="T1", title="Topic", question="What?", description="Research", task_type="review")


def test_research_plan_validates_nested_tasks() -> None:
    plan = ResearchPlan.model_validate(
        {"goal": "Understand the topic", "tasks": [{"id": "T1", "title": "Background", "question": "What is known?", "description": "Survey background"}]}
    )
    assert isinstance(plan.tasks[0], ResearchTask)
    with pytest.raises(ValidationError):
        ResearchPlan.model_validate({"goal": "Understand the topic", "tasks": [{"id": "T1"}]})


def test_source_optional_fields_and_required_url() -> None:
    source = Source(id="S1", title="A paper", url="https://example.org/paper", source_type="paper")
    assert source.snippet is None
    assert source.published_at is None
    with pytest.raises(ValidationError):
        Source(id="S1", title="A paper", url="", source_type="paper")


def test_evidence_confidence_bounds() -> None:
    evidence = Evidence(id="E1", claim="A claim", source_id="S1", task_id="T1", confidence=0.8)
    assert evidence.excerpt is None
    assert evidence.confidence == 0.8
    with pytest.raises(ValidationError):
        Evidence(id="E1", claim="A claim", source_id="S1", task_id="T1", confidence=1.1)
