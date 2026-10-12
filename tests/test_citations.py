"""Deterministic, report-scoped citations from the complete stored support graph."""

import copy
from types import SimpleNamespace

import pytest

from app.research.citations import CitationIntegrityError, CitationRegistry, CitationResolver
from app.research.evidence_store import EvidenceStore
from app.research.report_rendering import render_report, render_report_with_citations, validate_citations
from app.schemas import Claim, Evidence, ResearchReport, Source


def graph(*, source_order=("a", "b", "c", "unused")):
    store = EvidenceStore()
    for label in source_order:
        store.add_source(Source(source_id=f"src_{label}", task_id="D1", title=f"Stored title {label}",
            url=f"https://example.org/{label}", source_type="paper", published_at="2025-01-01" if label == "a" else None))
    for key, source in [("a1", "a"), ("a2", "a"), ("a3", "a"), ("b", "b"), ("c", "c"), ("unused", "unused")]:
        store.add_evidence(Evidence(evidence_id=f"ev_{key}", task_id="D1", source_id=f"src_{source}", content=f"Original evidence {key}"))
    for key, evidence in [("a", ["a1", "a2", "a3"]), ("ab", ["a1", "b"]), ("c", ["c"]), ("unused", ["unused"])]:
        store.add_claim(Claim(claim_id=f"claim_{key}", task_id="D1", text=f"Stored Claim {key}",
                              evidence_ids=[f"ev_{value}" for value in evidence]))
    return store


def paragraph(*keys, text="Existing factual paragraph."):
    return {"text": text, "claim_ids": [f"claim_{key}" for key in keys]}


def report(*, summary=None, findings=None, analysis=None, conclusion=None, limitations=None):
    return ResearchReport.model_validate(dict(title="Existing report title", scope="Original research scope",
        executive_summary=[paragraph("a")] if summary is None else summary,
        key_findings=[paragraph("a")] if findings is None else findings,
        sections=[{"heading": "Existing heading", "paragraphs": [paragraph("a")] if analysis is None else analysis}],
        limitations=limitations or [], conclusion=[paragraph("a")] if conclusion is None else conclusion))


def empty_report():
    return ResearchReport(title="No accepted Claims", scope="Original scope", executive_summary=[], key_findings=[],
        sections=[], conclusion=[], limitations=[{"text": "Current evidence is insufficient.", "gap_ids": ["gap_open"]}])


def store_state(store):
    return {key: copy.deepcopy(value) for key, value in vars(store).items() if key != "_lock"}


def test_three_evidence_from_one_source_generate_one_marker_per_element():
    store = graph()
    rendered = render_report_with_citations(report(), store)
    assert len(rendered.references) == 1
    assert all(item.citation_numbers == [1] for item in rendered.citations)
    assert rendered.usage.used_claims == rendered.usage.cited_sources == rendered.usage.references == 1
    assert rendered.usage.citation_markers == 4
    assert "[1][1]" not in rendered.markdown


def test_one_claim_with_two_sources_and_multiple_claims_with_shared_source_are_deduplicated():
    store = graph()
    resolver = CitationResolver(store)
    assert resolver.resolve_claims(["claim_a", "claim_ab", "claim_a"]) == ["src_a", "src_b"]
    result = render_report_with_citations(report(findings=[paragraph("a", "ab")]), store)
    assert result.citations[1].source_ids == ["src_a", "src_b"]
    assert result.citations[1].citation_numbers == [1, 2]
    assert "Existing factual paragraph. [1][2]" in result.markdown
    assert [entry.source_id for entry in result.references] == ["src_a", "src_b"]


def test_first_appearance_including_executive_summary_controls_numbering():
    raw = report(summary=[paragraph("c")], findings=[paragraph("ab")], analysis=[paragraph("a")], conclusion=[paragraph("c")])
    result = render_report_with_citations(raw, graph())
    assert [(entry.source_id, entry.citation_number) for entry in result.references] == [("src_c", 1), ("src_a", 2), ("src_b", 3)]
    assert [item.citation_numbers for item in result.citations] == [[1], [2, 3], [2], [1]]
    assert result.usage.citation_markers == 5


def test_numbers_are_stable_across_runs_and_independent_of_store_insertion_order():
    raw = report(summary=[paragraph("c")], findings=[paragraph("ab")])
    first = render_report_with_citations(raw, graph())
    assert first == render_report_with_citations(raw, graph())
    assert first == render_report_with_citations(raw, graph(source_order=("unused", "c", "b", "a")))


