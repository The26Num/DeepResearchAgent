"""Phase 3C: canonical references, provenance, reuse policy and bounded closure."""

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool

from app.agents.researcher import Researcher
from app.research.evidence_context import build_evidence_context, select_evidence_context
from app.research.evidence_store import EvidenceStore
from app.research.tool_budget import ToolBudget, budgeted_tools
from app.schemas import Claim, CompactResearchResult, Evidence, Source, TaskEvidenceBundle
from app.schemas.evidence_extraction import ExtractedEvidenceResult
from app.schemas.source import normalize_source_url
from app.schemas.task import ResearchTask


def task(identifier, kind="discovery", dependencies=()):
    return ResearchTask(id=identifier, task_type=kind, depends_on=list(dependencies),
                        title="Research", question="Compare methods", description="Research methods")


def bundle(identifier, url="https://arxiv.org/abs/2601.08107", content="Method A improves sparse rewards."):
    source = Source(task_id=identifier, title="Original paper", url=url)
    evidence = Evidence(task_id=identifier, source_id=source.source_id, content=content, location="Method")
    claim = Claim(task_id=identifier, text="A method finding", evidence_ids=[evidence.evidence_id])
    return TaskEvidenceBundle(task_id=identifier, sources=[source], evidence=[evidence], claims=[claim])


@pytest.mark.parametrize("path", ["html/2601.08107", "pdf/2601.08107", "pdf/2601.08107.pdf", "abs/2601.08107/#x"])
def test_known_arxiv_representations_share_identity(path):
    assert normalize_source_url("https://arxiv.org/" + path) == "https://arxiv.org/abs/2601.08107"


def test_arxiv_versions_and_legacy_paper_paths_remain_conservative():
    assert normalize_source_url("https://arxiv.org/pdf/cs/9901001v1.pdf") == "https://arxiv.org/abs/cs/9901001v1"
    assert normalize_source_url("https://arxiv.org/abs/2601.08107v1") != normalize_source_url("https://arxiv.org/html/2601.08107v2")
    assert normalize_source_url("https://example.org/html/2601.08107") != normalize_source_url("https://example.org/abs/2601.08107")


def test_source_and_evidence_remapping_updates_claim_and_keeps_first_provenance():
    store = EvidenceStore()
    first, second = bundle("D1"), bundle("D2", "https://arxiv.org/html/2601.08107", "Method A  improves\n sparse rewards.")
    store.merge(first)
    canonical = store.merge(second)
    root = first.evidence[0].evidence_id
    assert store.counts == {"sources": 1, "evidence": 1, "claims": 2}
    assert canonical.sources[0] == first.sources[0] and canonical.evidence == []
    assert canonical.claims[0].evidence_ids == [root]
    assert store.get_evidence(second.evidence[0].evidence_id) == first.evidence[0]
    assert store.get_source(second.sources[0].source_id) == first.sources[0]
    assert store.get_source_provenance(first.sources[0].source_id).used_by_tasks == ("D1", "D2")
    provenance = store.get_evidence_provenance(root)
    assert provenance.extracted_by_task == "D1" and provenance.used_by_tasks == ("D1", "D2")
    assert set(provenance.referenced_by_claim_ids) == {first.claims[0].claim_id, second.claims[0].claim_id}
    assert provenance.is_cited and first.sources[0].discovered_by_task == "D1"
    assert first.evidence[0].extracted_by_task == "D1"
    assert second.claims[0].evidence_ids == [second.evidence[0].evidence_id]  # Input unchanged.
    before = store.reuse_counts
    assert store.merge(second) == canonical and store.merge(canonical) == canonical
    assert store.reuse_counts == before


@pytest.mark.parametrize("content,url", [
    ("Method a improves sparse rewards.", "https://arxiv.org/abs/2601.08107"),
    ("Method A improves sparse rewards!", "https://arxiv.org/abs/2601.08107"),
    ("Method A improves sparse rewards.", "https://example.org/different-paper"),
])
def test_exact_dedup_does_not_merge_case_punctuation_or_different_sources(content, url):
    store = EvidenceStore()
    store.merge(bundle("D1"))
    store.merge(bundle("D2", url, content))
    assert store.counts["evidence"] == 2


