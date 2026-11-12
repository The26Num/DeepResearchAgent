"""Portable resource references, never full pages or machine-specific paths."""

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal


class ResourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotRef(ResourceModel):
    snapshot_id: str = Field(pattern=r"^snap_[0-9a-f]{64}$")
    run_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,100}$")
    relative_dir: str
    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    raw_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    metadata_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    parser_version: str
    requested_url: str = Field(max_length=2000)
    url: str = Field(max_length=2000)
    title: str = Field(max_length=500)
    status: str
    body_chars: int = Field(ge=0)
    discovered_by_task: str

    @model_validator(mode="after")
    def portable_location(self):
        if self.relative_dir != f"{self.run_id}/snapshots/{self.snapshot_id}":
            raise ValueError("Invalid archive relative directory.")
        return self


class ReadingRecord(ResourceModel):
    run_id: str
    task_id: str
    attempt_id: str
    snapshot_id: str
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    text_hash: str
    kind: Literal["fetch_preview", "read_source", "adapter_preview"]
    sequence: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered_range(self):
        if self.end_char <= self.start_char:
            raise ValueError("Reading range must be nonempty.")
        return self


class SkillPin(ResourceModel):
    skill_id: str
    version: str = Field(pattern=r"^[0-9a-f]{64}$")
    relative_dir: str
    files: dict[str, str]


class TaskResourceRecord(ResourceModel):
    task_id: str
    revision: int = Field(default=0, ge=0)
    attempt_id: str = ""
    attempts: int = Field(default=0, ge=0)
    committed: bool = False
    allowed_tasks: list[str] = Field(default_factory=list)
    snapshots: dict[str, SnapshotRef] = Field(default_factory=dict)
    readings: list[ReadingRecord] = Field(default_factory=list)
    search_urls: list[str] = Field(default_factory=list, max_length=15)
    active_skills: dict[str, str] = Field(default_factory=dict)
    active_references: list[str] = Field(default_factory=list)
    limits: dict[str, int] = Field(default_factory=dict)
    calls: dict[str, int] = Field(default_factory=dict)
    tool_history: list[str] = Field(default_factory=list)
    model_calls: dict[str, int] = Field(default_factory=dict)
    estimated_tokens: int = Field(default=0, ge=0)
    estimated_token_budget_enforced: bool = True
    actual_input_tokens: int = Field(default=0, ge=0)
    actual_output_tokens: int = Field(default=0, ge=0)
    usage_responses: int = Field(default=0, ge=0)
    reserved_source_chars: int = Field(default=0, ge=0)
    returned_source_chars: int = Field(default=0, ge=0)
    reserved_download_bytes: int = Field(default=0, ge=0)
    downloaded_bytes: int = Field(default=0, ge=0)
    skill_chars: int = Field(default=0, ge=0)
    context_compactions: int = Field(default=0, ge=0)
    omitted_messages: int = Field(default=0, ge=0)
    token_estimation: str = "utf8_bytes_plus_framing_v1_conservative"

    @model_validator(mode="after")
    def nonnegative_counters(self):
        if any(type(value) is not int or value < 0 for mapping in (self.calls, self.model_calls, self.limits) for value in mapping.values()):
            raise ValueError("Invalid resource counters/limits.")
        if any(len(url) > 2000 for url in self.search_urls):
            raise ValueError("Search navigation URL exceeds its metadata limit.")
        return self


class RunResourceState(ResourceModel):
    run_id: str | None = None
    skills: dict[str, SkillPin] = Field(default_factory=dict)
    tasks: dict[str, TaskResourceRecord] = Field(default_factory=dict)
