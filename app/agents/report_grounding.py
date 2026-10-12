"""One bounded, tool-free semantic gate; uncertain prose fails back to its Claims."""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import HumanMessage, SystemMessage
from openai import BadRequestError

from app.config import Settings, get_settings
from app.llm import bounded_output_options, create_verifier_llm
from app.research.runtime import ResearchRuntime
from app.research.writing_context import WritingContext
from app.schemas.report import ResearchReport
from app.schemas.report_grounding import ReportGroundingBatch


@dataclass
class ReportGroundingUsage:
    llm_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    usage_responses: int = 0
    context_chars: int = 0
    checked_items: int = 0
    repaired_items: int = 0
    fallback_reason: str = ""


@dataclass(frozen=True)
class ReportGroundingAudit:
    path: str
    status: str
    reason: str
    repaired: bool
    original_text: str
    final_text: str
    claim_ids: tuple[str, ...]


def _new_qualifier(text: str, supports: list[str]) -> str | None:
    """Conservative backstop for new names/settings/numbers, even if a judge errs."""
    source = "\n".join(supports)
    lower = source.lower()
    ignored = {"a", "an", "the", "this", "these", "those", "it", "its", "we", "our", "current",
               "research", "evidence", "scope", "goal", "original", "grounded", "direct", "specific",
               "findings", "finding", "summary", "conclusion", "limitations", "available", "within"}
    for name in re.findall(r"(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)*(?![A-Za-z0-9_])", text):
        if name.lower() not in ignored and not re.search(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])", source, re.I):
            return "新增的名称或缩写没有出现在所引用的 Claim 中。"
    if any(number not in re.findall(r"\d+(?:\.\d+)?%?", source)
           for number in re.findall(r"\d+(?:\.\d+)?%?", text)):
        return "新增的数字没有出现在所引用的 Claim 中。"
    for concept in (r"(?<![A-Za-z])off[- ]?line(?![A-Za-z])|离线", r"(?<![A-Za-z])on[- ]?line(?![A-Za-z])|在线",
                    r"(?<![A-Za-z])continuous[- ]control(?![A-Za-z])|连续控制", r"(?<![A-Za-z])discrete[- ]control(?![A-Za-z])|离散控制"):
        if re.search(concept, text, re.I) and not re.search(concept, lower, re.I):
            return "新增的学习设定或控制任务范围没有出现在所引用的 Claim 中。"
    return None


