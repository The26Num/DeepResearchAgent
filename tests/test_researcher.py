import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.researcher import MAX_RESEARCH_INPUT_CHARS, Researcher
from app.schemas import CompactResearchResult, ResearchTask, SourceSummary


def test_researcher_prompt_requires_chinese_and_diverse_broad_queries() -> None:
    from app.agents.researcher import _PROMPT

    prompt = _PROMPT.read_text(encoding="utf-8")
    assert "Simplified Chinese" in prompt
    assert "at least 2 meaningfully different search queries" in prompt
    assert "For a very narrow lookup, one search is enough" in prompt
    assert all(f"## {heading}" in prompt for heading in ("研究任务", "主要发现", "重要来源", "不确定性与缺失信息"))


def make_researcher(factory) -> Researcher:
    researcher = Researcher.__new__(Researcher)
    researcher.system_prompt = "System instructions"
    researcher._agent_factory = factory
    return researcher


def compact(task_id: str, url: str = "https://example.org/paper", point: str = "Brief point") -> CompactResearchResult:
    return CompactResearchResult(
        task_id=task_id, summary="A short prior result", key_findings=["One finding"],
        sources=[SourceSummary(title="Paper", url=url, key_point=point)],
    )


def test_researcher_places_compact_context_before_current_task() -> None:
    captured: list[str] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            captured.append(request["messages"][0]["content"])
            return {"messages": [AIMessage(content="## Key Findings\nA synthesis finding.")]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task2", title="Synthesis", question="What follows?", description="Use previous findings", task_type="synthesis")
    execution = researcher.research(task, {"task1": compact("task1")}, goal="Find latest work")
    assert execution.memo.startswith("## Key Findings")
    assert execution.compact.task_id == "task2"
    message = captured[0]
    assert message.index("RELEVANT PREVIOUS RESEARCH") < message.index("CURRENT TASK") < message.index("PRIMARY OBJECTIVE")
    assert "https://example.org/paper" in message
    assert "A short prior result" in message
    assert "Find latest work" in message


def test_researcher_omits_empty_context_section() -> None:
    captured: list[str] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            captured.append(request["messages"][0]["content"])
            return {"messages": [
                ToolMessage(content=json.dumps({"results": [{"url": "https://example.org"}]}), tool_call_id="call1", name="web_search"),
                ToolMessage(content=json.dumps({"url": "https://example.org", "text": "Page content"}), tool_call_id="call2", name="fetch_webpage"),
                AIMessage(content="Memo"),
            ]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task1", title="Methods", question="Which methods?", description="Research")
    researcher.research(task)
    assert "RELEVANT PREVIOUS RESEARCH" not in captured[0]


def test_researcher_rejects_ungrounded_memo_after_search_error() -> None:
    class AgentStub:
        def invoke(self, request: dict) -> dict:
            return {"messages": [
                ToolMessage(content=json.dumps({"error": "web search failed: timeout"}), tool_call_id="call1", name="web_search"),
                AIMessage(content="Invented memo with fake sources"),
            ]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task1", title="Methods", question="Which methods?", description="Research")
    with pytest.raises(RuntimeError, match="no accessible sources"):
        researcher.research(task)


def test_researcher_reminds_agent_to_read_search_result() -> None:
    requests: list[dict] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            requests.append(request)
            if len(requests) == 1:
                return {"messages": [
                    ToolMessage(content=json.dumps({"results": [{"url": "https://example.org/paper"}]}), tool_call_id="call1", name="web_search"),
                    AIMessage(content="Premature memo"),
                ]}
            return {"messages": [
                ToolMessage(content=json.dumps({"url": "https://example.org/paper", "text": "Paper body"}), tool_call_id="call2", name="fetch_webpage"),
                AIMessage(content="Revised memo"),
            ]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task1", title="Methods", question="Which methods?", description="Research")
    assert researcher.research(task).memo == "Revised memo"
    assert len(requests) == 2


def test_analysis_with_url_only_compact_context_requires_source_reading() -> None:
    requests: list[dict] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            requests.append(request)
            if len(requests) == 1:
                return {"messages": [AIMessage(content="Premature analysis")]}
            return {"messages": [
                ToolMessage(content=json.dumps({"url": "https://example.org/paper", "text": "Method details"}), tool_call_id="call1", name="fetch_webpage"),
                AIMessage(content="Method analysis"),
            ]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task2", title="Methods", question="What methods?", description="Analyze papers", task_type="analysis")
    assert researcher.research(task, {"task1": compact("task1")}).memo == "Method analysis"
    assert len(requests) == 2
    assert "fetch_webpage" in requests[0]["messages"][0]["content"]
    assert "https://example.org/paper" in requests[1]["messages"][-1].content


def test_analysis_rereads_even_a_detailed_compact_key_point() -> None:
    task = ResearchTask(id="task2", title="Methods", question="How?", description="Analyze", task_type="analysis")
    assert Researcher._analysis_needs_reading(task, "Source: Paper\nURL: https://example.org/paper\nKey Point: " + "detail " * 100)


def test_discovery_uses_prior_url_if_new_search_has_no_results() -> None:
    requests: list[dict] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            requests.append(request)
            if len(requests) == 1:
                return {"messages": [
                    ToolMessage(content=json.dumps({"results": []}), tool_call_id="search", name="web_search"),
                    AIMessage(content="No new sources"),
                ]}
            return {"messages": [
                ToolMessage(content=json.dumps({"url": "https://example.org/paper", "text": "Prior source details"}), tool_call_id="fetch", name="fetch_webpage"),
                AIMessage(content="Updated memo"),
            ]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task2", title="Benchmarks", question="Which results?", description="Discover")
    assert researcher.research(task, {"task1": compact("task1")}).memo == "Updated memo"
    assert len(requests) == 2
    assert "https://example.org/paper" in requests[1]["messages"][-1].content


def test_discovery_requires_new_search_even_with_context() -> None:
    class AgentStub:
        def invoke(self, request: dict) -> dict:
            return {"messages": [AIMessage(content="Old memo repeated")]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task2", title="New papers", question="Which new papers?", description="Discover")
    with pytest.raises(RuntimeError, match="did not search"):
        researcher.research(task, {"task1": compact("task1")})


def test_synthesis_requests_explicit_comparison_when_missing() -> None:
    requests: list[dict] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            requests.append(request)
            if len(requests) == 1:
                return {"messages": [AIMessage(content="## Key Findings\nPaper A is useful. Paper B is useful.\n## Important Sources\nNone")]}
            return {"messages": [AIMessage(content="## Key Findings\n### Shared Findings\nBoth address sparse rewards.\n### Differences\nA shapes rewards; B learns preferences.\n## Important Sources\nNone")]}

    researcher = make_researcher(lambda budget: AgentStub())
    task = ResearchTask(id="task3", title="Synthesis", question="What is the trend?", description="Compare", task_type="synthesis")
    execution = researcher.research(task, {"task1": compact("task1"), "task2": compact("task2", "https://example.org/other")})
    assert "### Differences" in execution.memo
    assert len(requests) == 2


def test_each_task_gets_fresh_agent_and_no_raw_history_crosses_boundary() -> None:
    agents: list[object] = []
    initial_requests: list[dict] = []

    class AgentStub:
        def invoke(self, request: dict) -> dict:
            initial_requests.append(request)
            if len(agents) == 1:
                return {"messages": [
                    ToolMessage(content=json.dumps({"results": [{"url": "https://example.org/paper"}]}), tool_call_id="s", name="web_search"),
                    ToolMessage(content=json.dumps({"url": "https://example.org/paper", "text": "RAW_PAGE_SECRET"}), tool_call_id="f", name="fetch_webpage"),
                    AIMessage(content="## Key Findings\nA concise finding.\n## Important Sources\n### Paper\n- URL: https://example.org/paper"),
                ]}
            return {"messages": [
                ToolMessage(content=json.dumps({"url": "https://example.org/paper", "text": "Fresh read"}), tool_call_id="f2", name="fetch_webpage"),
                AIMessage(content="## Key Findings\nA new analysis."),
            ]}

    def factory(budget):
        agent = AgentStub()
        agents.append(agent)
        return agent

    researcher = make_researcher(factory)
    first = researcher.research(ResearchTask(id="task1", title="Papers", question="Which?", description="Find"))
    researcher.research(ResearchTask(id="task2", title="Methods", question="How?", description="Analyze", task_type="analysis"), {"task1": first.compact})
    assert len(agents) == 2 and agents[0] is not agents[1]
    second_input = initial_requests[1]["messages"]
    assert len(second_input) == 1
    assert "RAW_PAGE_SECRET" not in second_input[0]["content"]
    assert "A concise finding" in second_input[0]["content"]


def test_input_safety_preserves_current_task_when_context_is_large() -> None:
    researcher = make_researcher(lambda budget: None)
    researcher.system_prompt = "S" * 14500
    task = ResearchTask(id="task2", title="Priority", question="UNTOUCHED_CURRENT_QUESTION", description="Analyze", task_type="synthesis")
    many = {f"task{i}": CompactResearchResult(task_id=f"task{i}", summary="x" * 500, key_findings=["y" * 300] * 6) for i in range(4)}
    request, previous = researcher.build_request(task, many, goal="Research goal")
    assert len(researcher.system_prompt) + len(request) <= MAX_RESEARCH_INPUT_CHARS
    assert 0 < len(previous) < 8000
    assert "UNTOUCHED_CURRENT_QUESTION" in request
    assert request.index("RELEVANT PREVIOUS RESEARCH") < request.index("CURRENT TASK")


def test_input_safety_does_not_silently_drop_all_dependencies() -> None:
    researcher = make_researcher(lambda budget: None)
    researcher.system_prompt = "S" * 19900
    task = ResearchTask(id="task2", title="Priority", question="What?", description="Analyze", task_type="analysis")
    with pytest.raises(ValueError, match="cannot retain any dependency context"):
        researcher.build_request(task, {"task1": compact("task1")})
