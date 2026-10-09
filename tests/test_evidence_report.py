"""Protect the public report and synthesis input from unlinked draft content."""

from types import SimpleNamespace
import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agents.researcher import Researcher
from app.research.evidence_context import EvidenceContext, build_evidence_context
from app.research.evidence_report import finalize_evidence_report
from app.research.evidence_store import EvidenceStore
from app.research.orchestrator import ResearchOrchestrator
from app.schemas import Claim, Evidence, ResearchPlan, ResearchTask, Source, TaskEvidenceBundle
from app.schemas.evidence_extraction import ExtractedEvidenceResult
from app.schemas.source import classify_source_url


def task(key, kind="discovery", deps=()):
    return ResearchTask(id=key, title=key, question="Compare the supported mechanisms",
                        description="Ground all conclusions", task_type=kind, depends_on=list(deps))


def bundle(key, url=None, text=None):
    source = Source(task_id=key, url=url or f"https://example.org/{key}", title=f"Original {key}",
                    published_at="2025-01-01")
    evidence = Evidence(task_id=key, source_id=source.source_id, content=text or f"The {key} method uses subgoals.")
    claim = Claim(task_id=key, text=f"Supported contribution from {key}", evidence_ids=[evidence.evidence_id])
    return TaskEvidenceBundle(task_id=key, sources=[source], evidence=[evidence], claims=[claim])


@pytest.mark.parametrize("kind", ["analysis", "synthesis"])
def test_public_report_and_compact_ignore_fabricated_draft_sources(kind):
    original = bundle("D1", "https://farama.org/Announcing-Minari")
    upstream = EvidenceContext(tuple(original.sources), tuple(original.evidence), tuple(original.claims))
    draft = "UNSUPPORTED_RESULT https://farama.org/Announcing-Mini https://example.org/never-read"

    class Agent:
        def invoke(self, request):
            return {"messages": [AIMessage(content=draft)]}

    class Extractor:
        def extract(self, payload, runtime):
            return ExtractedEvidenceResult(claims=[{"text": "Grounded result", "supporting_evidence_indexes": [0]}])

    result = Researcher(SimpleNamespace(), lambda budget: Agent(), evidence_extractor=Extractor()).research(
        task("A", kind, ["D1"]), evidence_context=upstream)
    assert "UNSUPPORTED_RESULT" not in result.memo and "never-read" not in result.memo
    assert "Announcing-Mini\n" not in result.memo
    assert "https://farama.org/Announcing-Minari" in result.memo
    assert result.compact.key_findings == ["Grounded result"]
    assert [item.url for item in result.compact.sources] == [original.sources[0].url]
    assert original.evidence[0].content in result.memo and "E1" in result.memo


def test_report_only_lists_sources_used_by_formal_claims():
    first, second = bundle("D1"), bundle("D2")
    upstream = EvidenceContext(tuple(first.sources + second.sources), tuple(first.evidence + second.evidence), ())
    selected = TaskEvidenceBundle(task_id="S", claims=[Claim(task_id="S", text="A supported conclusion",
                                  evidence_ids=[first.evidence[0].evidence_id])])
    memo, compact = finalize_evidence_report(task("S", "synthesis"), selected, upstream, has_reading=False)
    assert first.sources[0].url in memo and second.sources[0].url not in memo
    assert [item.url for item in compact.sources] == [first.sources[0].url]
    assert "1 个来源未用于" in memo


