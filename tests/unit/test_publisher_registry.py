"""providers.registry: publisher resolution and snapshot shape. No network."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from reel_harness.config import Settings
from reel_harness.providers.registry import (
    _resolve_fresh_tiktok_access_token,
    _resolve_fresh_youtube_access_token,
    provider_capabilities,
    publisher_snapshot,
    resolve_publisher,
)
from reel_harness.publisher.credentials import InMemoryCredentialBackend, OAuthCredential

FAKE_CLIENT_SECRET = "FAKE-REGISTRY-CLIENT-SECRET-0000000"


def _tiktok_settings(**overrides) -> Settings:
    base: dict = dict(
        tiktok_client_key="client-1", tiktok_client_secret=FAKE_CLIENT_SECRET,
        tiktok_redirect_uri="https://example.invalid/callback",
    )
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _settings(**overrides) -> Settings:
    base: dict = dict(youtube_client_id="client-1", youtube_client_secret=FAKE_CLIENT_SECRET)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_resolve_fake_publisher() -> None:
    assert resolve_publisher("fake").provider_id == "fake"


def test_resolve_unknown_publisher_is_unconfigured() -> None:
    from reel_harness.core.errors import ProviderNotConfiguredError

    unconfigured = resolve_publisher("facebook")
    assert unconfigured.provider_id == "unconfigured"
    with pytest.raises(ProviderNotConfiguredError):
        unconfigured.validate_configuration()


def test_resolve_tiktok_without_client_credentials_is_unconfigured() -> None:
    unconfigured = resolve_publisher("tiktok", settings=Settings(_env_file=None))
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_tiktok_without_saved_credential_is_unconfigured() -> None:
    settings = Settings(
        _env_file=None, tiktok_client_key="key-1", tiktok_client_secret=FAKE_CLIENT_SECRET,
        tiktok_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    unconfigured = resolve_publisher(
        "tiktok", settings=settings, credential_backend=backend, account_reference="default",
    )
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_tiktok_with_saved_credential_builds_real_adapter() -> None:
    settings = Settings(
        _env_file=None, tiktok_client_key="key-1", tiktok_client_secret=FAKE_CLIENT_SECRET,
        tiktok_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="access-1", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="video.publish",
        provider="tiktok", account_reference="default", channel_id="open-id-1",
    ))
    publisher = resolve_publisher("tiktok", settings=settings, credential_backend=backend)
    assert publisher.provider_id == "tiktok"


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


def test_fake_provider_capabilities_are_credential_free() -> None:
    caps = provider_capabilities("fake")
    assert caps.default_privacy == "private"
    assert caps.privacy_values == frozenset({"private", "unlisted", "public"})
    assert caps.public_privacy_values == frozenset({"public"})
    assert caps.requires_user_confirmation is False


def test_youtube_provider_capabilities_match_fake_shape() -> None:
    """No credentials, no Settings -- provider_capabilities must work before
    any adapter instance can even be constructed (dry-run, pre-auth
    validation)."""
    caps = provider_capabilities("youtube")
    assert caps.default_privacy == "private"
    assert caps.privacy_values == frozenset({"private", "unlisted", "public"})
    assert caps.public_privacy_values == frozenset({"public"})
    assert caps.supports_comments_control is False
    assert caps.supports_remix_control is False
    assert caps.requires_creator_info is False


def test_tiktok_provider_capabilities_are_credential_free() -> None:
    """No credentials, no Settings -- usable during --dry-run/validation
    before any adapter instance can be constructed, same as youtube's."""
    caps = provider_capabilities("tiktok")
    assert caps.default_privacy == "SELF_ONLY"
    assert caps.privacy_values == frozenset({
        "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY",
    })
    assert caps.public_privacy_values == frozenset({"PUBLIC_TO_EVERYONE"})
    assert caps.supports_comments_control is True
    assert caps.supports_remix_control is True
    assert caps.requires_creator_info is True
    assert caps.requires_user_confirmation is True


def test_unregistered_provider_capabilities_raise_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        provider_capabilities("facebook")


def test_tiktok_publisher_snapshot_excludes_secrets() -> None:
    settings = Settings(
        _env_file=None, tiktok_client_key="key-1", tiktok_client_secret=FAKE_CLIENT_SECRET,
        tiktok_redirect_uri="https://example.invalid/callback",
    )
    snap = publisher_snapshot(settings, "tiktok", "acct-1")
    assert snap["publisher_provider"] == "tiktok"
    assert snap["publisher_account_reference"] == "acct-1"
    assert snap["tiktok_chunk_size"] == settings.tiktok_upload_chunk_size
    assert FAKE_CLIENT_SECRET not in str(snap)


