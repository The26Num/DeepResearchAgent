from collections import Counter
from dataclasses import dataclass

from app.agents.researcher import Researcher
from app.agents.supervisor import Supervisor
from app.llm import create_llm
from app.research.context import build_dependency_context
from app.schemas.plan import ResearchPlan, validate_plan_dependencies
from app.schemas.research_result import CompactResearchResult


@dataclass
class ResearchResult:
    plan: ResearchPlan #Supervisor一开始生成的研究计划
    memos: dict[str, str] #给用户看
    compact_results: dict[str, CompactResearchResult]# 给task2看


class ResearchOrchestrator:
    def __init__(self, supervisor: Supervisor | None = None, researcher: Researcher | None = None) -> None:
        if supervisor is None or researcher is None:
            model = create_llm()
            supervisor = supervisor or Supervisor(model)
            researcher = researcher or Researcher(model)
        self.supervisor = supervisor
        self.researcher = researcher

    def run(self, query: str) -> ResearchResult:
        plan = self.supervisor.create_plan(query)  #先让supervisor生成一个研究计划
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

        #创建两个结果容器
        memos: dict[str, str] = {}  
        compact_results: dict[str, CompactResearchResult] = {}


        while pending_tasks := [task for task in plan.tasks if task.status == "pending"]:
            ready_tasks = [
                task for task in pending_tasks
                if all(dependency in compact_results for dependency in task.depends_on)
            ]
            if not ready_tasks:
                remaining = ", ".join(task.id for task in pending_tasks)
                raise RuntimeError(f"No research task is ready; unresolved dependencies for: {remaining}.")

            for task in ready_tasks:
                #收集dependence结果，注意这里传的是compact_results，因为compact_results是给task2看的，memos是给用户看的
                dependency_context = {dependency: compact_results[dependency] for dependency in task.depends_on}
                print("\n" + "=" * 40 + f"\n正在执行 {task.id}\n" + "=" * 40)
                print(f"任务类型：{task.task_type}")
                print(f"依赖：{', '.join(task.depends_on) or '无'}")
                if isinstance(self.researcher, Researcher):
                    _, context_text = self.researcher.build_request(task, dependency_context, query)
                else:
                    context_text = build_dependency_context(dependency_context)
                print(f"依赖上下文字符数：{len(context_text)}")
                task.status = "running"  #切换任务状态为running，表示任务正在执行
                #让researcher开始执行任务，researcher会根据任务类型和依赖上下文来决定如何执行任务，并返回一个execution对象，包含任务的执行结果
                try:
                    execution = self.researcher.research(task=task, context=dependency_context, goal=query)
                except Exception:
                    task.status = "failed"
                    raise   #把异常抛出给上层调用者，表示任务执行失败
                #如果researcher执行任务成功，则切换任务状态为completed，并将执行结果存入memos和compact_results中
                task.status = "completed"
                memos[task.id] = execution.memo
                compact_results[task.id] = execution.compact
                #打印执行结果的相关信息，包括工具调用情况和压缩结果的字符数
                counts = Counter(execution.tools_used)
                tools_line = ", ".join(f"{name} x{counts[name]}" for name in ("web_search", "fetch_webpage") if counts[name])
                print(f"工具调用：{tools_line or '无'}")
                print(f"压缩结果字符数：{len(execution.compact.model_dump_json())}")
                print("\n" + "=" * 40 + f"\n研究摘要 — {task.id}\n" + "=" * 40)
                print(execution.memo)

        return ResearchResult(plan=plan, memos=memos, compact_results=compact_results)
