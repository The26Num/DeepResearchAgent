from concurrent.futures import ThreadPoolExecutor
import pytest

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import SystemMessage
from langchain_core.tools import tool

from app.research.budget_middleware import ResearchBudgetMiddleware
from app.research.tool_budget import ToolBudget


def test_exhausted_tool_is_hidden_and_current_budget_is_injected():
    budget = ToolBudget(1, 2, search_calls=1, fetch_calls=1)
    request = ModelRequest(model=None, messages=[], system_message=SystemMessage(content="Original instructions"),
                           tools=[{"name": "web_search"}, {"function": {"name": "fetch_webpage"}}, {"name": "write_todos"}])
    captured = []
    ResearchBudgetMiddleware(budget).wrap_model_call(request, lambda value: captured.append(value))
    assert captured[0].tools == request.tools[1:]
    assert "web_search=0 remaining" in captured[0].system_message.text
    assert "fetch_webpage=1 remaining" in captured[0].system_message.text
    assert request.system_message.text == "Original instructions" and len(request.tools) == 3


def test_parallel_reservations_never_exceed_task_budget():
    budget = ToolBudget(3, 4)
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda _: budget.reserve("web_search"), range(30)))
    assert sum(value is not None for value in results) == 3
    assert budget.search_calls == 3 and budget.calls == ["web_search"] * 3


def test_unadvertised_and_excess_parallel_calls_are_withheld_before_execution():
    from langchain_core.messages import AIMessage
    from langchain.agents.middleware.types import ModelResponse
    budget = ToolBudget(1, 1, search_calls=1)
    calls = [{"id": "s", "name": "web_search", "args": {"query": "again"}},
             {"id": "f1", "name": "fetch_webpage", "args": {"url": "https://example.org/1"}},
             {"id": "f2", "name": "fetch_webpage", "args": {"url": "https://example.org/2"}}]
    reply = AIMessage(content="Planning", tool_calls=calls, additional_kwargs={"tool_calls": ["raw duplicate"]})
    filtered = ResearchBudgetMiddleware(budget)._response(ModelResponse(result=[reply])).result[0]
    assert [call["id"] for call in filtered.tool_calls] == ["f1"]
    assert "tool_calls" not in filtered.additional_kwargs and len(reply.tool_calls) == 3
    assert budget.fetch_calls == 0  # Reservations still happen only at execution.


def test_only_exhausted_calls_end_the_agent_pass_without_publishing_planning_text():
    from langchain_core.messages import AIMessage
    budget = ToolBudget(0, 0)
    reply = AIMessage(content="Unsupported planning text", tool_calls=[
        {"id": "s", "name": "web_search", "args": {"query": "again"}}])
    filtered = ResearchBudgetMiddleware(budget)._response(reply)
    assert filtered.text == "" and filtered.tool_calls == [] and budget.calls == []


@pytest.mark.parametrize("hallucinate_hidden_tool", [False, True])
def test_native_deep_agent_stops_advertising_source_tools_as_they_are_consumed(monkeypatch, hallucinate_hidden_tool):
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from pydantic import PrivateAttr
    import app.agents.researcher as module
    from app.research.tool_budget import budgeted_tools

    @tool
    def search(query: str, max_results: int = 5) -> str:
        """Search a test fixture."""
        return '{"results": []}'

    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Read a test fixture."""
        return '{"url":"https://example.org/paper","text":"A supported fact."}'

    class Model(BaseChatModel):
        _bound: list = PrivateAttr(default_factory=list)
        _turn: int = PrivateAttr(default=0)

        @property
        def _llm_type(self):
            return "budget-integration-test"

        def bind_tools(self, tools, **kwargs):
            self._bound = [item.name if hasattr(item, "name") else item["name"] for item in tools]
            return self

        def _generate(self, messages, **kwargs):
            self._turn += 1
            if self._turn == 1:
                assert "web_search" in self._bound and "fetch_webpage" in self._bound
                reply = AIMessage(content="", tool_calls=[{"id": "s", "name": "web_search", "args": {"query": "test"}}])
            elif self._turn == 2:
                assert "web_search" not in self._bound and "fetch_webpage" in self._bound
                if hallucinate_hidden_tool:
                    reply = AIMessage(content="Unsupported planning", tool_calls=[
                        {"id": "s2", "name": "web_search", "args": {"query": "again"}}])
                else:
                    reply = AIMessage(content="", tool_calls=[{"id": "f", "name": "fetch_webpage", "args": {"url": "https://example.org/paper"}}])
            else:
                assert "web_search" not in self._bound and "fetch_webpage" not in self._bound
                reply = AIMessage(content="Completed")
            return ChatResult(generations=[ChatGeneration(message=reply)])

    monkeypatch.setattr(module, "budgeted_tools", lambda budget: budgeted_tools(budget, search, fetch))
    budget = ToolBudget(1, 1)
    result = module.Researcher(Model())._create_agent(budget).invoke({"messages": [{"role": "user", "content": "Research"}]})
    assert result["messages"][-1].text == ("" if hallucinate_hidden_tool else "Completed")
    assert result["messages"][-1].tool_calls == []
    assert budget.calls == (["web_search"] if hallucinate_hidden_tool else ["web_search", "fetch_webpage"])
