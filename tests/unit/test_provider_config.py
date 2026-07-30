"""Provider configuration, snapshot persistence, and snapshot-honoring
resolution. All keys are obviously-fake placeholders; no network is touched.
"""
from __future__ import annotations

import pytest

from reel_harness.config import (
    ProviderConfigurationError,
    Settings,
    normalize_provider_name,
    validate_provider_settings,
)
from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job
from reel_harness.providers.registry import llm_provider_snapshot, resolve_llm_for_snapshot
from reel_harness.worker.runner import ProviderBundle, run_job

FAKE_KEY = "FAKE-PROVIDER-CONFIG-KEY-0000000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_canonical_env_vars_are_read(monkeypatch) -> None:
    monkeypatch.setenv("REEL_HARNESS_LLM_PROVIDER", "openai_compatible")
    monkeypatch.setenv("REEL_HARNESS_LLM_BASE_URL", "https://llm.example.invalid/v1")
    monkeypatch.setenv("REEL_HARNESS_LLM_MODEL", "test-model")
    monkeypatch.setenv("REEL_HARNESS_LLM_API_KEY", FAKE_KEY)
    monkeypatch.setenv("REEL_HARNESS_LLM_MAX_RETRIES", "7")
    monkeypatch.setenv("REEL_HARNESS_LLM_RETRY_BACKOFF", "0.5")
    settings = Settings(_env_file=None)
    assert settings.llm_provider == "openai_compatible"
    assert settings.llm_model == "test-model"
    assert settings.llm_api_key.get_secret_value() == FAKE_KEY
    assert settings.llm_max_retries == 7
    assert settings.llm_retry_backoff_seconds == 0.5


def test_api_key_never_appears_in_settings_repr() -> None:
    settings = _settings(llm_api_key=FAKE_KEY)
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)


def test_validation_passes_for_demo_with_no_credentials() -> None:
    validate_provider_settings(_settings(llm_provider="demo"))


def test_validation_passes_for_fake_and_fails_clearly_for_incomplete_real() -> None:
    validate_provider_settings(_settings())  # fake needs nothing

    with pytest.raises(ProviderConfigurationError, match="not configured"):
        validate_provider_settings(_settings(llm_provider="openai_compatible"))

    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_LLM_API_KEY"):
        validate_provider_settings(_settings(
            llm_provider="openai-compatible",
            llm_base_url="https://llm.example.invalid/v1", llm_model="m",
        ))

    with pytest.raises(ProviderConfigurationError, match="unknown llm provider"):
        validate_provider_settings(_settings(llm_provider="totally-made-up"))


def test_provider_name_normalization() -> None:
    assert normalize_provider_name("openai_compatible") == "openai-compatible"
    assert normalize_provider_name(" OPENAI_COMPATIBLE ") == "openai-compatible"
    assert normalize_provider_name(None) == "fake"
    assert normalize_provider_name("") == "fake"


def test_snapshot_shape_for_fake_and_real() -> None:
    fake_snapshot = llm_provider_snapshot(_settings())
    assert fake_snapshot["llm_provider"] == "fake"
    assert "prompt_version" in fake_snapshot

    demo_snapshot = llm_provider_snapshot(_settings(llm_provider="demo"))
    assert demo_snapshot["llm_provider"] == "demo"
    assert "prompt_version" in demo_snapshot

    real_snapshot = llm_provider_snapshot(_settings(
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.invalid/v1", llm_model="test-model", llm_api_key=FAKE_KEY,
        llm_temperature=0.3, llm_max_output_tokens=900,
    ))
    assert real_snapshot["llm_provider"] == "openai-compatible"
    assert real_snapshot["llm_model"] == "test-model"
    assert real_snapshot["llm_base_url_host"] == "llm.example.invalid"
    assert real_snapshot["temperature"] == 0.3
    assert real_snapshot["max_output_tokens"] == 900
    assert FAKE_KEY not in str(real_snapshot), "the API key must never enter the snapshot"
    assert "/v1" not in str(real_snapshot.get("llm_base_url_host")), "host only, not the full URL"


def test_job_persists_the_snapshot_at_creation(session_factory, storage) -> None:
    snapshot = {"llm_provider": "fake", "llm_model": "fake-deterministic-v1", "prompt_version": "v"}
    service = JobService(session_factory, storage=storage, provider_snapshot=snapshot)
    channel = service.create_channel(name="c", niche="n", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="snap-1", topic="t")
    with session_factory() as session:
        stored = session.get(Job, job.id)
        assert stored.provider_config == snapshot
        assert stored.provider_config is not snapshot, "each job stores its own copy"


