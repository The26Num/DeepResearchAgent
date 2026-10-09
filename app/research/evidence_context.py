"""Bounded, complete upstream provenance groups, without page bodies or global writes."""

import json
from dataclasses import dataclass
from itertools import zip_longest

from app.research.evidence_store import EvidenceStore
from app.schemas import Claim, Evidence, Source
from app.schemas.task import ResearchTask


MAX_EVIDENCE_CONTEXT_CHARS = 12000


def build_evidence_context(task: ResearchTask, store: EvidenceStore,
                           dependencies: list[str] | None = None) -> "EvidenceContext":
    """Select only explicitly permitted dependency tasks and complete chains."""
    return select_evidence_context(store, task.depends_on if dependencies is None else dependencies)


@dataclass(frozen=True)
class EvidenceContext:
    sources: tuple[Source, ...] = ()
    evidence: tuple[Evidence, ...] = ()
    claims: tuple[Claim, ...] = ()

    def payload(self) -> dict:
        source_indexes = {item.source_id: index for index, item in enumerate(self.sources)}
        evidence_indexes = {item.evidence_id: index for index, item in enumerate(self.evidence)}
        return {
            "sources": [{"source_index": index, "url": item.url, "title": item.title,
                         "source_type": item.source_type, "task_id": item.task_id,
                         "published_at": item.published_at}
                        for index, item in enumerate(self.sources)],
            "evidence": [{"evidence_index": index, "source_index": source_indexes[item.source_id],
                          "task_id": item.task_id, "content": item.content}
                         for index, item in enumerate(self.evidence)],
            "claims": [{"task_id": item.task_id, "text": item.text,
                        "supporting_evidence_indexes": [evidence_indexes[key] for key in item.evidence_ids]}
                       for item in self.claims],
        }

    def to_json(self) -> str:
        return json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"))


def select_evidence_context(
    store: EvidenceStore, dependencies: list[str], max_chars: int = MAX_EVIDENCE_CONTEXT_CHARS,
) -> EvidenceContext:
    """Round-robin direct dependencies; include a claim only with its entire chain.

    A synthesis dependency can itself reference discovery evidence. Following the
    Store's references preserves those original IDs without copying page bodies.
    """
    groups = [store.get_claims_for_task(task_id) for task_id in dependencies]
    selected = EvidenceContext()
    for row in zip_longest(*groups):
        for claim in row:
            if claim is None:
                continue
            sources = {item.source_id: item for item in selected.sources}
            evidence = {item.evidence_id: item for item in selected.evidence}
            for item in store.get_evidence_for_claim(claim.claim_id):
                evidence[item.evidence_id] = item
                source = store.get_source_for_evidence(item.evidence_id)
                sources[source.source_id] = source
            candidate = EvidenceContext(tuple(sources.values()), tuple(evidence.values()), (*selected.claims, claim))
            if len(candidate.to_json()) <= max_chars and len(candidate.evidence) <= 24 and len(candidate.claims) <= 12:
                selected = candidate
    return selected
