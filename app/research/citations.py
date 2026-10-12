"""Deterministic Claim -> Evidence -> canonical Source resolution and numbering."""

from app.research.evidence_store import EvidenceStore
from app.schemas.citation import ElementCitation, ReferenceEntry
from app.schemas.report import ResearchReport


class CitationIntegrityError(ValueError):
    """A report citation cannot be resolved or its rendered associations are invalid."""


class CitationResolver:
    def __init__(self, evidence_store: EvidenceStore):
        self.store = evidence_store

    def resolve_claims(self, claim_ids: list[str]) -> list[str]:
        """Preserve Claim/Evidence order, deduplicating canonical Source identities."""
        sources = {}
        for claim_id in dict.fromkeys(claim_ids):
            try:
                claim = self.store.get_claim(claim_id)
            except KeyError as error:
                raise CitationIntegrityError(f"Report references unknown claim_id: {claim_id}.") from error
            if not claim.evidence_ids:
                raise CitationIntegrityError(f"Claim {claim_id} has no supporting Evidence.")
            for evidence_id in claim.evidence_ids:
                try:
                    evidence = self.store.get_evidence(evidence_id)
                except KeyError as error:
                    raise CitationIntegrityError(f"Claim {claim_id} references unknown evidence_id: {evidence_id}.") from error
                try:
                    source = self.store.get_source(evidence.source_id)
                except KeyError as error:
                    raise CitationIntegrityError(f"Evidence {evidence_id} references unknown source_id: {evidence.source_id}.") from error
                sources.setdefault(source.source_id, None)
        return list(sources)


class CitationRegistry:
    """One registry per report; insertion order is first citation order."""
    def __init__(self):
        self._numbers: dict[str, int] = {}

    def get_or_assign(self, source_id: str) -> int:
        if not source_id:
            raise CitationIntegrityError("Cannot assign a citation number to an empty source_id.")
        if source_id not in self._numbers:
            self._numbers[source_id] = len(self._numbers) + 1
        return self._numbers[source_id]

    def get_number(self, source_id: str) -> int:
        return self._numbers[source_id]

    def all_sources_in_order(self) -> list[str]:
        return list(self._numbers)


def report_elements(report: ResearchReport):
    """Match the reading order of the Markdown view, including its summary."""
    for field in ("executive_summary", "key_findings"):
        for index, item in enumerate(getattr(report, field)):
            yield f"{field}.{index}", item
    for index, section in enumerate(report.sections):
        for paragraph_index, item in enumerate(section.paragraphs):
            yield f"sections.{index}.paragraphs.{paragraph_index}", item
    for index, item in enumerate(report.conclusion):
        yield f"conclusion.{index}", item
    # Current limitations have gap_ids/partial_claim_ids, not factual claim_ids.
    # Research scope, title and section headings are never assigned citations.


def resolve_report_citations(report: ResearchReport, store: EvidenceStore) -> tuple[list[ElementCitation], list[ReferenceEntry]]:
    resolver, registry = CitationResolver(store), CitationRegistry()
    citations = []
    for path, item in report_elements(report):
        if not item.claim_ids:
            raise CitationIntegrityError(f"Factual report element {path} has no Claim references.")
        source_ids = resolver.resolve_claims(item.claim_ids)
        if not source_ids:
            raise CitationIntegrityError(f"Factual report element {path} has no cited Sources.")
        citations.append(ElementCitation(path=path, claim_ids=list(item.claim_ids), source_ids=source_ids,
            citation_numbers=[registry.get_or_assign(source_id) for source_id in source_ids]))
    references = []
    for source_id in registry.all_sources_in_order():
        source = store.get_source(source_id)
        references.append(ReferenceEntry(citation_number=registry.get_number(source_id), source_id=source.source_id,
            title=source.title, url=source.url, source_type=source.source_type, published_at=source.published_at))
    return citations, references
