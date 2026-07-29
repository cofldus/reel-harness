"""config.Settings: TikTok OAuth/Content Posting API settings validation.
No network."""
from __future__ import annotations

import pytest

from reel_harness.config import (
    ProviderConfigurationError,
    Settings,
    validate_provider_settings,
    validate_tiktok_credentials_configured,
)

FAKE_SECRET = "FAKE-TIKTOK-CLIENT-SECRET-00000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_defaults_need_no_configuration() -> None:
    validate_provider_settings(_settings())  # tiktok is opt-in, never required


def test_canonical_env_vars_are_read(monkeypatch) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "client-key-1")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", FAKE_SECRET)
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_UPLOAD_CHUNK_SIZE", "5242880")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_MAX_RETRIES", "5")
    settings = Settings(_env_file=None)
    assert settings.tiktok_client_key == "client-key-1"
    assert settings.tiktok_client_secret.get_secret_value() == FAKE_SECRET
    assert settings.tiktok_redirect_uri == "https://example.invalid/callback"
    assert settings.tiktok_upload_chunk_size == 5242880
    assert settings.tiktok_max_retries == 5


def test_client_secret_never_appears_in_settings_repr() -> None:
    settings = _settings(tiktok_client_secret=FAKE_SECRET)
    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in str(settings)


def test_default_privacy_is_the_most_restrictive_option() -> None:
    assert _settings().tiktok_default_privacy == "SELF_ONLY"


def test_default_base_auth_token_urls_match_official_docs() -> None:
    settings = _settings()
    assert settings.tiktok_base_url == "https://open.tiktokapis.com"
    assert settings.tiktok_auth_url == "https://www.tiktok.com/v2/auth/authorize/"
    assert settings.tiktok_token_url == "https://open.tiktokapis.com/v2/oauth/token/"


def test_nonpositive_chunk_size_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="chunk size"):
        validate_provider_settings(_settings(tiktok_upload_chunk_size=0))


def test_negative_timeouts_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="timeouts"):
        validate_provider_settings(_settings(tiktok_connect_timeout_seconds=0))


def test_negative_retries_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="retry count"):
        validate_provider_settings(_settings(tiktok_max_retries=-1))


def test_redirect_uri_must_be_https_or_loopback() -> None:
    with pytest.raises(ProviderConfigurationError, match="redirect_uri"):
        validate_provider_settings(_settings(tiktok_redirect_uri="ftp://example.invalid/callback"))
    validate_provider_settings(_settings(tiktok_redirect_uri="https://example.invalid/callback"))
    validate_provider_settings(_settings(tiktok_redirect_uri="http://127.0.0.1:53682/callback"))
    validate_provider_settings(_settings(tiktok_redirect_uri="http://localhost:53682/callback"))


def test_empty_redirect_uri_is_allowed_at_startup_but_not_at_auth_time() -> None:
    """tiktok is entirely opt-in -- an unset redirect_uri never blocks
    startup, only actually running publisher-auth tiktok."""
    validate_provider_settings(_settings())
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_TIKTOK_REDIRECT_URI"):
        validate_tiktok_credentials_configured(_settings(
            tiktok_client_key="k", tiktok_client_secret=FAKE_SECRET,
        ))


def test_validate_tiktok_credentials_configured_reports_all_missing_vars() -> None:
    with pytest.raises(ProviderConfigurationError) as exc_info:
        validate_tiktok_credentials_configured(_settings())
    message = str(exc_info.value)
    assert "REEL_HARNESS_TIKTOK_CLIENT_KEY" in message
    assert "REEL_HARNESS_TIKTOK_CLIENT_SECRET" in message
    assert "REEL_HARNESS_TIKTOK_REDIRECT_URI" in message


def test_validate_tiktok_credentials_configured_passes_when_complete() -> None:
    validate_tiktok_credentials_configured(_settings(
        tiktok_client_key="k", tiktok_client_secret=FAKE_SECRET,
        tiktok_redirect_uri="https://example.invalid/callback",
    ))
