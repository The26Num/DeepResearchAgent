from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    siliconflow_api_key: str | None = None
    tavily_api_key: str | None = None
    model_name: str | None = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"
    model_request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    model_max_retries: int = Field(default=0, ge=0)
    research_task_timeout_seconds: float = Field(default=300, gt=0, allow_inf_nan=False)
    research_progress_interval_seconds: float = Field(default=15, gt=0, allow_inf_nan=False)


def get_settings() -> Settings:
    return Settings()
