from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    research_db_path: str = "workspace/research.db"

    siliconflow_api_key: str | None = None
    model_api_key: str | None = None
    model_base_url: str | None = None
    tavily_api_key: str | None = None
    model_name: str | None = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    model_request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    evidence_extraction_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    evidence_extraction_max_tokens: int = Field(default=8192, ge=512, le=8192)
    evidence_extraction_enable_thinking: bool = False
    model_max_retries: int = Field(default=0, ge=0)
    research_task_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    research_progress_interval_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)
    show_evidence_debug: bool = False
    verifier_model_name: str | None = None
    verifier_model_api_key: str | None = None
    verifier_model_base_url: str | None = None
    verifier_enable_thinking: bool = False
    max_verify_claims_per_batch: int = Field(default=6, ge=1, le=32)
    max_verification_context_chars: int = Field(default=18000, ge=500)
    verification_max_tokens: int = Field(default=3072, ge=512, le=8192)
    verification_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)
    show_verification_debug: bool = False
    max_relevance_claims_per_batch: int = Field(default=12, ge=1, le=48)
    max_relevance_context_chars: int = Field(default=12000, ge=500)
    relevance_max_tokens: int = Field(default=3072, ge=512, le=8192)
    relevance_timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    show_gap_debug: bool = False
    # Phase 4C first version deliberately permits at most one supplemental round.
    max_reresearch_rounds: int = Field(default=1, ge=0, le=1)
    max_reresearch_tasks_per_round: int = Field(default=2, ge=1, le=8)
    max_reresearch_gaps_per_task: int = Field(default=4, ge=1, le=12)
    max_reresearch_llm_calls: int = Field(default=12, ge=0, le=100)
    reresearch_max_search_calls: int = Field(default=2, ge=0, le=2)
    reresearch_max_fetch_calls: int = Field(default=2, ge=0, le=3)
    max_reresearch_planning_context_chars: int = Field(default=12000, ge=500)
    reresearch_planning_max_tokens: int = Field(default=3072, ge=512, le=8192)
    reresearch_planning_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    max_writer_context_chars: int = Field(default=36000, ge=500)
    max_report_output_tokens: int = Field(default=6144, ge=512, le=8192)
    max_evidence_per_writing_claim: int = Field(default=3, ge=1, le=12)
    max_writing_evidence_chars: int = Field(default=600, ge=80, le=3000)
    min_writer_claim_coverage: float = Field(default=0.6, ge=0, le=1, allow_inf_nan=False)
    writer_timeout_seconds: float = Field(default=180, gt=0, allow_inf_nan=False)
    writer_enable_thinking: bool = False
    show_writer_debug: bool = False
    max_report_grounding_context_chars: int = Field(default=36000, ge=500)
    report_grounding_max_tokens: int = Field(default=4096, ge=512, le=8192)
    report_grounding_timeout_seconds: float = Field(default=120, gt=0, allow_inf_nan=False)


def get_settings() -> Settings:
    return Settings()
