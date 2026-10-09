"""Failed/PDF discovery reads must never publish fabricated findings downstream."""

import importlib
import json
from types import SimpleNamespace

import pytest
import requests
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.agents.researcher import Researcher
from app.research.discovery_guard import readable_source_urls
from app.research.orchestrator import ResearchOrchestrator
from app.research.runtime import ResearchRuntime, ResearchTimeoutError
from app.research.tool_budget import budgeted_tools
from app.schemas import ResearchPlan, ResearchTask
from app.schemas.evidence_extraction import ExtractedEvidenceResult


fetch_module = importlib.import_module("app.tools.fetch_webpage")
PDF = "https://proceedings.mlr.press/v162/graesser22a/graesser22a.pdf"
HTML = "https://proceedings.mlr.press/v162/graesser22a.html"
OTHER = "https://example.org/actual-paper"
FAKE = "https://openreview.net/pdf?id=Q123456789"
BODY = "Introduction. The actual method uses subgoals. Discussion."


def task(identifier="D1", kind="discovery", dependencies=()):
    return ResearchTask(id=identifier, title="Research", question="Sparse reward offline RL?",
                        description="Read actual sources", task_type=kind, depends_on=list(dependencies))


@pytest.mark.parametrize("url,expected", [
    (PDF, [HTML]),
    ("https://arxiv.org/pdf/2601.08107v2.pdf", ["https://arxiv.org/html/2601.08107v2", "https://arxiv.org/abs/2601.08107v2"]),
    ("https://arxiv.org/pdf/cs/9901001v1", ["https://arxiv.org/html/cs/9901001v1", "https://arxiv.org/abs/cs/9901001v1"]),
    ("https://openreview.net/pdf?id=real-paper", ["https://openreview.net/forum?id=real-paper"]),
    ("https://example.org/paper.pdf", ["https://example.org/paper.pdf"]),
    ("https://example.org/abs/2601.08107", ["https://example.org/abs/2601.08107"]),
    ("https://proceedings.mlr.press/v162/one/different.pdf", ["https://proceedings.mlr.press/v162/one/different.pdf"]),
])
def test_known_pdf_alternatives_do_not_change_paper_versions_or_invent_generic_urls(url, expected):
    assert readable_source_urls(url) == expected


def html_response(url=HTML):
    return SimpleNamespace(url=url, headers={"Content-Type": "text/html"},
        content=b'<html><head><title>Site wrapper title</title><meta name="citation_title" content="Actual Paper"><meta name="citation_date" content="2025-03-01"></head><body><main>Introduction. The actual method uses subgoals. Discussion.</main></body></html>',
        raise_for_status=lambda: None)


