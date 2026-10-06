import pytest

from app.research.orchestrator import ResearchOrchestrator
from app.schemas import CompactResearchResult, ResearchExecutionResult, ResearchPlan, ResearchTask


def execution_for(task: ResearchTask) -> ResearchExecutionResult:
    return ResearchExecutionResult(
        memo=f"Memo for {task.id}",
        compact=CompactResearchResult(task_id=task.id, summary=f"Compact for {task.id}"),
        tools_used=["web_search"],
    )


def make_plan() -> ResearchPlan:
    return ResearchPlan(
        goal="Understand a topic",
        tasks=[
            ResearchTask(id="T1", title="First", question="Question one?", description="Investigate one"),
            ResearchTask(id="T2", title="Second", question="Question two?", description="Investigate two"),
        ],
    )


def test_tasks_run_in_order_and_complete(capsys) -> None:
    plan = make_plan()
    seen: list[tuple[str, str, dict[str, CompactResearchResult]]] = []

    class SupervisorStub:
        def create_plan(self, query: str) -> ResearchPlan:
            assert query == "topic"
            return plan

    class ResearcherStub:
        def research(self, task: ResearchTask, context: dict[str, CompactResearchResult] | None = None, goal: str | None = None) -> ResearchExecutionResult:
            assert goal == "topic"
            seen.append((task.id, task.status, context or {}))
            return execution_for(task)

    result = ResearchOrchestrator(SupervisorStub(), ResearcherStub(), max_concurrency=1).run("topic")
    assert seen == [("T1", "running", {}), ("T2", "running", {})]
    assert [task.status for task in result.plan.tasks] == ["completed", "completed"]
    assert result.memos == {"T1": "Memo for T1", "T2": "Memo for T2"}
    assert result.compact_results["T1"].summary == "Compact for T1"
    output = capsys.readouterr().out
    assert "研究计划" in output
    assert "任务类型：discovery" in output
    assert "依赖上下文字符数：0" in output
    assert "工具调用：web_search x1" in output
    assert "压缩结果字符数：" in output


def test_failed_task_stops_and_marks_failed() -> None:
    plan = make_plan()

    class SupervisorStub:
        def create_plan(self, query: str) -> ResearchPlan:
            return plan

    class ResearcherStub:
        def research(self, task: ResearchTask, context: dict[str, CompactResearchResult] | None = None, goal: str | None = None) -> ResearchExecutionResult:
            raise RuntimeError("search unavailable")

    with pytest.raises(RuntimeError, match="search unavailable"):
        ResearchOrchestrator(SupervisorStub(), ResearcherStub(), max_concurrency=1).run("topic")
    assert [task.status for task in plan.tasks] == ["failed", "pending"]


def test_dependency_memos_reach_later_task_even_when_plan_is_out_of_order(capsys) -> None:
    plan = ResearchPlan(
        goal="Research",
        tasks=[
            ResearchTask(id="task3", title="Synthesis", question="What follows?", description="Synthesize", depends_on=["task1", "task2"]),
            ResearchTask(id="task1", title="Methods", question="Which methods?", description="Research methods"),
            ResearchTask(id="task2", title="Papers", question="Which papers?", description="Research papers"),
        ],
    )
    seen: list[tuple[str, dict[str, CompactResearchResult]]] = []

    class SupervisorStub:
        def create_plan(self, query: str) -> ResearchPlan:
            return plan

    class ResearcherStub:
        def research(self, task: ResearchTask, context: dict[str, CompactResearchResult] | None = None, goal: str | None = None) -> ResearchExecutionResult:
            seen.append((task.id, dict(context or {})))
            return execution_for(task)

    result = ResearchOrchestrator(SupervisorStub(), ResearcherStub(), max_concurrency=1).run("topic")
    assert seen == [
        ("task1", {}),
        ("task2", {}),
        ("task3", {"task1": execution_for(plan.tasks[1]).compact, "task2": execution_for(plan.tasks[2]).compact}),
    ]
    assert all(task.status == "completed" for task in result.plan.tasks)
    assert "依赖：task1, task2" in capsys.readouterr().out
