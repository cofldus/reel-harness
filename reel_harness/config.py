from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./reel_harness.db"
    jobs_dir: Path = Path("./jobs")
    app_api_key: str = "changeme-local-dev-key"
    log_level: str = "INFO"

    # Worker lease policy. The heartbeat interval must stay well below the
    # timeout (<= 1/3) so a healthy worker in a long ffmpeg/provider stage is
    # never reclaimed as stale.
    lease_timeout_seconds: int = 300
    lease_heartbeat_seconds: int = 60


def load_settings() -> Settings:
    return Settings()