def test_two_local_references_collapsing_to_one_are_canonical_and_nonempty():
    local = bundle("D1")
    duplicate = Evidence(task_id="D1", source_id=local.sources[0].source_id,
                         content=local.evidence[0].content, location="Another section")
    local.evidence.append(duplicate)
    local.claims[0] = local.claims[0].model_copy(update={"evidence_ids": [local.evidence[0].evidence_id, duplicate.evidence_id]})
    store = EvidenceStore()
    merged = store.merge(local)
    assert len(merged.evidence) == 1 and merged.claims[0].evidence_ids == [local.evidence[0].evidence_id]
    assert store.get_evidence(duplicate.evidence_id).location == "Method"


def test_analysis_and_synthesis_cross_task_queries_and_reverse_indexes():
    store = EvidenceStore()
    d1, d2 = bundle("D1"), bundle("D2", "https://example.org/paper-b", "Method B uses a different mechanism.")
    store.merge(d1)
    store.merge(d2)
    refs = [d1.evidence[0].evidence_id, d2.evidence[0].evidence_id]
    analysis = Claim(task_id="A", text="The mechanisms differ.", evidence_ids=refs)
    synthesis = Claim(task_id="S", text="The mechanisms differ.", evidence_ids=refs)
    store.merge(TaskEvidenceBundle(task_id="A", claims=[analysis]))
    merged = store.merge(TaskEvidenceBundle(task_id="S", claims=[synthesis]))
    assert merged.sources == merged.evidence == []
    assert store.get_claim(analysis.claim_id) != store.get_claim(synthesis.claim_id)  # No text dedup.
    assert store.get_claims_for_task("S") == [synthesis]
    assert store.get_evidence_for_claim(synthesis.claim_id) == [d1.evidence[0], d2.evidence[0]]
    assert store.get_sources_for_claim(synthesis.claim_id) == [d1.sources[0], d2.sources[0]]
    assert store.get_evidence_for_task("S") == [d1.evidence[0], d2.evidence[0]]
    assert store.get_sources_for_task("S") == [d1.sources[0], d2.sources[0]]
    assert store.get_evidence_from_source(d1.sources[0].source_id) == [d1.evidence[0]]
    assert store.get_claims_using_evidence(refs[0]) == [d1.claims[0], analysis, synthesis]
    assert store.get_evidence_provenance(refs[0]).used_by_tasks == ("D1", "A", "S")
    assert store.get_source_provenance(d1.sources[0].source_id).used_by_tasks == ("D1", "A", "S")
    assert store.reuse_counts == {"source_reuse": 4, "evidence_reuse": 4}
    context = build_evidence_context(task("S", "synthesis", ["A"]), store)
    assert context.claims == (analysis,) and {item.evidence_id for item in context.evidence} == set(refs)


def test_unused_evidence_is_preserved_marked_and_excluded_from_dependency_context():
    local = bundle("D1")
    unused = Evidence(task_id="D1", source_id=local.sources[0].source_id, content="A potentially useful uncited fact.")
    local.evidence.append(unused)
    store = EvidenceStore()
    store.merge(local)
    assert store.get_evidence(unused.evidence_id) == unused
    provenance = store.get_evidence_provenance(unused.evidence_id)
    assert not provenance.is_cited and provenance.referenced_by_claim_ids == ()
    assert store.get_cited_evidence() == [local.evidence[0]]
    assert store.get_claims_using_evidence(unused.evidence_id) == []
    assert build_evidence_context(task("A", "analysis", ["D1"]), store).evidence == (local.evidence[0],)
    late = Claim(task_id="A", text="A later supported conclusion", evidence_ids=[unused.evidence_id])
    store.add_claim(late)
    assert store.get_evidence_provenance(unused.evidence_id).is_cited


def test_dependency_context_filters_unrelated_tasks_and_keeps_whole_chains_under_budget():
    store = EvidenceStore()
    for identifier in ("D1", "D2", "UNRELATED"):
        store.merge(bundle(identifier, f"https://example.org/{identifier}", f"Actual fact from {identifier}."))
    context = build_evidence_context(task("A", "analysis", ["D1", "D2"]), store)
    assert {item.task_id for item in context.claims} == {"D1", "D2"}
    assert "UNRELATED" not in context.to_json() and len(context.to_json()) <= 6000
    bounded = select_evidence_context(store, ["D1", "D2"], max_chars=450)
    assert len(bounded.to_json()) <= 450
    for claim in bounded.claims:
        assert set(claim.evidence_ids) <= {item.evidence_id for item in bounded.evidence}
    context.claims[0].evidence_ids.append("external-mutation")
    assert "external-mutation" not in store.get_claim(context.claims[0].claim_id).evidence_ids


