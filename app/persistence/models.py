"""Versioned JSON contracts; only research data and non-secret execution limits."""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.agents.relevance import RelevanceUsage
from app.agents.verifier import VerificationUsage
from app.agents.writer import WriterUsage
from app.agents.report_grounding import ReportGroundingAudit
from app.research.reresearch import GapGroup, ReResearchSummary
from app.research.writing_context import WritingContext
from app.schemas.source import Source
from app.schemas.evidence import Evidence
from app.schemas.claim import Claim
from app.schemas.gap import ResearchGap
from app.schemas.plan import ResearchPlan
from app.schemas.research_result import ResearchExecutionResult
from app.schemas.verification import VerificationResult
from app.schemas.relevance import ClaimRelevanceResult
from app.schemas.report import ResearchReport
from app.schemas.citation import RenderedResearchReport

STATE_VERSION = 1


class RunStatus(str, Enum):
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    COMPLETED = "COMPLETED"


class ResearchRun(BaseModel):
    run_id: str
    goal: str
    status: RunStatus
    current_stage: str
    created_at: str
    updated_at: str
    resume_count: int = 0
    last_checkpoint_id: int | None = None
    error_type: str | None = None
    error_message: str | None = None
    state_version: int = STATE_VERSION


class EvidenceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sources: list[Source] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    source_aliases: dict[str, str] = Field(default_factory=dict)
    evidence_aliases: dict[str, str] = Field(default_factory=dict)
    evidence_alias_origins: dict[str, tuple[str, str | None]] = Field(default_factory=dict)
    source_users: dict[str, list[str]] = Field(default_factory=dict)
    evidence_users: dict[str, list[str]] = Field(default_factory=dict)


class ResearchRunState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    state_version: int = STATE_VERSION
    goal: str = Field(min_length=1)
    research_plan: ResearchPlan | None = None
    task_results: dict[str, ResearchExecutionResult] = Field(default_factory=dict)
    evidence_store: EvidenceSnapshot = Field(default_factory=EvidenceSnapshot)
    verification_results: list[VerificationResult] = Field(default_factory=list)
    relevance_results: dict[str, ClaimRelevanceResult] = Field(default_factory=dict)
    gaps: list[ResearchGap] = Field(default_factory=list)
    reresearch_summary: ReResearchSummary = Field(default_factory=ReResearchSummary)
    supplemental_results: dict[str, ResearchExecutionResult] = Field(default_factory=dict)
    selected_gap_groups: list[GapGroup] = Field(default_factory=list)
    reresearch_rounds_completed: int = Field(default=0, ge=0, le=1)
    reresearch_budget: dict = Field(default_factory=dict)
    reresearch_initial_counts: dict[str, int] = Field(default_factory=dict)
    reresearch_started_tasks: set[str] = Field(default_factory=set)
    verification_usage: VerificationUsage = Field(default_factory=VerificationUsage)
    relevance_usage: RelevanceUsage = Field(default_factory=RelevanceUsage)
    writer_usage: WriterUsage = Field(default_factory=WriterUsage)
    writing_context: WritingContext | None = None
    report_grounding: tuple[ReportGroundingAudit, ...] = ()
    research_report: ResearchReport | None = None
    rendered_report: RenderedResearchReport | None = None
    citation_debug: bool = False
    completed_stages: set[str] = Field(default_factory=set)
    # Explicit allowlist, never Settings/model/tool objects or credentials.
    phase_options: dict[str, bool] = Field(default_factory=dict)


class Checkpoint(BaseModel):
    checkpoint_id: int
    run_id: str
    sequence: int
    stage: str
    created_at: str
    state: ResearchRunState
