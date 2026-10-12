"""Goal/task alignment is independent of Evidence support verification."""

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


MAX_RELEVANCE_REASON_CHARS = 160


class RelevanceStatus(str, Enum):
    ALIGNED = "ALIGNED"
    PARTIAL = "PARTIAL"
    MISALIGNED = "MISALIGNED"


class ClaimRelevanceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim_id: str = Field(min_length=1)
    status: RelevanceStatus
    reason: str = Field(min_length=1, max_length=MAX_RELEVANCE_REASON_CHARS)


class BatchClaimRelevance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[ClaimRelevanceDecision] = Field(min_length=1)


class ClaimRelevanceResult(ClaimRelevanceDecision):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    relevance_id: str = Field(default_factory=lambda: f"relevance_{uuid4().hex}", min_length=1)
    task_id: str = Field(min_length=1)