def test_default_platform_options() -> None:
    from reel_harness.providers.registry import default_platform_options

    assert default_platform_options("youtube") == {}
    assert default_platform_options("fake") == {}
    tiktok_options = default_platform_options("tiktok")
    assert tiktok_options["disable_comment"] is True
    assert tiktok_options["disable_duet"] is True
    assert tiktok_options["disable_stitch"] is True
    assert tiktok_options["is_aigc"] is False
    instagram_options = default_platform_options("instagram")
    assert instagram_options["share_to_feed"] is False
    assert instagram_options["collaborators"] == []


def test_instagram_provider_capabilities_are_credential_free() -> None:
    """No credentials, no Settings -- usable during --dry-run/validation
    before any adapter instance can be constructed, same as tiktok's."""
    caps = provider_capabilities("instagram")
    assert caps.default_privacy == "PUBLIC"
    assert caps.privacy_values == frozenset({"PUBLIC"})
    assert caps.public_privacy_values == frozenset({"PUBLIC"})  # every publish is public -- see docs/PUBLISHING.md
    assert caps.supports_comments_control is False
    assert caps.supports_remix_control is False
    assert caps.requires_creator_info is True
    assert caps.requires_user_confirmation is True


def test_instagram_publisher_snapshot_excludes_secrets() -> None:
    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    snap = publisher_snapshot(settings, "instagram", "acct-1")
    assert snap["publisher_provider"] == "instagram"
    assert snap["publisher_account_reference"] == "acct-1"
    assert snap["instagram_api_version"] == settings.instagram_graph_api_version
    assert FAKE_CLIENT_SECRET not in str(snap)


def test_resolve_instagram_without_client_credentials_is_unconfigured() -> None:
    unconfigured = resolve_publisher("instagram", settings=Settings(_env_file=None))
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_instagram_without_saved_credential_is_unconfigured() -> None:
    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    unconfigured = resolve_publisher(
        "instagram", settings=settings, credential_backend=backend, account_reference="default",
    )
    assert unconfigured.provider_id == "unconfigured"


def test_resolve_instagram_with_saved_credential_builds_real_adapter() -> None:
    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="access-1", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="instagram_business_content_publish",
        provider="instagram", account_reference="default", channel_id="17841400", channel_title="my_reel_account",
    ))
    publisher = resolve_publisher("instagram", settings=settings, credential_backend=backend)
    assert publisher.provider_id == "instagram"


def test_resolve_instagram_credential_without_account_id_is_unconfigured() -> None:
    """A credential missing channel_id (the instagram account id, required
    to build every content-publishing URL) can't build a real adapter --
    this should never happen for a credential publisher-auth actually
    saved, but is defensively checked anyway."""
    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="access-1", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="instagram_business_content_publish",
        provider="instagram", account_reference="default", channel_id=None,
    ))
    unconfigured = resolve_publisher("instagram", settings=settings, credential_backend=backend)
    assert unconfigured.provider_id == "unconfigured"


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


def test_tiktok_access_token_refresh_returns_cached_token_when_not_near_expiry() -> None:
    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="still-valid", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(hours=1), scope="video.publish",
        provider="tiktok", account_reference="default",
    ))
    token = _resolve_fresh_tiktok_access_token(settings, backend, "default")
    assert token == "still-valid"


def test_tiktok_access_token_refresh_when_near_expiry_calls_token_endpoint() -> None:
    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="refresh-1",
        expires_at=datetime.now(UTC) + timedelta(seconds=30), scope="video.publish",
        provider="tiktok", account_reference="default",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "fresh-token", "expires_in": 86400, "refresh_expires_in": 31536000,
            "scope": "video.publish", "open_id": "open-id-1",
        })

    token = _resolve_fresh_tiktok_access_token(
        settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
    )

    assert token == "fresh-token"
    refreshed = backend.get_credential("tiktok", "default")
    assert refreshed.access_token == "fresh-token"
    assert refreshed.refresh_token == "refresh-1"  # kept, since the response didn't include a new one
    assert refreshed.refresh_expires_at is not None


def test_tiktok_refresh_response_rotating_the_refresh_token_replaces_the_stored_one() -> None:
    """A real behavioral difference from YouTube: TikTok's refresh call MAY
    return a *different* refresh_token, which must always replace the
    stored one -- never assumed unchanged."""
    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="old-refresh",
        expires_at=datetime.now(UTC) + timedelta(seconds=30), scope="video.publish",
        provider="tiktok", account_reference="default",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "access_token": "fresh-token", "refresh_token": "brand-new-refresh", "expires_in": 86400,
            "refresh_expires_in": 31536000, "scope": "video.publish", "open_id": "open-id-1",
        })

    _resolve_fresh_tiktok_access_token(
        settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
    )
    refreshed = backend.get_credential("tiktok", "default")
    assert refreshed.refresh_token == "brand-new-refresh"


