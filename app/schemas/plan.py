from pydantic import BaseModel, Field

from app.schemas.task import ResearchTask


class ResearchPlan(BaseModel):
    goal: str = Field(min_length=1)
    tasks: list[ResearchTask]


def validate_plan_dependencies(plan: ResearchPlan) -> None:
    """Reject unknown, repeated, self-referential, and cyclic task dependencies."""
    tasks_by_id = {task.id: task for task in plan.tasks}
    if len(tasks_by_id) != len(plan.tasks):
        raise ValueError("ResearchPlan task IDs must be unique.")

    for task in plan.tasks:
        if len(set(task.depends_on)) != len(task.depends_on):
            raise ValueError(f"Task {task.id} has duplicate dependencies.")
        for dependency in task.depends_on:
            if dependency not in tasks_by_id:
                raise ValueError(f"Task {task.id} depends on unknown task {dependency}.")
            if dependency == task.id:
                raise ValueError(f"Task {task.id} cannot depend on itself.")

    state: dict[str, int] = {}

    def visit(task_id: str) -> None:
        if state.get(task_id) == 1:
            raise ValueError(f"ResearchPlan contains a dependency cycle involving {task_id}.")
        if state.get(task_id) == 2:
            return
        state[task_id] = 1
        for dependency in tasks_by_id[task_id].depends_on:
            visit(dependency)
        state[task_id] = 2

    for task in plan.tasks:
        visit(task.id)
