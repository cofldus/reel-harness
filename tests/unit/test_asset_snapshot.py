"""Stock-media (asset) provider snapshot persistence and pinned resolution,
including legacy-snapshot fallback. All keys are fake placeholders; no
network. The asset block reuses the same provider_config JSON column added in
schema v3 -- no new migration is needed to add snapshot keys to it."""
from __future__ import annotations

import pytest

from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job
from reel_harness.providers.registry import (
    asset_provider_snapshot,
    provider_snapshot,
    resolve_stock_media_for_snapshot,
)
from reel_harness.worker.runner import ProviderBundle, run_job

FAKE_KEY = "FAKE-ASSET-SNAPSHOT-KEY-000000000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _real_asset_settings(**overrides) -> Settings:
    base: dict = dict(
        asset_provider="pexels", asset_api_key=FAKE_KEY,
        asset_base_url="https://api.pexels.com/videos", asset_orientation="portrait",
        asset_min_width=600, asset_min_height=1000,
    )
    base.update(overrides)
    return _settings(**base)


def test_asset_snapshot_shape_and_key_exclusion() -> None:
    snapshot = asset_provider_snapshot(_real_asset_settings())
    assert snapshot["asset_provider"] == "pexels"
    assert snapshot["asset_base_url_host"] == "api.pexels.com"
    assert snapshot["asset_adapter_version"] == "pexels-videos-v1"
    assert snapshot["asset_search_policy"]["orientation"] == "portrait"
    assert snapshot["asset_search_policy"]["min_width"] == 600
    assert snapshot["asset_query_version"]
    assert snapshot["asset_selection_version"]
    assert FAKE_KEY not in str(snapshot)

    combined = provider_snapshot(_real_asset_settings())
    assert combined["llm_provider"] == "fake"
    assert combined["tts_provider"] == "fake"
    assert combined["asset_provider"] == "pexels"


def test_asset_startup_validation_rejects_incomplete_config() -> None:
    validate_provider_settings(_settings())  # fake needs nothing
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_ASSET_API_KEY"):
        validate_provider_settings(_real_asset_settings(asset_api_key=""))


def test_resolution_pins_provider_and_host_from_snapshot() -> None:
    """Environment changes after job creation must not change which asset
    provider/endpoint a job's retries and resumes use."""
    snapshot = asset_provider_snapshot(_real_asset_settings())
    changed_env = _real_asset_settings(asset_min_width=99)  # host unchanged
    provider = resolve_stock_media_for_snapshot(snapshot, changed_env)
    assert provider.provider_id == "pexels"


def test_resolution_honors_fake_snapshot_even_when_env_is_real() -> None:
    provider = resolve_stock_media_for_snapshot({"asset_provider": "fake"}, _real_asset_settings())
    assert provider.provider_id == "fake"


def test_demo_asset_snapshot_shape_and_resolution() -> None:
    snapshot = asset_provider_snapshot(_settings(asset_provider="demo"))
    assert snapshot["asset_provider"] == "demo"
    assert "asset_adapter_version" in snapshot
    validate_provider_settings(_settings(asset_provider="demo"))

    provider = resolve_stock_media_for_snapshot({"asset_provider": "demo"}, _real_asset_settings())
    assert provider.provider_id == "demo"


def test_legacy_snapshots_without_asset_block_fall_back_to_current_settings() -> None:
    """Jobs created before the asset block existed keep working: an LLM/TTS-only
    snapshot resolves the stock-media provider from current settings."""
    legacy_snapshot = {"llm_provider": "fake", "tts_provider": "fake"}
    provider = resolve_stock_media_for_snapshot(legacy_snapshot, _settings())
    assert provider.provider_id == "fake"
    assert resolve_stock_media_for_snapshot(None, _settings()).provider_id == "fake"


def test_host_mismatch_and_unregistered_provider_refuse() -> None:
    snapshot = asset_provider_snapshot(_real_asset_settings())
    moved = _real_asset_settings(asset_base_url="https://other-provider.invalid/videos")
    assert resolve_stock_media_for_snapshot(snapshot, moved).provider_id == "unconfigured"
    vanished = resolve_stock_media_for_snapshot({"asset_provider": "vanished"}, _settings())
    assert vanished.provider_id == "unconfigured"


def test_unsatisfiable_asset_snapshot_fails_the_job_explicitly(
    job_service, channel, session_factory, storage,
) -> None:
    from reel_harness.providers.fake_llm import FakeLLMProvider
    from reel_harness.providers.fake_tts import FakeTTSProvider

    snapshot = {"asset_provider": "pexels", "asset_base_url_host": "api.pexels.com"}
    job, _ = job_service.create_job(channel.id, idempotency_key="asset-pin-1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.provider_config = snapshot
        session.commit()

    bundle = ProviderBundle(
        llm=FakeLLMProvider(),
        tts=FakeTTSProvider(),
        stock_media=resolve_stock_media_for_snapshot(snapshot, _settings()),  # no credentials configured
    )
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, bundle, storage)
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.failure_code == "PROVIDER_NOT_CONFIGURED"
        assert "REEL_HARNESS_ASSET" in db_job.failure_summary


def test_job_creation_persists_the_combined_snapshot(session_factory, storage) -> None:
    from reel_harness.core.service import JobService

    snapshot = provider_snapshot(_real_asset_settings())
    service = JobService(session_factory, storage=storage, provider_snapshot=snapshot)
    channel = service.create_channel(name="c", niche="n", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="combined-asset-1", topic="t")
    with session_factory() as session:
        stored = session.get(Job, job.id).provider_config
        assert stored["asset_provider"] == "pexels"
        assert stored["llm_provider"] == "fake"
        assert FAKE_KEY not in str(stored)
