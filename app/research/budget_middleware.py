"""Hide exhausted source tools before the next model request."""

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage

from app.research.tool_budget import ToolBudget


class ResearchBudgetMiddleware(AgentMiddleware):
    def __init__(self, budget: ToolBudget):
        self.budget = budget

    def _request(self, request):
        with self.budget.lock:
            remaining = {"web_search": max(0, self.budget.max_search_calls - self.budget.search_calls),
                         "fetch_webpage": max(0, self.budget.max_fetch_calls - self.budget.fetch_calls)}
        def name(item):
            return item.name if hasattr(item, "name") else item.get("function", item).get("name")
        tools = [item for item in request.tools if remaining.get(name(item), 1) > 0]
        instruction = ("\nCURRENT SOURCE TOOL BUDGET: " + ", ".join(f"{key}={value} remaining" for key, value in remaining.items())
                       + ". The total number of calls in this turn must not exceed each remaining count. "
                       "Exhausted tools are unavailable; use successful readings already obtained. "
                       "When no source tools remain, finish the current memo without further source calls.")
        message = request.system_message or SystemMessage(content="")
        return request.override(tools=tools, system_message=message.model_copy(update={"content": message.text + instruction}))

    def wrap_model_call(self, request, handler):
        return self._response(handler(self._request(request)))

    async def awrap_model_call(self, request, handler):
        return self._response(await handler(self._request(request)))

    def _response(self, response):
        """Providers may still emit hidden tools or more calls than remain."""
        if isinstance(response, ExtendedModelResponse):
            return replace(response, model_response=self._response(response.model_response))
        if isinstance(response, ModelResponse):
            return replace(response, result=[self._response(message) for message in response.result])
        if not isinstance(response, AIMessage) or not response.tool_calls:
            return response
        with self.budget.lock:
            slots = {"web_search": self.budget.max_search_calls - self.budget.search_calls,
                     "fetch_webpage": self.budget.max_fetch_calls - self.budget.fetch_calls}
        allowed, blocked = [], []
        for call in response.tool_calls:
            name = call["name"]
            if name in slots:
                if slots[name] <= 0:
                    blocked.append(name)
                    continue
                slots[name] -= 1
            allowed.append(call)
        if not blocked:
            return response
        if self.budget.report:
            self.budget.report(f"withheld {len(blocked)} source calls outside remaining budget: {', '.join(sorted(set(blocked)))}")
        extra = {key: value for key, value in response.additional_kwargs.items() if key != "tool_calls"}
        # With no valid call left, terminate this agent pass with an empty final
        # message. Researcher's existing one-attempt, no-tools memo recovery
        # then finalizes from actual readings without a tool retry loop.
        return response.model_copy(update={"tool_calls": allowed, "additional_kwargs": extra,
                                           "content": response.content if allowed else ""})
