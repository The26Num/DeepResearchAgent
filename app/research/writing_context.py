"""Read-only selection and bounded compact views for the report consumer."""

import json
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.research.evidence_store import EvidenceStore
from app.research.gap_store import GapStore
from app.research.verification_store import VerificationStore
from app.schemas.gap import GapStatus
from app.schemas.plan import ResearchPlan
from app.schemas.relevance import ClaimRelevanceResult, RelevanceStatus
from app.schemas.verification import VerificationStatus


class WritingIntegrityError(ValueError):
    pass


class WritingContextLimitError(ValueError):
    pass


@dataclass(frozen=True)
class WritingClaim:
    claim_id: str
    text: str
    evidence_ids: tuple[str, ...]
    source_ids: tuple[str, ...]
    origin_task_id: str


@dataclass(frozen=True)
class WritingContext:
    goal: str
    accepted_claims: tuple[WritingClaim, ...]
    text: str

    @property
    def payload(self) -> dict:
        # Return a fresh view: callers cannot mutate the validated model input.
        return json.loads(self.text)

    @property
    def context_chars(self) -> int:
        return len(self.text)


class WritingContextBuilder:
    def __init__(self, *, settings: Settings | None = None):
        self.settings = settings or get_settings()

    def build(self, goal: str, evidence_store: EvidenceStore, verification_store: VerificationStore,
              relevance_results: dict[str, ClaimRelevanceResult], gap_store: GapStore,
              *, plan: ResearchPlan | None = None) -> WritingContext:
        if not goal.strip():
            raise WritingIntegrityError("Writer requires the original research goal.")
        accepted, partial, fragments, sources = [], [], {}, {}
        for claim in evidence_store.all_claims():
            verified = verification_store.get_latest_verification(claim.claim_id)
            relevant = relevance_results.get(claim.claim_id)
            # Supplemental work may exhaust its budget before verification. Fail
            # closed for these claims, without preventing use of verified siblings.
            if verified is None or relevant is None:
                continue
            if (verified.claim_id != claim.claim_id or relevant.claim_id != claim.claim_id
                    or verified.task_id != claim.task_id or relevant.task_id != claim.task_id
                    or set(verified.evidence_ids) != set(claim.evidence_ids)):
                raise WritingIntegrityError(f"Writer found stale/mismatched decisions for {claim.claim_id}.")
            if verified.status == VerificationStatus.UNSUPPORTED or relevant.status == RelevanceStatus.MISALIGNED:
                continue
            if verified.status == VerificationStatus.PARTIAL or relevant.status == RelevanceStatus.PARTIAL:
                partial.append({"claim_id": claim.claim_id, "text": claim.text[:240],
                    "verification_status": verified.status.value, "relevance_status": relevant.status.value,
                    "verification_reason": verified.reason, "relevance_reason": relevant.reason})
                continue
            if not (verified.status == VerificationStatus.SUPPORTED and relevant.status == RelevanceStatus.ALIGNED):
                continue
            try:
                linked = evidence_store.get_evidence_for_claim(claim.claim_id)
                linked_sources = [evidence_store.get_source_for_evidence(item.evidence_id) for item in linked]
            except KeyError as error:
                raise WritingIntegrityError(f"Accepted Claim has a broken Evidence/Source chain: {claim.claim_id}.") from error
            if not linked:
                raise WritingIntegrityError("Accepted Claims must have at least one Evidence.")
            fragments.update((item.evidence_id, item) for item in linked)
            sources.update((item.source_id, item) for item in linked_sources)
            accepted.append(WritingClaim(claim.claim_id, claim.text, tuple(claim.evidence_ids),
                tuple(dict.fromkeys(item.source_id for item in linked_sources)), claim.task_id))
        gaps = [{"gap_id": item.gap_id, "status": item.status.value, "gap_type": item.gap_type.value,
                 "description": item.description[:240], "missing_information": item.missing_information[:400]}
                for item in gap_store.all_gaps() if item.status in {GapStatus.OPEN, GapStatus.IMPROVED}]
        dimensions = []
        if plan is not None:
            scope_tasks = [task for task in plan.tasks if task.task_type == "discovery"] or plan.tasks
            dimensions = list(dict.fromkeys(task.question for task in scope_tasks))

        def compose(per_claim, excerpt_chars):
            claims, evidence, metadata = [], {}, {}
            for item in accepted:
                selected = item.evidence_ids[:per_claim]
                selected_sources = list(dict.fromkeys(fragments[key].source_id for key in selected))
                claims.append({"claim_id": item.claim_id, "text": item.text, "evidence_ids": list(selected),
                               "source_ids": selected_sources, "origin_task_id": item.origin_task_id})
                for key in selected:
                    fragment = fragments[key]
                    evidence[key] = {"source_id": fragment.source_id, "content": fragment.content[:excerpt_chars],
                                     "excerpt_truncated": len(fragment.content) > excerpt_chars}
                    source = sources[fragment.source_id]
                    metadata[source.source_id] = {"title": source.title[:300], "url": source.url,
                                                   "published_at": source.published_at}
            return {"goal": goal, "scope_dimensions": dimensions, "accepted_claims": claims,
                    "evidence": evidence, "sources": metadata, "open_gaps": gaps,
                    "partial_claims_for_limitations_only": partial}

        # Keep every accepted Claim text intact. First reduce repeated supporting
        # fragments, then excerpt length; never edit full evidence_ids in the Store.
        max_evidence = self.settings.max_evidence_per_writing_claim
        max_excerpt = self.settings.max_writing_evidence_chars
        attempts = [(count, max_excerpt) for count in range(max_evidence, 0, -1)]
        attempts += [(1, size) for size in (240, 160, 80) if size < max_excerpt]
        for per_claim, excerpt_chars in attempts:
            payload = compose(per_claim, excerpt_chars)
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            # A deterministic insufficient-material report never sends this view
            # to a model, so its gap list need not fit an LLM input budget.
            if not accepted or len(text) <= self.settings.max_writer_context_chars:
                return WritingContext(goal, tuple(accepted), text)
        raise WritingContextLimitError(
            "Writer context exceeds MAX_WRITER_CONTEXT_CHARS even with one short Evidence per Claim; "
            "accepted Claims and the research goal were not dropped or truncated.")
