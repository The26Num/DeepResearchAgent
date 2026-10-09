from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from pydantic import ValidationError

from app.research.evidence_store import EvidenceStore
from app.schemas import Claim, CompactResearchResult, Evidence, ResearchExecutionResult, Source, TaskEvidenceBundle
from app.schemas.source import normalize_source_url
from demo_evidence import build_demo_store, main as demo_main


def bundle(task_id="D1", url="https://example.org/paper", *, source_id=None):
    fields = {"source_id": source_id} if source_id else {}
    source = Source(task_id=task_id, url=url, title="Paper", source_type="paper", **fields)
    evidence = Evidence(task_id=task_id, source_id=source.source_id, content="A meaningful method description.")
    claim = Claim(task_id=task_id, text="The paper proposes this method.", evidence_ids=[evidence.evidence_id])
    return TaskEvidenceBundle(task_id=task_id, sources=[source], evidence=[evidence], claims=[claim])


def test_create_source_and_generate_internal_ids():
    source = Source(task_id="D1", url="https://example.org/paper", title="Paper")
    assert source.source_id.startswith("src_")
    assert source.source_id != source.url
    assert source.source_type == "unknown"
    assert source.published_at is None
    assert "content" not in Source.model_fields and "snippet" not in Source.model_fields
    ids = set()
    for _ in range(100):
        artifacts = bundle()
        ids.update([artifacts.sources[0].source_id, artifacts.evidence[0].evidence_id, artifacts.claims[0].claim_id])
    assert len(ids) == 300


@pytest.mark.parametrize("url,expected", [
    ("HTTPS://EXAMPLE.ORG:443/paper/#abstract", "https://example.org/paper"),
    ("http://EXAMPLE.ORG:80/", "http://example.org"),
    ("https://example.org", "https://example.org"),
    ("https://example.org/Paper/?version=1#method", "https://example.org/Paper?version=1"),
    ("https://example.org:8443/paper/", "https://example.org:8443/paper"),
    ("http://[::1]:80/paper#method", "http://[::1]/paper"),
])
def test_url_normalization(url, expected):
    assert normalize_source_url(url) == expected


@pytest.mark.parametrize("left,right", [
    ("https://arxiv.org/html/2601.08107v1", "https://arxiv.org/html/2601.08107v2"),
    ("https://example.org/paper?v=1", "https://example.org/paper?v=2"),
    ("https://example.org/Paper", "https://example.org/paper"),
    ("http://example.org/paper", "https://example.org/paper"),
])
def test_distinct_content_urls_are_not_collapsed(left, right):
    assert normalize_source_url(left) != normalize_source_url(right)


@pytest.mark.parametrize("url", ["", "relative/path", "ftp://example.org/paper", "https:///paper", "https://example.org:bad/paper", "https://example.org/a b"])
def test_invalid_source_urls_are_rejected(url):
    with pytest.raises(ValidationError):
        Source(task_id="D1", title="Paper", url=url)


def test_duplicate_url_returns_first_canonical_source():
    store = EvidenceStore()
    first = store.add_source(Source(source_id="src_first", task_id="D1", title="Original", url="https://example.org/paper"))
    second = store.add_source(Source(source_id="src_second", task_id="D2", title="Other title", url="HTTPS://EXAMPLE.ORG:443/paper/#section"))
    assert first == second
    assert second.source_id == "src_first"
    assert second.task_id == "D1" and second.title == "Original"
    assert store.counts == {"sources": 1, "evidence": 0, "claims": 0}


def test_add_evidence_and_query_source():
    store = EvidenceStore()
    source = store.add_source(Source(task_id="D1", title="Paper", url="https://example.org/paper"))
    evidence = Evidence(task_id="D1", source_id=source.source_id, content="The method uses a temporal ordering.", location="Section 3")
    assert store.add_evidence(evidence) == evidence
    assert store.get_evidence(evidence.evidence_id).location == "Section 3"
    assert store.get_source_for_evidence(evidence.evidence_id) == source


