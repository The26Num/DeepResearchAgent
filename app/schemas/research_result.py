"""Bounded findings that may cross ResearchTask boundaries."""

from pydantic import BaseModel, ConfigDict, Field


class SourceSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=180)
    url: str = Field(min_length=1, max_length=1000)
    year: str | None = Field(default=None, max_length=32)
    key_point: str | None = Field(default=None, max_length=240)


class CompactResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1, max_length=80)
    summary: str = Field(max_length=500)
    key_findings: list[str] = Field(default_factory=list, max_length=6)
    sources: list[SourceSummary] = Field(default_factory=list, max_length=6)
    uncertainties: list[str] = Field(default_factory=list, max_length=4)


class ResearchExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memo: str
    compact: CompactResearchResult
    tools_used: list[str]
