"""A meaningful source fragment; no verifier status or quality score."""

from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    evidence_id: str = Field(default_factory=lambda: f"ev_{uuid4().hex}", min_length=1)
    source_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    content: str = Field(min_length=1, max_length=2000)
    location: str | None = None

    @property
    def extracted_by_task(self) -> str:
        """task_id retains the first extractor after global deduplication."""
        return self.task_id
