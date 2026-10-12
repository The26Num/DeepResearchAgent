"""Compact goal/objective/Claim batches; deliberately no Evidence or Memo API."""

import json
from dataclasses import dataclass

from app.schemas.claim import Claim
from app.schemas.task import ResearchTask


class RelevanceIntegrityError(ValueError):
    """Claims/tasks do not form an unambiguous relevance input."""


class RelevanceContextLimitError(ValueError):
    """One complete Claim/objective cannot fit the relevance budget."""


@dataclass(frozen=True)
class RelevanceContext:
    claims: tuple[Claim, ...]
    claim_ids: dict[str, str]
    payload: dict
    text: str

    @property
    def context_chars(self) -> int:
        return len(self.text)


def validate_relevance_input(goal: str, tasks: list[ResearchTask], claims: list[Claim]) -> list[Claim]:
    if not goal.strip():
        raise RelevanceIntegrityError("Research goal must not be empty.")
    task_ids = [item.id for item in tasks]
    if len(set(task_ids)) != len(task_ids):
        raise RelevanceIntegrityError("Duplicate task IDs in relevance input.")
    unique = {}
    for claim in claims:
        if claim.task_id not in task_ids:
            raise RelevanceIntegrityError(f"Claim {claim.claim_id} references unknown task_id: {claim.task_id}.")
        if claim.claim_id in unique and unique[claim.claim_id] != claim:
            raise RelevanceIntegrityError(f"Conflicting claim_id in relevance input: {claim.claim_id}.")
        unique[claim.claim_id] = claim
    return list(unique.values())


def build_relevance_context(goal: str, tasks: list[ResearchTask], claims: list[Claim]) -> RelevanceContext:
    tasks_by_id = {item.id: item for item in tasks}
    needed = dict.fromkeys(item.task_id for item in claims)
    claim_ids = {f"C{index}": item.claim_id for index, item in enumerate(claims, 1)}
    payload = {
        "research_goal": goal,
        "tasks": {key: {"title": tasks_by_id[key].title, "question": tasks_by_id[key].question,
                        "description": tasks_by_id[key].description} for key in needed},
        "claims": [{"claim_id": label, "task_id": item.task_id, "text": item.text}
                   for label, item in zip(claim_ids, claims)],
    }
    return RelevanceContext(tuple(claims), claim_ids, payload,
                            json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def split_relevance_batches(goal: str, tasks: list[ResearchTask], claims: list[Claim], *,
                             max_claims: int, max_context_chars: int) -> list[RelevanceContext]:
    if max_claims < 1 or max_context_chars < 1:
        raise ValueError("Relevance budgets must be positive.")
    claims = validate_relevance_input(goal, tasks, claims)
    batches, pending = [], []
    for claim in claims:
        candidate = build_relevance_context(goal, tasks, [*pending, claim])
        if len(candidate.claims) <= max_claims and candidate.context_chars <= max_context_chars:
            pending.append(claim)
            continue
        if pending:
            batches.append(build_relevance_context(goal, tasks, pending))
        single = build_relevance_context(goal, tasks, [claim])
        if single.context_chars > max_context_chars:
            raise RelevanceContextLimitError(
                f"Claim {claim.claim_id} complete relevance context has {single.context_chars} characters; "
                f"limit={max_context_chars}. Goal, objective and Claim were not truncated; "
                "increase MAX_RELEVANCE_CONTEXT_CHARS."
            )
        pending = [claim]
    if pending:
        batches.append(build_relevance_context(goal, tasks, pending))
    return batches
