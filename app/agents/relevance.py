"""One run-wide relevance pass, bounded into small tool-free structured batches."""

import json
from collections import Counter
from dataclasses import dataclass, fields
from pathlib import Path

import httpx
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from openai import APITimeoutError, BadRequestError, LengthFinishReasonError

from app.config import Settings, get_settings
from app.llm import bounded_output_options, create_verifier_llm
from app.research.relevance_context import RelevanceContext, split_relevance_batches
from app.research.runtime import ResearchRuntime
from app.schemas.claim import Claim
from app.schemas.relevance import BatchClaimRelevance, ClaimRelevanceResult
from app.schemas.task import ResearchTask


class RelevanceOutputError(ValueError):
    """The relevance model did not return a complete valid decision set."""


class RelevanceTimeoutError(TimeoutError):
    """A relevance batch exhausted its single model-request timeout retry."""


@dataclass
class RelevanceUsage:
    llm_calls: int = 0
    batches: int = 0
    context_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_responses: int = 0

    def add(self, other: "RelevanceUsage") -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


class RelevanceAssessor:
    def __init__(self, model=None, *, settings: Settings | None = None) -> None:
        self.model = model
        self.settings = settings or get_settings()
        self.prompt = (Path(__file__).resolve().parents[1] / "prompts" / "relevance.md").read_text(encoding="utf-8")
        self.last_usage = RelevanceUsage()
        self._native_available = True

    def assess_batch(self, goal: str, tasks: list[ResearchTask], claims: list[Claim], *, on_batch_complete=None) -> list[ClaimRelevanceResult]:
        self.last_usage = RelevanceUsage()
        contexts = split_relevance_batches(
            goal, tasks, claims, max_claims=self.settings.max_relevance_claims_per_batch,
            max_context_chars=self.settings.max_relevance_context_chars,
        )
        if not contexts:
            return []
        runtime = ResearchRuntime("Gap Detection", self.settings.relevance_timeout_seconds,
                                  self.settings.research_progress_interval_seconds)

        def work():
            results = []
            for index, context in enumerate(contexts, start=1):
                start = len(results)
                with runtime.operation(f"claim relevance batch {index}/{len(contexts)}"):
                    decisions = self._assess(context, runtime, index, len(contexts))
                    for claim in context.claims:
                        decision = decisions[claim.claim_id]
                        results.append(ClaimRelevanceResult(claim_id=claim.claim_id, task_id=claim.task_id,
                                                           status=decision.status, reason=decision.reason))
                if on_batch_complete:
                    on_batch_complete(results[start:], RelevanceUsage(**vars(self.last_usage)))
            return results

        results = runtime.run(work)
        counts = Counter(item.status.value for item in results)
        print(f"[Gap Detection] result: aligned={counts['ALIGNED']} partial={counts['PARTIAL']} "
              f"misaligned={counts['MISALIGNED']}", flush=True)
        return results

    def _assess(self, context: RelevanceContext, runtime: ResearchRuntime,
                batch_index: int, batch_count: int) -> dict:
        if self.model is None:
            # Reuse the cheap, thinking-off verifier factory, not its business logic.
            self.model = create_verifier_llm()
        schema = BatchClaimRelevance.model_json_schema()
        schema["properties"]["results"].update(minItems=len(context.claims), maxItems=len(context.claims))
        schema["$defs"]["ClaimRelevanceDecision"]["properties"]["claim_id"]["enum"] = list(context.claim_ids)
        messages = [SystemMessage(content=self.prompt), HumanMessage(content=context.text)]
        usage = self.last_usage
        usage.batches += 1
        calls_before = usage.llm_calls
        # One timeout retry per batch, shared by native and JSON fallback paths.
        timeout_retries = 0
        batch_label = f"relevance batch {batch_index}/{batch_count}"

        def invoke(model, request):
            nonlocal timeout_retries

            while True:
                captured = []

                class CaptureResponse(BaseCallbackHandler):
                    def on_llm_end(self, response, **kwargs):
                        for generations in response.generations:
                            if generations:
                                captured.append(getattr(generations[0], "message", None))

                remaining = runtime.remaining()
                options = bounded_output_options(self.model, self.settings.relevance_max_tokens)
                # Leave time for the one retry; never extend the run-wide deadline.
                options["timeout"] = min(self.settings.model_request_timeout_seconds,
                                         remaining / (2 - timeout_retries))
                if runtime.call_budget is not None:
                    runtime.call_budget.reserve("relevance")
                usage.llm_calls += 1
                usage.context_chars += context.context_chars
                runtime.log(f"{batch_label}: claims={len(context.claims)} tasks={len(context.payload['tasks'])} "
                            f"context_chars={context.context_chars} retry={timeout_retries}/1 "
                            f"request_timeout={options['timeout']:.2f}s remaining={remaining:.2f}s")
                try:
                    response = model.invoke(request, config={"callbacks": [CaptureResponse()]}, **options)
                    break
                except (APITimeoutError, httpx.TimeoutException) as error:
                    if timeout_retries == 1:
                        raise RelevanceTimeoutError(
                            f"{batch_label} request timed out after retry=1/1; "
                            "no relevance results/gaps published."
                        ) from error
                    remaining = runtime.remaining()
                    timeout_retries += 1
                    runtime.log(f"{batch_label}: request timed out; retrying current batch "
                                f"retry=1/1 remaining={remaining:.2f}s")
                except LengthFinishReasonError as error:
                    raise RelevanceOutputError(
                        "Relevance output was truncated; no relevance results/gaps published. "
                        "Increase RELEVANCE_MAX_TOKENS or reduce MAX_RELEVANCE_CLAIMS_PER_BATCH."
                    ) from error
            runtime.remaining()
            raw = captured[0] if captured else response
            metadata = getattr(raw, "usage_metadata", None)
            if metadata:
                usage.input_tokens += metadata.get("input_tokens", 0)
                usage.output_tokens += metadata.get("output_tokens", 0)
                usage.usage_responses += 1
            if getattr(raw, "response_metadata", {}).get("finish_reason") == "length":
                raise RelevanceOutputError("Relevance output was truncated; no relevance results/gaps published.")
            return response

        response = None
        if self._native_available:
            try:
                structured = self.model.with_structured_output(schema, method="json_schema")
                response = invoke(structured, messages)
                if response is None:
                    raise RelevanceOutputError("Relevance returned no structured decisions.")
            except (NotImplementedError, TypeError, BadRequestError) as error:
                if isinstance(error, BadRequestError) and not any(
                    term in str(error).lower() for term in ("json_schema", "response_format")
                ):
                    raise
                runtime.remaining()
                self._native_available = False
                runtime.log("native relevance JSON Schema unavailable; using schema-validated JSON")
        if response is None:
            request = [SystemMessage(content=self.prompt + "\nReturn only JSON matching:\n"
                                      + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))), messages[1]]
            raw = invoke(self.model, request)
            text = raw.text.strip()
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            parsed = BatchClaimRelevance.model_validate_json(text)
        else:
            parsed = BatchClaimRelevance.model_validate(response)
        labels = [item.claim_id for item in parsed.results]
        if len(labels) != len(set(labels)) or set(labels) != set(context.claim_ids):
            raise RelevanceOutputError("Relevance must return exactly one decision for every requested claim_id.")
        runtime.log(f"{batch_label} completed: claims={len(context.claims)} tasks={len(context.payload['tasks'])} "
                    f"context_chars={context.context_chars} calls={usage.llm_calls - calls_before}")
        return {context.claim_ids[item.claim_id]: item for item in parsed.results}
