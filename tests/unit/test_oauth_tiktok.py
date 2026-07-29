"""publisher.oauth_tiktok: authorization URL building and token
exchange/refresh contract tests (httpx.MockTransport -- no external
network). PKCE/state generation and the loopback server are already
covered by test_oauth_youtube.py (shared publisher.oauth_common)."""
from __future__ import annotations

import urllib.parse

import httpx
import pytest

from reel_harness.core.errors import ProviderAuthError, TransientProviderError
from reel_harness.publisher.oauth_common import generate_pkce, generate_state
from reel_harness.publisher.oauth_tiktok import TikTokOAuthClient, build_authorization_url

FAKE_CLIENT_KEY = "fake-tiktok-client-key"
FAKE_CLIENT_SECRET = "FAKE-TIKTOK-OAUTH-CLIENT-SECRET-0000"
AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"


def test_authorization_url_carries_pkce_state_and_video_publish_scope_never_the_secret() -> None:
    pkce = generate_pkce()
    state = generate_state()
    url = build_authorization_url(FAKE_CLIENT_KEY, "https://example.invalid/callback", state, pkce, AUTH_URL)
    assert url.startswith(AUTH_URL)
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_key"] == [FAKE_CLIENT_KEY]
    assert params["code_challenge"] == [pkce.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [state]
    assert params["scope"] == ["video.publish"]
    assert "client_secret" not in url


def _oauth_client(handler) -> TikTokOAuthClient:
    return TikTokOAuthClient(
        FAKE_CLIENT_KEY, FAKE_CLIENT_SECRET, TOKEN_URL, transport=httpx.MockTransport(handler),
    )


def test_exchange_code_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth-code-1"
        assert body["client_secret"] == FAKE_CLIENT_SECRET
        return httpx.Response(200, json={
            "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 86400,
            "refresh_expires_in": 31536000, "scope": "video.publish", "open_id": "open-id-1",
            "token_type": "Bearer",
        })

    client = _oauth_client(handler)
    tokens = client.exchange_code("auth-code-1", "verifier-1", "https://example.invalid/callback")
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_in == 86400
    assert tokens.refresh_expires_in == 31536000
    assert tokens.open_id == "open-id-1"
    client.close()


def test_refresh_may_return_a_different_refresh_token() -> None:
    """A real behavioral difference from YouTube's refresh (which never
    rotates the refresh_token) -- the caller must always replace the
    stored refresh_token with whatever a refresh response carries."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "old-refresh"
        return httpx.Response(200, json={
            "access_token": "access-2", "refresh_token": "brand-new-refresh", "expires_in": 86400,
            "refresh_expires_in": 31536000, "scope": "video.publish", "open_id": "open-id-1",
        })

    client = _oauth_client(handler)
    tokens = client.refresh("old-refresh")
    assert tokens.access_token == "access-2"
    assert tokens.refresh_token == "brand-new-refresh"
    client.close()


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_token_endpoint_auth_errors_are_not_retried(status_code: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status_code, json={"error": {"code": "invalid_grant"}})

    client = _oauth_client(handler)
    with pytest.raises(ProviderAuthError) as exc_info:
        client.exchange_code("bad-code", "v", "https://example.invalid/callback")
    assert FAKE_CLIENT_SECRET not in str(exc_info.value)
    assert len(calls) == 1
    client.close()


def test_error_envelope_with_http_200_is_treated_as_auth_error() -> None:
    """TikTok's own error envelope can arrive with HTTP 200 -- an
    error.code != 'ok' must never be treated as a successful token."""
    client = _oauth_client(lambda r: httpx.Response(200, json={
        "error": {"code": "invalid_client", "message": "bad client", "log_id": "abc"},
    }))
    with pytest.raises(ProviderAuthError):
        client.refresh("refresh-1")
    client.close()


def test_error_envelope_with_ok_code_is_not_an_error() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, json={
        "error": {"code": "ok", "message": "", "log_id": "abc"},
        "access_token": "access-1", "expires_in": 86400, "scope": "video.publish", "open_id": "o",
    }))
    tokens = client.refresh("refresh-1")
    assert tokens.access_token == "access-1"
    client.close()


def test_token_endpoint_500_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(500))
    with pytest.raises(TransientProviderError):
        client.refresh("refresh-1")
    client.close()


def test_token_endpoint_malformed_json_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(TransientProviderError):
        client.refresh("refresh-1")
    client.close()


def test_token_endpoint_missing_access_token_field_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, json={"expires_in": 10}))
    with pytest.raises(TransientProviderError):
        client.refresh("refresh-1")
    client.close()