def test_evidence_cannot_reference_an_unknown_source():
    store = EvidenceStore()
    evidence = Evidence(task_id="D1", source_id="src_missing", content="A fragment")
    with pytest.raises(ValueError, match="unknown source_id: src_missing"):
        store.add_evidence(evidence)
    assert store.counts == {"sources": 0, "evidence": 0, "claims": 0}


def test_claim_can_reference_multiple_evidence_and_resolve_full_chain():
    store = EvidenceStore()
    first = bundle("D1", "https://example.org/paper-a")
    second = bundle("D2", "https://example.org/paper-b")
    store.merge(first)
    store.merge(second)
    claim = Claim(task_id="A", text="Both sources describe related methods.", evidence_ids=[
        first.evidence[0].evidence_id, second.evidence[0].evidence_id,
    ])
    store.add_claim(claim)
    linked = store.get_evidence_for_claim(claim.claim_id)
    assert linked == [first.evidence[0], second.evidence[0]]
    assert [store.get_source_for_evidence(item.evidence_id).url for item in linked] == [
        "https://example.org/paper-a", "https://example.org/paper-b",
    ]
    assert store.get_claims_for_task("A") == [claim]
    assert store.get_claims_for_task("missing-task") == []


def test_claim_cannot_reference_unknown_evidence_even_with_some_valid_references():
    store = EvidenceStore()
    existing = bundle()
    store.merge(existing)
    before = store.counts
    claim = Claim(task_id="A", text="A conclusion", evidence_ids=[existing.evidence[0].evidence_id, "ev_missing"])
    with pytest.raises(ValueError, match="unknown evidence_ids: ev_missing"):
        store.add_claim(claim)
    assert store.counts == before


@pytest.mark.parametrize("getter,identifier", [
    ("get_source", "src_missing"), ("get_evidence", "ev_missing"), ("get_claim", "claim_missing"),
    ("get_evidence_for_claim", "claim_missing"), ("get_source_for_evidence", "ev_missing"),
])
def test_missing_records_raise_clear_errors(getter, identifier):
    with pytest.raises(KeyError, match=identifier):
        getattr(EvidenceStore(), getter)(identifier)


def test_merge_two_independent_task_bundles():
    store = EvidenceStore()
    d1 = bundle("D1", "https://example.org/a")
    d2 = bundle("D2", "https://example.org/b")
    assert store.merge(d1) == d1
    assert store.merge(d2) == d2
    assert store.counts == {"sources": 2, "evidence": 2, "claims": 2}
    assert store.get_claims_for_task("D1") == d1.claims
    assert store.get_claims_for_task("D2") == d2.claims


def test_merge_remaps_same_url_to_one_source_without_mutating_local_bundles():
    store = EvidenceStore()
    d1 = bundle("D1", "https://example.org/paper", source_id="src_d1")
    d2 = bundle("D2", "https://EXAMPLE.ORG/paper/#methods", source_id="src_d2")
    d2.evidence[0] = d2.evidence[0].model_copy(update={"content": "A different method description."})
    store.merge(d1)
    merged = store.merge(d2)
    assert store.counts == {"sources": 1, "evidence": 2, "claims": 2}
    assert merged.sources[0].source_id == "src_d1"
    assert merged.sources[0].task_id == "D1"
    assert merged.evidence[0].source_id == "src_d1"
    assert d2.evidence[0].source_id == "src_d2"
    assert store.get_source_for_evidence(d2.evidence[0].evidence_id).source_id == "src_d1"
    assert store.get_claims_for_task("D2")[0].evidence_ids == [d2.evidence[0].evidence_id]


def test_merge_deduplicates_sources_inside_one_bundle():
    local = bundle(source_id="src_first")
    duplicate = Source(source_id="src_other", task_id="D1", title="Duplicate", url=local.sources[0].url + "#method")
    other_evidence = Evidence(task_id="D1", source_id=duplicate.source_id, content="A second fragment")
    local.sources.append(duplicate)
    local.evidence.append(other_evidence)
    store = EvidenceStore()
    merged = store.merge(local)
    assert store.counts == {"sources": 1, "evidence": 2, "claims": 1}
    assert len(merged.sources) == 1
    assert {item.source_id for item in merged.evidence} == {"src_first"}


