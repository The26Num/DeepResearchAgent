"""Static Phase 3A demo: python demo_evidence.py (no LLM, network or persistence)."""

from app.research.evidence_store import EvidenceStore
from app.schemas import Claim, Evidence, Source, TaskEvidenceBundle


def build_demo_store() -> EvidenceStore:
    store = EvidenceStore()
    for task_id, paper, fragment, conclusion in (
        ("D1", "A", "Paper A describes a reward-shaping method.", "Paper A uses reward shaping."),
        ("D2", "B", "Paper B describes a sequence-modelling method.", "Paper B uses sequence modelling."),
    ):
        source = Source(task_id=task_id, url=f"https://example.org/paper-{paper.lower()}", title=f"Paper {paper}", source_type="paper")
        evidence = Evidence(task_id=task_id, source_id=source.source_id, content=fragment, location="Methods")
        claim = Claim(task_id=task_id, text=conclusion, evidence_ids=[evidence.evidence_id])
        store.merge(TaskEvidenceBundle(task_id=task_id, sources=[source], evidence=[evidence], claims=[claim]))
    return store


def main() -> None:
    store = build_demo_store()
    counts = store.counts
    print(f"Sources: {counts['sources']}\nEvidence: {counts['evidence']}\nClaims: {counts['claims']}")
    for task_id in ("D1", "D2"):
        for claim in store.get_claims_for_task(task_id):
            for evidence in store.get_evidence_for_claim(claim.claim_id):
                source = store.get_source_for_evidence(evidence.evidence_id)
                assert evidence.evidence_id in claim.evidence_ids
                assert evidence.source_id == source.source_id
                print(f"{task_id}: Claim {claim.claim_id} -> Evidence {evidence.evidence_id} -> Source {source.source_id} ({source.url})")


if __name__ == "__main__":
    main()
