"""A meaningful source fragment; no verifier status or quality score."""

from uuid import uuid4
from hashlib import sha256

from pydantic import BaseModel, ConfigDict, Field, model_validator


MAX_EVIDENCE_CHARS = 3000


class EvidenceOrigin(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot_id: str = Field(pattern=r"^snap_[0-9a-f]{64}$")
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_range(self):
        if self.end_char <= self.start_char:
            raise ValueError("Invalid evidence character range.")
        return self


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex}", min_length=1)
    source_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=MAX_EVIDENCE_CHARS)
    location: str | None = None
    origin: EvidenceOrigin | None = None

    @model_validator(mode="after")
    def valid_origin_content(self):
        if self.origin and (self.origin.end_char - self.origin.start_char != len(self.content)
                            or sha256(self.content.encode("utf-8")).hexdigest() != self.origin.text_hash):
            raise ValueError("Evidence content does not match its origin length/hash.")
        return self

    @property
    def extracted_by_task(self) -> str:
        """task_id retains the first extractor after global deduplication."""
        return self.task_id
