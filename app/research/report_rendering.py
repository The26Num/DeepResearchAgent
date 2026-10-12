"""Markdown views over unchanged report text, with optional deterministic citations."""

import re

from app.schemas.report import ResearchReport
from app.schemas.citation import CitationUsage, RenderedResearchReport
from app.research.evidence_store import EvidenceStore
from app.research.citations import CitationIntegrityError, CitationResolver, report_elements, resolve_report_citations


_MARKER = re.compile(r"(?<!\\)\[(\d+)\]")


def render_report(report: ResearchReport, *, debug: bool = False) -> str:
    """Keep the Phase 5A plain view available for existing callers."""
    return _report_body(report, debug=debug)


def _report_body(report: ResearchReport, *, debug=False, citations=None) -> str:
    citations = citations or {}

    def paragraph(item, path):
        text = item.text
        if path in citations:
            text += " " + "".join(f"[{number}]" for number in citations[path].citation_numbers)
        if debug:
            text += "\n  claim_ids: " + ", ".join(item.claim_ids)
        return text

    parts = ["# " + report.title, "## 执行摘要"]
    parts.extend(paragraph(item, f"executive_summary.{i}") for i, item in enumerate(report.executive_summary))
    if not report.executive_summary:
        parts.append("当前研究没有获得足够支持且与目标对齐的结论。")
    parts += ["## 研究范围", report.scope, "## 主要发现"]
    parts.extend("- " + paragraph(item, f"key_findings.{i}") for i, item in enumerate(report.key_findings))
    if not report.key_findings:
        parts.append("当前没有可作为确定性发现的合格结论。")
    parts.append("## 详细分析")
    for i, section in enumerate(report.sections):
        parts.append("### " + section.heading)
        parts.extend(paragraph(item, f"sections.{i}.paragraphs.{j}") for j, item in enumerate(section.paragraphs))
    if not report.sections:
        parts.append("现有合格材料不足以展开进一步分析。")
    parts.append("## 局限性与未解决问题")
    for item in report.limitations:
        text = "- " + item.text
        if debug:
            text += f"\n  gap_ids: {item.gap_ids}\n  partial_claim_ids: {item.partial_claim_ids}"
        parts.append(text)
    if not report.limitations:
        parts.append("当前记录未提供其他未解决缺口；这不代表研究已穷尽。")
    parts.append("## 结论")
    parts.extend(paragraph(item, f"conclusion.{i}") for i, item in enumerate(report.conclusion))
    if not report.conclusion:
        parts.append("当前材料不足以形成确定性结论，应保留上述研究缺口。")
    return "\n\n".join(parts)


def _metadata_text(text: str) -> str:
    # Metadata can contain line breaks or literal brackets. Keep them from
    # creating additional report sections/citation markers; never alter the Store.
    return re.sub(r"([\\\[\]])", r"\\\1", " ".join(text.split()))


def _reference_text(entry) -> str:
    fields = [entry.title]
    if entry.published_at:
        fields.append(entry.published_at)
    fields.append(entry.url)
    return f"[{entry.citation_number}] " + ". ".join(_metadata_text(field) for field in fields)


def _cited_markdown(report, citations, references, *, debug=False, store=None):
    body = _report_body(report, debug=debug, citations={item.path: item for item in citations})
    if references:
        body += "\n\n## References\n\n" + "\n\n".join(_reference_text(entry) for entry in references)
    if debug and references:
        resolver = CitationResolver(store)
        used = report.used_claim_ids
        mapping = []
        for entry in references:
            claims = [key for key in used if entry.source_id in resolver.resolve_claims([key])]
            mapping.append(f"citation_number={entry.citation_number} source_id={entry.source_id}\n"
                           + "claim_ids: " + ", ".join(claims))
        body += "\n\n## Citation Mapping\n\n" + "\n\n".join(mapping)
    return body


def validate_citations(report: ResearchReport, rendered: RenderedResearchReport, store: EvidenceStore, *, debug=False) -> None:
    """Check both the graph associations and the actual rendered Markdown."""
    elements = list(report_elements(report))
    references = rendered.references
    numbers = [entry.citation_number for entry in references]
    source_ids = [entry.source_id for entry in references]
    if numbers != list(range(1, len(references) + 1)) or len(set(source_ids)) != len(source_ids):
        raise CitationIntegrityError("References must have unique Sources and consecutive citation numbers starting at 1.")
    if [item.path for item in rendered.citations] != [path for path, _ in elements]:
        raise CitationIntegrityError("Every factual report element must have exactly one citation association in reading order.")
    resolver = CitationResolver(store)
    registry_order = []
    lookup = {entry.source_id: entry.citation_number for entry in references}
    for (_, item), citation in zip(elements, rendered.citations):
        resolved = resolver.resolve_claims(item.claim_ids)
        if (citation.claim_ids != item.claim_ids or citation.source_ids != resolved
                or len(set(citation.citation_numbers)) != len(citation.citation_numbers)
                or citation.citation_numbers != [lookup.get(key) for key in resolved]):
            raise CitationIntegrityError("Citation numbers or Sources do not match the element's complete Claim support chains.")
        registry_order.extend(key for key in resolved if key not in registry_order)
    if registry_order != source_ids:
        raise CitationIntegrityError("References must contain only cited Sources, in first appearance order.")
    for entry in references:
        try:
            source = store.get_source(entry.source_id)
        except KeyError as error:
            raise CitationIntegrityError("Reference points to an unknown Source.") from error
        if (entry.source_id, entry.title, entry.url, entry.source_type, entry.published_at) != (
                source.source_id, source.title, source.url, source.source_type, source.published_at):
            raise CitationIntegrityError("Reference metadata must exactly match the stored canonical Source.")
    body = rendered.markdown.split("\n\n## References\n\n", 1)[0]
    actual_markers = [int(match) for match in _MARKER.findall(body)]
    expected_markers = [number for item in rendered.citations for number in item.citation_numbers]
    if actual_markers != expected_markers or set(actual_markers) != set(numbers):
        raise CitationIntegrityError("Rendered citation markers have missing, unexpected or unreferenced numbers.")
    usage = CitationUsage(used_claims=len(report.used_claim_ids), cited_sources=len(references),
                          citation_markers=len(expected_markers), references=len(references))
    if rendered.usage != usage:
        raise CitationIntegrityError("Citation usage does not match the rendered associations.")
    if rendered.markdown != _cited_markdown(report, rendered.citations, references, debug=debug, store=store):
        raise CitationIntegrityError("Rendered text or bibliography differs from the deterministic report view.")


def render_report_with_citations(report: ResearchReport, store: EvidenceStore, *, debug=False) -> RenderedResearchReport:
    """Append numeric markers and stored bibliography metadata; make zero LLM calls."""
    if _MARKER.search(render_report(report)):
        raise CitationIntegrityError("Raw ResearchReport already contains numeric citation markers; do not guess or renumber them.")
    citations, references = resolve_report_citations(report, store)
    rendered = RenderedResearchReport(markdown=_cited_markdown(report, citations, references, debug=debug, store=store),
        citations=citations, references=references,
        usage=CitationUsage(used_claims=len(report.used_claim_ids), cited_sources=len(references),
                            citation_markers=sum(len(item.citation_numbers) for item in citations), references=len(references)))
    validate_citations(report, rendered, store, debug=debug)
    usage = rendered.usage
    print(f"[Citation] resolved: used_claims={usage.used_claims} cited_sources={usage.cited_sources} "
          f"citation_markers={usage.citation_markers} references={usage.references} llm_calls=0 input_tokens=0 output_tokens=0", flush=True)
    return rendered
