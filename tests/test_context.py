from app.research.context import MAX_DEPENDENCY_CHARS, build_dependency_context
from app.schemas import CompactResearchResult, SourceSummary


def make_result(task_id: str, url: str) -> CompactResearchResult:
    return CompactResearchResult(
        task_id=task_id, summary="A focused summary", key_findings=["Finding " + "x" * 250] * 6,
        sources=[SourceSummary(title="Relevant paper", url=url, key_point="A short method point")],
        uncertainties=["More detail is needed"] * 4,
    )


def test_single_dependency_has_source_lead_and_is_bounded() -> None:
    context = build_dependency_context({"task1": make_result("task1", "https://example.org/paper")})
    assert "[task1]" in context
    assert "Summary: A focused summary" in context
    assert "https://example.org/paper" in context
    assert len(context) <= MAX_DEPENDENCY_CHARS


def test_multiple_dependencies_all_represented_under_total_budget() -> None:
    dependencies = {f"task{i}": make_result(f"task{i}", f"https://example.org/paper{i}") for i in range(1, 5)}
    context = build_dependency_context(dependencies, max_total_chars=1000)
    assert len(context) <= 1000
    for task_id in dependencies:
        assert f"[{task_id}]" in context
    assert "https://example.org/paper1" in context


def test_context_never_cuts_url_midway() -> None:
    long_url = "https://example.org/" + "a" * 180
    context = build_dependency_context({"task1": make_result("task1", long_url)}, max_total_chars=300)
    assert len(context) <= 300
    assert long_url not in context or f"URL: {long_url}" in context
    assert long_url[:50] not in context or long_url in context