def test_merge_is_idempotent_for_identical_records():
    store = EvidenceStore()
    first, second = bundle("D1"), bundle("D2")
    store.merge(first)
    canonical = store.merge(second)
    assert store.merge(second) == canonical
    assert store.merge(canonical) == canonical
    assert store.counts == {"sources": 1, "evidence": 1, "claims": 2}
    assert store.reuse_counts == {"source_reuse": 1, "evidence_reuse": 1}


def test_failed_merge_is_atomic_and_can_be_retried_after_fixing_references():
    store = EvidenceStore()
    store.merge(bundle("D1", "https://example.org/a"))
    invalid = bundle("D2", "https://example.org/b")
    invalid.claims.append(Claim(task_id="D2", text="Bad reference", evidence_ids=["ev_missing"]))
    before = store.counts
    with pytest.raises(ValueError, match="unknown evidence_ids"):
        store.merge(invalid)
    assert store.counts == before
    with pytest.raises(KeyError):
        store.get_source(invalid.sources[0].source_id)
    with pytest.raises(KeyError):
        store.get_evidence(invalid.evidence[0].evidence_id)
    with pytest.raises(KeyError):
        store.get_claim(invalid.claims[0].claim_id)
    invalid.claims.pop()
    store.merge(invalid)
    assert store.counts == {"sources": 2, "evidence": 2, "claims": 2}


@pytest.mark.parametrize("kind", ["source", "evidence", "claim"])
def test_conflicting_ids_are_rejected_without_overwriting_or_partial_inserts(kind):
    store = EvidenceStore()
    existing = bundle("D1", "https://example.org/a")
    store.merge(existing)
    incoming = bundle("D2", "https://example.org/b")
    if kind == "source":
        incoming.sources[0] = incoming.sources[0].model_copy(update={"source_id": existing.sources[0].source_id})
    elif kind == "evidence":
        incoming.evidence[0] = incoming.evidence[0].model_copy(update={"evidence_id": existing.evidence[0].evidence_id})
    else:
        incoming.claims[0] = incoming.claims[0].model_copy(update={"claim_id": existing.claims[0].claim_id})
    before = store.counts
    with pytest.raises(ValueError, match=f"Conflicting {kind}_id"):
        store.merge(incoming)
    assert store.counts == before
    assert store.get_source(existing.sources[0].source_id) == existing.sources[0]
    assert store.get_evidence(existing.evidence[0].evidence_id) == existing.evidence[0]
    assert store.get_claim(existing.claims[0].claim_id) == existing.claims[0]


def test_merge_can_reference_existing_artifacts_from_other_tasks():
    store = EvidenceStore()
    d1 = bundle()
    store.merge(d1)
    evidence = Evidence(task_id="A", source_id=d1.sources[0].source_id, content="Another observation")
    claim = Claim(task_id="A", text="A cross-task conclusion", evidence_ids=[d1.evidence[0].evidence_id, evidence.evidence_id])
    store.merge(TaskEvidenceBundle(task_id="A", evidence=[evidence], claims=[claim]))
    assert store.get_source_for_evidence(evidence.evidence_id) == d1.sources[0]
    assert len(store.get_evidence_for_claim(claim.claim_id)) == 2


def test_store_snapshots_protect_references_from_external_mutation():
    local = bundle()
    store = EvidenceStore()
    merged = store.merge(local)
    original_evidence_ids = [local.evidence[0].evidence_id]
    local.claims[0].evidence_ids.append("local_mutation")
    merged.claims[0].evidence_ids.append("merge_result_mutation")
    store.get_claim(merged.claims[0].claim_id).evidence_ids.append("getter_mutation")
    store.get_claims_for_task("D1")[0].evidence_ids.append("query_mutation")
    counts = store.counts
    counts["sources"] = 999
    assert store.get_claim(merged.claims[0].claim_id).evidence_ids == original_evidence_ids
    assert store.counts == {"sources": 1, "evidence": 1, "claims": 1}


