from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./reel_harness.db"
    jobs_dir: Path = Path("./jobs")
    app_api_key: str = "changeme-local-dev-key"
    log_level: str = "INFO"


def load_settings() -> Settings:
    return Settings()
