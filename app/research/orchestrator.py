import asyncio
from collections import Counter
from dataclasses import dataclass

from app.agents.researcher import Researcher
from app.agents.supervisor import Supervisor
from app.llm import create_llm
from app.research.context import build_dependency_context
from app.schemas.plan import ResearchPlan, validate_plan_dependencies
from app.schemas.research_result import CompactResearchResult, ResearchExecutionResult
from app.schemas.task import ResearchTask


@dataclass
class ResearchResult:
    plan: ResearchPlan #Supervisor一开始生成的研究计划
    memos: dict[str, str] #给用户看
    compact_results: dict[str, CompactResearchResult]# 给task2看


class ResearchOrchestrator:
    def __init__(
        self, supervisor: Supervisor | None = None, researcher: Researcher | None = None,
        *, max_concurrency: int = 3,
    ) -> None:
        if isinstance(max_concurrency, bool) or not isinstance(max_concurrency, int) or max_concurrency < 1:
            raise ValueError("max_concurrency must be a positive integer.")
        if supervisor is None or researcher is None:
            model = create_llm()
            supervisor = supervisor or Supervisor(model)
            researcher = researcher or Researcher(model)
        self.supervisor = supervisor
        self.researcher = researcher
        self.max_concurrency = max_concurrency

    def run(self, query: str) -> ResearchResult:
        """Synchronous entry point for the CLI; async callers should await arun()."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.arun(query))
        raise RuntimeError("An event loop is already running; use await ResearchOrchestrator.arun(query).")

    async def arun(self, query: str) -> ResearchResult:
        plan = await asyncio.to_thread(self.supervisor.create_plan, query)
        validate_plan_dependencies(plan)  #对研究计划进行依赖性验证，确保没有循环依赖或未定义的依赖
        if any(task.status != "pending" for task in plan.tasks):  #Supervisor生成的研究计划中的所有任务状态必须是pending，如果不是则抛出异常
            raise ValueError("ResearchPlan tasks must start as pending.")
        #为什么要求所有任务状态必须是pending？因为在研究计划生成后，所有任务都还没有开始执行。
        #supervisor只负责生成研究计划，而不执行任务。任务的状态在执行过程中会被更新为running、completed或failed。
        #任务状态由ResearchOrchestrator在执行任务时进行管理，以确保任务的执行顺序和依赖关系得到正确处理。
        print("=" * 40 + "\n研究计划\n" + "=" * 40) 
        print(f"目标：{plan.goal}\n")  #这里打印的就是supervisor生成的研究计划
        for task in plan.tasks:
            print(f"[{task.id}] {task.title} — {task.question}")
            print(f"任务类型：{task.task_type}")
            print(f"依赖：{', '.join(task.depends_on) or '无'}\n")

        executions: dict[str, ResearchExecutionResult] = {}
        while pending_tasks := [task for task in plan.tasks if task.status == "pending"]:
            ready_tasks = self._get_ready_tasks(plan, executions)
            if not ready_tasks:
                remaining = ", ".join(task.id for task in pending_tasks)
                raise RuntimeError(f"No research task is ready; unresolved dependencies for: {remaining}.")

            batch = ready_tasks[:self.max_concurrency]
            print(
                "\n" + "=" * 40
                + f"\n正在执行批次：{len(batch)} 项任务（并发上限 {self.max_concurrency}）\n"
                + "=" * 40,
                flush=True,
            )
            # Wait for every worker in this batch, including when a sibling fails.
            # Cancelling an await on to_thread() cannot stop its synchronous worker.
            outcomes = await asyncio.gather(
                *(self._run_task(task, executions, query) for task in batch),
                return_exceptions=True,
            )
            failures: list[tuple[ResearchTask, BaseException]] = []
            for task, outcome in zip(batch, outcomes):
                if isinstance(outcome, BaseException):
                    failures.append((task, outcome))
                else:
                    executions[task.id] = outcome
            if failures:
                self._print_results(plan, executions)
                detail = "; ".join(
                    f"{task.id}: {type(error).__name__}: {error}" for task, error in failures
                )
                remaining = ", ".join(task.id for task in plan.tasks if task.status == "pending")
                if remaining:
                    print(f"未执行任务：{remaining}（本批次有任务失败，停止后续调度）", flush=True)
                raise RuntimeError(
                    f"Research tasks failed: {detail}. Pending tasks not executed: {remaining or 'none'}."
                ) from failures[0][1]

        self._print_results(plan, executions)
        return ResearchResult(
            plan=plan,
            memos={task.id: executions[task.id].memo for task in plan.tasks},
            compact_results={task.id: executions[task.id].compact for task in plan.tasks},
        )

    @staticmethod
    def _get_ready_tasks(
        plan: ResearchPlan, executions: dict[str, ResearchExecutionResult],
    ) -> list[ResearchTask]:
        tasks_by_id = {task.id: task for task in plan.tasks}
        return [
            task for task in plan.tasks
            if task.status == "pending" and all(
                tasks_by_id[dependency].status == "completed" and dependency in executions
                for dependency in task.depends_on
            )
        ]

    async def _run_task(
        self, task: ResearchTask, executions: dict[str, ResearchExecutionResult], query: str,
    ) -> ResearchExecutionResult:
        task.status = "running"
        try:
            # Isolate mutable compact results even when sibling tasks share dependencies.
            dependency_context = {
                dependency: executions[dependency].compact.model_copy(deep=True)
                for dependency in task.depends_on
            }
            if isinstance(self.researcher, Researcher):
                _, context_text = self.researcher.build_request(task, dependency_context, query)
            else:
                context_text = build_dependency_context(dependency_context)
            print(
                f"[{task.id}] 正在执行\n"
                f"[{task.id}] 任务类型：{task.task_type}\n"
                f"[{task.id}] 依赖：{', '.join(task.depends_on) or '无'}\n"
                f"[{task.id}] 依赖上下文字符数：{len(context_text)}",
                flush=True,
            )
            execution = await asyncio.to_thread(
                self.researcher.research, task=task, context=dependency_context, goal=query,
            )
        except Exception as error:
            task.status = "failed"
            print(f"[{task.id}] failed — {type(error).__name__}: {error}", flush=True)
            raise
        task.status = "completed"
        print(f"[{task.id}] completed", flush=True)
        return execution

    @staticmethod
    def _print_results(plan: ResearchPlan, executions: dict[str, ResearchExecutionResult]) -> None:
        """Print successful memos in the original plan order, regardless of finish order."""
        for task in plan.tasks:
            if task.id not in executions:
                continue
            execution = executions[task.id]
            counts = Counter(execution.tools_used)
            tools_line = ", ".join(
                f"{name} x{counts[name]}" for name in ("web_search", "fetch_webpage") if counts[name]
            )
            print(
                "\n" + "=" * 40 + f"\n研究摘要 — {task.id}\n" + "=" * 40
                + f"\n任务类型：{task.task_type}"
                + f"\n依赖：{', '.join(task.depends_on) or '无'}"
                + f"\n工具调用：{tools_line or '无'}"
                + f"\n压缩结果字符数：{len(execution.compact.model_dump_json())}"
                + "\n" + execution.memo,
                flush=True,
            )
