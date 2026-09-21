from pydantic import ValidationError
import pytest

from app.research.compact import MAX_COMPACT_CHARS, compact_from_memo
from app.schemas import CompactResearchResult, SourceSummary


def test_compact_result_extracts_bounded_findings_and_sources() -> None:
    memo = """## Research Task
Find methods.
## Key Findings
1. Paper A addresses sparse rewards with subgoals.
2. Paper B uses preferences.
## Important Sources
### Paper A
- URL: https://example.org/paper-a
- Year: 2026
- Key Point: It creates subgoals.
## Uncertainties / Missing Information
- Full experimental table was unavailable.
"""
    result = compact_from_memo("task1", memo)
    assert result.task_id == "task1"
    assert len(result.key_findings) == 2
    assert result.sources[0].url == "https://example.org/paper-a"
    assert result.sources[0].key_point == "It creates subgoals."
    assert result.uncertainties == ["Full experimental table was unavailable."]
    assert len(result.model_dump_json()) <= MAX_COMPACT_CHARS


def test_compact_result_has_no_raw_page_or_history_fields() -> None:
    with pytest.raises(ValidationError):
        CompactResearchResult(task_id="task1", summary="Summary", raw_webpage="page")
    with pytest.raises(ValidationError):
        SourceSummary(title="Paper", url="https://example.org", tool_messages=["raw"])


def test_long_memo_is_compressed_without_raw_page_text() -> None:
    memo = "## Research Task\nQuestion\n## Key Findings\n" + "\n".join(
        f"{i}. " + "Detailed finding " * 100 for i in range(15)
    ) + "\n## Important Sources\n### Paper\n- URL: https://example.org/paper\n- Key Point: Short.\n"
    result = compact_from_memo("task1", memo)
    assert len(result.model_dump_json()) <= MAX_COMPACT_CHARS
    assert len(result.key_findings) <= 6
    assert result.sources[0].url == "https://example.org/paper"


def test_compact_result_reads_chinese_memo_and_source_labels() -> None:
    memo = """## 研究任务
调研稀疏奖励离线强化学习。
## 主要发现
1. STO-RL 利用子目标缓解稀疏奖励。
## 重要来源
### STO-RL: Offline RL under Sparse Rewards
- URL：https://example.org/sto-rl
- 年份：2026
- 相关性：与研究主题直接相关。
- 关键点：利用有序子目标。
## 不确定性与缺失信息
- 尚未核实完整实验表。
"""
    result = compact_from_memo("task1", memo)
    assert result.key_findings == ["STO-RL 利用子目标缓解稀疏奖励。"]
    assert result.sources[0].title == "STO-RL: Offline RL under Sparse Rewards"
    assert result.sources[0].year == "2026"
    assert result.sources[0].key_point == "利用有序子目标。"
    assert result.uncertainties == ["尚未核实完整实验表。"]
