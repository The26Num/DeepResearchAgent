"""Selective, bounded context shared between dependent research tasks."""

from app.schemas.research_result import CompactResearchResult


MAX_DEPENDENCY_CHARS = 2800
MAX_TOTAL_CONTEXT_CHARS = 8000


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _format_block(result: CompactResearchResult, limit: int) -> str:
    lines = [f"[{result.task_id}]"[:limit]]

    def add(line: str) -> bool:
        if len("\n".join([*lines, line])) > limit:
            return False
        lines.append(line)
        return True

    remaining = limit - len(lines[0]) - len("\nSummary: ")
    if remaining > 0:
        add("Summary: " + _short(result.summary, min(450, remaining)))

    # Preserve the most useful findings and source URLs before secondary details.
    for finding in result.key_findings[:2]:
        add("Finding: " + _short(finding, 320))
    for source in result.sources:
        title_and_url = f"Source: {_short(source.title, 140)}\nURL: {source.url}"
        if not add(title_and_url):
            continue  # Never truncate a URL.
        if source.year:
            add("Year: " + source.year)
        if source.key_point:
            add("Key Point: " + _short(source.key_point, 180))
    for finding in result.key_findings[2:]:
        add("Finding: " + _short(finding, 260))
    for uncertainty in result.uncertainties:
        add("Uncertainty: " + _short(uncertainty, 160))
    return "\n".join(lines)


def build_dependency_context(
    dependencies: dict[str, CompactResearchResult],
    max_total_chars: int = MAX_TOTAL_CONTEXT_CHARS,
) -> str:
    """Give each dependency a fair block without cutting any source URL."""
    if not dependencies or max_total_chars <= 0:
        return ""
    separator_chars = 2 * (len(dependencies) - 1)
    if max_total_chars < separator_chars + len(dependencies):
        raise ValueError("Context budget is too small to represent every dependency.")
    block_limit = min(MAX_DEPENDENCY_CHARS, max(1, (max_total_chars - separator_chars) // len(dependencies)))
    blocks = [_format_block(result, block_limit) for result in dependencies.values()]
    return "\n\n".join(blocks)
