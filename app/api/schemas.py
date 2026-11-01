"""Public JSON contracts, containing research data only."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.persistence.models import RunStatus
from app.schemas.citation import ReferenceEntry
from app.schemas.report import ResearchReport
from app.schemas.claim import Claim
from app.schemas.evidence import Evidence
from app.schemas.source import Source
from app.schemas.verification import VerificationResult
from app.schemas.relevance import ClaimRelevanceResult
from app.research.events import PhaseProgress


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=10000)


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    error: ErrorDetail


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class RunAcceptedResponse(BaseModel):
    run_id: str
    status: RunStatus


class TaskResponse(BaseModel):
    id: str
    title: str
    question: str
    description: str
    task_type: Literal["discovery", "analysis", "synthesis"]
    depends_on: list[str]
    status: Literal["pending", "running", "completed", "failed"]


class PlanResponse(BaseModel):
    goal: str
    tasks: list[TaskResponse]


class RunResponse(RunAcceptedResponse):
    question: str
    current_stage: str
    created_at: datetime
    updated_at: datetime
    resume_count: int
    checkpoint_available: bool
    resumable: bool
    plan: PlanResponse | None = None
    supplemental_tasks: list[TaskResponse] = Field(default_factory=list)
    error: ErrorDetail | None = None
    phase_statuses: list[PhaseProgress] = Field(default_factory=list)


class RunListResponse(BaseModel):
    runs: list[RunResponse]


class ReportResponse(BaseModel):
    run_id: str
    status: Literal["COMPLETED"] = "COMPLETED"
    report: ResearchReport
    markdown: str
    references: list[ReferenceEntry]


class EvidenceExplorerResponse(BaseModel):
    run_id: str
    checkpoint_available: bool
    checkpoint_stage: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    sources: list[Source] = Field(default_factory=list)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    relevance_results: list[ClaimRelevanceResult] = Field(default_factory=list)
