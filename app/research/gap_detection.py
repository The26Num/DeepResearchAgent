"""Consume support results once, assess alignment once, generate gaps without LLMs."""

from dataclasses import dataclass

from app.agents.relevance import RelevanceAssessor, RelevanceUsage
from app.research.gap_store import GapStore
from app.research.relevance_context import RelevanceIntegrityError, validate_relevance_input
from app.research.verification_store import VerificationStore
from app.schemas.claim import Claim
from app.schemas.gap import GapType, ResearchGap
from app.schemas.plan import ResearchPlan
from app.schemas.relevance import ClaimRelevanceResult, RelevanceStatus
from app.schemas.task import ResearchTask
from app.schemas.verification import VerificationResult, VerificationStatus


class GapIntegrityError(ValueError):
    """Gap decisions require complete matching support and relevance results."""


@dataclass
class GapDetectionResult:
    gap_store: GapStore
    relevance_results: dict[str, ClaimRelevanceResult]
    relevance_usage: RelevanceUsage


def _check_verifications(claims: list[Claim], verifications: dict[str, VerificationResult]) -> None:
    if set(verifications) != {item.claim_id for item in claims}:
        raise GapIntegrityError("Every Claim must have a latest VerificationResult before gap detection.")
    for claim in claims:
        result = verifications[claim.claim_id]
        if (result.claim_id != claim.claim_id or result.task_id != claim.task_id
                or set(result.evidence_ids) != set(claim.evidence_ids)):
            raise GapIntegrityError(f"VerificationResult does not match current Claim references: {claim.claim_id}.")


def generate_research_gaps(tasks: list[ResearchTask], claims: list[Claim],
                           verifications: dict[str, VerificationResult],
                           relevance: dict[str, ClaimRelevanceResult]) -> GapStore:
    """Pure programmatic support/relevance/coverage rules; no tools or model calls."""
    tasks_by_id = {item.id: item for item in tasks}
    if len(tasks_by_id) != len(tasks) or any(item.task_id not in tasks_by_id for item in claims):
        raise GapIntegrityError("Gap input has duplicate task IDs or unknown Claim task references.")
    ids = [item.claim_id for item in claims]
    if len(set(ids)) != len(ids):
        raise GapIntegrityError("Gap input Claim IDs must be unique.")
    _check_verifications(claims, verifications)
    if set(relevance) != set(ids):
        raise GapIntegrityError("Every Claim must have exactly one relevance result before gap generation.")
    for claim in claims:
        item = relevance[claim.claim_id]
        if item.claim_id != claim.claim_id or item.task_id != claim.task_id:
            raise GapIntegrityError(f"ClaimRelevanceResult does not match Claim: {claim.claim_id}.")

    store = GapStore()
    covered = set()
    for claim in claims:
        support, alignment = verifications[claim.claim_id], relevance[claim.claim_id]
        task = tasks_by_id[claim.task_id]
        objective = f"{task.title}. {task.question} {task.description}"
        if support.status in {VerificationStatus.PARTIAL, VerificationStatus.UNSUPPORTED}:
            store.add_gap(ResearchGap(
                gap_type=GapType.SUPPORT_GAP, task_id=task.id, claim_id=claim.claim_id,
                description=(("Claim is only partially supported by its cited evidence. "
                              if support.status == VerificationStatus.PARTIAL else
                              "Claim is not supported by its cited evidence. ") + support.reason),
                missing_information=f"Evidence addressing this verifier limitation is needed: {support.reason}",
            ))
        if alignment.status in {RelevanceStatus.PARTIAL, RelevanceStatus.MISALIGNED}:
            store.add_gap(ResearchGap(
                gap_type=GapType.RELEVANCE_GAP, task_id=task.id, claim_id=claim.claim_id,
                description=f"Claim does not fully address its research goal/task objective. {alignment.reason}",
                missing_information=f"Direct findings addressing the task objective are needed: {objective}",
            ))
        if support.status == VerificationStatus.SUPPORTED and alignment.status == RelevanceStatus.ALIGNED:
            covered.add(task.id)
    for task in tasks:
        if task.id not in covered:
            has_claims = any(item.task_id == task.id for item in claims)
            store.add_gap(ResearchGap(
                gap_type=GapType.COVERAGE_GAP, task_id=task.id,
                description=("No claim is both fully evidence-supported and aligned with this task."
                             if has_claims else "No evidence-backed claim was produced for this research task."),
                missing_information=f"At least one supported, aligned finding addressing: {task.title}. {task.question} {task.description}",
            ))
    return store


class GapDetector:
    def __init__(self, assessor: RelevanceAssessor | None = None) -> None:
        self.assessor = assessor or RelevanceAssessor()

    def detect(self, plan: ResearchPlan, claims: list[Claim], verification_store: VerificationStore,
               *, research_goal: str | None = None, existing_relevance=None, on_batch_complete=None) -> GapDetectionResult:
        self.assessor.last_usage = RelevanceUsage()
        goal = plan.goal if research_goal is None else research_goal
        claims = validate_relevance_input(goal, plan.tasks, claims)
        latest = {item.claim_id: verification_store.get_latest_verification(item.claim_id) for item in claims}
        # Do not spend a relevance call when support verification is incomplete/stale.
        _check_verifications(claims, {key: value for key, value in latest.items() if value is not None})
        existing = dict(existing_relevance or {})
        by_id = {item.claim_id: item for item in claims}
        if any(key not in by_id or result.claim_id != key or result.task_id != by_id[key].task_id
               for key, result in existing.items()):
            raise RelevanceIntegrityError("Restored relevance does not match the current Claims.")
        missing = [item for item in claims if item.claim_id not in existing]
        options = {"on_batch_complete": on_batch_complete} if on_batch_complete else {}
        results = self.assessor.assess_batch(goal, plan.tasks, missing, **options) if missing else []
        relevance = {**existing, **{item.claim_id: item for item in results}}
        if len(relevance) != len(existing) + len(results):
            raise RelevanceIntegrityError("Duplicate Claim IDs returned by relevance assessor.")
        gaps = generate_research_gaps(plan.tasks, claims, latest, relevance)
        return GapDetectionResult(gaps, relevance, self.assessor.last_usage)
