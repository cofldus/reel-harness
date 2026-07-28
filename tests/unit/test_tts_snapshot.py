"""TTS snapshot persistence and pinned resolution, including legacy-DB
migration behavior. All keys are fake placeholders; no network."""
from __future__ import annotations

import pytest

from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job
from reel_harness.providers.registry import (
    provider_snapshot,
    resolve_tts_for_snapshot,
    tts_provider_snapshot,
)
from reel_harness.worker.runner import ProviderBundle, run_job

FAKE_KEY = "FAKE-TTS-SNAPSHOT-KEY-000000000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _real_tts_settings(**overrides) -> Settings:
    base = dict(
        tts_provider="openai_compatible",
        tts_base_url="https://tts.example.invalid/v1",
        tts_model="tts-model-1",
        tts_voice="alloy-like",
        tts_api_key=FAKE_KEY,
        tts_format="mp3",
        tts_speed=1.25,
    )
    base.update(overrides)
    return _settings(**base)


def test_tts_snapshot_shape_and_key_exclusion() -> None:
    snapshot = tts_provider_snapshot(_real_tts_settings())
    assert snapshot["tts_provider"] == "openai-compatible"
    assert snapshot["tts_model"] == "tts-model-1"
    assert snapshot["tts_base_url_host"] == "tts.example.invalid"
    assert snapshot["tts_voice"] == "alloy-like"
    assert snapshot["tts_format"] == "mp3"
    assert snapshot["tts_speed"] == 1.25
    assert snapshot["tts_adapter_version"] == "openai-compat-tts-v1"
    assert snapshot["tts_output_policy"]["codec"] == "pcm_s16le"
    assert FAKE_KEY not in str(snapshot)

    combined = provider_snapshot(_real_tts_settings())
    assert combined["llm_provider"] == "fake"
    assert combined["tts_provider"] == "openai-compatible"


def test_tts_startup_validation_rejects_incomplete_or_invalid_config() -> None:
    validate_provider_settings(_settings())  # fake needs nothing

    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_TTS_VOICE"):
        validate_provider_settings(_real_tts_settings(tts_voice=""))
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_TTS_API_KEY"):
        validate_provider_settings(_real_tts_settings(tts_api_key=""))
    with pytest.raises(ProviderConfigurationError, match="unsupported tts format"):
        validate_provider_settings(_settings(tts_format="ogg"))
    with pytest.raises(ProviderConfigurationError, match="speed"):
        validate_provider_settings(_settings(tts_speed=10.0))
    with pytest.raises(ProviderConfigurationError, match="timeouts"):
        validate_provider_settings(_settings(tts_read_timeout_seconds=0))
    with pytest.raises(ProviderConfigurationError, match="retry count"):
        validate_provider_settings(_settings(tts_max_retries=-1))


def test_resolution_pins_voice_model_format_from_snapshot() -> None:
    """Environment changes after job creation must not change the voice/model."""
    snapshot = tts_provider_snapshot(_real_tts_settings())
    changed_env = _real_tts_settings(
        tts_model="different-model", tts_voice="different-voice", tts_format="wav", tts_speed=2.0,
    )
    provider = resolve_tts_for_snapshot(snapshot, changed_env)
    assert provider.provider_id == "openai-compatible"
    assert provider.model_id == "tts-model-1"
    assert provider.voice_id == "alloy-like"
    assert provider.audio_format == "mp3"
    assert provider.speed == 1.25
    provider.close()


def test_resolution_honors_fake_snapshot_even_when_env_is_real() -> None:
    provider = resolve_tts_for_snapshot({"tts_provider": "fake"}, _real_tts_settings())
    assert provider.provider_id == "fake"


def test_legacy_snapshots_without_tts_block_fall_back_to_current_settings() -> None:
    """Jobs created before the TTS block existed (schema v3 DBs) keep working:
    an LLM-only snapshot resolves TTS from current settings."""
    legacy_snapshot = {"llm_provider": "fake", "llm_model": "fake-deterministic-v1"}
    provider = resolve_tts_for_snapshot(legacy_snapshot, _settings())
    assert provider.provider_id == "fake"

    none_at_all = resolve_tts_for_snapshot(None, _settings())
    assert none_at_all.provider_id == "fake"


def test_unsatisfiable_tts_snapshot_fails_the_job_explicitly(
    job_service, channel, session_factory, storage,
) -> None:
    from reel_harness.providers.fake_llm import FakeLLMProvider
    from reel_harness.providers.fake_stock_media import FakeStockMediaProvider

    snapshot = {
        "tts_provider": "openai-compatible", "tts_model": "m", "tts_voice": "v",
        "tts_base_url_host": "tts.example.invalid",
    }
    job, _ = job_service.create_job(channel.id, idempotency_key="tts-pin-1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.provider_config = snapshot
        session.commit()

    bundle = ProviderBundle(
        llm=FakeLLMProvider(),
        tts=resolve_tts_for_snapshot(snapshot, _settings()),  # no credentials configured
        stock_media=FakeStockMediaProvider(),
    )
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, bundle, storage)
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.failure_code == "PROVIDER_NOT_CONFIGURED"
        assert "REEL_HARNESS_TTS" in db_job.failure_summary


def test_host_mismatch_and_unregistered_provider_refuse() -> None:
    snapshot = tts_provider_snapshot(_real_tts_settings())
    moved = _real_tts_settings(tts_base_url="https://other-tts.invalid/v1")
    assert resolve_tts_for_snapshot(snapshot, moved).provider_id == "unconfigured"
    assert resolve_tts_for_snapshot({"tts_provider": "vanished"}, _settings()).provider_id == "unconfigured"


def test_job_creation_persists_the_combined_snapshot(session_factory, storage) -> None:
    from reel_harness.core.service import JobService

    snapshot = provider_snapshot(_real_tts_settings())
    service = JobService(session_factory, storage=storage, provider_snapshot=snapshot)
    channel = service.create_channel(name="c", niche="n", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="combined-1", topic="t")
    with session_factory() as session:
        stored = session.get(Job, job.id).provider_config
        assert stored["tts_provider"] == "openai-compatible"
        assert stored["llm_provider"] == "fake"
        assert FAKE_KEY not in str(stored)
