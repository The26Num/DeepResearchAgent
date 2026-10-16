"""Versioned benchmark and evaluation contracts, independent of research decisions."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class BenchmarkCase(EvaluationModel):
    case_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    question: str = Field(min_length=1)
    category: str = Field(min_length=1)
    question_type: Literal["discovery", "comparison", "trend", "narrow_focus"] | None = None
    expected_dimensions: list[str] = Field(default_factory=list)
    notes: str = ""
    gold_sources: list[str] | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_dimensions(self):
        if not self.question.strip() or any(not item.strip() for item in self.expected_dimensions):
            raise ValueError("Question and supplied dimensions must not be blank.")
        if len(set(self.expected_dimensions)) != len(self.expected_dimensions):
            raise ValueError("Duplicate expected_dimensions.")
        if self.gold_sources:
            from app.schemas.source import normalize_source_url
            for url in self.gold_sources:
                normalize_source_url(url)
        return self


class BenchmarkDataset(EvaluationModel):
    benchmark_version: str | int
    cases: list[BenchmarkCase] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_cases(self):
        if not str(self.benchmark_version).strip():
            raise ValueError("benchmark_version must not be blank.")
        ids = [item.case_id for item in self.cases]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate case_id in benchmark dataset.")
        return self


class DimensionStatus(str, Enum):
    COVERED = "COVERED"
    PARTIAL = "PARTIAL"
    MISSING = "MISSING"


class DimensionAssessment(EvaluationModel):
    dimension: str
    status: DimensionStatus


class JudgeResult(EvaluationModel):
    goal_relevance: float = Field(ge=0, le=4)
    coverage: float | None = Field(ge=0, le=4)
    groundedness: float = Field(ge=0, le=4)
    clarity: float = Field(ge=0, le=4)
    dimensions: list[DimensionAssessment]
    missing_dimensions: list[str]
    unsupported_statements: list[str]
    assessed_statement_count: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def consistent(self):
        names = [item.dimension for item in self.dimensions]
        if len(names) != len(set(names)):
            raise ValueError("Duplicate judge dimensions.")
        missing = {item.dimension for item in self.dimensions if item.status == DimensionStatus.MISSING}
        if set(self.missing_dimensions) != missing or len(set(self.missing_dimensions)) != len(self.missing_dimensions):
            raise ValueError("missing_dimensions must match MISSING decisions.")
        if len(self.unsupported_statements) > self.assessed_statement_count:
            raise ValueError("Unsupported count exceeds assessed statement count.")
        return self

    @property
    def dimension_coverage_score(self) -> float | None:
        weights = {DimensionStatus.COVERED: 1, DimensionStatus.PARTIAL: .5, DimensionStatus.MISSING: 0}
        return sum(weights[item.status] for item in self.dimensions) / len(self.dimensions) if self.dimensions else None

    @property
    def unsupported_statement_rate(self) -> float | None:
        return len(self.unsupported_statements) / self.assessed_statement_count if self.assessed_statement_count else None


class EvaluationError(EvaluationModel):
    stage: str
    error_type: str
    error_message: str


class MetricVector(EvaluationModel):
    research: dict = Field(default_factory=dict)
    quality: dict = Field(default_factory=dict)
    efficiency: dict = Field(default_factory=dict)
    timing: dict = Field(default_factory=dict)
    reliability: dict = Field(default_factory=dict)

    def flat(self) -> dict:
        return {f"{group}.{key}": value for group, values in self.model_dump().items() for key, value in values.items()}


class EvaluationResult(EvaluationModel):
    case_id: str
    benchmark_version: str | int
    variant: Literal["full", "baseline"]
    run_id: str | None = None
    status: Literal["COMPLETED", "FAILED", "INTERRUPTED"]
    metrics: MetricVector = Field(default_factory=MetricVector)
    judge_result: JudgeResult | None = None
    judge_usage: dict = Field(default_factory=dict)
    errors: list[EvaluationError] = Field(default_factory=list)
    report_path: str | None = None


class ComparisonResult(EvaluationModel):
    case_id: str
    full_status: str
    baseline_status: str
    full_run_id: str | None = None
    full: dict
    baseline: dict
    delta: dict


class BaselineSource(EvaluationModel):
    source_id: str
    url: str
    title: str
    excerpt: str
    basis: Literal["page_excerpt", "search_snippet"]


class BaselineRunResult(EvaluationModel):
    question: str
    sources: list[BaselineSource]
    report: str
    tool_usage: dict
    llm_calls: int
    tokens: dict
    runtime_seconds: float