def test_create_job_snapshot_override_applies_only_to_that_job(session_factory, storage) -> None:
    """The web UI's per-job provider-profile choice: an explicit
    provider_snapshot passed to create_job wins over the service's
    constructor-level default for that one job, without changing what any
    other job (or a job created without an override) gets."""
    default_snapshot = {"llm_provider": "fake", "llm_model": "fake-deterministic-v1", "prompt_version": "v"}
    service = JobService(session_factory, storage=storage, provider_snapshot=default_snapshot)
    channel = service.create_channel(name="c", niche="n", language="en")

    override_snapshot = {"llm_provider": "demo", "llm_model": "demo-deterministic-v1", "prompt_version": "v2"}
    overridden_job, _ = service.create_job(
        channel.id, idempotency_key="override-1", topic="t", provider_snapshot=override_snapshot,
    )
    default_job, _ = service.create_job(channel.id, idempotency_key="default-1", topic="t")

    with session_factory() as session:
        assert session.get(Job, overridden_job.id).provider_config == override_snapshot
        assert session.get(Job, default_job.id).provider_config == default_snapshot


def test_resolution_honors_fake_snapshot_even_when_env_is_real() -> None:
    """Changing environment variables after job creation must not switch an
    existing job's provider."""
    real_settings = _settings(
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.invalid/v1", llm_model="env-model", llm_api_key=FAKE_KEY,
    )
    llm = resolve_llm_for_snapshot({"llm_provider": "fake"}, real_settings)
    assert llm.provider_id == "fake"


def test_resolution_honors_demo_snapshot_even_when_env_is_real() -> None:
    real_settings = _settings(
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.invalid/v1", llm_model="env-model", llm_api_key=FAKE_KEY,
    )
    llm = resolve_llm_for_snapshot({"llm_provider": "demo"}, real_settings)
    assert llm.provider_id == "demo"


def test_resolution_pins_model_and_sampling_from_the_snapshot() -> None:
    settings = _settings(
        llm_provider="openai_compatible",
        llm_base_url="https://llm.example.invalid/v1", llm_model="env-model", llm_api_key=FAKE_KEY,
        llm_temperature=0.9, llm_max_output_tokens=2000,
    )
    snapshot = {
        "llm_provider": "openai-compatible", "llm_model": "pinned-model",
        "llm_base_url_host": "llm.example.invalid", "temperature": 0.2, "max_output_tokens": 700,
    }
    llm = resolve_llm_for_snapshot(snapshot, settings)
    assert llm.provider_id == "openai-compatible"
    assert llm.model_id == "pinned-model"  # snapshot wins over env model
    llm.close()


def test_resolution_refuses_host_mismatch_and_missing_credentials() -> None:
    snapshot = {
        "llm_provider": "openai-compatible", "llm_model": "m",
        "llm_base_url_host": "llm.example.invalid",
    }
    moved = _settings(
        llm_provider="openai_compatible",
        llm_base_url="https://other-host.invalid/v1", llm_model="m", llm_api_key=FAKE_KEY,
    )
    assert resolve_llm_for_snapshot(snapshot, moved).provider_id == "unconfigured"

    unconfigured = resolve_llm_for_snapshot(snapshot, _settings())
    assert unconfigured.provider_id == "unconfigured"

    unknown = resolve_llm_for_snapshot({"llm_provider": "vanished-provider"}, _settings())
    assert unknown.provider_id == "unconfigured"


def test_unsatisfiable_snapshot_fails_the_job_explicitly(
    job_service, channel, session_factory, storage,
) -> None:
    """A pinned-but-unresolvable provider must fail the job with
    PROVIDER_NOT_CONFIGURED -- never crash the worker or fall back silently."""
    from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
    from reel_harness.providers.fake_tts import FakeTTSProvider
    from reel_harness.providers.registry import resolve_llm_for_snapshot as resolve

    job, _ = job_service.create_job(channel.id, idempotency_key="pin-1", topic="t")
    snapshot = {"llm_provider": "openai-compatible", "llm_model": "m", "llm_base_url_host": "x.invalid"}
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.provider_config = snapshot
        session.commit()

    bundle = ProviderBundle(
        llm=resolve(snapshot, _settings()), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider(),
    )
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, bundle, storage)
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.failure_code == "PROVIDER_NOT_CONFIGURED"
        assert "not configured" in db_job.failure_summary or "REEL_HARNESS" in db_job.failure_summary


def test_provider_smoke_cli_refuses_when_credentials_not_configured(monkeypatch, tmp_path) -> None:
    from reel_harness.cli import main as cli_main

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)  # no repo .env; no accidental repo DB writes

    # Real provider selected but no credentials: clear startup failure, exit 2.
    monkeypatch.setenv("REEL_HARNESS_LLM_PROVIDER", "openai_compatible")
    assert cli_main.main(["provider-smoke", "llm"]) == 2

    # Fake provider: nothing to smoke, exit 2 with guidance (no network).
    monkeypatch.setenv("REEL_HARNESS_LLM_PROVIDER", "fake")
    assert cli_main.main(["provider-smoke", "llm"]) == 2