class ReportGroundingChecker:
    def __init__(self, model=None, *, settings: Settings | None = None):
        self.model = model
        self.settings = settings or get_settings()
        self.prompt = (Path(__file__).resolve().parents[1] / "prompts" / "report_grounding.md").read_text(encoding="utf-8")
        self.last_usage = ReportGroundingUsage()
        self.last_audit: tuple[ReportGroundingAudit, ...] = ()
        self._native_available = True

    def check(self, report: ResearchReport, context: WritingContext) -> ResearchReport:
        usage = self.last_usage = ReportGroundingUsage()
        self.last_audit = ()
        data = report.model_dump()
        claims = {item.claim_id: item.text for item in context.accepted_claims}
        payload = context.payload
        gaps = {item["gap_id"]: item for item in payload["open_gaps"]}
        partial = {item["claim_id"]: item for item in payload["partial_claims_for_limitations_only"]}
        # The goal is explicitly metadata, never a source for empirical assertions.
        data["title"] = ("研究报告：" + context.goal)[:180]
        data["scope"] = ("本报告的研究目标为：" + context.goal[:1000]
                         + "。这是研究范围，不表示所有范围均已有实验依据；下文结论仅限于已接受的 Claim。")
        occurrences = []
        for field in ("executive_summary", "key_findings", "conclusion"):
            for i, item in enumerate(data[field]):
                occurrences.append((f"{field}.{i}", item, "text", "paragraph", item["claim_ids"]))
        for i, section in enumerate(data["sections"]):
            refs = list(dict.fromkeys(cid for p in section["paragraphs"] for cid in p["claim_ids"]))
            occurrences.append((f"sections.{i}.heading", section, "heading", "heading", refs))
            for j, item in enumerate(section["paragraphs"]):
                occurrences.append((f"sections.{i}.paragraphs.{j}", item, "text", "paragraph", item["claim_ids"]))
        for i, item in enumerate(data["limitations"]):
            occurrences.append((f"limitations.{i}", item, "text", "limitation", []))

        units, lookup, locations = [], {}, []
        for path, item, key, kind, refs in occurrences:
            supports = ([claims[cid] for cid in refs] if kind != "limitation" else
                        [json.dumps(gaps[gid], ensure_ascii=False) for gid in item["gap_ids"]]
                        + [json.dumps(partial[cid], ensure_ascii=False) for cid in item["partial_claim_ids"]])
            signature = (kind, item[key], tuple(supports))
            if signature not in lookup:
                lookup[signature] = len(units)
                units.append({"kind": kind, "text": item[key], "supports": supports})
            locations.append((path, item, key, kind, refs, lookup[signature]))
        usage.checked_items = len(units)
        decisions = {}
        pending = []
        for index, unit in enumerate(units):
            if unit["kind"] == "heading":
                # A weak semantic judge can approve a heading with swapped method
                # attributes. Publish only neutral labels or literal Claim text.
                literal = unit["text"].rstrip("。.!！?？")
                safe = unit["text"].lower() in {"研究发现", "结论分析", "证据概述", "分析", "research findings", "evidence and scope"}
                safe = safe or any(literal == text.rstrip("。.!！?？") for text in unit["supports"])
                decisions[index] = (("SUPPORTED", "中性组织标题或所引 Claim 原文。") if safe else
                                    ("UNSUPPORTED", "非原文标题保守回退，避免标题引入未经支持的方法属性或范围。"))
            elif unit["kind"] == "paragraph" and unit["text"] in unit["supports"]:
                decisions[index] = ("SUPPORTED", "逐字保留了所引用的 Claim。")
            elif unit["kind"] != "limitation" and (reason := _new_qualifier(unit["text"], unit["supports"])):
                decisions[index] = ("UNSUPPORTED", reason)
            else:
                pending.append(index)
        if pending:
            support_texts = list(dict.fromkeys(s for index in pending for s in units[index]["supports"]))
            support_indexes = {text: i for i, text in enumerate(support_texts)}
            request = json.dumps({"required_item_count": len(pending), "supports": support_texts, "items": [
                {"item_index": request_index, "kind": units[index]["kind"], "text": units[index]["text"],
                 "support_indexes": [support_indexes[s] for s in units[index]["supports"]]}
            for request_index, index in enumerate(pending)]}, ensure_ascii=False, separators=(",", ":"))
            usage.context_chars = len(request)
            try:
                if len(request) > self.settings.max_report_grounding_context_chars:
                    raise ValueError("grounding context exceeds configured limit")
                runtime = ResearchRuntime("Report grounding", self.settings.report_grounding_timeout_seconds,
                                          self.settings.research_progress_interval_seconds)
                result = runtime.run(lambda: self._judge(request, runtime, usage))
                indexes = [item.item_index for item in result.results]
                if len(set(indexes)) != len(indexes) or set(indexes) != set(range(len(pending))):
                    raise ValueError(f"grounding response contains missing, duplicate or unknown item indexes: "
                                     f"received={indexes}, expected=0..{len(pending) - 1}")
                decisions.update({pending[item.item_index]: (item.status, item.reason) for item in result.results})
            except Exception as error:
                usage.fallback_reason = type(error).__name__ + ": " + str(error)[:180]
                for index in pending:
                    decisions[index] = ("UNCERTAIN", "核验未完成，保守回退；" + type(error).__name__)
                print(f"[Report grounding] conservative fallback: {usage.fallback_reason}", flush=True)

        audit = []
        for path, item, key, kind, refs, index in locations:
            status, reason = decisions[index]
            original = item[key]
            repaired = status != "SUPPORTED"
            if repaired:
                if kind == "heading":
                    item[key] = "研究发现"
                elif kind == "limitation":
                    item[key] = "当前收集材料仍存在未解决或仅部分改善的缺口。" if item["gap_ids"] else "部分结论的证据或目标覆盖不足，不能作为确定性发现。"
                    if item["gap_ids"] and item["partial_claim_ids"]:
                        item[key] += "部分结论尚不能作为确定性发现。"
                else:
                    selected, texts = [], []
                    for cid in refs:
                        candidate = texts + [claims[cid]]
                        if len("\n\n".join(candidate)) <= 1200:
                            selected.append(cid)
                            texts = candidate
                    if not selected:
                        raise ValueError("No complete cited Claim fits the report paragraph; report not published.")
                    item[key], item["claim_ids"] = "\n\n".join(texts), selected
                usage.repaired_items += 1
            audit.append(ReportGroundingAudit(path, status, reason, repaired, original, item[key],
                                             tuple(item.get("claim_ids", refs))))
        self.last_audit = tuple(audit)
        result = ResearchReport.model_validate(data)
        print(f"Report Grounding Summary: checked_items={usage.checked_items} repaired_items={usage.repaired_items} "
              f"context_chars={usage.context_chars} calls={usage.llm_calls} "
              f"input_tokens={usage.input_tokens} output_tokens={usage.output_tokens}", flush=True)
        return result

    def _judge(self, text, runtime, usage):
        if self.model is None:
            self.model = create_verifier_llm()
        schema = ReportGroundingBatch.model_json_schema()
        schema["$defs"]["ReportGroundingDecision"]["properties"]["item_index"]["enum"] = [
            item["item_index"] for item in json.loads(text)["items"]]
        count = len(json.loads(text)["items"])
        schema["properties"]["results"].update(minItems=count, maxItems=count)
        messages = [SystemMessage(content=self.prompt), HumanMessage(content=text)]

        def invoke(model, request):
            captured = []

            class CaptureResponse(BaseCallbackHandler):
                def on_llm_end(self, response, **kwargs):
                    for generations in response.generations:
                        if generations:
                            captured.append(getattr(generations[0], "message", None))

            options = bounded_output_options(self.model, self.settings.report_grounding_max_tokens)
            options["timeout"] = min(self.settings.model_request_timeout_seconds, runtime.remaining())
            usage.llm_calls += 1
            try:
                with runtime.operation("paragraph and heading grounding"):
                    response = model.invoke(request, config={"callbacks": [CaptureResponse()]}, **options)
            finally:
                for raw in captured:
                    metadata = getattr(raw, "usage_metadata", None)
                    if metadata:
                        usage.input_tokens += metadata.get("input_tokens", 0)
                        usage.output_tokens += metadata.get("output_tokens", 0)
                        usage.usage_responses += 1
            if any(getattr(raw, "response_metadata", {}).get("finish_reason") == "length" for raw in captured):
                raise ValueError("grounding response was truncated")
            if getattr(response, "response_metadata", {}).get("finish_reason") == "length":
                raise ValueError("grounding response was truncated")
            return response

        if self._native_available:
            try:
                result = invoke(self.model.with_structured_output(schema, method="json_schema"), messages)
                return ReportGroundingBatch.model_validate(result)
            except (NotImplementedError, TypeError, BadRequestError) as error:
                if isinstance(error, BadRequestError) and not any(term in str(error).lower()
                                                                for term in ("json_schema", "response_format")):
                    raise
                runtime.remaining()
                self._native_available = False
        fallback = [SystemMessage(content=self.prompt + "\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False)), messages[1]]
        raw = invoke(self.model, fallback)
        content = raw.text.strip()
        if content.startswith("```") and content.endswith("```"):
            content = content.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        return ReportGroundingBatch.model_validate_json(content)
