"""Task-local artifacts for a later merge; no extraction or shared Store writes."""

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.claim import Claim
from app.schemas.evidence import Evidence
from app.schemas.source import Source


class TaskEvidenceBundle(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, frozen=True)

    task_id: str = Field(min_length=1)
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_artifacts(self) -> "TaskEvidenceBundle":
        for items, field in (
            (self.sources, "source_id"), (self.evidence, "evidence_id"), (self.claims, "claim_id"),
        ):
            ids = [getattr(item, field) for item in items]
            if len(set(ids)) != len(ids):
                raise ValueError(f"TaskEvidenceBundle contains duplicate {field} values.")
        # Sources may originate in another task; evidence and claims here belong
        # to this task. Their references may target records already in the Store.
        for item in [*self.evidence, *self.claims]:
            if item.task_id != self.task_id:
                raise ValueError("Bundle evidence and claims must belong to its task_id.")
        return self
