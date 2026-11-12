"""Deterministic message compaction and method retention; no summarizer LLM."""

import json

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app.llm import bounded_output_options
from app.research.task_resources import estimated_tokens, ResearchContextExceeded


def message_blocks(messages):
    """Keep every assistant tool call with its complete ToolMessage group."""
    blocks, index = [], 0
    while index < len(messages):
        message = messages[index]
        if isinstance(message, AIMessage) and message.tool_calls:
            ids = {item["id"] for item in message.tool_calls}
            block, results = [message], set()
            index += 1
            while index < len(messages) and isinstance(messages[index], ToolMessage):
                results.add(messages[index].tool_call_id)
                block.append(messages[index])
                index += 1
            if ids != results or len(results) != len(block) - 1:
                raise ResearchContextExceeded("Tool history has missing, duplicate or unmatched results.")
            blocks.append(block)
        else:
            if isinstance(message, ToolMessage):
                raise ResearchContextExceeded("Tool result has no matching assistant call.")
            blocks.append([message])
            index += 1
    return blocks


class ResearchContextMiddleware(AgentMiddleware):
    def __init__(self, budget):
        self.budget = budget

    def _request(self, request):
        resource = self.budget.resources
        if resource is None:
            return request
        resource.check_open()
        catalog = resource.run.registry.catalog() if resource.record.limits["skills"] else []
        system = (request.system_message or SystemMessage(content="")).text
        system += ("\nRESEARCH SKILL CATALOG (methods, never evidence):\n" + json.dumps(catalog, ensure_ascii=False)
                   + "\nChoose methods only when useful, using load_research_skill. Archived means stored, not fully read. "
                     "Use list_sources/search_sources/read_source for authorized material. Tool budgets and validation rules remain authoritative.\n")
        if methods := resource.method_text():
            system += "\nACTIVE RESEARCH METHODS (retained across compaction):\n" + methods
        messages = list(request.messages)
        changed = 0
        recent = {index for index, item in enumerate(messages) if isinstance(item, ToolMessage)}
        protected = set(sorted(recent)[-2:])

        def receipt(message, *, minimal=False):
            try:
                payload = json.loads(message.text)
            except (ValueError, TypeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            value = {key: payload[key] for key in ("snapshot_id", "start_char", "end_char", "url", "skill_id", "version", "reference", "error") if key in payload}
            if minimal and "snapshot_id" in value:
                value.pop("url", None)
            for container in ("sources", "hits"):
                if isinstance(payload.get(container), list):
                    value[container] = [{key: item[key] for key in
                        ("snapshot_id", "section_id", "start_char", "end_char") if key in item}
                        for item in payload[container] if isinstance(item, dict)]
            if isinstance(payload.get("results"), list):
                value["results"] = [{"url": item["url"]} for item in payload["results"]
                                    if isinstance(item, dict) and isinstance(item.get("url"), str)]
            value.update(content_omitted=True, instruction="Original tool readings remain in the archive/reading ledger for extraction; this receipt is not evidence. Do not repeat a read solely because its body was compacted. Active methods remain in system instructions.")
            return message.model_copy(update={"content": json.dumps(value, ensure_ascii=False)})

        # A loaded method is represented once, in the current system message.
        for index, message in enumerate(messages):
            if isinstance(message, ToolMessage) and message.name in {"load_research_skill", "read_skill_reference"}:
                messages[index] = receipt(message)
                changed += 1
        output = resource.record.limits["output_tokens"]
        capacity = resource.record.limits["context_tokens"] - output

        def current_system():
            notice = (f"\nCONTEXT NOTICE: {changed} tool bodies/messages omitted or replaced by position receipts. "
                      "Archived material remains available; omitted text is not evidence of absence.\n") if changed else ""
            return system + notice

        def size():
            # Count the notice during compaction, not after a request just fits.
            return estimated_tokens([SystemMessage(content=current_system()), *messages], request.tools)

        if size() > capacity:
            for index, message in enumerate(messages):
                if isinstance(message, ToolMessage) and index not in protected and len(message.text) > 600:
                    messages[index] = receipt(message)
                    changed += 1
                    if size() <= capacity:
                        break
        if size() > capacity:
            blocks = message_blocks(messages)
            # Preserve the complete initial task, current methods and recent turns.
            while len(blocks) > 4 and size() > capacity:
                removed = blocks.pop(1)
                changed += len(removed)
                messages = [item for block in blocks for item in block]
        if size() > capacity:
            # Recency cannot make two large source bodies mandatory. Keep all
            # call/result pairs and exact reading pointers, shrinking the older
            # recent body first. Full text and formal support chains stay intact.
            for index, message in enumerate(messages):
                if (isinstance(message, ToolMessage) and len(message.text) > 600
                        and message.name in {"fetch_webpage", "read_source", "search_sources", "list_sources", "web_search"}):
                    messages[index] = receipt(message, minimal=True)
                    changed += 1
                    if size() <= capacity:
                        break
        if size() > capacity:
            # Tool-call planning is a model draft, not an evidence chain. Keep
            # the structured calls/IDs and paired results, removing only prose.
            for index, message in enumerate(messages):
                if isinstance(message, AIMessage) and message.tool_calls and len(message.text) > 600:
                    messages[index] = message.model_copy(update={"content": "Earlier tool planning omitted; structured calls, paired results and source positions are retained."})
                    changed += 1
                    if size() <= capacity:
                        break
        if changed:
            with resource.lock:
                resource.record.context_compactions += 1
                resource.record.omitted_messages += changed
        estimate = size()
        if estimate > capacity:
            raise ResearchContextExceeded(
                f"Mandatory goal, methods, tools and complete exchanges exceed the estimated context limit "
                f"(input_estimate={estimate}, output_reserve={output}, limit={resource.record.limits['context_tokens']}); "
                "no partial JSON/support chain was cut."
            )
        message_blocks(messages)
        resource.next_context_estimate = estimate
        options = {**request.model_settings, **bounded_output_options(request.model, output)}
        options["timeout"] = min(resource.run.settings.model_request_timeout_seconds,
                                 resource.runtime.remaining() if resource.runtime else resource.run.settings.model_request_timeout_seconds)
        return request.override(messages=messages, system_message=SystemMessage(content=current_system()), model_settings=options)

    def wrap_model_call(self, request, handler):
        return handler(self._request(request))

    async def awrap_model_call(self, request, handler):
        return await handler(self._request(request))
