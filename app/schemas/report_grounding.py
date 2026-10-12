"""Bounded semantic decisions for report units, separate from Claim verification."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ReportGroundingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    item_index: int = Field(ge=0)
    status: Literal["SUPPORTED", "UNSUPPORTED", "UNCERTAIN"]
    reason: str = Field(min_length=1, max_length=160)


class ReportGroundingBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    results: list[ReportGroundingDecision] = Field(min_length=1, max_length=100)
