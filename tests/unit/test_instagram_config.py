"""config.Settings: Instagram OAuth/Content Publishing API settings
validation. No network."""
from __future__ import annotations

import pytest

from reel_harness.config import (
    ProviderConfigurationError,
    Settings,
    validate_instagram_credentials_configured,
    validate_provider_settings,
)

FAKE_SECRET = "FAKE-INSTAGRAM-APP-SECRET-000000000"


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_defaults_need_no_configuration() -> None:
    validate_provider_settings(_settings())  # instagram is opt-in, never required


def test_canonical_env_vars_are_read(monkeypatch) -> None:
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "app-id-1")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", FAKE_SECRET)
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "https://example.invalid/callback")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_MAX_RETRIES", "5")
    settings = Settings(_env_file=None)
    assert settings.instagram_app_id == "app-id-1"
    assert settings.instagram_app_secret.get_secret_value() == FAKE_SECRET
    assert settings.instagram_redirect_uri == "https://example.invalid/callback"
    assert settings.instagram_max_retries == 5


def test_app_secret_never_appears_in_settings_repr() -> None:
    settings = _settings(instagram_app_secret=FAKE_SECRET)
    assert FAKE_SECRET not in repr(settings)
    assert FAKE_SECRET not in str(settings)


def test_default_media_url_mode_is_resumable_no_hosting_needed() -> None:
    assert _settings().instagram_media_url_mode == "resumable"


def test_default_urls_match_official_docs() -> None:
    settings = _settings()
    assert settings.instagram_auth_url == "https://www.instagram.com/oauth/authorize"
    assert settings.instagram_token_url == "https://api.instagram.com/oauth/access_token"
    assert settings.instagram_graph_url == "https://graph.instagram.com"
    assert settings.instagram_graph_api_version == "v25.0"


def test_negative_timeouts_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="timeouts"):
        validate_provider_settings(_settings(instagram_connect_timeout_seconds=0))


def test_negative_retries_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="retry count"):
        validate_provider_settings(_settings(instagram_max_retries=-1))


def test_nonpositive_media_url_ttl_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="TTL"):
        validate_provider_settings(_settings(instagram_media_url_ttl_seconds=0))


def test_unknown_media_url_mode_rejected() -> None:
    with pytest.raises(ProviderConfigurationError, match="media_url_mode"):
        validate_provider_settings(_settings(instagram_media_url_mode="ftp_upload"))


def test_redirect_uri_must_be_https_or_loopback() -> None:
    with pytest.raises(ProviderConfigurationError, match="redirect_uri"):
        validate_provider_settings(_settings(instagram_redirect_uri="ftp://example.invalid/callback"))
    validate_provider_settings(_settings(instagram_redirect_uri="https://example.invalid/callback"))
    validate_provider_settings(_settings(instagram_redirect_uri="http://127.0.0.1:53682/callback"))
    validate_provider_settings(_settings(instagram_redirect_uri="http://localhost:53682/callback"))


def test_empty_redirect_uri_is_allowed_at_startup_but_not_at_auth_time() -> None:
    """instagram is entirely opt-in -- an unset redirect_uri never blocks
    startup, only actually running publisher-auth instagram."""
    validate_provider_settings(_settings())
    with pytest.raises(ProviderConfigurationError, match="REEL_HARNESS_INSTAGRAM_REDIRECT_URI"):
        validate_instagram_credentials_configured(_settings(
            instagram_app_id="a", instagram_app_secret=FAKE_SECRET,
        ))


def test_validate_instagram_credentials_configured_reports_all_missing_vars() -> None:
    with pytest.raises(ProviderConfigurationError) as exc_info:
        validate_instagram_credentials_configured(_settings())
    message = str(exc_info.value)
    assert "REEL_HARNESS_INSTAGRAM_APP_ID" in message
    assert "REEL_HARNESS_INSTAGRAM_APP_SECRET" in message
    assert "REEL_HARNESS_INSTAGRAM_REDIRECT_URI" in message


def test_validate_instagram_credentials_configured_passes_when_complete() -> None:
    validate_instagram_credentials_configured(_settings(
        instagram_app_id="a", instagram_app_secret=FAKE_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    ))


def test_external_url_media_mode_is_explicitly_refused_not_implemented() -> None:
    """external_url is a valid config VALUE (so enabling it later is a
    pure adapter change), but selecting it must fail loudly before any
    network call -- see docs/PUBLISHING.md's 'Two upload paths' section."""
    settings = _settings(
        instagram_app_id="a", instagram_app_secret=FAKE_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
        instagram_media_url_mode="external_url",
    )
    validate_provider_settings(settings)  # a valid config value at startup
    with pytest.raises(ProviderConfigurationError, match="not implemented"):
        validate_instagram_credentials_configured(settings)  # refused when actually attempting to publish
