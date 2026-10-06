from typing import Literal

from pydantic import BaseModel, Field


class ResearchTask(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    question: str = Field(
        min_length=1,
        description="A self-contained subquestion explicitly preserving the full research subject and qualifiers from the original user question.",
    )
    description: str = Field(
        min_length=1,
        description="Investigate this subproblem within the original research subject and restrictions; do not replace it with a general component or technology survey.",
    )
    task_type: Literal["discovery", "analysis", "synthesis"] = Field(
        default="discovery",
        description="discovery independently searches new external information; analysis reads and analyzes dependency results; synthesis integrates dependency results into the final answer.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="For generated plans, discovery must use []; analysis and synthesis must reference one or more existing task IDs whose results they need.",
    )
    status: Literal["pending", "running", "completed", "failed"] = "pending"
