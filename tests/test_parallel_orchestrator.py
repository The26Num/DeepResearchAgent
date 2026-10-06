import asyncio
import json
import re
from threading import Barrier, Event, Lock, get_ident

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.agents.researcher import Researcher
from app.research.orchestrator import ResearchOrchestrator
from app.research.tool_budget import budgeted_tools
from app.schemas import CompactResearchResult, ResearchExecutionResult, ResearchPlan, ResearchTask


def task(task_id, dependencies=None, task_type="discovery"):
    return ResearchTask(id=task_id, title=task_id, question=f"Question {task_id}",
                        description=f"Research {task_id}", task_type=task_type,
                        depends_on=dependencies or [])


def execution(task_id):
    return ResearchExecutionResult(memo=f"Memo {task_id}", tools_used=[],
                                   compact=CompactResearchResult(task_id=task_id, summary=f"Result {task_id}"))


class SavedSupervisor:
    def __init__(self, plan):
        self.plan = plan

    def create_plan(self, query):
        return self.plan


def test_three_independent_tasks_really_overlap(capsys):
    plan = ResearchPlan(goal="Research", tasks=[task(f"D{i}") for i in range(1, 4)])
    barrier = Barrier(3, timeout=5)
    worker_threads = set()
    lock = Lock()

    class ResearcherStub:
        def research(self, task, context, goal):
            with lock:
                worker_threads.add(get_ident())
            assert context == {}
            barrier.wait()  # A serial implementation cannot pass this barrier.
            return execution(task.id)

    result = ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")
    assert len(worker_threads) == 3
    assert all(item.status == "completed" for item in result.plan.tasks)
    assert "正在执行批次：3 项任务（并发上限 3）" in capsys.readouterr().out


def test_five_ready_tasks_use_batches_of_three_and_two(capsys):
    plan = ResearchPlan(goal="Research", tasks=[task(f"D{i}") for i in range(1, 6)])
    first_barrier, second_barrier = Barrier(3, timeout=5), Barrier(2, timeout=5)
    first_ids = {"D1", "D2", "D3"}
    active = peak = 0
    finished = set()
    lock = Lock()

    class ResearcherStub:
        def research(self, task, context, goal):
            nonlocal active, peak
            with lock:
                if task.id not in first_ids:
                    assert first_ids <= finished
                active += 1
                peak = max(peak, active)
            try:
                (first_barrier if task.id in first_ids else second_barrier).wait()
                return execution(task.id)
            finally:
                with lock:
                    active -= 1
                    finished.add(task.id)

    result = ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub(), max_concurrency=3).run("Research")
    assert peak == 3 and active == 0
    assert list(result.memos) == [f"D{i}" for i in range(1, 6)]
    output = capsys.readouterr().out
    assert "正在执行批次：3 项任务" in output
    assert "正在执行批次：2 项任务" in output


def test_dependencies_wait_for_every_result_and_final_order_is_stable(capsys):
    roots = [task(f"D{i}") for i in range(1, 4)]
    analysis = task("A", [item.id for item in roots], "analysis")
    synthesis = task("S", ["A"], "synthesis")
    # Forward references must work; final output follows this original order.
    plan = ResearchPlan(goal="Research", tasks=[synthesis, analysis, *roots])
    barrier = Barrier(3, timeout=5)
    completed = {item.id: Event() for item in roots}
    finish_order = []
    lock = Lock()

    class ResearcherStub:
        def research(self, task, context, goal):
            assert list(context) == task.depends_on
            assert all(next(item for item in plan.tasks if item.id == dep).status == "completed"
                       for dep in task.depends_on)
            if task.id.startswith("D"):
                barrier.wait()
                if task.id == "D1":
                    assert completed["D3"].wait(5)
                elif task.id == "D3":
                    assert completed["D2"].wait(5)
                with lock:
                    finish_order.append(task.id)
                completed[task.id].set()
            else:
                assert all(event.is_set() for event in completed.values())
                assert all(context[dep].summary == f"Result {dep}" for dep in task.depends_on)
                finish_order.append(task.id)
            return execution(task.id)

    result = ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")
    assert finish_order == ["D2", "D3", "D1", "A", "S"]
    assert list(result.memos) == ["S", "A", "D1", "D2", "D3"]
    assert list(result.compact_results) == list(result.memos)
    output = capsys.readouterr().out
    positions = [output.index(f"研究摘要 — {item.id}") for item in plan.tasks]
    assert positions == sorted(positions)


def test_failure_waits_for_batch_and_stops_downstream(capsys):
    roots = [task(f"D{i}") for i in range(1, 4)]
    plan = ResearchPlan(goal="Research", tasks=[*roots, task("A", [item.id for item in roots], "analysis"),
                                               task("S", ["A"], "synthesis"), task("later")])
    barrier = Barrier(3, timeout=5)
    failed = Event()
    finished = set()
    lock = Lock()

    class ResearcherStub:
        def research(self, task, context, goal):
            assert task.id in {"D1", "D2", "D3"}
            barrier.wait()
            if task.id == "D2":
                failed.set()
                raise ValueError("simulated source failure")
            assert failed.wait(5)
            with lock:
                finished.add(task.id)
            return execution(task.id)

    with pytest.raises(RuntimeError, match="D2: ValueError: simulated source failure"):
        ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")
    assert [item.status for item in plan.tasks] == ["completed", "failed", "completed", "pending", "pending", "pending"]
    assert finished == {"D1", "D3"}
    output = capsys.readouterr().out
    assert "[D2] failed — ValueError: simulated source failure" in output
    assert "未执行任务：A, S, later" in output
    assert "研究摘要 — D1" in output and "研究摘要 — D3" in output