def test_parallel_merges_have_unique_ids_deduplication_and_complete_references():
    workers = 6
    barrier = Barrier(workers, timeout=5)
    store = EvidenceStore()
    locals_ = [bundle(f"D{i}", f"https://EXAMPLE.org/paper-{i % 3}/#method") for i in range(36)]
    for index, local in enumerate(locals_):
        local.evidence[0] = local.evidence[0].model_copy(update={"content": f"Independent fact {index}."})

    def merge_batch(items):
        barrier.wait()
        return [store.merge(item) for item in items]

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(merge_batch, [locals_[i::workers] for i in range(workers)]))
    assert store.counts == {"sources": 3, "evidence": 36, "claims": 36}
    assert len({item.evidence[0].evidence_id for batch in results for item in batch}) == 36
    assert len({item.claims[0].claim_id for batch in results for item in batch}) == 36
    for local in locals_:
        linked = store.get_evidence_for_claim(local.claims[0].claim_id)
        assert linked[0].task_id == local.task_id
        source = store.get_source_for_evidence(linked[0].evidence_id)
        assert normalize_source_url(source.url) == normalize_source_url(local.sources[0].url)


def test_optional_bundle_preserves_phase_two_result_construction():
    compact = CompactResearchResult(task_id="D1", summary="Original Phase 2 result")
    result = ResearchExecutionResult(memo="Memo", compact=compact, tools_used=[])
    assert result.evidence_bundle is None
    attached = ResearchExecutionResult(memo="Memo", compact=compact, tools_used=[], evidence_bundle=bundle())
    assert attached.memo == result.memo and attached.compact == result.compact
    assert attached.evidence_bundle.task_id == "D1"


def test_bundle_defaults_are_independent_and_wrong_task_provenance_is_rejected():
    first, second = TaskEvidenceBundle(task_id="D1"), TaskEvidenceBundle(task_id="D2")
    first.sources.append(Source(task_id="D1", title="Paper", url="https://example.org/a"))
    assert second.sources == [] and second.evidence == [] and second.claims == []
    with pytest.raises(ValidationError, match="belong to its task_id"):
        TaskEvidenceBundle(task_id="D1", claims=[Claim(task_id="D2", text="Wrong task", evidence_ids=["ev_1"])])


def test_bundle_revalidates_mutated_input_before_merge():
    local = bundle()
    local.evidence.append(local.evidence[0])
    store = EvidenceStore()
    with pytest.raises(ValidationError, match="duplicate evidence_id"):
        store.merge(local)
    assert store.counts == {"sources": 0, "evidence": 0, "claims": 0}


@pytest.mark.parametrize("field,value", [("content", " "), ("content", "x" * 5000)])
def test_evidence_requires_a_bounded_nonempty_fragment(field, value):
    with pytest.raises(ValidationError):
        Evidence(source_id="src_1", task_id="D1", **{field: value})


@pytest.mark.parametrize("evidence_ids", [[""], [" "], ["ev_1", "ev_1"]])
def test_claim_reference_ids_must_be_nonempty_and_unique(evidence_ids):
    with pytest.raises(ValidationError):
        Claim(task_id="D1", text="Conclusion", evidence_ids=evidence_ids)


def test_formal_claim_requires_evidence_without_adding_verification():
    with pytest.raises(ValidationError):
        Claim(task_id="D1", text="A conclusion awaiting evidence", evidence_ids=[])
    assert not {"verified", "supported", "rejected", "confidence_score", "verifier_status"} & Claim.model_fields.keys()
    with pytest.raises(ValidationError):
        Claim(task_id="D1", text="Conclusion", verified=True)


def test_static_demo_has_two_complete_provenance_chains(capsys):
    store = build_demo_store()
    assert store.counts == {"sources": 2, "evidence": 2, "claims": 2}
    for task_id in ("D1", "D2"):
        claim = store.get_claims_for_task(task_id)[0]
        evidence = store.get_evidence_for_claim(claim.claim_id)[0]
        source = store.get_source_for_evidence(evidence.evidence_id)
        assert claim.task_id == evidence.task_id == source.task_id == task_id
    demo_main()
    output = capsys.readouterr().out
    assert "Sources: 2\nEvidence: 2\nClaims: 2" in output
    assert "D1: Claim" in output and "D2: Claim" in output
