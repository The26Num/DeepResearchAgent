"""Model-facing selections from numbered, real evidence candidates; no internal IDs."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


EvidenceIndex = Annotated[int, Field(ge=0, strict=True)]


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    text: str = Field(min_length=1, max_length=2000)
    supporting_evidence_indexes: list[EvidenceIndex] = Field(min_length=1, max_length=12)

    @field_validator("supporting_evidence_indexes")
    @classmethod
    def normalize_references(cls, values):
        return sorted(set(values))


class ExtractedEvidenceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_evidence_indexes: list[EvidenceIndex] = Field(default_factory=list, max_length=12)
    claims: list[ExtractedClaim] = Field(default_factory=list, max_length=8)

    @field_validator("selected_evidence_indexes")
    @classmethod
    def normalize_references(cls, values):
        return sorted(set(values))