def test_bad_merge_rolls_back_provenance_aliases_indexes_and_counts():
    store = EvidenceStore()
    first, second = bundle("D1"), bundle("D2", "https://arxiv.org/html/2601.08107")
    store.merge(first)
    second.claims.append(Claim(task_id="D2", text="Bad", evidence_ids=["ev_unknown"]))
    before = store.get_evidence_provenance(first.evidence[0].evidence_id)
    with pytest.raises(ValueError, match="unknown evidence_ids"):
        store.merge(second)
    assert store.get_evidence_provenance(first.evidence[0].evidence_id) == before
    assert store.reuse_counts == {"source_reuse": 0, "evidence_reuse": 0}
    assert store.counts == {"sources": 1, "evidence": 1, "claims": 1}
    with pytest.raises(KeyError):
        store.get_evidence(second.evidence[0].evidence_id)
    with pytest.raises(KeyError):
        store.get_source(second.sources[0].source_id)
    assert store.get_claims_using_evidence(first.evidence[0].evidence_id) == first.claims
    second.claims.pop()
    store.merge(second)
    assert store.reuse_counts == {"source_reuse": 1, "evidence_reuse": 1}


def test_alias_id_conflicts_are_rejected_but_later_claims_can_reference_known_aliases():
    store = EvidenceStore()
    first, second = bundle("D1"), bundle("D2")
    store.merge(first)
    store.merge(second)
    later = Claim(task_id="A", text="A reused finding", evidence_ids=[second.evidence[0].evidence_id])
    canonical = store.add_claim(later)
    assert canonical.evidence_ids == [first.evidence[0].evidence_id]
    conflicting = second.evidence[0].model_copy(update={"location": "Changed"})
    with pytest.raises(ValueError, match="Conflicting evidence_id"):
        store.add_evidence(conflicting)


def test_parallel_exact_dedup_has_one_identity_and_all_provenance():
    store = EvidenceStore()
    locals_ = [bundle(f"D{index}") for index in range(20)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(store.merge, locals_))
    assert store.counts == {"sources": 1, "evidence": 1, "claims": 20}
    root = store.get_evidence_for_claim(locals_[0].claims[0].claim_id)[0]
    assert len(store.get_evidence_provenance(root.evidence_id).used_by_tasks) == 20
    assert len(store.get_claims_using_evidence(root.evidence_id)) == 20
    assert store.reuse_counts == {"source_reuse": 19, "evidence_reuse": 19}
    assert all(store.get_evidence_for_claim(local.claims[0].claim_id) == [root] for local in locals_)


def test_synthesis_cannot_execute_web_tools_even_when_requested():
    @tool
    def forbidden(query: str = "", url: str = "", max_chars: int = 5000, max_results: int = 5) -> str:
        """A tool that must never execute for synthesis."""
        pytest.fail("Synthesis performed external research")
    budget = ToolBudget.for_task(task("S", "synthesis"))
    search, fetch = budgeted_tools(budget, forbidden, forbidden)
    assert "error" in search.invoke({"query": "anything"})
    assert "error" in fetch.invoke({"url": "https://example.org"})
    assert budget.calls == budget.readings == []


def test_analysis_reuses_context_but_keeps_source_tools_for_explicit_missing_detail():
    store = EvidenceStore()
    store.merge(bundle("D1"))
    analysis = task("A", "analysis", ["D1"])
    context = build_evidence_context(analysis, store)
    previous = "https://arxiv.org/abs/2601.08107"
    assert not Researcher._analysis_needs_reading(analysis, previous, context)
    assert Researcher._analysis_needs_reading(analysis, previous)
    budget = ToolBudget.for_task(analysis)
    assert (budget.max_search_calls, budget.max_fetch_calls) == (2, 3)
    assert "freshly fetched" not in " ".join(Researcher._objectives(analysis, previous, context))


