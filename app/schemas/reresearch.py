"""Small supplemental batches; the original plan and DAG remain separate."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.task import ResearchTask


class ReResearchTask(ResearchTask):
    model_config = ConfigDict(extra="forbid")
    task_type: Literal["discovery"] = "discovery"
    origin_task_id: str = Field(min_length=1)
    origin_gap_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def independent_task(self):
        if self.depends_on or len(self.origin_gap_ids) != len(set(self.origin_gap_ids)):
            raise ValueError("Supplemental tasks are independent and origin gaps must be unique.")
        return self


class ReResearchPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    round_id: int = Field(ge=1, le=1)
    selected_gap_ids: list[str]
    tasks: list[ReResearchTask]

    @model_validator(mode="after")
    def validate_origins(self):
        origins = [item.origin_task_id for item in self.tasks]
        ids = [item.id for item in self.tasks]
        selected = [key for item in self.tasks for key in item.origin_gap_ids]
        if (len(origins) != len(set(origins)) or len(ids) != len(set(ids))
                or len(selected) != len(set(selected)) or set(selected) != set(self.selected_gap_ids)
                or len(self.selected_gap_ids) != len(set(self.selected_gap_ids))):
            raise ValueError("Supplemental tasks and selected gaps must have unambiguous origins.")
        return self
