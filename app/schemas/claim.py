"""A task's conclusion and its declared supporting evidence, without verification."""

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator


class Claim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    claim_id: str = Field(default_factory=lambda: f"claim_{uuid4().hex}", min_length=1)
    task_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)

    @field_validator("evidence_ids")
    @classmethod
    def validate_evidence_ids(cls, values: list[str]) -> list[str]:
        if any(not value for value in values):
            raise ValueError("Claim evidence_ids must not contain empty IDs.")
        if len(set(values)) != len(values):
            raise ValueError("Claim evidence_ids must be unique.")
        return values
