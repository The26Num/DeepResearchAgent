"""Deterministic full-loop model and HTTP fixtures; no provider credentials."""

import json
import re

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from pydantic import PrivateAttr

from app.agents.researcher import Researcher
from app.agents.report_grounding import ReportGroundingChecker
from app.agents.relevance import RelevanceAssessor
from app.agents.verifier import ClaimVerifier
from app.agents.writer import ResearchWriter
from app.research.gap_detection import GapDetector
from app.research.orchestrator import ResearchOrchestrator
from app.research.tool_budget import budgeted_tools
from app.schemas import ResearchPlan, ResearchTask
from app.tools.fetch_webpage import fetch_webpage

FACT = "TAIL_METRIC equals 97 units."
URL = "https://example.org/long-study"
HTML = ("<html><head><title>Fixed academic fixture</title></head><body><main><p>"
        + "background material " * 900 + "</p><h2>Measurements</h2><p>" + FACT
        + "</p><p>Study limitations remain explicitly stated.</p></main></body></html>").encode()


class Response:
    url = URL
    headers = {"Content-Type": "text/html; charset=utf-8"}
    status_code = 200
    def __init__(self, raw=HTML, **values):
        self.content, self.closed = raw, False
        self.__dict__.update(values)
    def raise_for_status(self):
        pass
    def iter_content(self, chunk_size):
        for index in range(0, len(self.content), chunk_size):
            yield self.content[index:index + chunk_size]
    def close(self):
        self.closed = True


@tool
def search(query: str, max_results: int = 5) -> str:
    """Return a fixed navigation result, never factual evidence."""
    return json.dumps({"results": [{"url": URL, "title": "Fixed academic fixture", "snippet": "Search navigation is not evidence."}]})


