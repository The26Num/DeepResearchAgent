"""Small observational progress contracts; no model messages or research text."""

from datetime import datetime, timezone
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

EventType = Literal[
    "run_started", "run_snapshot", "stage_changed", "plan_created", "task_started",
    "task_research_completed", "task_completed", "task_failed", "verification_started",
    "verification_completed", "verification_failed", "initial_research_completed",
    "gap_detection_started", "gap_detection_completed", "reresearch_started",
    "reresearch_plan_created", "reresearch_completed", "writer_started", "writer_completed",
    "citation_started", "report_completed", "run_failed", "run_interrupted", "run_completed",
    "stream_reset", "stream_error",
]
Phase = Literal["planning", "research", "verification", "gap_detection", "reresearch", "writer", "citation"]
PhaseStatus = Literal["pending", "running", "completed", "failed", "interrupted", "skipped"]
TaskStatus = Literal["pending", "running", "completed", "failed"]


class PhaseProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    phase: Phase
    status: PhaseStatus


class TaskProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    task_id: str = Field(min_length=1, max_length=160)
    status: TaskStatus


class ProgressData(BaseModel):
    """An allowlist: arbitrary tool output/exception strings cannot be added."""
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    run_status: Literal["RUNNING", "FAILED", "INTERRUPTED", "COMPLETED"] | None = None
    task_status: TaskStatus | None = None
    phase: Phase | None = None
    phase_status: PhaseStatus | None = None
    elapsed_seconds: float | None = Field(default=None, ge=0)
    task_count: int | None = Field(default=None, ge=0)
    completed_tasks: int | None = Field(default=None, ge=0)
    claims: int | None = Field(default=None, ge=0)
    evidence: int | None = Field(default=None, ge=0)
    sources: int | None = Field(default=None, ge=0)
    resume_count: int | None = Field(default=None, ge=0)
    tasks: list[TaskProgress] = Field(default_factory=list, max_length=32)
    phases: list[PhaseProgress] = Field(default_factory=list, max_length=7)


class ProgressUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_type: EventType
    stage: str = Field(min_length=1, max_length=240)
    task_id: str | None = Field(default=None, max_length=160)
    progress_data: ProgressData = Field(default_factory=ProgressData)


MESSAGES = {
    "run_started": "研究执行已启动", "run_snapshot": "已同步当前研究状态", "stage_changed": "研究阶段已更新",
    "plan_created": "研究计划已保存", "task_started": "任务开始执行", "task_research_completed": "任务研究已返回，等待后续处理",
    "task_completed": "任务与必要验证已完成并保存", "task_failed": "任务执行失败，已保留安全检查点",
    "verification_started": "开始验证任务结论", "verification_completed": "任务结论验证已完成",
    "verification_failed": "任务结论验证失败", "initial_research_completed": "初始研究任务已全部完成",
    "gap_detection_started": "开始检查研究相关性与缺口", "gap_detection_completed": "研究缺口检查已完成",
    "reresearch_started": "进入补充研究阶段", "reresearch_plan_created": "补充研究计划已保存",
    "reresearch_completed": "补充研究阶段已结束", "writer_started": "开始撰写研究报告",
    "writer_completed": "研究报告内容已保存，等待引用整理", "citation_started": "开始整理报告引用",
    "report_completed": "带引用的研究报告已保存", "run_failed": "研究执行失败，请查询已保存状态",
    "run_interrupted": "研究执行已中断，请查询已保存状态", "run_completed": "研究已完成",
    "stream_reset": "部分实时事件已过期，请重新同步研究状态", "stream_error": "实时状态暂时不可用，请重新同步",
}


class ResearchEvent(ProgressUpdate):
    run_id: str
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    message: str


EventSink = Callable[[ProgressUpdate], None]


def make_event(run_id: str, update: ProgressUpdate) -> ResearchEvent:
    return ResearchEvent(run_id=run_id, message=MESSAGES[update.event_type], **update.model_dump())
