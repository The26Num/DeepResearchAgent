"""Hide exhausted source tools before the next model request."""

from dataclasses import replace

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ExtendedModelResponse, ModelResponse
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from pydantic import ValidationError
import json

from app.research.tool_budget import ToolBudget
from app.research.reresearch_budget import ReResearchTaskBudgetExhausted


class ResearchBudgetMiddleware(AgentMiddleware):
    def __init__(self, budget: ToolBudget):
        self.budget = budget

    def _request(self, request):
        runtime = self.budget.runtime
        shared = runtime.call_budget if runtime else None
        if (shared is not None and shared.has_task_allocation(runtime.task_id)
                and shared.remaining_for("researcher", runtime.task_id) == 0):
            # Expected transition to extraction, before callback/HTTP dispatch.
            raise ReResearchTaskBudgetExhausted("Supplemental research allocation complete; continue with reserved extraction/audits.")
        with self.budget.lock:
            remaining = {"web_search": max(0, self.budget.max_search_calls - self.budget.search_calls),
                         "fetch_webpage": max(0, self.budget.max_fetch_calls - self.budget.fetch_calls)}
        if self.budget.resources:
            for name in ("web_search", "fetch_webpage", "list_sources", "search_sources", "read_source",
                         "load_research_skill", "read_skill_reference"):
                durable = self.budget.resources.remaining(name)
                remaining[name] = min(remaining.get(name, durable), durable)
            if len(self.budget.resources.record.active_skills) >= self.budget.resources.record.limits["skills"]:
                remaining["load_research_skill"] = 0
        def name(item):
            return item.name if hasattr(item, "name") else item.get("function", item).get("name")
        tools = [item for item in request.tools if remaining.get(name(item), 0) > 0]
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
        if self.budget.resources:
            for name in ("web_search", "fetch_webpage", "list_sources", "search_sources", "read_source",
                         "load_research_skill", "read_skill_reference"):
                durable = self.budget.resources.remaining(name)
                slots[name] = min(slots.get(name, durable), durable)
        allowed, blocked = [], []
        for call in response.tool_calls:
            name = call["name"]
            if slots.get(name, 0) <= 0:
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

    def _invalid_tool(self, request):
        name = request.tool_call["name"]
        resource = self.budget.resources
        if request.tool is None:
            if resource:
                resource.reserve_tool("invalid_tool")
            return ToolMessage(content=json.dumps({"error": "Unregistered tool is forbidden."}),
                               tool_call_id=request.tool_call["id"], name=name)
        schema = getattr(request.tool, "args_schema", None)
        if schema is not None and hasattr(schema, "model_validate"):
            try:
                schema.model_validate(request.tool_call["args"])
            except ValidationError:
                if resource:
                    resource.reserve_tool(name)
                else:
                    self.budget.reserve(name)
                return ToolMessage(content=json.dumps({"error": "Invalid tool arguments; the attempt consumed budget."}),
                                   tool_call_id=request.tool_call["id"], name=name)
        return None

    def wrap_tool_call(self, request, handler):
        rejected = self._invalid_tool(request)
        return rejected if rejected is not None else handler(request)

    async def awrap_tool_call(self, request, handler):
        rejected = self._invalid_tool(request)
        return rejected if rejected is not None else await handler(request)
