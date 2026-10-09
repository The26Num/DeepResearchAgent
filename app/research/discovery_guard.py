"""Discovery reading recovery and output provenance, without claim verification."""

import re
from urllib.parse import urlsplit, urlunsplit

from app.research.evidence_context import EvidenceContext
from app.schemas import CompactResearchResult, ResearchTask, TaskEvidenceBundle


def readable_source_urls(url: str) -> list[str]:
    """Known PDF -> HTML/abstract conventions; never invent general page URLs."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return [url]  # The reader reports malformed input as a failed reading.
    host, path = (parsed.hostname or "").lower(), parsed.path
    paths = []
    if host == "arxiv.org" and path.startswith("/pdf/"):
        paper = path[5:]
        if paper.endswith(".pdf"):
            paper = paper[:-4]
        if re.fullmatch(r"(?:\d{4}\.\d{4,5}|[a-zA-Z.-]+/\d{7})(?:v\d+)?", paper):
            paths = ["/html/" + paper, "/abs/" + paper]
    elif host == "proceedings.mlr.press":
        match = re.fullmatch(r"/(v\d+)/([^/]+)/([^/]+)\.pdf", path)
        if match and match.group(2) == match.group(3):
            paths = [f"/{match.group(1)}/{match.group(2)}.html"]
    elif host == "openreview.net" and path == "/pdf" and parsed.query:
        paths = ["/forum"]
    if paths:
        return [urlunsplit((parsed.scheme, parsed.netloc, value, parsed.query, "")) for value in paths]
    return [url]


def finalize_discovery(
    task: ResearchTask, bundle: TaskEvidenceBundle, upstream: EvidenceContext,
    *, has_reading: bool,
) -> tuple[str, CompactResearchResult]:
    """Compatibility entry point for the shared formal-evidence renderer."""
    from app.research.evidence_report import finalize_evidence_report
    return finalize_evidence_report(task, bundle, upstream, has_reading=has_reading)