def test_known_pdf_fetch_reads_actual_html_and_returns_actual_title_url(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert kwargs["timeout"] == 15
        return html_response(url)
    monkeypatch.setattr(fetch_module.requests, "get", get)
    page = json.loads(fetch_module.fetch_webpage.invoke({"url": PDF}))
    assert calls == [HTML]  # Do not download a known PDF.
    assert page["url"] == HTML and page["requested_url"] == PDF
    assert page["title"] == "Actual Paper" and page["published_at"] == "2025-03-01"
    assert page["text"] == BODY


def test_arxiv_html_failure_tries_abstract_once(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if "/html/" in url:
            raise requests.HTTPError("HTML unavailable")
        return html_response(url)
    monkeypatch.setattr(fetch_module.requests, "get", get)
    page = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://arxiv.org/pdf/2601.08107v2.pdf"}))
    assert calls == ["https://arxiv.org/html/2601.08107v2", "https://arxiv.org/abs/2601.08107v2"]
    assert page["url"].endswith("/abs/2601.08107v2") and "error" not in page


def test_pdf_redirect_uses_a_known_alternative_without_unbounded_requests(monkeypatch):
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url == OTHER:
            return SimpleNamespace(url=PDF, headers={"Content-Type": "application/pdf"},
                                   raise_for_status=lambda: None)
        return html_response(url)
    monkeypatch.setattr(fetch_module.requests, "get", get)
    page = json.loads(fetch_module.fetch_webpage.invoke({"url": OTHER}))
    assert calls == [OTHER, HTML] and page["url"] == HTML


def test_generic_pdf_is_a_failure_without_downloading_or_inventing_an_alternative(monkeypatch):
    monkeypatch.setattr(fetch_module.requests, "get", lambda *args, **kwargs: pytest.fail("Should not download PDF"))
    page = json.loads(fetch_module.fetch_webpage.invoke({"url": "https://example.org/paper.pdf"}))
    assert "PDF parsing is not supported" in page["error"] and "text" not in page


def test_html_fallback_respects_task_deadline(monkeypatch):
    runtime = ResearchRuntime("D1", 0.5, 0.1)
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        assert 0 < kwargs["timeout"] <= 0.5
        runtime.closed.set()
        raise requests.Timeout("Deadline used by first request")
    monkeypatch.setattr(fetch_module.requests, "get", get)
    with runtime.activate(), pytest.raises(ResearchTimeoutError):
        fetch_module.fetch_webpage.invoke({"url": "https://arxiv.org/pdf/2601.08107v2.pdf"})
    assert calls == ["https://arxiv.org/html/2601.08107v2"]


def make_researcher(*, all_fail=False, exhaust_budget=False, claims=True, repeat_failed_html=False):
    calls, budgets, requests_ = [], [], []

    @tool
    def search(query: str, max_results: int = 5) -> str:
        """Return actual candidates, never the fabricated memo URL."""
        return json.dumps({"results": [{"url": PDF}, {"url": OTHER}]})

    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Model a failed PDF/HTML and an available independent source."""
        calls.append(url)
        if all_fail or url != OTHER:
            response = {"url": url, "error": "PDF/HTML unavailable"}
            if repeat_failed_html and url == PDF:
                response["attempted_urls"] = [HTML]
            return json.dumps(response)
        return json.dumps({"url": url, "title": "Actual Paper", "published_at": "2025", "text": BODY})

    class Agent:
        def __init__(self, budget):
            self.budget = budget
            self.tools = budgeted_tools(budget, search, fetch)
            self.calls = 0
        def invoke(self, request):
            self.calls += 1
            requests_.append(request)
            if self.calls == 1:
                raw = self.tools[0].invoke({"query": "Sparse reward offline RL"})
                messages = [ToolMessage(content=raw, name="web_search", tool_call_id="s")]
                for _ in range(4 if exhaust_budget else 1):
                    raw = self.tools[1].invoke({"url": PDF})
                    messages.append(ToolMessage(content=raw, name="fetch_webpage", tool_call_id=f"f{_}"))
            else:
                messages = []  # Ignore one reminder, like the problematic model.
            messages.append(AIMessage(content=f"## 主要发现\nINVENTED_PERFORMANCE\n## 重要来源\n### Invented Paper\n- URL：{FAKE}"))
            return {"messages": messages}

    def factory(budget):
        budgets.append(budget)
        return Agent(budget)

    class Model:
        def invoke(self, messages, **kwargs):
            calls.append("memo_model")
            assert "MEMO FINALIZATION" in messages[1].text
            assert "The actual method uses subgoals." in messages[1].text
            assert "INVENTED_PERFORMANCE" not in messages[1].text and FAKE not in messages[1].text
            return AIMessage(content=f"## 主要发现\nThe real finding.\nUNLINKED_FINDING\n## 重要来源\n### Wrong Title\n- URL：{FAKE}\n## 不确定性与缺失信息\nUNLINKED_UNCERTAINTY_CLAIM")

    def extract(payload, runtime):
        calls.append("extractor")
        if all_fail:
            pytest.fail("Unread source must not trigger extraction")
        if not claims:
            return ExtractedEvidenceResult()
        index = next(item["evidence_index"] for item in payload["candidates"]
                     if item["content"] == "The actual method uses subgoals.")
        return ExtractedEvidenceResult(claims=[{"text": "实际方法使用子目标。", "supporting_evidence_indexes": [index]}])

    researcher = Researcher(Model(), factory, evidence_extractor=SimpleNamespace(extract=extract))
    researcher._source_fetch_tool = fetch
    return researcher, calls, budgets, requests_


def test_failed_pdf_continues_to_other_actual_candidate_then_discards_fabricated_memo(capsys):
    researcher, calls, budgets, agent_requests = make_researcher()
    result = researcher.research(task())
    assert calls == [PDF, HTML, OTHER, "memo_model", "extractor"]
    assert len(agent_requests) == 2 and budgets[0].fetch_calls == 3
    assert "INVENTED_PERFORMANCE" not in result.memo and "UNLINKED_FINDING" not in result.memo
    assert FAKE not in result.memo and "Wrong Title" not in result.memo
    assert "UNLINKED_UNCERTAINTY_CLAIM" not in result.memo
    assert "正式结论引用 1 个来源" in result.memo
    assert "### Actual Paper" in result.memo and f"- URL：{OTHER}" in result.memo
    assert result.compact.key_findings == ["实际方法使用子目标。"]
    assert [source.url for source in result.compact.sources] == [OTHER]
    assert result.evidence_bundle.claims and result.evidence_bundle.evidence[0].content == "The actual method uses subgoals."
    assert "discovery reading recovery" in capsys.readouterr().out


def test_failed_internal_html_attempt_is_not_repeated_during_discovery_recovery():
    researcher, calls, budgets, _ = make_researcher(repeat_failed_html=True)
    researcher.research(task())
    assert calls == [PDF, OTHER, "memo_model", "extractor"]
    assert budgets[0].fetch_calls == 2


def test_all_failed_reads_return_explicit_gap_with_no_claims_or_downstream_findings(capsys):
    researcher, calls, budgets, _ = make_researcher(all_fail=True)
    result = researcher.research(task())
    assert "INVENTED_PERFORMANCE" not in result.memo and FAKE not in result.memo
    assert "候选来源未能成功读取" in result.memo
    assert result.compact.key_findings == result.compact.sources == []
    assert result.evidence_bundle.claims == result.evidence_bundle.sources == result.evidence_bundle.evidence == []
    assert calls == [PDF, HTML, OTHER] and budgets[0].fetch_calls == 3
    assert "discovery incomplete" in capsys.readouterr().out


def test_reading_recovery_stops_at_original_fetch_budget_without_an_extra_model_loop():
    researcher, calls, budgets, agent_requests = make_researcher(all_fail=True, exhaust_budget=True)
    result = researcher.research(task())
    assert budgets[0].fetch_calls == 4 and calls == [PDF] * 4
    assert len(agent_requests) == 1  # No pointless reminder after all reads are used.
    assert result.compact.key_findings == []


def test_successful_read_without_formal_claims_withholds_the_freeform_findings():
    researcher, calls, _, _ = make_researcher(claims=False)
    result = researcher.research(task())
    assert "已读取材料" in result.memo and result.compact.key_findings == []
    assert FAKE not in result.memo and "UNLINKED_FINDING" not in result.memo
    assert result.evidence_bundle.claims == []


def test_failed_discovery_gap_reaches_synthesis_without_invented_upstream_content():
    researcher, _, _, _ = make_researcher(all_fail=True)
    tasks = [task(), task("S", "synthesis", ["D1"])]
    plan = ResearchPlan(goal="Research", tasks=tasks)
    original_factory = researcher._agent_factory
    captured = []
    def factory(budget):
        if budget.max_fetch_calls:
            return original_factory(budget)
        class Agent:
            def invoke(self, request):
                text = request["messages"][0]["content"]
                captured.append(text)
                assert "INVENTED_PERFORMANCE" not in text and FAKE not in text
                assert "未形成有来源支持的结论" in text
                return {"messages": [AIMessage(content="## 研究任务\nS\n## 主要发现\n无法确认本分支结论。\n## 重要来源\n暂无\n## 不确定性与缺失信息\n缺少已读取的来源。") ]}
        return Agent()
    researcher._agent_factory = factory
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), researcher).run("Research")
    assert captured and all(item.status == "completed" for item in result.plan.tasks)
    assert result.evidence_store.counts == {"sources": 0, "evidence": 0, "claims": 0}