class OfflineModel(BaseChatModel):
    role: str = "research"
    use_skills: bool = True
    _requests: list = PrivateAttr(default_factory=list)

    @property
    def _llm_type(self):
        return "offline-archive-contract"

    @property
    def requests(self):
        return self._requests

    def with_structured_output(self, *args, **kwargs):
        raise NotImplementedError("Exercise the existing schema-validated JSON compatibility path.")

    def bind_tools(self, tools, **kwargs):
        return self.bind(advertised_tools=[item.name if hasattr(item, "name") else item["name"] for item in tools])

    def _generate(self, messages, **kwargs):
        self._requests.append((list(messages), dict(kwargs)))
        try:
            data = json.loads(messages[-1].text)
        except (ValueError, TypeError):
            data = None
        result = None
        if isinstance(data, dict) and "evidence_candidates" in data:
            choices = [item for item in data["evidence_candidates"] if FACT in item["content"]]
            if choices:
                chosen = min(choices, key=lambda item: len(item["content"]))
                result = {"selected_evidence_indexes": [], "claims": [{"text": FACT, "supporting_evidence_indexes": [chosen["evidence_index"]]}]}
            else:
                result = {"selected_evidence_indexes": [], "claims": []}
        elif isinstance(data, dict) and self.role in {"verification", "relevance"}:
            result = {"results": [{"claim_id": item["claim_id"], "status": "SUPPORTED" if self.role == "verification" else "ALIGNED",
                                    "reason": "The fixed claim is directly quoted from its evidence."} for item in data["claims"]]}
        elif isinstance(data, dict) and "items" in data:
            result = {"results": [{"item_index": item["item_index"], "status": "SUPPORTED", "reason": "Fixed fixture."} for item in data["items"]]}
        elif isinstance(data, dict) and self.role == "writer":
            paragraphs = [{"text": item["text"], "claim_ids": [item["claim_id"]]} for item in data["accepted_claims"]]
            result = {"title": "离线研究资源验证", "scope": data["goal"], "executive_summary": paragraphs[:1], "key_findings": paragraphs,
                      "sections": [{"heading": "研究发现", "paragraphs": paragraphs}], "limitations": [], "conclusion": paragraphs[:1]}
        if result is not None:
            reply = AIMessage(content=json.dumps(result, ensure_ascii=False))
        else:
            task = next((match.group(1) for message in messages if (match := re.search(r"\nID: ([^\n]+)", message.text))), "D")
            seen = {message.name for message in messages if isinstance(message, ToolMessage)}
            tools = kwargs.get("advertised_tools", [])
            call = None
            if task == "D":
                if self.use_skills and "load_research_skill" not in seen:
                    call = ("load_research_skill", {"skill_id": "academic_review"})
                elif "web_search" not in seen:
                    call = ("web_search", {"query": "fixed academic study"})
                elif "fetch_webpage" not in seen:
                    call = ("fetch_webpage", {"url": URL, "max_chars": 1000})
                elif "search_sources" not in seen:
                    call = ("search_sources", {"query": "TAIL_METRIC"})
                elif "read_source" not in seen:
                    hit = json.loads(next(message.text for message in reversed(messages)
                                          if isinstance(message, ToolMessage) and message.name == "search_sources"))["hits"][0]
                    call = ("read_source", {"snapshot_id": hit["snapshot_id"], "section_id": hit["section_id"]})
            elif task == "S":
                assert "web_search" not in tools and "fetch_webpage" not in tools
                if "list_sources" not in seen:
                    call = ("list_sources", {})
                elif "search_sources" not in seen:
                    call = ("search_sources", {"query": "TAIL_METRIC"})
                elif "read_source" not in seen:
                    hit = json.loads(next(message.text for message in reversed(messages)
                                          if isinstance(message, ToolMessage) and message.name == "search_sources"))["hits"][0]
                    call = ("read_source", {"snapshot_id": hit["snapshot_id"], "section_id": hit["section_id"]})
            if call:
                assert call[0] in tools, (call[0], tools)
                reply = AIMessage(content="", tool_calls=[{"id": f"call_{task}_{len(self._requests)}", "name": call[0], "args": call[1]}])
            else:
                reply = AIMessage(content="## 研究任务\n固定资料调研\n## 主要发现\n" + FACT + "\n## 重要来源\n" + URL + "\n## 不确定性与缺失信息\n仅为离线验证。")
        return ChatResult(generations=[ChatGeneration(message=reply)])


def pipeline(db, cfg, monkeypatch, *, failure=None, use_skills=True):
    import app.agents.researcher as module
    import app.research.orchestrator as orchestrator
    monkeypatch.setattr(module, "budgeted_tools", lambda budget, **kwargs: budgeted_tools(budget, search, kwargs.get("fetch_tool", fetch_webpage)))
    monkeypatch.setattr(module, "get_settings", lambda: cfg)
    monkeypatch.setattr(orchestrator, "get_settings", lambda: cfg)
    monkeypatch.setattr("app.research.evidence_extraction.get_settings", lambda: cfg)
    model, verification, relevance = OfflineModel(use_skills=use_skills), OfflineModel(role="verification"), OfflineModel(role="relevance")
    class Supervisor:
        def create_plan(self, question):
            return ResearchPlan(goal=question, tasks=[
                ResearchTask(id="D", title="Discovery sources", question="Locate numerical evidence", description="Discover original material"),
                ResearchTask(id="A", title="Analysis", question="Analyze findings", description="Analyze", task_type="analysis", depends_on=["D"]),
                ResearchTask(id="S", title="Synthesis", question="Synthesize findings", description="Synthesize", task_type="synthesis", depends_on=["A"])])
    worker = ResearchOrchestrator(Supervisor(), Researcher(model), run_store=db,
        verifier=ClaimVerifier(verification, settings=cfg), gap_detector=GapDetector(RelevanceAssessor(relevance, settings=cfg)),
        writer=ResearchWriter(OfflineModel(role="writer"), settings=cfg,
                              grounding_checker=ReportGroundingChecker(OfflineModel(role="grounding"), settings=cfg)),
        failure_injector=failure)
    return worker, model
