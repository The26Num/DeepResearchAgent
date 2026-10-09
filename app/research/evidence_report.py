"""Render every task's public report exclusively from its formal evidence bundle."""

from app.research.compact import bound_compact, compact_from_memo
from app.research.evidence_context import EvidenceContext
from app.schemas import CompactResearchResult, Evidence, ResearchTask, TaskEvidenceBundle


def finalize_evidence_report(
    task: ResearchTask, bundle: TaskEvidenceBundle, upstream: EvidenceContext,
    *, has_reading: bool,
) -> tuple[str, CompactResearchResult]:
    sources = {item.source_id: item for item in (*upstream.sources, *bundle.sources)}
    evidence = {item.evidence_id: item for item in (*upstream.evidence, *bundle.evidence)}
    labels = {}
    cited: dict[str, list[Evidence]] = {}
    findings = []
    for claim in bundle.claims:
        for key in claim.evidence_ids:
            item = evidence[key]  # Fail closed for a broken chain.
            labels.setdefault(key, f"E{len(labels) + 1}")
            if item.source_id not in sources:
                raise ValueError("Report evidence has no corresponding source.")
            items = cited.setdefault(item.source_id, [])
            if item not in items:
                items.append(item)
        # These references resolve to the exact Evidence IDs displayed below.
        findings.append("- " + " ".join(claim.text.split())
                        + "\n  - 证据：" + ", ".join(labels[key] for key in claim.evidence_ids))
    if findings:
        used = {key for claim in bundle.claims for key in claim.evidence_ids}
        gaps = [f"本任务的正式结论引用 {len(cited)} 个来源、{len(used)} 条证据；这些引用尚未经过语义支持性验证。",
                "已读取材料不能保证覆盖全部最新研究；未形成正式结论的内容不作为本报告的发现。"]
        omitted = len(set(sources) - set(cited))
        if omitted:
            gaps.append(f"收到的材料中另有 {omitted} 个来源未用于本任务正式结论，不能据此认定其贡献已被综合。")
        if not any(sources[key].source_type == "paper" for key in cited):
            gaps.append("已引用材料未包含论文页面来源，仍需补充直接针对本问题的论文证据。")
    else:
        findings = ["本任务暂未形成有来源支持的研究结论。"]
        gaps = [("已读取材料或收到上游证据，但尚未形成可追溯的正式结论。"
                 if has_reading or upstream.evidence else
                 "候选来源未能成功读取，且缺少可复用证据，无法确认具体方法或实验结果。")]
    entries = []
    for key, items in cited.items():
        source = sources[key]
        excerpts = "\n".join(f"- 证据 {labels[item.evidence_id]}（摘录预览）：{item.content[:500]}"
                             + ("…" if len(item.content) > 500 else "") for item in items)
        entries.append(f"### {' '.join(source.title.split())}\n- URL：{source.url}\n"
                       f"- 日期：{' '.join((source.published_at or '未知').split())}\n{excerpts}")
    source_text = "\n\n".join(entries) or "暂无用于支持本任务结论的来源。"
    uncertainty = "\n".join("- " + gap for gap in gaps)
    result = (f"## 研究任务\n{task.question}\n\n## 主要发现\n" + "\n".join(findings)
              + f"\n\n## 重要来源\n{source_text}\n\n## 不确定性与缺失信息\n{uncertainty}")
    compact = compact_from_memo(task.id, result)
    points = {sources[key].url: items[0].content for key, items in cited.items()}
    for source in compact.sources:
        source.key_point = points[source.url][:240]
    # The compact channel carries conclusions, never rendering-only ID lines.
    compact.key_findings = [" ".join(claim.text.split())[:360] for claim in bundle.claims[:6]]
    if not bundle.claims:
        compact.key_findings = []
        compact.sources = []
        compact.summary = "本任务未形成有来源支持的结论；请保留缺口，勿推断具体方法或性能。"
    return result, bound_compact(compact)
