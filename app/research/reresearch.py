"""Deterministic gap consolidation, selection, and approximate improvement rules."""

from dataclasses import dataclass, field

from app.config import Settings
from app.research.gap_store import GapStore
from app.schemas.claim import Claim
from app.schemas.gap import GapStatus, GapType, ResearchGap
from app.schemas.reresearch import ReResearchPlan


_PRIORITY = {GapType.COVERAGE_GAP: 0, GapType.RELEVANCE_GAP: 1, GapType.SUPPORT_GAP: 2}


@dataclass(frozen=True)
class GapGroup:
    group_id: str
    task_id: str
    gap_ids: tuple[str, ...]
    gap_types: tuple[GapType, ...]
    claim_ids: tuple[str, ...]
    missing_information: tuple[str, ...]


def select_gap_groups(gaps: list[ResearchGap], settings: Settings) -> list[GapGroup]:
    by_task: dict[str, list[ResearchGap]] = {}
    for gap in gaps:
        if gap.status != GapStatus.RESOLVED:
            by_task.setdefault(gap.task_id, []).append(gap)
    # Stable task order breaks ties; no LLM scores or one-gap-one-task fan-out.
    ordered = sorted(by_task.items(), key=lambda pair: min(_PRIORITY[item.gap_type] for item in pair[1]))
    groups = []
    for task_id, items in ordered[:settings.max_reresearch_tasks_per_round]:
        items = sorted(items, key=lambda item: _PRIORITY[item.gap_type])[:settings.max_reresearch_gaps_per_task]
        groups.append(GapGroup(
            group_id=f"G{len(groups) + 1}", task_id=task_id,
            gap_ids=tuple(item.gap_id for item in items),
            gap_types=tuple(dict.fromkeys(item.gap_type for item in items)),
            claim_ids=tuple(dict.fromkeys(item.claim_id for item in items if item.claim_id)),
            missing_information=tuple(dict.fromkeys(item.missing_information for item in items)),
        ))
    return groups


def update_group_status(store: GapStore, gap_ids: list[str], claims: list[Claim], verifications: dict,
                        relevance: dict) -> GapStatus:
    status = GapStatus.OPEN
    for claim in claims:
        verified, aligned = verifications.get(claim.claim_id), relevance.get(claim.claim_id)
        if verified is None or aligned is None:
            continue
        if verified.task_id != claim.task_id or aligned.task_id != claim.task_id:
            raise ValueError("Gap improvement decisions must belong to the supplemental claim task.")
        if verified.status == "SUPPORTED" and aligned.status == "ALIGNED":
            status = GapStatus.RESOLVED
            break
        if verified.status in {"SUPPORTED", "PARTIAL"} and aligned.status in {"ALIGNED", "PARTIAL"}:
            status = GapStatus.IMPROVED
    for key in gap_ids:
        store.update_status(key, status)
    return status


@dataclass
class ReResearchSummary:
    initial_gaps: int = 0
    selected_groups: int = 0
    rounds: int = 0
    tasks: int = 0
    new_sources: int = 0
    new_evidence: int = 0
    new_claims: int = 0
    verification_calls: int = 0
    relevance_calls: int = 0
    llm_calls: int = 0
    calls_by_stage: dict[str, int] = field(default_factory=dict)
    stop_reason: str = "disabled"
    failures: dict[str, str] = field(default_factory=dict)
    plans: list[ReResearchPlan] = field(default_factory=list)