def test_registry_reuses_numbers_and_resets_for_each_report():
    registry = CitationRegistry()
    assert registry.get_or_assign("src_b") == registry.get_or_assign("src_b") == 1
    assert registry.get_or_assign("src_a") == registry.get_number("src_a") == 2
    assert registry.all_sources_in_order() == ["src_b", "src_a"]
    assert CitationRegistry().get_or_assign("src_a") == 1
    with pytest.raises(KeyError):
        registry.get_number("missing")
    with pytest.raises(CitationIntegrityError):
        registry.get_or_assign("")
    first = render_report_with_citations(report(summary=[paragraph("c")]), graph())
    second = render_report_with_citations(report(summary=[paragraph("ab")]), graph())
    assert first.references[0].source_id == "src_c" and second.references[0].source_id == "src_a"
    assert first.references[0].citation_number == second.references[0].citation_number == 1


def test_alias_sources_resolve_to_one_canonical_reference():
    store = graph()
    store.add_source(Source(source_id="src_alias", task_id="D2", title="Later title", url="https://example.org/a#fragment"))
    store.add_evidence(Evidence(evidence_id="ev_alias", source_id="src_alias", task_id="D2", content="Additional reading"))
    store.add_claim(Claim(claim_id="claim_alias", task_id="D2", text="Additional Claim", evidence_ids=["ev_alias"]))
    assert CitationResolver(store).resolve_claims(["claim_a", "claim_alias"]) == ["src_a"]
    result = render_report_with_citations(report(findings=[paragraph("alias")]), store)
    assert len(result.references) == 1 and result.references[0].title == "Stored title a"


def test_complete_store_chain_is_used_instead_of_clipped_writer_evidence():
    from app.config import Settings
    from app.research.gap_store import GapStore
    from app.research.verification_store import VerificationStore
    from app.research.writing_context import WritingContextBuilder
    from app.schemas import ClaimRelevanceResult, VerificationResult
    store = graph()
    claim = store.add_claim(Claim(claim_id="claim_full", task_id="D1", text="Complete supported Claim",
                               evidence_ids=["ev_a1", "ev_a2", "ev_a3", "ev_b", "ev_c"]))
    verified = VerificationStore()
    verified.add_verification(VerificationResult(claim_id=claim.claim_id, task_id="D1", status="SUPPORTED", reason="Support", evidence_ids=claim.evidence_ids))
    relevant = {claim.claim_id: ClaimRelevanceResult(claim_id=claim.claim_id, task_id="D1", status="ALIGNED", reason="Alignment")}
    context = WritingContextBuilder(settings=Settings(_env_file=None)).build("Goal", store, verified, relevant, GapStore())
    assert len(context.payload["evidence"]) == 3  # excerpts contain only Source a
    raw = report(summary=[paragraph("full")])
    result = render_report_with_citations(raw, store)
    assert result.citations[0].source_ids == ["src_a", "src_b", "src_c"]
    assert len(result.references) == 3


@pytest.mark.parametrize("broken", ["claim", "evidence", "source", "empty_evidence"])
def test_broken_support_chain_fails_explicitly_without_mutating_inputs(broken):
    store = graph()
    if broken == "claim":
        del store._claims["claim_a"]
    elif broken == "evidence":
        del store._evidence["ev_a2"]
    elif broken == "source":
        del store._sources["src_a"]
    else:
        store._claims["claim_a"] = store._claims["claim_a"].model_copy(update={"evidence_ids": []})
    before = store_state(store)
    with pytest.raises(CitationIntegrityError, match="unknown|no supporting"):
        render_report_with_citations(report(), store)
    assert store_state(store) == before


def test_unused_accepted_claim_and_source_never_enter_references():
    store = graph()
    assert store.get_claim("claim_unused")
    result = render_report_with_citations(report(), store)
    assert [entry.source_id for entry in result.references] == ["src_a"]
    assert "Stored title unused" not in result.markdown


def test_gap_and_partial_limitations_and_scope_title_headings_have_no_citations():
    raw = report(limitations=[{"text": "Current coverage is limited.", "gap_ids": ["gap_open"]},
                              {"text": "This result is uncertain.", "partial_claim_ids": ["claim_ab"]}])
    result = render_report_with_citations(raw, graph())
    limits = result.markdown.split("## 局限性与未解决问题", 1)[1].split("## 结论", 1)[0]
    assert "[1]" not in limits and "[2]" not in limits
    assert [entry.source_id for entry in result.references] == ["src_a"]
    for value in (raw.title, raw.scope, raw.sections[0].heading):
        assert value + " [" not in result.markdown


