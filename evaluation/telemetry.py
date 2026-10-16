"""Opt-in local callbacks: observe calls, never retain prompts or change requests.

The configure hook also reaches ResearchRuntime's independent worker threads.
One benchmark variant runs at a time; its internal research tasks remain parallel.
Every handler captures its collector at construction, so late callbacks cannot
be attributed to the next case. Snapshots close the collector.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
from pathlib import Path
from threading import Lock

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.tracers.context import register_configure_hook


_ACTIVE = None
_SESSION_LOCK = Lock()
_REGISTERED = False
_FLAG = "DEEPAGENT_LOCAL_EVALUATION_CALLBACKS"
_VAR = ContextVar("deepagent_evaluation_callback", default=None)
_PROMPTS = None


def _stage(messages):
    global _PROMPTS
    if _PROMPTS is None:
        root = Path(__file__).resolve().parents[1] / "app" / "prompts"
        names = {"supervisor": "supervisor", "decomposition": "supervisor", "researcher": "researcher",
                 "evidence_extraction": "extraction", "verifier": "verification", "relevance": "relevance",
                 "reresearch_planner": "reresearch_planner", "writer": "writer", "report_grounding": "grounding"}
        _PROMPTS = [(root.joinpath(name + ".md").read_text(encoding="utf-8")[:120], stage)
                    for name, stage in names.items()]
    # Only inspect in memory. Never persist input messages or full responses.
    system = "\n".join(str(message.content) for group in messages for message in group
                       if getattr(message, "type", None) == "system")
    for marker, stage in (("EVALUATION_BASELINE_PLANNER", "baseline_planner"),
                          ("EVALUATION_BASELINE_SUMMARY", "baseline_summary"), ("EVALUATION_JUDGE", "judge")):
        if marker in system:
            return stage
    return next((stage for prefix, stage in _PROMPTS if prefix in system), "unclassified")


def _tokens(response):
    for group in getattr(response, "generations", []):
        for generation in group:
            usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
            if isinstance(usage, dict) and "input_tokens" in usage and "output_tokens" in usage:
                return usage["input_tokens"], usage["output_tokens"]
    usage = (getattr(response, "llm_output", None) or {}).get("token_usage")
    if isinstance(usage, dict) and "prompt_tokens" in usage and "completion_tokens" in usage:
        return usage["prompt_tokens"], usage["completion_tokens"]
    return None


class TelemetryCollector:
    def __init__(self):
        self.lock = Lock()
        self.closed = False
        self.models = {}
        self.tools = {}

    def model_start(self, run_id, messages):
        with self.lock:
            if not self.closed:
                self.models.setdefault(str(run_id), {"stage": _stage(messages), "response": False, "tokens": None})

    def model_end(self, run_id, response):
        with self.lock:
            if not self.closed and str(run_id) in self.models:
                self.models[str(run_id)].update(response=True, tokens=_tokens(response))

    def tool_start(self, run_id, parent_run_id, serialized):
        name = (serialized or {}).get("name", "")
        if name not in {"web_search", "fetch_webpage"}:
            return
        with self.lock:
            if not self.closed:
                self.tools[str(run_id)] = {"name": name, "parent": str(parent_run_id), "result": None}

    def tool_end(self, run_id, output):
        raw = getattr(output, "content", output)
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
            valid = isinstance(payload, dict)
            error = str(payload.get("error", "")) if valid else "Invalid tool output"
            success = valid and not error and (bool(payload.get("text", "").strip())
                       if self.tools.get(str(run_id), {}).get("name") == "fetch_webpage" else True)
            result = {"success": success, "pdf_failure": "pdf" in error.lower(),
                      "budget_denied": "budget exhausted" in error.lower()}
        except (ValueError, TypeError, AttributeError):
            result = {"success": False, "pdf_failure": False, "budget_denied": False}
        with self.lock:
            if not self.closed and str(run_id) in self.tools:
                self.tools[str(run_id)]["result"] = result

    def snapshot(self):
        with self.lock:
            self.closed = True
            stages = {}
            for row in self.models.values():
                item = stages.setdefault(row["stage"], {"llm_calls": 0, "model_responses": 0,
                                         "usage_responses": 0, "observed_input_tokens": 0, "observed_output_tokens": 0})
                item["llm_calls"] += 1
                item["model_responses"] += int(row["response"])
                if row["tokens"] is not None:
                    item["usage_responses"] += 1
                    item["observed_input_tokens"] += row["tokens"][0]
                    item["observed_output_tokens"] += row["tokens"][1]
            # A budget wrapper invokes the original tool under the same name.
            # Count the leaf invocation once, excluding denied wrapper calls.
            parents = {row["parent"] for row in self.tools.values()}
            rows = [row for key, row in self.tools.items() if key not in parents
                    and not (row["result"] or {}).get("budget_denied")]
            searches = [row for row in rows if row["name"] == "web_search"]
            fetches = [row for row in rows if row["name"] == "fetch_webpage"]
            success = sum(bool((row["result"] or {}).get("success")) for row in fetches)
            return {"stages": stages, "tools": {"web_search_calls": len(searches), "fetch_webpage_calls": len(fetches),
                    "successful_fetches": success, "failed_fetches": len(fetches) - success,
                    "pdf_failure_count": sum(bool((row["result"] or {}).get("pdf_failure")) for row in fetches)}}


class EvaluationCallbacks(BaseCallbackHandler):
    run_inline = True

    def __init__(self):
        self.collector = _ACTIVE

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        if self.collector:
            self.collector.model_start(run_id, messages)

    def on_llm_end(self, response, *, run_id, **kwargs):
        if self.collector:
            self.collector.model_end(run_id, response)

    def on_tool_start(self, serialized, input_str, *, run_id, parent_run_id=None, **kwargs):
        if self.collector:
            self.collector.tool_start(run_id, parent_run_id, serialized)

    def on_tool_end(self, output, *, run_id, **kwargs):
        if self.collector:
            self.collector.tool_end(run_id, output)


@contextmanager
def observe_execution():
    global _ACTIVE, _REGISTERED
    if not _SESSION_LOCK.acquire(blocking=False):
        raise RuntimeError("Only one evaluation observation session may run per process.")
    old = os.environ.get(_FLAG)
    try:
        if not _REGISTERED:
            register_configure_hook(_VAR, inheritable=True, handle_class=EvaluationCallbacks, env_var=_FLAG)
            _REGISTERED = True
        collector = _ACTIVE = TelemetryCollector()
        os.environ[_FLAG] = "true"
        yield collector
    finally:
        if _ACTIVE:
            _ACTIVE.closed = True
        _ACTIVE = None
        if old is None:
            os.environ.pop(_FLAG, None)
        else:
            os.environ[_FLAG] = old
        _SESSION_LOCK.release()
