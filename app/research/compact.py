"""Deterministically compress the final memo without touching tool history."""

import re

from app.schemas.research_result import CompactResearchResult, SourceSummary


MAX_COMPACT_CHARS = 3000
_HEADING = re.compile(r"(?m)^##\s+(.+?)\s*$")
_SOURCE_HEADING = re.compile(r"(?m)^###\s+(.+?)\s*$")
_URL = re.compile(r"https?://[^\s)>\]]+")


def _clip(value: str, limit: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _sections(memo: str) -> dict[str, str]:
    matches = list(_HEADING.finditer(memo))
    return {
        match.group(1): memo[match.end() : matches[index + 1].start() if index + 1 < len(matches) else len(memo)]
        for index, match in enumerate(matches)
    }


def _field(block: str, *names: str) -> str | None:
    for line in block.splitlines():
        clean = re.sub(r"^\s*[-*]\s*", "", line).replace("**", "").strip()
        for name in names:
            match = re.match(rf"^{re.escape(name)}\s*[:：]\s*(.*)$", clean, re.IGNORECASE)
            if match:
                return match.group(1).strip().strip("<>") or None
    return None


def compact_from_memo(task_id: str, memo: str) -> CompactResearchResult:
    """Extract short findings and source leads from the human-facing Markdown memo."""
    sections = _sections(memo)
    findings_text = sections.get("主要发现", sections.get("Key Findings", ""))
    findings: list[str] = []
    current_heading = ""
    for line in findings_text.splitlines():
        line = line.strip()
        if line.startswith("### "):
            current_heading = line[4:].strip(' "')
            continue
        if not line:
            continue
        line = re.sub(r"^(?:[-*]\s+|\d+[.)]\s*)", "", line).replace("**", "")
        finding = f"{current_heading}: {line}" if current_heading else line
        if finding and finding not in findings:
            findings.append(_clip(finding, 360))
        if len(findings) >= 6:
            break

    source_section = sections.get("重要来源", sections.get("Important Sources", ""))
    headings = list(_SOURCE_HEADING.finditer(source_section))
    sources: list[SourceSummary] = []
    seen_urls: set[str] = set()
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(source_section)
        block = source_section[heading.end():end]
        url_text = _field(block, "URL") or ""
        url_match = _URL.search(url_text)
        if not url_match:
            continue
        url = url_match.group()
        if url in seen_urls or len(url) > 1000:
            continue
        seen_urls.add(url)
        title = re.sub(r"^#+\s*", "", heading.group(1)).strip()
        title = re.sub(r"^\d+[.)]\s*", "", title).strip(' "')
        year = _field(block, "年份", "日期", "Year", "Date")
        point = _field(block, "关键点", "关键摘录", "Key Point", "Key Extract")
        sources.append(SourceSummary(
            title=_clip(title or url, 180),
            url=url,
            year=_clip(year, 32) if year and year.casefold() not in {"unknown", "未知"} else None,
            key_point=_clip(point, 240) if point else None,
        ))
        if len(sources) >= 6:
            break

    uncertainties: list[str] = []
    for line in sections.get("不确定性与缺失信息", sections.get("Uncertainties / Missing Information", "")).splitlines():
        line = re.sub(r"^\s*(?:[-*]\s+|\d+[.)]\s*)", "", line).replace("**", "").strip()
        if line:
            uncertainties.append(_clip(line, 180))
        if len(uncertainties) >= 4:
            break

    summary = _clip(findings[0] if findings else sections.get("研究任务", sections.get("Research Task", "")), 420)
    compact = CompactResearchResult(
        task_id=task_id, summary=summary, key_findings=findings,
        sources=sources, uncertainties=uncertainties,
    )
    return bound_compact(compact)


def bound_compact(compact: CompactResearchResult) -> CompactResearchResult:
    """Retain the existing public size limit after deterministic rendering."""
    while len(compact.model_dump_json()) > MAX_COMPACT_CHARS:
        if compact.uncertainties:
            compact.uncertainties.pop()
        elif len(compact.key_findings) > 2:
            compact.key_findings.pop()
        elif any(source.key_point for source in compact.sources):
            next(source for source in reversed(compact.sources) if source.key_point).key_point = None
        elif len(compact.sources) > 1:
            compact.sources.pop()
        elif compact.key_findings:
            compact.key_findings.pop()
        elif len(compact.summary) > 80:
            compact.summary = _clip(compact.summary, 80)
        else:
            break
    return compact
