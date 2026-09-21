from app.research.source_dedup import deduplicate_memo_sources, normalize_source_title, retain_memo_sections


def test_normalize_source_title_ignores_case_space_and_punctuation() -> None:
    assert normalize_source_title("  1. STO-RL:  Offline RL! ") == normalize_source_title("sto rl offline rl")


def test_deduplicate_sources_prefers_official_entry() -> None:
    memo = """## Research Task
Compare two works.

## Important Sources

### 1. STO-RL: Offline RL
- URL: https://arxiv.org/abs/1234
- Year: 2026
- Why Relevant: Earlier listing.
- Key Point: A method.

### 2. STO RL Offline RL
- URL: https://dl.acm.org/doi/1234
- Year: 2026
- Why Relevant: Publisher page.
- Key Point: A method.

### 3. A different work
- URL: https://example.org/other

## Uncertainties / Missing Information
None.
"""
    result = deduplicate_memo_sources(memo)
    assert "https://dl.acm.org/doi/1234" in result
    assert "https://arxiv.org/abs/1234" not in result
    assert "https://example.org/other" in result
    assert "## Uncertainties / Missing Information" in result


def test_only_requested_memo_sections_remain() -> None:
    memo = "## Research Task\nQuestion\n## Important Sources\n### Paper\n- URL: https://example.org\n## Additional Reading\nRepeated paper\n## Uncertainties / Missing Information\nUnknown details"
    result = retain_memo_sections(memo)
    assert "## Additional Reading" not in result
    assert "Repeated paper" not in result
    assert "## Uncertainties / Missing Information\nUnknown details" in result


def test_source_heading_drops_accidental_extra_markers() -> None:
    memo = "## Important Sources\n### ### Paper one\n- URL: https://example.org/one\n\n### Paper two\n- URL: https://example.org/two\n"
    result = deduplicate_memo_sources(memo)
    assert "### Paper one" in result
    assert "### ### Paper one" not in result


def test_chinese_memo_sections_and_source_deduplication() -> None:
    memo = """## 研究任务
比较方法。
## 主要发现
两种来源介绍同一论文。
## 重要来源
### STO-RL: Offline RL
- URL：https://arxiv.org/abs/1234
### STO RL Offline RL
- URL：https://dl.acm.org/doi/1234
## 额外阅读
应移除。
## 不确定性与缺失信息
实验细节待核实。
"""
    result = deduplicate_memo_sources(retain_memo_sections(memo))
    assert "## 主要发现" in result
    assert "https://dl.acm.org/doi/1234" in result
    assert "https://arxiv.org/abs/1234" not in result
    assert "## 额外阅读" not in result
    assert "## 不确定性与缺失信息" in result
