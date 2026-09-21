import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.supervisor import Supervisor
from app.schemas import ResearchPlan


def test_supervisor_prompt_defaults_to_chinese() -> None:
    from app.agents.supervisor import _PROMPT

    assert "Simplified Chinese" in _PROMPT.read_text(encoding="utf-8")


def test_json_fallback_produces_valid_plan() -> None:
    payload = {
        "goal": "Understand a topic",
        "tasks": [
            {"id": f"T{i}", "title": f"Aspect {i}", "question": f"Question {i}?", "description": f"Research aspect {i}", "task_type": "discovery"}
            for i in range(1, 4)
        ],
    }

    class ModelStub:
        def with_structured_output(self, schema, method):
            raise NotImplementedError("json_schema unavailable")

        def invoke(self, messages):
            return AIMessage(content=json.dumps(payload))

    plan = Supervisor(ModelStub()).create_plan("Research this topic")
    assert [task.id for task in plan.tasks] == ["T1", "T2", "T3"]


def test_supervisor_requires_explicit_task_type() -> None:
    payload = {"goal": "Research", "tasks": [
        {"id": f"T{i}", "title": f"Aspect {i}", "question": "What?", "description": "Research"}
        for i in range(1, 4)
    ]}
    with pytest.raises(ValueError, match="explicitly set task_type"):
        Supervisor._validate_plan(ResearchPlan.model_validate(payload))


def test_supervisor_rejects_invalid_task_type() -> None:
    payload = {"goal": "Research", "tasks": [
        {"id": f"T{i}", "title": f"Aspect {i}", "question": "What?", "description": "Research", "task_type": "wrong"}
        for i in range(1, 4)
    ]}
    with pytest.raises(ValueError, match="task_type"):
        ResearchPlan.model_validate(payload)


def test_invalid_fallback_is_reported() -> None:
    class ModelStub:
        def with_structured_output(self, schema, method):
            raise NotImplementedError("json_schema unavailable")

        def invoke(self, messages):
            return AIMessage(content="not JSON")

    try:
        Supervisor(ModelStub()).create_plan("Research this topic")
    except RuntimeError as exc:
        assert "JSON fallback failed" in str(exc)
    else:
        raise AssertionError("Expected a clear parse error")
