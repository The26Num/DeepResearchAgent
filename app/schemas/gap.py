"""Actionable missing information with a small supplemental-improvement lifecycle."""

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GapType(str, Enum):
    SUPPORT_GAP = "SUPPORT_GAP"
    RELEVANCE_GAP = "RELEVANCE_GAP"
    COVERAGE_GAP = "COVERAGE_GAP"


class GapStatus(str, Enum):
    OPEN = "OPEN"
    IMPROVED = "IMPROVED"
    RESOLVED = "RESOLVED"


class ResearchGap(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    gap_id: str = Field(default_factory=lambda: f"gap_{uuid4().hex}", min_length=1)
    gap_type: GapType
    task_id: str = Field(min_length=1)
    claim_id: str | None = Field(default=None, min_length=1)
    description: str = Field(min_length=1, max_length=1000)
    missing_information: str = Field(min_length=1)
    status: GapStatus = GapStatus.OPEN

    @model_validator(mode="after")
    def validate_scope(self):
        if (self.gap_type == GapType.COVERAGE_GAP) != (self.claim_id is None):
            raise ValueError("COVERAGE_GAP is task-level; other gap types require a claim_id.")
        return self
