"""Bounded findings that may cross ResearchTask boundaries."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.evidence_bundle import TaskEvidenceBundle


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    year: str | None = Field(default=None, max_length=64)
    key_point: str | None = Field(default=None, max_length=400)


class CompactResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=120)
    summary: str = Field(max_length=800)
    key_findings: list[str] = Field(default_factory=list, max_length=8)
    sources: list[SourceSummary] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=6)


class ResearchExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memo: str
    compact: CompactResearchResult
    tools_used: list[str]
    evidence_bundle: TaskEvidenceBundle | None = None