def test_zero_factual_claims_allow_nonempty_store_and_omit_references(capsys):
    raw = empty_report()
    result = render_report_with_citations(raw, graph())
    assert result.markdown == render_report(raw)
    assert not result.citations and not result.references
    assert result.usage.model_dump() == dict(used_claims=0, cited_sources=0, citation_markers=0, references=0,
                                            llm_calls=0, input_tokens=0, output_tokens=0)
    assert "## References" not in result.markdown
    assert "cited_sources=0 citation_markers=0 references=0 llm_calls=0" in capsys.readouterr().out


def test_rendering_preserves_report_and_all_store_data_and_text_exactly():
    store = graph()
    raw = report(summary=[paragraph("ab", text="第一句。\n第二句含有  内部空格！")])
    original, state = copy.deepcopy(raw.model_dump()), store_state(store)
    rendered = render_report_with_citations(raw, store)
    assert "第一句。\n第二句含有  内部空格！ [1][2]" in rendered.markdown
    assert raw.model_dump() == original and store_state(store) == state
    assert "claim_a" not in rendered.markdown and "ev_a1" not in rendered.markdown and "src_a" not in rendered.markdown


def test_metadata_is_only_from_store_dates_are_optional_and_not_inferred():
    result = render_report_with_citations(report(summary=[paragraph("c")]), graph())
    assert result.references[0].published_at is None
    assert "[1] Stored title c. https://example.org/c" in result.markdown
    assert "[2] Stored title a. 2025-01-01. https://example.org/a" in result.markdown
    assert "authors" not in type(result.references[0]).model_fields


def test_reference_metadata_cannot_add_sections_or_fake_numeric_markers():
    store = graph()
    store._sources["src_a"] = store._sources["src_a"].model_copy(update={"title": "A [9]\n## References"})
    result = render_report_with_citations(report(), store)
    assert result.references[0].title == "A [9]\n## References"
    assert "A \\[9\\] ## References" in result.markdown
    assert result.markdown.count("\n\n## References\n\n") == 1
    validate_citations(report(), result, store)


def test_debug_mapping_includes_only_claims_that_actually_use_each_source():
    store = graph()
    raw = report(findings=[paragraph("ab")])
    rendered = render_report_with_citations(raw, store, debug=True)
    mapping = rendered.markdown.split("## Citation Mapping", 1)[1]
    assert "citation_number=2 source_id=src_b\nclaim_ids: claim_ab" in mapping
    assert "claim_ids: claim_a, claim_ab" in mapping
    assert "ev_a1" not in rendered.markdown
    validate_citations(raw, rendered, store, debug=True)


@pytest.mark.parametrize("empty", [False, True])
def test_citation_phase_cannot_call_models_or_network(monkeypatch, empty):
    import app.llm as llm
    import requests
    def forbidden(*args, **kwargs):
        pytest.fail("Citation rendering must not invoke any model or network tool")
    for name in ("create_llm", "create_writer_llm", "create_verifier_llm"):
        monkeypatch.setattr(llm, name, forbidden)
    monkeypatch.setattr(requests, "post", forbidden)
    monkeypatch.setattr(requests, "get", forbidden)
    result = render_report_with_citations(empty_report() if empty else report(), graph())
    assert result.usage.llm_calls == result.usage.input_tokens == result.usage.output_tokens == 0


@pytest.mark.parametrize("change", ["missing_reference", "orphan_reference", "duplicate_number", "skipped_number", "duplicate_source",
                                   "wrong_claim", "wrong_source", "wrong_number", "missing_element", "duplicate_element",
                                   "missing_marker", "extra_marker", "changed_text", "changed_metadata", "changed_bibliography", "wrong_usage"])
def test_post_render_validation_detects_broken_mapping_and_markdown(change):
    store = graph()
    raw = report(summary=[paragraph("ab")])
    result = render_report_with_citations(raw, store)
    refs, cites = list(result.references), list(result.citations)
    updates = {}
    if change == "missing_reference": refs.pop()
    if change == "orphan_reference":
        refs.append(refs[0].model_copy(update={"citation_number": 3, "source_id": "src_c"}))
    if change == "duplicate_number": refs[1] = refs[1].model_copy(update={"citation_number": 1})
    if change == "skipped_number": refs[1] = refs[1].model_copy(update={"citation_number": 3})
    if change == "duplicate_source": refs[1] = refs[1].model_copy(update={"source_id": "src_a"})
    if change == "wrong_claim": cites[0] = cites[0].model_copy(update={"claim_ids": ["claim_c"]})
    if change == "wrong_source": cites[0] = cites[0].model_copy(update={"source_ids": ["src_c"]})
    if change == "wrong_number": cites[0] = cites[0].model_copy(update={"citation_numbers": [2, 1]})
    if change == "missing_element": cites.pop()
    if change == "duplicate_element": cites.append(cites[0])
    if change == "missing_marker": updates["markdown"] = result.markdown.replace(" [1][2]", "", 1)
    if change == "extra_marker": updates["markdown"] = result.markdown.replace(" [1][2]", " [1][2][9]", 1)
    if change == "changed_text": updates["markdown"] = result.markdown.replace("Existing factual paragraph.", "New unsupported fact.", 1)
    if change == "changed_metadata": refs[0] = refs[0].model_copy(update={"title": "Invented publication"})
    if change == "changed_bibliography": updates["markdown"] = result.markdown + "\n\n[3] Invented source."
    if change == "wrong_usage": updates["usage"] = result.usage.model_copy(update={"citation_markers": 99})
    broken = result.model_copy(update={"references": refs, "citations": cites, **updates})
    with pytest.raises(CitationIntegrityError):
        validate_citations(raw, broken, store)


