"""Restricted source and method tools. No arbitrary file or shell access."""

from contextlib import nullcontext
import json

from langchain_core.tools import tool

from app.research.source_archive import ArchiveIntegrityError, ArchiveLimitError
from app.research.task_resources import ResearchBudgetExceeded


def resource_tools(budget):
    resource = budget.resources

    def call(name, action):
        if not budget.reserve_local(name):
            return json.dumps({"error": f"{name} budget exhausted; finish with available evidence."})
        try:
            operation = budget.runtime.operation(name) if budget.runtime else nullcontext()
            with operation, resource.activate():
                result = action()
                if name == "read_source":
                    budget.record_reading(json.dumps(result, ensure_ascii=False))
            return json.dumps(result, ensure_ascii=False)
        except (ValueError, PermissionError, ArchiveIntegrityError, ArchiveLimitError, ResearchBudgetExceeded) as error:
            return json.dumps({"error": str(error)[:400], "error_type": type(error).__name__}, ensure_ascii=False)

    @tool
    def list_sources(offset: int = 0, limit: int = 5, task_id: str | None = None, keyword: str = "") -> str:
        """Page through authorized saved source cards, optionally filtered by owning task or title/URL keyword. Bodies are not returned."""
        return call("list_sources", lambda: resource.list_sources(offset=offset, limit=limit, task_id=task_id, keyword=keyword))

    @tool
    def search_sources(query: str, limit: int = 5) -> str:
        """Search all authorized archived text, including page tails. Hits are navigation only; confirm facts with read_source."""
        return call("search_sources", lambda: resource.search_sources(query, limit))

    @tool
    def read_source(snapshot_id: str, start_char: int = 0, end_char: int | None = None,
                    section_id: str | None = None, max_chars: int | None = None) -> str:
        """Read an authorized snapshot section or Python-character range. Returns exact positions and next_start_char when capped."""
        return call("read_source", lambda: resource.read_source(snapshot_id, start_char=start_char, end_char=end_char,
                                                               section_id=section_id, max_chars=max_chars))

    @tool
    def load_research_skill(skill_id: str) -> str:
        """Load one method from the short research skill catalog. Rules remain active after context compaction; methods are not evidence."""
        return call("load_research_skill", lambda: resource.load_skill(skill_id))

    @tool
    def read_skill_reference(skill_id: str, reference: str) -> str:
        """Read a listed references/name.md from an active pinned research skill. No arbitrary file paths are allowed."""
        return call("read_skill_reference", lambda: resource.skill_reference(skill_id, reference))

    return [list_sources, search_sources, read_source, load_research_skill, read_skill_reference]