def test_orchestrator_rerenders_metadata_after_cross_task_canonical_merge():
    plan = ResearchPlan(goal="Research", tasks=[task("D1"), task("D2")])

    class Agent:
        def invoke(self, request):
            key = "D1" if "ID: D1\n" in request["messages"][0]["content"] else "D2"
            return {"messages": [
                ToolMessage(name="web_search", tool_call_id="s", content='{"results":[{"url":"https://example.org/shared"}]}'),
                ToolMessage(name="fetch_webpage", tool_call_id="f", content=json.dumps({
                    "url": "https://example.org/shared", "title": f"Original {key}",
                    "text": "The method uses subgoals. Discussion follows."})),
                AIMessage(content="Unlinked draft")
            ]}

    class Extractor:
        def extract(self, payload, runtime):
            return ExtractedEvidenceResult(claims=[{"text": "Supported result", "supporting_evidence_indexes": [0]}])

    researcher = Researcher(SimpleNamespace(), lambda budget: Agent(), evidence_extractor=Extractor())
    result = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: plan), researcher).run("Research")
    assert result.evidence_store.counts == {"sources": 1, "evidence": 1, "claims": 2}
    assert "Original D1" in result.memos["D2"] and "Original D2" not in result.memos["D2"]
    assert result.compact_results["D2"].sources[0].title == "Original D1"


def test_synthesis_recovers_reachable_discovery_dimensions_without_unrelated_tasks():
    store = EvidenceStore()
    d1, d2, unrelated = bundle("D1"), bundle("D2"), bundle("UNRELATED")
    for item in (d1, d2, unrelated):
        store.merge(item)
    analysis = Claim(task_id="A", text="Only D1 survived the analysis", evidence_ids=d1.claims[0].evidence_ids)
    store.merge(TaskEvidenceBundle(task_id="A", claims=[analysis]))
    synthesis = task("S", "synthesis", ["A"])
    plan = ResearchPlan(goal="Research", tasks=[task("D1"), task("D2"), task("UNRELATED"),
                                              task("A", "analysis", ["D1", "D2"]), synthesis])
    allowed = ResearchOrchestrator._evidence_dependencies(synthesis, plan)
    assert allowed == ["A", "D1", "D2"]
    context = build_evidence_context(synthesis, store, allowed)
    assert {item.task_id for item in context.claims} == {"A", "D1", "D2"}
    assert {item.source_id for item in context.sources} == {d1.sources[0].source_id, d2.sources[0].source_id}
    assert "UNRELATED" not in context.to_json()
    assert context.payload()["sources"][0]["published_at"] == "2025-01-01"


def test_larger_context_retains_complete_multi_branch_chains_over_old_6000_limit():
    store = EvidenceStore()
    for key in ("D1", "D2", "D3"):
        item = bundle(key, text=(key + " supported detail. ") * 90)
        store.merge(item)
    context = build_evidence_context(task("A", "analysis", ["D1", "D2", "D3"]), store)
    assert {item.task_id for item in context.claims} == {"D1", "D2", "D3"}
    assert 6000 < len(context.to_json()) <= 12000
    assert all(set(claim.evidence_ids) <= {item.evidence_id for item in context.evidence} for claim in context.claims)


@pytest.mark.parametrize("url", [
    "https://proceedings.mlr.press/v164/sinha22a.html",
    "https://www.alphaxiv.org/abs/2601.08107",
    "https://openreview.net/forum?id=abc",
    "https://neurips.cc/virtual/2025/loc/san-diego/poster/116306",
    "https://iclr.cc/virtual/2026/poster/10006399",
    "https://link.springer.com/article/10.1007/s00521-026-11966-8",
])
def test_paper_landing_pages_are_classified_as_papers(url):
    assert classify_source_url(url) == "paper"


@pytest.mark.parametrize("url", ["https://neurips.cc/", "https://openreview.net/group?id=ICLR",
                                 "https://farama.org/Announcing-Minari", "https://arxiv.org/help"])
def test_academic_hosts_and_blogs_are_not_unconditionally_papers(url):
    assert classify_source_url(url) == "webpage"


def test_paper_abstract_does_not_trigger_no_paper_warning_or_claim_full_text_was_read():
    item = bundle("D1", "https://proceedings.mlr.press/v164/sinha22a.html")
    item.sources[0] = item.sources[0].model_copy(update={"source_type": "paper"})
    memo, _ = finalize_evidence_report(task("D1"), item, EvidenceContext(), has_reading=True)
    assert "未包含论文" not in memo and "已阅读全文" not in memo
