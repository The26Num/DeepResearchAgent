from pydantic import BaseModel, Field

from app.schemas.task import ResearchTask


class ResearchPlan(BaseModel):
    goal: str = Field(min_length=1)
    tasks: list[ResearchTask] = Field(
        description=(
            "根据用户问题动态拆分的 2～6 项研究任务。可拆分的复杂调研必须包含多个互补、"
            "保持原主题的 discovery 子问题，每个 discovery 的 depends_on=[]；"
            "之后的 analysis 分析这些已有结果，synthesis 做最终综合，二者必须有依赖。"
            "不要用一个笼统的搜索任务覆盖所有研究维度，也不要固定任务数量或领域方向。"
        ),
    )


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
