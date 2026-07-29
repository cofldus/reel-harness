"""providers.registry: publisher resolution and snapshot shape. No network."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from reel_harness.config import Settings
from reel_harness.providers.registry import (
    _resolve_fresh_youtube_access_token,
    publisher_snapshot,
    resolve_publisher,
)
from reel_harness.publisher.credentials import InMemoryCredentialBackend, OAuthCredential

FAKE_CLIENT_SECRET = "FAKE-REGISTRY-CLIENT-SECRET-0000000"


def _settings(**overrides) -> Settings:
    base: dict = dict(youtube_client_id="client-1", youtube_client_secret=FAKE_CLIENT_SECRET)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_resolve_fake_publisher() -> None:
    assert resolve_publisher("fake").provider_id == "fake"


def test_resolve_unknown_publisher_is_unconfigured() -> None:
    from reel_harness.core.errors import ProviderNotConfiguredError

    unconfigured = resolve_publisher("tiktok")
    assert unconfigured.provider_id == "unconfigured"
    with pytest.raises(ProviderNotConfiguredError):
        unconfigured.validate_configuration()


def test_resolve_youtube_without_client_credentials_is_unconfigured() -> None:
    unconfigured = resolve_publisher("youtube", settings=Settings(_env_file=None))
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_youtube_without_saved_credential_is_unconfigured() -> None:
    settings = _settings()
    backend = InMemoryCredentialBackend()
    unconfigured = resolve_publisher(
        "youtube", settings=settings, credential_backend=backend, account_reference="default",
    )
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_youtube_with_saved_credential_builds_real_adapter() -> None:
    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="access-1", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="s",
        provider="youtube", account_reference="default",
    ))
    publisher = resolve_publisher("youtube", settings=settings, credential_backend=backend)
    assert publisher.provider_id == "youtube"


def test_fake_publisher_snapshot_shape() -> None:
    snap = publisher_snapshot(None, "fake", "acct-1")
    assert snap["publisher_provider"] == "fake"
    assert snap["publisher_account_reference"] == "acct-1"


def test_youtube_publisher_snapshot_excludes_secrets() -> None:
    settings = _settings()
    snap = publisher_snapshot(settings, "youtube", "acct-1")
    assert snap["publisher_provider"] == "youtube"
    assert snap["publisher_account_reference"] == "acct-1"
    assert snap["youtube_category_id"] == settings.youtube_category_id
    assert FAKE_CLIENT_SECRET not in str(snap)


def test_access_token_refresh_returns_cached_token_when_not_near_expiry() -> None:
    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="still-valid", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="s",
        provider="youtube", account_reference="default",
    ))
    token = _resolve_fresh_youtube_access_token(settings, backend, "default")
    assert token == "still-valid"


def test_access_token_refresh_when_near_expiry_calls_token_endpoint() -> None:
    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(seconds=30), scope="s",
        provider="youtube", account_reference="default",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 3600, "scope": "s"})

    token = _resolve_fresh_youtube_access_token(
        settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
    )

    assert token == "fresh-token"
    refreshed = backend.get_credential("youtube", "default")
    assert refreshed.access_token == "fresh-token"
    assert refreshed.refresh_token == "refresh-1"  # kept, since the response didn't include a new one


def test_access_token_refresh_without_refresh_token_raises_auth_error() -> None:
    from reel_harness.core.errors import ProviderAuthError

    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1), scope="s",
        provider="youtube", account_reference="default",
    ))
    with pytest.raises(ProviderAuthError):
        _resolve_fresh_youtube_access_token(settings, backend, "default")


def test_access_token_refresh_no_credential_raises_not_configured() -> None:
    from reel_harness.core.errors import ProviderNotConfiguredError

    settings = _settings()
    backend = InMemoryCredentialBackend()
    with pytest.raises(ProviderNotConfiguredError):
        _resolve_fresh_youtube_access_token(settings, backend, "default")


def test_refresh_failure_marks_the_credential_invalid_and_records_the_error(monkeypatch) -> None:
    """Crash-recovery scenario E: an expired access token whose refresh
    token Google has revoked. The failure is recorded on the credential
    (never deleted) so publisher-doctor/publisher-account-show can surface
    it without a network call, and the caller still gets ProviderAuthError
    so run_publication's ordinary AUTH_REQUIRED handling applies -- upload
    is never silently retried against a token we know is dead."""
    from reel_harness.core.errors import ProviderAuthError

    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="revoked-refresh-token",
        expires_at=datetime.now(UTC) - timedelta(seconds=1), scope="s",
        provider="youtube", account_reference="default",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    with pytest.raises(ProviderAuthError):
        _resolve_fresh_youtube_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )

    broken = backend.get_credential("youtube", "default")
    assert broken.invalid is True
    assert broken.last_refresh_error is not None
    assert broken.refresh_token == "revoked-refresh-token"  # preserved for the record, not erased
    assert "revoked-refresh-token" not in broken.last_refresh_error


def test_an_invalid_credential_refuses_further_attempts_without_a_new_network_call() -> None:
    """Once marked invalid, a second attempt must not even try to refresh
    again (no upload retry should ever re-hit a token endpoint we already
    know rejects this credential) -- it must fail immediately."""
    from reel_harness.core.errors import ProviderAuthError

    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="dead", expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scope="s", provider="youtube", account_reference="default", invalid=True,
        last_refresh_error="invalid_grant",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact the token endpoint for an already-invalid credential")

    with pytest.raises(ProviderAuthError, match="invalid"):
        _resolve_fresh_youtube_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )


def test_recovery_after_reauth_clears_the_invalid_marker() -> None:
    """Simulates the operator fixing the problem by re-running
    publisher-auth: a brand-new credential (invalid=False, a fresh access
    token) is saved for the same account, and the next resolution succeeds
    normally -- crash-recovery scenario E's "credential 복구 후 retry 가능"."""
    settings = _settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="dead", expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scope="s", provider="youtube", account_reference="default", invalid=True,
        last_refresh_error="invalid_grant",
    ))
    backend.save_credential(OAuthCredential(
        access_token="brand-new-token", refresh_token="brand-new-refresh",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="s",
        provider="youtube", account_reference="default", invalid=False,
    ))

    token = _resolve_fresh_youtube_access_token(settings, backend, "default")
    assert token == "brand-new-token"
