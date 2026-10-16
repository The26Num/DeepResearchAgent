"""Batch evidence-support classification; no Agent, tools, Memo or Store scanning."""

from collections import Counter
from dataclasses import dataclass, fields
import json
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from openai import BadRequestError, LengthFinishReasonError
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.llm import bounded_output_options, create_verifier_llm
from app.structured_output import json_instance_instructions, json_object_options, parse_json_instance, prefers_json_object
from app.research.evidence_store import EvidenceStore
from app.research.runtime import ResearchRuntime
from app.research.verification_context import VerificationContext, split_verification_batches
from app.schemas.claim import Claim
from app.schemas.verification import (MAX_VERIFICATION_REASON_CHARS, BatchVerificationResult,
                                      VerificationResult, VerificationStatus)


class VerificationOutputError(ValueError):
    """A verifier response is incomplete or does not match the requested claims."""


@dataclass
class VerificationUsage:
    llm_calls: int = 0
    batches: int = 0
    context_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_responses: int = 0

    def add(self, other: "VerificationUsage") -> None:
        for item in fields(self):
            setattr(self, item.name, getattr(self, item.name) + getattr(other, item.name))


class ClaimVerifier:
    def __init__(self, model=None, *, settings: Settings | None = None) -> None:
        self.model = model  # Lazy: empty/precheck-only batches need no model credentials.
        self.settings = settings or get_settings()
        self.prompt = (Path(__file__).resolve().parents[1] / "prompts" / "verifier.md").read_text(encoding="utf-8")
        self.last_usage = VerificationUsage()
        self._native_available = True

    def verify_claim(self, claim: Claim, evidence_store: EvidenceStore) -> VerificationResult:
        return self.verify_batch([claim], evidence_store)[0]

    def verify_batch(self, claims: list[Claim], evidence_store: EvidenceStore) -> list[VerificationResult]:
        self.last_usage = VerificationUsage()
        unique: dict[str, Claim] = {}
        for claim in claims:
            if claim.claim_id in unique and claim != unique[claim.claim_id]:
                raise ValueError(f"Conflicting claim_id in verification batch: {claim.claim_id}.")
            unique[claim.claim_id] = claim
        claims = list(unique.values())
        if not claims:
            return []
        if len({item.task_id for item in claims}) != 1:
            raise ValueError("verify_batch accepts Claims from one task at a time.")
        task_id = claims[0].task_id
        decisions = {
            item.claim_id: self._result(item, VerificationStatus.UNSUPPORTED,
                                       "No supporting evidence is linked to this claim.")
            for item in claims if not item.evidence_ids
        }
        # Validate every chain and prepare every split before spending any API calls.
        contexts = split_verification_batches(
            [item for item in claims if item.evidence_ids], evidence_store,
            max_claims=self.settings.max_verify_claims_per_batch,
            max_context_chars=self.settings.max_verification_context_chars,
        )
        runtime = ResearchRuntime(task_id, self.settings.verification_timeout_seconds,
                                  self.settings.research_progress_interval_seconds)

        def work():
            for index, context in enumerate(contexts, 1):
                with runtime.operation("claim verification"):
                    result = self._classify(context, runtime, batch_index=index, batch_count=len(contexts))
                    for claim in context.claims:
                        decision = result[claim.claim_id]
                        decisions[claim.claim_id] = self._result(claim, decision.status, decision.reason)
            return [decisions[item.claim_id] for item in claims]

        result = runtime.run(work) if contexts else [decisions[item.claim_id] for item in claims]
        counts = Counter(item.status.value for item in result)
        print(f"[{task_id}] verification: claims={len(result)} supported={counts['SUPPORTED']} "
              f"partial={counts['PARTIAL']} unsupported={counts['UNSUPPORTED']} "
              f"calls={self.last_usage.llm_calls}", flush=True)
        return result

    @staticmethod
    def _result(claim: Claim, status: VerificationStatus, reason: str) -> VerificationResult:
        return VerificationResult(claim_id=claim.claim_id, task_id=claim.task_id, status=status,
                                  reason=reason, evidence_ids=list(claim.evidence_ids))

    def _classify(self, context: VerificationContext, runtime: ResearchRuntime, *,
                  batch_index: int = 1, batch_count: int = 1) -> dict:
        if self.model is None:
            self.model = create_verifier_llm()
        schema = BatchVerificationResult.model_json_schema()
        schema["properties"]["results"]["maxItems"] = len(context.claims)
        schema["properties"]["results"]["minItems"] = len(context.claims)
        schema["$defs"]["VerificationDecision"]["properties"]["claim_id"]["enum"] = list(context.claim_ids)
        request = [SystemMessage(content=self.prompt), HumanMessage(content=context.text)]
        usage = self.last_usage
        usage.batches += 1
        calls_before = usage.llm_calls
        format_retry = 0
        retry_limit = int(prefers_json_object(self.model))

        def invoke(model, messages):
            raw_messages = []

            class CaptureResponse(BaseCallbackHandler):
                def on_llm_end(self, response, **kwargs):
                    for generations in response.generations:
                        if generations:
                            raw_messages.append(getattr(generations[0], "message", None))

            options = bounded_output_options(self.model, self.settings.verification_max_tokens)
            options.update(json_object_options(self.model))
            options["timeout"] = min(self.settings.model_request_timeout_seconds, runtime.remaining())
            if runtime.call_budget is not None:
                runtime.call_budget.reserve("verification")
            runtime.log(f"verification batch {batch_index}/{batch_count}: claims={len(context.claims)} "
                        f"context_chars={context.context_chars} format_retry={format_retry}/{retry_limit} "
                        f"request_timeout={options['timeout']:.2f}s")
            usage.llm_calls += 1
            usage.context_chars += context.context_chars
            try:
                response = model.invoke(messages, config={"callbacks": [CaptureResponse()]}, **options)
            except LengthFinishReasonError as error:
                raise VerificationOutputError(
                    "Verification output was truncated; no decisions saved. Increase VERIFICATION_MAX_TOKENS "
                    "or reduce MAX_VERIFY_CLAIMS_PER_BATCH."
                ) from error
            runtime.remaining()
            raw = raw_messages[0] if raw_messages else response
            metadata = getattr(raw, "usage_metadata", None)
            if metadata:
                usage.input_tokens += metadata.get("input_tokens", 0)
                usage.output_tokens += metadata.get("output_tokens", 0)
                usage.usage_responses += 1
            if getattr(raw, "response_metadata", {}).get("finish_reason") == "length":
                raise VerificationOutputError("Verification output was truncated; no decisions saved.")
            return response

        response = None
        if self._native_available and not prefers_json_object(self.model):
            try:
                # include_raw=True adds a RunnableMap that does not forward invoke
                # kwargs to its model branch in the installed LangChain version.
                # Capture usage through callbacks so timeout/output bounds reach HTTP.
                structured = self.model.with_structured_output(schema, method="json_schema")
                response = invoke(structured, request)
                if response is None:
                    raise VerificationOutputError("Verification returned no structured decisions; no decisions saved.")
            except (NotImplementedError, TypeError, BadRequestError) as error:
                if isinstance(error, BadRequestError) and not any(
                    term in str(error).lower() for term in ("json_schema", "response_format")
                ):
                    raise
                runtime.remaining()
                self._native_available = False
                runtime.log("native verification JSON Schema unavailable; using schema-validated JSON")
        if response is None:
            if prefers_json_object(self.model):
                runtime.log("verification output mode=json_object; thinking disabled")
            contract = (f"\nresults must contain exactly {len(context.claims)} JSON objects, one for each label "
                        f"in {json.dumps(list(context.claim_ids))}. Never place strings or field names in the array. "
                        f"Each reason must be at most {MAX_VERIFICATION_REASON_CHARS} CHARACTERS, not words; "
                        f"aim for {MAX_VERIFICATION_REASON_CHARS // 2} characters. "
                        "State the key support or limitation without copying IDs or quotes.\n")
            fallback = [SystemMessage(content=self.prompt + json_instance_instructions(schema) + contract), request[1]]
            for format_retry in range(retry_limit + 1):
                raw = invoke(self.model, fallback)
                try:
                    parsed = BatchVerificationResult.model_validate(parse_json_instance(raw.text))
                    break
                except ValidationError as error:
                    if not retry_limit:
                        raise
                    issues = "; ".join(".".join(map(str, item["loc"])) + ":" + item["type"]
                                       for item in error.errors(include_input=False, include_url=False)[:6])[:400]
                    if format_retry == retry_limit:
                        raise VerificationOutputError(
                            f"Verification batch {batch_index}/{batch_count} failed schema validation after "
                            f"one format retry ({issues}); no decisions saved."
                        ) from error
                    # Reclassify the same complete evidence input. Never drop bad
                    # rows or truncate reasons into apparently valid decisions.
                    runtime.log(f"verification batch {batch_index}/{batch_count}: schema validation failed "
                                f"({issues}); retrying current batch once")
                    fallback = [SystemMessage(content=fallback[0].content + "\nFORMAT CORRECTION: " + issues
                                              + ". Return a complete fresh result for the same input; "
                                                "all IDs and support criteria remain unchanged.\n"), request[1]]
        else:
            parsed = BatchVerificationResult.model_validate(response)
        ids = [item.claim_id for item in parsed.results]
        if len(ids) != len(set(ids)) or set(ids) != set(context.claim_ids):
            raise VerificationOutputError("Verification must return exactly one decision for every requested claim_id.")
        print(f"[{runtime.task_id}] verification batch: claims={len(context.claims)} "
              f"unique_evidence={len(context.payload['evidence'])} context_chars={context.context_chars} "
              f"calls={usage.llm_calls - calls_before}", flush=True)
        return {context.claim_ids[item.claim_id]: item for item in parsed.results}
