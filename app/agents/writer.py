"""One tool-free writing pass over program-selected, traceable research facts."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from openai import BadRequestError, LengthFinishReasonError

from app.config import Settings, get_settings
from app.llm import bounded_output_options, create_writer_llm
from app.research.runtime import ResearchRuntime
from app.research.writing_context import WritingContext
from app.schemas.report import ResearchReport, ReportLimitation
from app.agents.report_grounding import ReportGroundingChecker, ReportGroundingUsage


class WriterOutputError(ValueError):
    pass


@dataclass
class WriterUsage:
    accepted_claims: int = 0
    used_claims: int = 0
    unused_claims: int = 0
    coverage: float = 0.0
    llm_calls: int = 0
    context_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_responses: int = 0
    grounding: ReportGroundingUsage = field(default_factory=ReportGroundingUsage)


def validate_report(report: ResearchReport, context: WritingContext) -> None:
    payload = context.payload
    allowed = {item.claim_id for item in context.accepted_claims}
    gaps = {item["gap_id"] for item in payload["open_gaps"]}
    partial = {item["claim_id"] for item in payload["partial_claims_for_limitations_only"]}
    if not report.key_findings or not report.executive_summary or not report.sections or not report.conclusion:
        raise WriterOutputError("Writer must populate summary, findings, analysis and conclusion from accepted Claims.")
    for item in report.factual_paragraphs():
        if not set(item.claim_ids) <= allowed:
            raise WriterOutputError("Writer referenced an unknown or non-accepted claim_id; report not published.")
    for item in report.limitations:
        if (not set(item.gap_ids) <= gaps or not set(item.partial_claim_ids) <= partial
                or not (item.gap_ids or item.partial_claim_ids)):
            raise WriterOutputError("Writer limitation must reference supplied OPEN/IMPROVED gaps or partial Claims.")
    texts = [report.title, report.scope, *(item.text for item in report.factual_paragraphs()),
             *(item.heading for item in report.sections), *(item.text for item in report.limitations)]
    prohibited = re.compile(
        r"(?:claim|evidence|ev|source|src|gap)_[A-Za-z0-9_]+|RR_[A-Za-z0-9_]+"
        r"|\[\s*(?:\^?\d+)(?:\s*[,，–-]\s*\d+)*\s*\]"
        r"|[（(][^()（）\n]{1,80}[,，]\s*(?:19|20)\d{2}[a-z]?[）)]"
        r"|https?://", re.IGNORECASE,
    )
    for text in texts:
        if prohibited.search(text) or re.search(
            r"(?im)^\s*(?:#{1,6}\s*)?(?:references|bibliography|footnotes|参考文献|参考资料|脚注)\s*[:：]?\s*$", text
        ):
            raise WriterOutputError("Writer prose contains internal IDs, citations or bibliography formatting reserved for Phase 5B.")


class ResearchWriter:
    def __init__(self, model=None, *, settings: Settings | None = None, grounding_checker=None):
        self.model = model
        self.settings = settings or get_settings()
        self.grounding_checker = grounding_checker or ReportGroundingChecker(settings=self.settings)
        self.last_grounding = ()
        self.prompt = (Path(__file__).resolve().parents[1] / "prompts" / "writer.md").read_text(encoding="utf-8")
        self.last_usage = WriterUsage()
        self._native_available = True

    def write(self, context: WritingContext) -> ResearchReport:
        usage = self.last_usage = WriterUsage(accepted_claims=len(context.accepted_claims), context_chars=context.context_chars)
        self.last_grounding = ()
        payload = context.payload
        print(f"[Writer] context: accepted_claims={usage.accepted_claims} evidence={len(payload['evidence'])} "
              f"sources={len(payload['sources'])} open_gaps={len(payload['open_gaps'])} "
              f"context_chars={usage.context_chars}", flush=True)
        if not context.accepted_claims:
            report = self._minimal_report(context)
        else:
            if context.context_chars > self.settings.max_writer_context_chars:
                raise WriterOutputError("Writer input exceeds MAX_WRITER_CONTEXT_CHARS; no model request sent.")
            runtime = ResearchRuntime("Writer", self.settings.writer_timeout_seconds,
                                      self.settings.research_progress_interval_seconds)
            report = runtime.run(lambda: self._write(context, runtime, usage))
            report = self.grounding_checker.check(report, context)
            usage.grounding = self.grounding_checker.last_usage
            self.last_grounding = self.grounding_checker.last_audit
            validate_report(report, context)
        usage.used_claims = len(report.used_claim_ids)
        usage.unused_claims = usage.accepted_claims - usage.used_claims
        usage.coverage = usage.used_claims / usage.accepted_claims if usage.accepted_claims else 0.0
        print(f"[Writer] completed: used_claims={usage.used_claims} unused_claims={usage.unused_claims} "
              f"coverage={usage.coverage:.2f} calls={usage.llm_calls}", flush=True)
        print(f"Writer Summary: accepted_claims={usage.accepted_claims} used_claims={usage.used_claims} "
              f"unused_claims={usage.unused_claims} coverage={usage.coverage:.2f} "
              f"context_chars={usage.context_chars} calls={usage.llm_calls}", flush=True)
        if usage.usage_responses:
            print(f"Writer token usage: input_tokens={usage.input_tokens} output_tokens={usage.output_tokens} "
                  f"usage_responses={usage.usage_responses}/{usage.llm_calls}", flush=True)
        if usage.accepted_claims and usage.coverage < self.settings.min_writer_claim_coverage:
            print(f"[Writer] warning: Claim coverage {usage.coverage:.2f} is below "
                  f"MIN_WRITER_CLAIM_COVERAGE={self.settings.min_writer_claim_coverage:g}; no automatic rewrite.", flush=True)
        return report

    @staticmethod
    def _minimal_report(context):
        payload = context.payload
        limitations = [ReportLimitation(text=("当前收集材料仍未充分回答：" if item["status"] == "OPEN"
                        else "已有补充材料，但证据仍有限：") + item["missing_information"], gap_ids=[item["gap_id"]])
                       for item in payload["open_gaps"]]
        limitations += [ReportLimitation(text="当前证据或目标覆盖不足，以下内容不能作为确定性结论：" + item["text"],
                                         partial_claim_ids=[item["claim_id"]])
                        for item in payload["partial_claims_for_limitations_only"]]
        if len(limitations) > 20:
            # Consolidate without losing gap/partial reference IDs.
            limitations = []
            for offset in range(0, len(payload["open_gaps"]), 30):
                batch = payload["open_gaps"][offset:offset + 30]
                limitations.append(ReportLimitation(text="当前材料仍有未解决或仅部分改善的研究缺口，包括："
                    + batch[0]["missing_information"], gap_ids=[item["gap_id"] for item in batch]))
            for offset in range(0, len(payload["partial_claims_for_limitations_only"]), 20):
                batch = payload["partial_claims_for_limitations_only"][offset:offset + 20]
                limitations.append(ReportLimitation(text="部分结论的证据或目标覆盖仍不充分，不能作为确定事实，包括："
                    + batch[0]["text"], partial_claim_ids=[item["claim_id"] for item in batch]))
        return ResearchReport(title=("研究报告：" + context.goal)[:180], scope=context.goal[:1200],
            executive_summary=[], key_findings=[], sections=[], limitations=limitations, conclusion=[])

    def _write(self, context, runtime, usage):
        if self.model is None:
            self.model = create_writer_llm()
        schema = ResearchReport.model_json_schema()
        allowed = [item.claim_id for item in context.accepted_claims]
        payload = context.payload
        for name in ("ReportParagraph", "Finding"):
            schema["$defs"][name]["properties"]["claim_ids"]["items"]["enum"] = allowed
        for field in ("executive_summary", "key_findings", "sections", "conclusion"):
            schema["properties"][field]["minItems"] = 1
        for field, refs in (("gap_ids", [item["gap_id"] for item in payload["open_gaps"]]),
                            ("partial_claim_ids", [item["claim_id"] for item in payload["partial_claims_for_limitations_only"]])):
            prop = schema["$defs"]["ReportLimitation"]["properties"][field]
            if refs:
                prop["items"]["enum"] = refs
            else:
                prop["maxItems"] = 0
        messages = [SystemMessage(content=self.prompt), HumanMessage(content=context.text)]

        def invoke(model, request):
            captured = []

            class CaptureResponse(BaseCallbackHandler):
                def on_llm_end(self, response, **kwargs):
                    for generations in response.generations:
                        if generations:
                            captured.append(getattr(generations[0], "message", None))

            options = bounded_output_options(self.model, self.settings.max_report_output_tokens)
            options["timeout"] = min(self.settings.model_request_timeout_seconds, runtime.remaining())
            usage.llm_calls += 1
            try:
                with runtime.operation("report writing"):
                    response = model.invoke(request, config={"callbacks": [CaptureResponse()]}, **options)
            except LengthFinishReasonError as error:
                raise WriterOutputError("Writer output was truncated; no incomplete report published. "
                                        "Increase MAX_REPORT_OUTPUT_TOKENS or request a shorter report.") from error
            finally:
                for raw in captured:
                    metadata = getattr(raw, "usage_metadata", None)
                    if metadata:
                        usage.input_tokens += metadata.get("input_tokens", 0)
                        usage.output_tokens += metadata.get("output_tokens", 0)
                        usage.usage_responses += 1
            raw = captured[0] if captured else response
            if getattr(raw, "response_metadata", {}).get("finish_reason") == "length":
                raise WriterOutputError("Writer output was truncated; no incomplete report published.")
            return response

        response = None
        if self._native_available:
            try:
                structured = self.model.with_structured_output(schema, method="json_schema")
                response = invoke(structured, messages)
                if response is None:
                    raise WriterOutputError("Writer returned no structured report.")
            except (NotImplementedError, TypeError, BadRequestError) as error:
                if isinstance(error, BadRequestError) and not any(
                    term in str(error).lower() for term in ("json_schema", "response_format")
                ):
                    raise
                runtime.remaining()
                self._native_available = False
        if response is None:
            fallback = [SystemMessage(content=self.prompt + "\nReturn only JSON matching:\n"
                                      + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))), messages[1]]
            raw = invoke(self.model, fallback)
            text = raw.text.strip()
            if text.startswith("```") and text.endswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            report = ResearchReport.model_validate_json(text)
        else:
            report = ResearchReport.model_validate(response)
        validate_report(report, context)
        if not report.limitations and (payload["open_gaps"] or payload["partial_claims_for_limitations_only"]):
            # Small compatible models can omit limitations despite valid facts.
            # Keep current gaps visible without inventing facts or another call.
            limitations = []
            descriptions = {
                "SUPPORT_GAP": "部分结论仍缺少充分的直接证据支持。",
                "RELEVANCE_GAP": "部分已收集材料与原始研究目标的对应仍不充分。",
                "COVERAGE_GAP": "部分研究维度尚未获得证据充分且与目标对齐的结论。",
            }
            for kind, description in descriptions.items():
                for status in ("OPEN", "IMPROVED"):
                    selected = [item["gap_id"] for item in payload["open_gaps"]
                                if item["gap_type"] == kind and item["status"] == status]
                    for offset in range(0, len(selected), 30):
                        prefix = "当前收集的材料仍有不足：" if status == "OPEN" else "已有补充材料，但证据仍有限："
                        limitations.append(ReportLimitation(text=prefix + description, gap_ids=selected[offset:offset + 30]))
            partial = [item["claim_id"] for item in payload["partial_claims_for_limitations_only"]]
            for offset in range(0, len(partial), 20):
                limitations.append(ReportLimitation(text="部分相关结论的证据支持或目标覆盖仍不充分，不能作为确定性发现。",
                                                    partial_claim_ids=partial[offset:offset + 20]))
            report = ResearchReport.model_validate({**report.model_dump(), "limitations": limitations})
            validate_report(report, context)
            runtime.log("model omitted limitations; retained current gap/partial summaries without another model call")
        return report
