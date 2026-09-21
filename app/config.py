from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    siliconflow_api_key: str | None = None
    model_name: str | None = None
    siliconflow_base_url: str = "https://api.siliconflow.cn/v1"


def get_settings() -> Settings:
    return Settings()
