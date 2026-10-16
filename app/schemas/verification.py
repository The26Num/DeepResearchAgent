"""Evidence support decisions, separate from immutable research Claims."""

from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_VERIFICATION_REASON_CHARS = 400


class VerificationStatus(str, Enum):
    SUPPORTED = "SUPPORTED"
    PARTIAL = "PARTIAL"
    UNSUPPORTED = "UNSUPPORTED"


class VerificationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    claim_id: str = Field(min_length=1)
    status: VerificationStatus
    reason: str = Field(min_length=1, max_length=MAX_VERIFICATION_REASON_CHARS)


class BatchVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[VerificationDecision] = Field(min_length=1)


class VerificationResult(VerificationDecision):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    verification_id: str = Field(default_factory=lambda: f"verify_{uuid4().hex}", min_length=1)
    task_id: str = Field(min_length=1)
    evidence_ids: list[str]

    @field_validator("evidence_ids")
    @classmethod
    def valid_references(cls, values: list[str]) -> list[str]:
        if any(not value for value in values) or len(values) != len(set(values)):
            raise ValueError("Verification evidence_ids must be nonempty unique IDs (or an empty list).")
        return values