def test_shared_dependencies_are_copied_for_each_execution():
    plan = ResearchPlan(goal="Research", tasks=[task("D"), task("A1", ["D"], "analysis"),
                                               task("A2", ["D"], "analysis")])
    barrier = Barrier(2, timeout=5)

    class ResearcherStub:
        def research(self, task, context, goal):
            if task.id != "D":
                assert context["D"].summary == "Result D"
                context["D"].summary = task.id
                context["D"].key_findings.append(task.id)
                barrier.wait()
                assert context["D"].summary == task.id
                assert context["D"].key_findings == [task.id]
            return execution(task.id)

    result = ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")
    assert result.compact_results["D"].summary == "Result D"
    assert result.compact_results["D"].key_findings == []


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3"])
def test_invalid_concurrency_is_rejected(limit):
    with pytest.raises(ValueError, match="positive integer"):
        ResearchOrchestrator(max_concurrency=limit)


def test_ready_selection_requires_completed_status_and_saved_result():
    root = task("D")
    plan = ResearchPlan(goal="Research", tasks=[root, task("A", ["D"], "analysis")])
    root.status = "running"
    assert ResearchOrchestrator._get_ready_tasks(plan, {"D": execution("D")}) == []
    root.status = "completed"
    assert ResearchOrchestrator._get_ready_tasks(plan, {}) == []
    assert [item.id for item in ResearchOrchestrator._get_ready_tasks(plan, {"D": execution("D")})] == ["A"]
    root.status = "failed"
    assert ResearchOrchestrator._get_ready_tasks(plan, {"D": execution("D")}) == []


def test_async_entry_point_and_sync_entry_point_guard():
    plan = ResearchPlan(goal="Research", tasks=[task("D")])

    class ResearcherStub:
        def research(self, task, context, goal):
            return execution(task.id)

    orchestrator = ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub())

    async def run():
        with pytest.raises(RuntimeError, match="await ResearchOrchestrator.arun"):
            orchestrator.run("Research")
        return await orchestrator.arun("Research")

    assert list(asyncio.run(run()).memos) == ["D"]


def test_invalid_dag_is_rejected_before_execution():
    class ResearcherStub:
        def research(self, **kwargs):
            raise AssertionError("Invalid plans must not execute")

    plan = ResearchPlan(goal="Research", tasks=[task("D1", ["D2"]), task("D2", ["D1"])])
    with pytest.raises(ValueError, match="cycle"):
        ResearchOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")


def test_no_ready_tasks_terminates_instead_of_looping():
    # Simulate an upstream state change after validation, without adding scheduler states.
    root = task("D")
    plan = ResearchPlan(goal="Research", tasks=[root, task("A", ["D"], "analysis")])

    class ResearcherStub:
        def research(self, task, context, goal):
            return execution(task.id)

    class StalledOrchestrator(ResearchOrchestrator):
        @staticmethod
        def _get_ready_tasks(plan, executions):
            root.status = "failed"
            return ResearchOrchestrator._get_ready_tasks(plan, executions)

    with pytest.raises(RuntimeError, match="No research task is ready; unresolved dependencies"):
        StalledOrchestrator(SavedSupervisor(plan), ResearcherStub()).run("Research")


def test_native_researcher_has_separate_agents_messages_budgets_and_task_logs(capsys):
    plan = ResearchPlan(goal="Research", tasks=[task(f"D{i}") for i in range(1, 4)])
    barrier = Barrier(3, timeout=5)
    budgets, agents, requests = [], [], []
    lock = Lock()

    @tool
    def fake_search(query: str, max_results: int = 5) -> str:
        """Return a local fake search result."""
        return json.dumps({"results": [{"url": f"https://example.org/{query}"}]})

    @tool
    def fake_fetch(url: str, max_chars: int = 5000) -> str:
        """Return a local fake source page."""
        return json.dumps({"url": url, "text": "Source content"})

    def factory(budget):
        tools = budgeted_tools(budget, fake_search, fake_fetch)

        class AgentStub:
            def invoke(self, request):
                task_id = re.search(r"\nID: (D\d)\n", request["messages"][0]["content"]).group(1)
                with lock:
                    requests.append(request)
                barrier.wait()
                search = tools[0].invoke({"query": task_id})
                fetch = tools[1].invoke({"url": f"https://example.org/{task_id}"})
                return {"messages": [
                    ToolMessage(content=search, tool_call_id="search", name="web_search"),
                    ToolMessage(content=fetch, tool_call_id="fetch", name="fetch_webpage"),
                    AIMessage(content=f"## 研究任务\n{task_id}\n## 主要发现\nFinding {task_id}\n## 重要来源\nNone\n## 不确定性与缺失信息\nNone"),
                ]}

        agent = AgentStub()
        with lock:
            budgets.append(budget)
            agents.append(agent)
        return agent

    researcher = Researcher.__new__(Researcher)
    researcher.system_prompt = "Research instructions"
    researcher._agent_factory = factory
    result = ResearchOrchestrator(SavedSupervisor(plan), researcher).run("Research")
    assert len({id(agent) for agent in agents}) == 3
    assert len({id(budget) for budget in budgets}) == 3
    assert len({id(request["messages"]) for request in requests}) == 3
    assert all(budget.calls == ["web_search", "fetch_webpage"] for budget in budgets)
    assert all(budget.search_calls == budget.fetch_calls == 1 for budget in budgets)
    for task_id, memo in result.memos.items():
        assert task_id in memo
        assert all(other.id not in memo for other in plan.tasks if other.id != task_id)
    output = capsys.readouterr().out
    for item in plan.tasks:
        assert f"[{item.id}] Tool progress: web_search 1/3" in output
        assert f"[{item.id}] Tool progress: fetch_webpage 1/4" in output