def test_tiktok_access_token_refresh_without_refresh_token_raises_auth_error() -> None:
    from reel_harness.core.errors import ProviderAuthError

    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1), scope="video.publish",
        provider="tiktok", account_reference="default",
    ))
    with pytest.raises(ProviderAuthError):
        _resolve_fresh_tiktok_access_token(settings, backend, "default")


def test_tiktok_access_token_refresh_no_credential_raises_not_configured() -> None:
    from reel_harness.core.errors import ProviderNotConfiguredError

    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    with pytest.raises(ProviderNotConfiguredError):
        _resolve_fresh_tiktok_access_token(settings, backend, "default")


def test_tiktok_refresh_failure_marks_the_credential_invalid_and_records_the_error() -> None:
    from reel_harness.core.errors import ProviderAuthError

    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="revoked-refresh-token",
        expires_at=datetime.now(UTC) - timedelta(seconds=1), scope="video.publish",
        provider="tiktok", account_reference="default",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"code": "invalid_grant"}})

    with pytest.raises(ProviderAuthError):
        _resolve_fresh_tiktok_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )

    broken = backend.get_credential("tiktok", "default")
    assert broken.invalid is True
    assert broken.last_refresh_error is not None
    assert broken.refresh_token == "revoked-refresh-token"
    assert "revoked-refresh-token" not in broken.last_refresh_error


def test_tiktok_invalid_credential_refuses_further_attempts_without_a_new_network_call() -> None:
    from reel_harness.core.errors import ProviderAuthError

    settings = _tiktok_settings()
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token="dead", expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scope="video.publish", provider="tiktok", account_reference="default", invalid=True,
        last_refresh_error="invalid_grant",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact the token endpoint for an already-invalid credential")

    with pytest.raises(ProviderAuthError, match="invalid"):
        _resolve_fresh_tiktok_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )


def test_instagram_access_token_refresh_returns_cached_token_when_not_near_expiry() -> None:
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="still-valid", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(days=30), scope="instagram_business_content_publish",
        provider="instagram", account_reference="default", channel_id="17841400",
    ))
    token = _resolve_fresh_instagram_access_token(settings, backend, "default")
    assert token == "still-valid"


def test_instagram_access_token_refresh_when_near_expiry_calls_refresh_endpoint() -> None:
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="stale", refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=30), scope="instagram_business_content_publish",
        provider="instagram", account_reference="default", channel_id="17841400", channel_title="my_reel_account",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/refresh_access_token"
        assert dict(request.url.params)["access_token"] == "stale"  # presents itself, not a separate refresh_token
        return httpx.Response(200, json={"access_token": "fresh-token", "expires_in": 5184000})

    token = _resolve_fresh_instagram_access_token(
        settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
    )

    assert token == "fresh-token"
    refreshed = backend.get_credential("instagram", "default")
    assert refreshed.access_token == "fresh-token"
    assert refreshed.refresh_token is None  # instagram never has a separate refresh_token
    assert refreshed.channel_id == "17841400"
    assert refreshed.channel_title == "my_reel_account"


def test_instagram_access_token_refresh_no_credential_raises_not_configured() -> None:
    from reel_harness.core.errors import ProviderNotConfiguredError
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    with pytest.raises(ProviderNotConfiguredError):
        _resolve_fresh_instagram_access_token(settings, backend, "default")


def test_instagram_refresh_failure_marks_the_credential_invalid_and_records_the_error() -> None:
    from reel_harness.core.errors import ProviderAuthError
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="too-young-or-expired", refresh_token=None,
        expires_at=datetime.now(UTC) - timedelta(seconds=1), scope="instagram_business_content_publish",
        provider="instagram", account_reference="default", channel_id="17841400",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "token too young to refresh"}})

    with pytest.raises(ProviderAuthError):
        _resolve_fresh_instagram_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )

    broken = backend.get_credential("instagram", "default")
    assert broken.invalid is True
    assert broken.last_refresh_error is not None


def test_instagram_invalid_credential_refuses_further_attempts_without_a_new_network_call() -> None:
    from reel_harness.core.errors import ProviderAuthError
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    settings = Settings(
        _env_file=None, instagram_app_id="app-1", instagram_app_secret=FAKE_CLIENT_SECRET,
        instagram_redirect_uri="https://example.invalid/callback",
    )
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="dead", refresh_token=None, expires_at=datetime.now(UTC) - timedelta(seconds=1),
        scope="instagram_business_content_publish", provider="instagram", account_reference="default",
        channel_id="17841400", invalid=True, last_refresh_error="token too young to refresh",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not contact the refresh endpoint for an already-invalid credential")

    with pytest.raises(ProviderAuthError, match="invalid"):
        _resolve_fresh_instagram_access_token(
            settings, backend, "default", oauth_transport=httpx.MockTransport(handler),
        )
