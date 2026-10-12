"""Report-scoped presentation data; never written into the evidence graph."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveInt


class _CitationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReferenceEntry(_CitationModel):
    citation_number: PositiveInt
    source_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    source_type: str = "unknown"
    published_at: str | None = None


class ElementCitation(_CitationModel):
    path: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    citation_numbers: list[PositiveInt] = Field(min_length=1)


class CitationUsage(_CitationModel):
    used_claims: int = Field(default=0, ge=0)
    cited_sources: int = Field(default=0, ge=0)
    citation_markers: int = Field(default=0, ge=0)
    references: int = Field(default=0, ge=0)
    llm_calls: Literal[0] = 0
    input_tokens: Literal[0] = 0
    output_tokens: Literal[0] = 0


class RenderedResearchReport(_CitationModel):
    markdown: str
    references: list[ReferenceEntry]
    citations: list[ElementCitation]
    usage: CitationUsage
