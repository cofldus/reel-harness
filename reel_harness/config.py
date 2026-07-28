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

    # LLM provider selection and its adapter configuration. "fake" needs none
    # of the rest; "openai-compatible" talks to any /chat/completions-style
    # endpoint chosen via llm_base_url/llm_model. The API key is read from the
    # environment or .env only -- it is registered as a redaction secret at
    # bootstrap and must never be written to the DB, manifests, or logs.
    llm_provider: str = "fake"
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_connect_timeout_seconds: float = 10.0
    llm_read_timeout_seconds: float = 60.0
    llm_max_retries: int = 3
    llm_retry_backoff_seconds: float = 2.0
    llm_temperature: float = 0.7
    llm_max_output_tokens: int = 1200


def load_settings() -> Settings:
    return Settings()
