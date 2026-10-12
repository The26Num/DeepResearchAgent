"""Only cited, complete evidence chains enter a verifier request."""

import json
from dataclasses import dataclass

from app.research.evidence_store import EvidenceStore
from app.schemas.claim import Claim


class VerificationIntegrityError(ValueError):
    """A declared Evidence/Source reference cannot be resolved."""


class VerificationContextLimitError(ValueError):
    """One complete Claim support chain exceeds the verifier context budget."""


@dataclass(frozen=True)
class VerificationContext:
    claims: tuple[Claim, ...]
    payload: dict
    text: str
    claim_ids: dict[str, str]

    @property
    def context_chars(self) -> int:
        return len(self.text)


def build_verification_context(claims: list[Claim], evidence_store: EvidenceStore) -> VerificationContext:
    evidence, sources = {}, {}
    for claim in claims:
        for key in claim.evidence_ids:
            if key in evidence:
                continue
            try:
                item = evidence_store.get_evidence(key)
                source = evidence_store.get_source(item.source_id)
            except KeyError as error:
                raise VerificationIntegrityError(
                    f"Claim {claim.claim_id} has an unresolved Evidence/Source reference: {key}."
                ) from error
            evidence[key] = {"content": item.content, "source_id": source.source_id}
            sources[source.source_id] = {"title": source.title, "url": source.url, "source_type": source.source_type}
    claim_ids = {f"C{index}": item.claim_id for index, item in enumerate(claims, 1)}
    payload = {
        "claims": [{"claim_id": label, "text": item.text, "evidence_ids": list(item.evidence_ids)}
                   for label, item in zip(claim_ids, claims)],
        "evidence": evidence,
        "sources": sources,
    }
    return VerificationContext(tuple(claims), payload,
                               json.dumps(payload, ensure_ascii=False, separators=(",", ":")), claim_ids)


def split_verification_batches(claims: list[Claim], evidence_store: EvidenceStore, *,
                               max_claims: int, max_context_chars: int) -> list[VerificationContext]:
    if max_claims < 1 or max_context_chars < 1:
        raise ValueError("Verification batch budgets must be positive.")
    batches, pending = [], []
    for claim in claims:
        candidate = build_verification_context([*pending, claim], evidence_store)
        if len(candidate.claims) <= max_claims and candidate.context_chars <= max_context_chars:
            pending.append(claim)
            continue
        if pending:
            batches.append(build_verification_context(pending, evidence_store))
        single = build_verification_context([claim], evidence_store)
        if single.context_chars > max_context_chars:
            raise VerificationContextLimitError(
                f"Claim {claim.claim_id} complete verification context has {single.context_chars} characters; "
                f"limit={max_context_chars}. Evidence was not truncated; increase MAX_VERIFICATION_CONTEXT_CHARS."
            )
        pending = [claim]
    if pending:
        batches.append(build_verification_context(pending, evidence_store))
    return batches
