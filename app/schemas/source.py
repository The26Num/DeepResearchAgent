from pydantic import BaseModel, Field


class Source(BaseModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    url: str = Field(min_length=1)
    snippet: str | None = None
    source_type: str = Field(min_length=1)
    published_at: str | None = None
