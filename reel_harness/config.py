from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfigurationError(ValueError):
    """A real provider is selected but its required configuration is missing or
    invalid. Raised at startup so misconfiguration fails loudly and early --
    never at some later point mid-pipeline."""


def normalize_provider_name(name: str | None) -> str:
    """Accept both spellings ('openai_compatible' and 'openai-compatible');
    canonical form uses hyphens."""
    return (name or "fake").strip().lower().replace("_", "-")


def _llm_alias(canonical: str, *legacy: str) -> AliasChoices:
    return AliasChoices(canonical, *legacy)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    database_url: str = "sqlite:///./reel_harness.db"
    jobs_dir: Path = Path("./jobs")
    app_api_key: str = "changeme-local-dev-key"
    log_level: str = "INFO"

    # Worker lease policy. The heartbeat interval must stay well below the
    # timeout (<= 1/3) so a healthy worker in a long ffmpeg/provider stage is
    # never reclaimed as stale.
    lease_timeout_seconds: int = 300
    lease_heartbeat_seconds: int = 60

    # Continuous worker daemon (reel-harness worker-run). CLI flags override
    # these per invocation.
    worker_poll_interval_seconds: float = 5.0
    worker_idle_exit_after_seconds: float | None = None  # None = run until stopped
    worker_max_jobs: int | None = None  # None = unlimited
    worker_stop_on_error: bool = False

    # LLM provider selection and its adapter configuration. "fake" needs none
    # of the rest; "openai-compatible" (also accepted: "openai_compatible")
    # talks to any /chat/completions-style endpoint chosen via
    # llm_base_url/llm_model. Canonical env vars are REEL_HARNESS_LLM_*; the
    # bare LLM_* names remain accepted for backward compatibility. The API key
    # is a SecretStr (never shown in repr), read from the environment/.env
    # only, registered as a redaction secret at bootstrap, and never written
    # to the DB, manifests, or logs.
    llm_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_LLM_PROVIDER", "LLM_PROVIDER"))
    llm_base_url: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_LLM_BASE_URL", "LLM_BASE_URL"))
    llm_model: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_LLM_MODEL", "LLM_MODEL"))
    llm_api_key: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_LLM_API_KEY", "LLM_API_KEY"))
    llm_connect_timeout_seconds: float = Field(
        10.0, validation_alias=_llm_alias(
            "REEL_HARNESS_LLM_CONNECT_TIMEOUT", "LLM_CONNECT_TIMEOUT_SECONDS"))
    llm_read_timeout_seconds: float = Field(
        60.0, validation_alias=_llm_alias("REEL_HARNESS_LLM_READ_TIMEOUT", "LLM_READ_TIMEOUT_SECONDS"))
    llm_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_LLM_MAX_RETRIES", "LLM_MAX_RETRIES"))
    llm_retry_backoff_seconds: float = Field(
        2.0, validation_alias=_llm_alias("REEL_HARNESS_LLM_RETRY_BACKOFF", "LLM_RETRY_BACKOFF_SECONDS"))
    llm_temperature: float = Field(
        0.7, validation_alias=_llm_alias("REEL_HARNESS_LLM_TEMPERATURE", "LLM_TEMPERATURE"))
    llm_max_output_tokens: int = Field(
        1200, validation_alias=_llm_alias("REEL_HARNESS_LLM_MAX_OUTPUT_TOKENS", "LLM_MAX_OUTPUT_TOKENS"))


def validate_provider_settings(settings: Settings) -> None:
    """Startup gate: selecting a real provider with incomplete configuration
    fails immediately with a clear message (no network is touched). The fake
    provider never requires anything."""
    name = normalize_provider_name(settings.llm_provider)
    if name == "fake":
        return
    if name != "openai-compatible":
        raise ProviderConfigurationError(
            f"unknown llm provider {settings.llm_provider!r} (supported: fake, openai_compatible)"
        )
    missing = [
        var for var, value in (
            ("REEL_HARNESS_LLM_BASE_URL", settings.llm_base_url),
            ("REEL_HARNESS_LLM_MODEL", settings.llm_model),
            ("REEL_HARNESS_LLM_API_KEY", settings.llm_api_key.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "llm provider 'openai-compatible' is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def load_settings() -> Settings:
    # mypy's pydantic integration treats aliased fields as required constructor
    # arguments even though every one has a default and populate_by_name is on.
    return Settings()  # type: ignore[call-arg]
