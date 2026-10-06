import json

import pytest
from langchain_core.messages import AIMessage

from app.agents.supervisor import Supervisor
from app.schemas import CompactResearchResult, ResearchExecutionResult, ResearchPlan, ResearchTask


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


def make_decomposed_plan(discovery_count: int = 3) -> ResearchPlan:
    discoveries = [
        ResearchTask(id=f"D{i}", title=f"Subproblem {i}", question=f"What about subproblem {i}?",
                     description=f"Investigate subproblem {i}", task_type="discovery")
        for i in range(1, discovery_count + 1)
    ]
    return ResearchPlan(goal="Research", tasks=[
        *discoveries,
        ResearchTask(id="A", title="Analysis", question="How do the findings compare?",
                     description="Analyze the combined results", task_type="analysis",
                     depends_on=[task.id for task in discoveries]),
        ResearchTask(id="S", title="Synthesis", question="What is the overall answer?",
                     description="Integrate the findings", task_type="synthesis", depends_on=["A"]),
    ])


@pytest.mark.parametrize("use_fallback", [False, True])
@pytest.mark.parametrize("discovery_count", [1, 2, 3, 4])
def test_supervisor_accepts_dynamic_discovery_fan_out(discovery_count: int, use_fallback: bool) -> None:
    expected = make_decomposed_plan(discovery_count)

    class ModelStub:
        def with_structured_output(self, schema, method):
            assert schema is ResearchPlan
            assert method == "json_schema"
            if use_fallback:
                raise NotImplementedError("json_schema unavailable")
            class StructuredStub:
                def invoke(self, messages):
                    return expected.model_dump()

            return StructuredStub()

        def invoke(self, messages):
            return AIMessage(content=expected.model_dump_json())

    plan = Supervisor(ModelStub()).create_plan("Research this topic")
    assert plan == expected


def test_supervisor_accepts_simple_two_task_plan() -> None:
    plan = make_decomposed_plan(1)
    plan.tasks.pop(1)
    plan.tasks[-1].depends_on = ["D1"]
    Supervisor._validate_plan(plan)


def test_supervisor_uses_model_selected_subproblems_in_both_output_paths() -> None:
    plan = make_decomposed_plan(2)
    subproblems = "1. A distinct subquestion\n2. Another complementary subquestion"
    stages: list[str] = []

    class ModelStub:
        def with_structured_output(self, schema, method):
            stages.append("structured")

            class StructuredStub:
                def invoke(self, messages):
                    assert subproblems in messages[-1].content
                    assert "Research question: Original topic" in messages[-1].content
                    raise NotImplementedError("json_schema unavailable")

            return StructuredStub()

        def invoke(self, messages):
            if not stages:
                stages.append("decomposition")
                return AIMessage(content=subproblems)
            stages.append("fallback")
            assert subproblems in messages[-1].content
            return AIMessage(content=plan.model_dump_json())

    assert Supervisor(ModelStub()).create_plan("Original topic") == plan
    assert stages == ["decomposition", "structured", "fallback"]


def test_supervisor_rejects_empty_decomposition() -> None:
    class ModelStub:
        def invoke(self, messages):
            return AIMessage(content=" ")

        def with_structured_output(self, schema, method):
            raise AssertionError("An empty decomposition must not reach plan generation")

    with pytest.raises(ValueError, match="identify research subproblems"):
        Supervisor(ModelStub()).create_plan("Research")


@pytest.mark.parametrize("count", [0, 1, 7])
def test_supervisor_rejects_out_of_range_task_count(count: int) -> None:
    plan = ResearchPlan(goal="Research", tasks=[
        ResearchTask(id=f"D{i}", title="Topic", question="What?", description="Research", task_type="discovery")
        for i in range(count)
    ])
    with pytest.raises(ValueError, match="2 to 6"):
        Supervisor._validate_plan(plan)


@pytest.mark.parametrize("task_type", ["analysis", "synthesis"])
@pytest.mark.parametrize("use_fallback", [False, True])
def test_generated_analysis_and_synthesis_require_context(task_type: str, use_fallback: bool) -> None:
    plan = make_decomposed_plan()
    next(task for task in plan.tasks if task.task_type == task_type).depends_on = []

    class ModelStub:
        def with_structured_output(self, schema, method):
            if use_fallback:
                raise NotImplementedError("json_schema unavailable")
            class StructuredStub:
                def invoke(self, messages):
                    return plan

            return StructuredStub()

        def invoke(self, messages):
            return AIMessage(content=plan.model_dump_json())

    supervisor = Supervisor(ModelStub())
    with pytest.raises(RuntimeError, match="must depend on prior research results"):
        supervisor.create_plan("Research this topic")


def test_supervisor_rejects_dependent_discovery() -> None:
    plan = make_decomposed_plan()
    plan.tasks[1].depends_on = ["D1"]
    with pytest.raises(ValueError, match="must be independent"):
        Supervisor._validate_plan(plan)


def test_decomposed_plan_runs_serially_with_only_dependency_context(capsys) -> None:
    from app.research.orchestrator import ResearchOrchestrator

    plan = make_decomposed_plan()
    Supervisor._validate_plan(plan)
    seen: list[tuple[str, list[str]]] = []

    class SupervisorStub:
        def create_plan(self, query):
            return plan

    class ResearcherStub:
        def research(self, task, context, goal):
            assert task.status == "running"
            assert sum(item.status == "running" for item in plan.tasks) == 1
            assert all(item.status == "completed" for item in plan.tasks if item.id in context)
            seen.append((task.id, list(context)))
            return ResearchExecutionResult(
                memo=f"Memo for {task.id}",
                compact=CompactResearchResult(task_id=task.id, summary=f"Result for {task.id}"),
                tools_used=[],
            )

    result = ResearchOrchestrator(SupervisorStub(), ResearcherStub(), max_concurrency=1).run("Research")
    assert seen == [("D1", []), ("D2", []), ("D3", []), ("A", ["D1", "D2", "D3"]), ("S", ["A"])]
    assert all(task.status == "completed" for task in result.plan.tasks)
    output = capsys.readouterr().out
    assert [output.index(f"[{task_id}] 正在执行") for task_id, _ in seen] == sorted(
        output.index(f"[{task_id}] 正在执行") for task_id, _ in seen
    )
