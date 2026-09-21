from pydantic import BaseModel, Field


class Evidence(BaseModel):
    id: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    excerpt: str | None = None
    source_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0, le=1)