def test_synthesis_extraction_ignores_adapter_readings_and_cannot_create_source_or_evidence():
    store = EvidenceStore()
    store.merge(bundle("D1"))
    synthesis = task("S", "synthesis", ["D1"])
    upstream = build_evidence_context(synthesis, store)
    observed = []

    class Extractor:
        def extract(self, payload, runtime):
            observed.append(payload)
            assert payload["fetched_pages"] == []
            assert all(item["origin"] == "upstream" for item in payload["candidates"])
            return ExtractedEvidenceResult(selected_evidence_indexes=[0], claims=[{
                "text": "A synthesis finding", "supporting_evidence_indexes": [0]}])

    class Agent:
        def invoke(self, request):
            return {"messages": [ToolMessage(name="fetch_webpage", tool_call_id="f", content=
                '{"url":"https://example.org/new","title":"New","text":"A new reading. Extra text."}'),
                AIMessage(content="## 研究任务\nS\n## 主要发现\n综合发现。\n## 重要来源\nNone\n## 不确定性与缺失信息\nNone")]}

    researcher = Researcher(SimpleNamespace(), lambda budget: Agent(), evidence_extractor=Extractor())
    result = researcher.research(synthesis, {"D1": CompactResearchResult(task_id="D1", summary="Dependency")},
                                 evidence_context=upstream)
    assert observed and result.evidence_bundle.sources == result.evidence_bundle.evidence == []
    assert result.evidence_bundle.claims[0].evidence_ids == [upstream.evidence[0].evidence_id]


def test_analysis_can_supplement_existing_evidence_without_mandatory_prefetch():
    store = EvidenceStore()
    original = bundle("D1")
    store.merge(original)
    analysis = task("A", "analysis", ["D1"])
    upstream = build_evidence_context(analysis, store)
    budgets = []

    @tool
    def fetch(url: str, max_chars: int = 5000) -> str:
        """Read additional method details selected by the analysis agent."""
        return '{"url":"https://arxiv.org/html/2601.08107","title":"Original paper","text":"The additional experiment uses 100 trials. Discussion follows."}'

    def factory(budget):
        budgets.append(budget)
        reader = budgeted_tools(budget, fetch_tool=fetch)[1]
        class Agent:
            def invoke(self, request):
                assert "UPSTREAM EVIDENCE AND CLAIMS" in request["messages"][0]["content"]
                raw = reader.invoke({"url": "https://arxiv.org/html/2601.08107"})
                return {"messages": [ToolMessage(content=raw, name="fetch_webpage", tool_call_id="f"),
                    AIMessage(content="## 研究任务\nA\n## 主要发现\n补充实验细节。\n## 重要来源\nNone\n## 不确定性与缺失信息\nNone")]}
        return Agent()

    class Extractor:
        def extract(self, payload, runtime):
            fetched = next(item["evidence_index"] for item in payload["candidates"]
                           if item["origin"] == "fetched" and item["content"] == "The additional experiment uses 100 trials.")
            previous = next(item["evidence_index"] for item in payload["candidates"] if item["origin"] == "upstream")
            return ExtractedEvidenceResult(claims=[{"text": "Analysis with additional detail",
                "supporting_evidence_indexes": [fetched, previous]}])

    researcher = Researcher(SimpleNamespace(), factory, evidence_extractor=Extractor())
    researcher._source_fetch_tool = SimpleNamespace(invoke=lambda arguments: pytest.fail("Unexpected mandatory prefetch"))
    result = researcher.research(analysis, evidence_context=upstream)  # Artifact-only context is valid.
    assert budgets[0].calls == ["fetch_webpage"]
    canonical = store.merge(result.evidence_bundle)
    assert store.counts == {"sources": 1, "evidence": 2, "claims": 2}
    linked = store.get_evidence_for_claim(canonical.claims[0].claim_id)
    assert {item.task_id for item in linked} == {"D1", "A"}
    assert len(store.get_sources_for_claim(canonical.claims[0].claim_id)) == 1


def test_synthesis_without_upstream_evidence_produces_no_formal_claims():
    class Extractor:
        def extract(self, payload, runtime):
            pytest.fail("No Evidence candidates should mean no extraction request")

    class Agent:
        def invoke(self, request):
            return {"messages": [AIMessage(content="## 研究任务\nS\n## 主要发现\n暂无可支持的结论。\n## 重要来源\nNone\n## 不确定性与缺失信息\n缺少上游证据。") ]}

    researcher = Researcher(SimpleNamespace(), lambda budget: Agent(), evidence_extractor=Extractor())
    result = researcher.research(task("S", "synthesis", ["D1"]),
                                 {"D1": CompactResearchResult(task_id="D1", summary="Memo without evidence")})
    assert result.evidence_bundle == TaskEvidenceBundle(task_id="S")
