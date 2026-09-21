"""Lightweight title-level deduplication for Markdown Important Sources."""

import re
from urllib.parse import urlparse


_SOURCE_SECTION = re.compile(r"(?im)^##\s+(?:Important Sources|重要来源)\s*$")
_NEXT_SECTION = re.compile(r"(?m)^##\s+")
_SOURCE_HEADING = re.compile(r"(?m)^###\s+(.+?)\s*$")
_URL = re.compile(r"https?://[^\s)>]+")
_OFFICIAL_HOSTS = (
    "acm.org", "ieee.org", "nature.com", "springer.com", "sciencedirect.com",
    "openreview.net", "proceedings.mlr.press", "neurips.cc", "aaai.org", "thecvf.com",
)
_MEMO_HEADINGS = {
    "Research Task", "Key Findings", "Important Sources", "Uncertainties / Missing Information",
    "研究任务", "主要发现", "重要来源", "不确定性与缺失信息",
}


def retain_memo_sections(memo: str) -> str:
    """Drop unrequested top-level sections when the model adds extra reading lists."""
    headings = list(re.finditer(r"(?m)^##\s+(.+?)\s*$", memo))
    if not headings:
        return memo
    parts = [memo[:headings[0].start()]]
    for index, heading in enumerate(headings):
        if heading.group(1) in _MEMO_HEADINGS:
            end = headings[index + 1].start() if index + 1 < len(headings) else len(memo)
            parts.append(memo[heading.start():end])
    return "".join(parts).rstrip()


def normalize_source_title(title: str) -> str:
    title = re.sub(r"^\d+[.)]\s*", "", title.strip())
    linked = re.fullmatch(r"\[(.+)\]\([^)]+\)", title)
    if linked:
        title = linked.group(1)
    return " ".join("".join(character.casefold() if character.isalnum() else " " for character in title).split())


def _source_rank(block: str) -> int:
    url_match = _URL.search(block)
    if not url_match:
        return 0
    host = (urlparse(url_match.group()).hostname or "").lower()
    if any(host == official or host.endswith("." + official) for official in _OFFICIAL_HOSTS):
        return 3
    if host == "arxiv.org" or host.endswith(".arxiv.org"):
        return 2
    return 1


def deduplicate_memo_sources(memo: str) -> str:
    """Keep one entry per normalized title when sources use ### headings."""
    section_match = _SOURCE_SECTION.search(memo)
    if not section_match:
        return memo
    section_start = section_match.end()
    next_section = _NEXT_SECTION.search(memo, section_start)
    section_end = next_section.start() if next_section else len(memo)
    section = memo[section_start:section_end]
    headings = list(_SOURCE_HEADING.finditer(section))
    if len(headings) < 2:
        return memo

    prefix = section[:headings[0].start()]
    entries: list[tuple[str, str]] = []
    indices: dict[str, int] = {}
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(section)
        block = section[heading.start():end]
        block = re.sub(r"^###\s+#+\s+", "### ", block, count=1)
        title_key = normalize_source_title(heading.group(1))
        if not title_key:
            entries.append(("", block))
        elif title_key not in indices:
            indices[title_key] = len(entries)
            entries.append((title_key, block))
        else:
            old_index = indices[title_key]
            if _source_rank(block) > _source_rank(entries[old_index][1]):
                entries[old_index] = (title_key, block)

    deduplicated = prefix + "".join(block for _, block in entries)
    return memo[:section_start] + deduplicated + memo[section_end:]
