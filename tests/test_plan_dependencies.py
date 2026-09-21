import pytest

from app.schemas import ResearchPlan, ResearchTask
from app.schemas.plan import validate_plan_dependencies


def task(task_id: str, depends_on: list[str] | None = None) -> ResearchTask:
    return ResearchTask(
        id=task_id,
        title=task_id,
        question=f"What about {task_id}?",
        description=f"Research {task_id}",
        depends_on=depends_on or [],
    )


def test_valid_dependencies_allow_forward_references() -> None:
    plan = ResearchPlan(goal="Research", tasks=[task("task3", ["task1", "task2"]), task("task1"), task("task2")])
    validate_plan_dependencies(plan)


@pytest.mark.parametrize(
    ("tasks", "message"),
    [
        ([task("task1"), task("task1")], "unique"),
        ([task("task1", ["missing"])], "unknown"),
        ([task("task1", ["task1"])], "itself"),
        ([task("task1", ["task2"]), task("task2", ["task1"])], "cycle"),
    ],
)
def test_invalid_dependencies_fail(tasks: list[ResearchTask], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_plan_dependencies(ResearchPlan(goal="Research", tasks=tasks))
