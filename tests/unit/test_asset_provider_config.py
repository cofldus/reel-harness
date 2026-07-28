"""Startup validation for the stock-media (asset) provider configuration.
All keys are fake placeholders; no network."""
from __future__ import annotations

import pytest

from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

FAKE_KEY = "FAKE-ASSET-CONFIG-KEY-000000000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def _real_asset_settings(**overrides) -> Settings:
    base = dict(asset_provider="pexels", asset_api_key=FAKE_KEY)
    base.update(overrides)
    return _settings(**base)


def test_fake_asset_provider_needs_no_configuration() -> None:
    validate_provider_settings(_settings())


def test_pexels_provider_requires_api_key() -> None:
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_ASSET_API_KEY"):
        validate_provider_settings(_real_asset_settings(asset_api_key=""))


def test_pexels_provider_requires_base_url() -> None:
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_ASSET_BASE_URL"):
        validate_provider_settings(_real_asset_settings(asset_base_url=""))


def test_unknown_asset_provider_name_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="unknown asset provider"):
        validate_provider_settings(_settings(asset_provider="some-other-vendor"))


def test_unsupported_orientation_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="orientation"):
        validate_provider_settings(_settings(asset_orientation="diagonal"))


@pytest.mark.parametrize("field", ["asset_connect_timeout_seconds", "asset_read_timeout_seconds"])
def test_non_positive_timeouts_rejected(field: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="timeouts"):
        validate_provider_settings(_settings(**{field: 0}))


def test_negative_retry_count_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="retry count"):
        validate_provider_settings(_settings(asset_max_retries=-1))


def test_non_positive_per_page_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="per-page"):
        validate_provider_settings(_settings(asset_per_page=0))


@pytest.mark.parametrize("field", ["asset_min_width", "asset_min_height"])
def test_non_positive_min_dimension_rejected(field: str) -> None:
    with pytest.raises(ProviderConfigurationError, match="width/height"):
        validate_provider_settings(_settings(**{field: 0}))


def test_non_positive_min_duration_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="duration"):
        validate_provider_settings(_settings(asset_min_duration_seconds=0))


def test_max_duration_below_min_duration_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="duration"):
        validate_provider_settings(_settings(asset_min_duration_seconds=10.0, asset_max_duration_seconds=5.0))


def test_api_key_never_appears_in_settings_repr() -> None:
    settings = _real_asset_settings()
    assert FAKE_KEY not in repr(settings)
    assert FAKE_KEY not in str(settings)