def test_preexisting_numeric_citation_is_rejected_without_guessing_or_rewriting():
    raw = report(summary=[paragraph("a", text="Already numbered [9].")])
    with pytest.raises(CitationIntegrityError, match="already contains"):
        render_report_with_citations(raw, graph())


@pytest.mark.parametrize("mode", ["positive", "empty", "broken"])
def test_default_cli_pipeline_renders_after_writer_and_never_restarts_on_citation_error(mode, monkeypatch, capsys):
    from app.agents.relevance import RelevanceUsage
    from app.agents.verifier import VerificationUsage
    from app.agents.writer import WriterUsage
    from app.config import Settings
    from app.research.gap_store import GapStore
    from app.research.orchestrator import ResearchOrchestrator
    from app.research.writing_context import WritingContextBuilder
    from app.schemas import ClaimRelevanceResult, CompactResearchResult, ResearchExecutionResult, ResearchPlan, ResearchTask, TaskEvidenceBundle, VerificationResult
    cfg = Settings(_env_file=None)
    monkeypatch.setattr("app.research.orchestrator.get_settings", lambda: cfg)
    store = graph()
    bundle = TaskEvidenceBundle(task_id="D1", sources=list(store._sources.values()), evidence=list(store._evidence.values()), claims=store.all_claims())
    events = []
    def research(**kwargs):
        events.append("research")
        return ResearchExecutionResult(memo="Existing memo", compact=CompactResearchResult(task_id="D1", summary="Summary"),
                                       tools_used=[], evidence_bundle=bundle)
    verifier = SimpleNamespace(last_usage=VerificationUsage(), verify_batch=lambda claims, store: [
        VerificationResult(claim_id=c.claim_id, task_id=c.task_id, status="SUPPORTED", reason="Support", evidence_ids=c.evidence_ids) for c in claims])
    def detect(plan, claims, verified, **kwargs):
        return SimpleNamespace(relevance_results={c.claim_id: ClaimRelevanceResult(claim_id=c.claim_id, task_id=c.task_id,
            status="MISALIGNED" if mode == "empty" else "ALIGNED", reason="Alignment") for c in claims},
            relevance_usage=RelevanceUsage(), gap_store=GapStore())
    def write(context):
        events.append("writer")
        if mode == "empty":
            assert not context.accepted_claims
            return empty_report()
        return report(summary=[paragraph("missing")]) if mode == "broken" else report()
    writer = SimpleNamespace(write=write, last_usage=WriterUsage(), last_grounding=())
    task = ResearchTask(id="D1", title="Research", question="Goal", description="Task")
    orchestrator = ResearchOrchestrator(SimpleNamespace(create_plan=lambda query: ResearchPlan(goal=query, tasks=[task])),
        SimpleNamespace(research=research), verifier=verifier, gap_detector=SimpleNamespace(detect=detect),
        reresearch=False, writer=writer, writing_context_builder=WritingContextBuilder(settings=cfg))
    if mode == "broken":
        with pytest.raises(RuntimeError, match="Citation failed.*unknown claim_id"):
            orchestrator.run("Goal")
        output = capsys.readouterr().out
        assert "[Citation] failed" in output and "FINAL RESEARCH REPORT" not in output
    else:
        result = orchestrator.run("Goal")
        output = capsys.readouterr().out
        assert result.report and result.rendered_report and result.citation_usage.llm_calls == 0
        assert output.index("[Citation] resolved") < output.index("FINAL RESEARCH REPORT")
        assert ("## References" in output.split("FINAL RESEARCH REPORT", 1)[1]) == (mode == "positive")
        assert result.citation_usage.cited_sources == (1 if mode == "positive" else 0)
        assert result.evidence_store.all_claims() == bundle.claims
    assert events == ["research", "writer"]
