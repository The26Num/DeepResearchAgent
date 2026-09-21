from typing import Literal

from pydantic import BaseModel, Field


class ResearchTask(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    question: str = Field(min_length=1)
    description: str = Field(min_length=1)
    task_type: Literal["discovery", "analysis", "synthesis"] = "discovery"
    depends_on: list[str] = Field(default_factory=list)
    status: Literal["pending", "running", "completed", "failed"] = "pending"
