"""publisher.oauth_instagram: authorization URL building and the
three-step token flow (code exchange -> long-lived exchange -> refresh)
contract tests (httpx.MockTransport -- no external network). PKCE/state
generation and the loopback server are already covered by
test_oauth_youtube.py (shared publisher.oauth_common)."""
from __future__ import annotations

import urllib.parse

import httpx
import pytest

from reel_harness.core.errors import ProviderAuthError, TransientProviderError
from reel_harness.publisher.oauth_common import generate_pkce, generate_state
from reel_harness.publisher.oauth_instagram import InstagramOAuthClient, build_authorization_url

FAKE_APP_ID = "fake-instagram-app-id"
FAKE_APP_SECRET = "FAKE-INSTAGRAM-OAUTH-APP-SECRET-000"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_URL = "https://graph.instagram.com"


def test_authorization_url_carries_pkce_state_and_scopes_never_the_secret() -> None:
    pkce = generate_pkce()
    state = generate_state()
    url = build_authorization_url(
        FAKE_APP_ID, "https://example.invalid/callback", state, pkce,
        "https://www.instagram.com/oauth/authorize",
    )
    assert url.startswith("https://www.instagram.com/oauth/authorize")
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_id"] == [FAKE_APP_ID]
    assert params["code_challenge"] == [pkce.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [state]
    assert "instagram_business_content_publish" in params["scope"][0]
    assert "instagram_business_basic" in params["scope"][0]
    assert "client_secret" not in url


def _oauth_client(handler) -> InstagramOAuthClient:
    return InstagramOAuthClient(
        FAKE_APP_ID, FAKE_APP_SECRET, TOKEN_URL, GRAPH_URL, transport=httpx.MockTransport(handler),
    )


def test_exchange_code_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/oauth/access_token"
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth-code-1"
        assert body["client_secret"] == FAKE_APP_SECRET
        return httpx.Response(200, json={"access_token": "short-lived-1", "user_id": "17841400"})

    client = _oauth_client(handler)
    tokens = client.exchange_code("auth-code-1", "verifier-1", "https://example.invalid/callback")
    assert tokens.access_token == "short-lived-1"
    assert tokens.user_id == "17841400"
    client.close()


def test_exchange_long_lived_token_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/access_token"
        params = dict(request.url.params)
        assert params["grant_type"] == "ig_exchange_token"
        assert params["client_secret"] == FAKE_APP_SECRET
        assert params["access_token"] == "short-lived-1"
        return httpx.Response(200, json={"access_token": "long-lived-1", "expires_in": 5184000})

    client = _oauth_client(handler)
    tokens = client.exchange_long_lived_token("short-lived-1")
    assert tokens.access_token == "long-lived-1"
    assert tokens.expires_in == 5184000
    client.close()


def test_refresh_long_lived_token_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/refresh_access_token"
        params = dict(request.url.params)
        assert params["grant_type"] == "ig_refresh_token"
        assert params["access_token"] == "long-lived-1"
        assert "client_secret" not in params  # refresh presents the token itself, not the app secret
        return httpx.Response(200, json={"access_token": "long-lived-2", "expires_in": 5184000})

    client = _oauth_client(handler)
    tokens = client.refresh_long_lived_token("long-lived-1")
    assert tokens.access_token == "long-lived-2"
    client.close()


def test_fetch_account_identity_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/me"
        return httpx.Response(200, json={"user_id": "17841400", "username": "my_reel_account"})

    client = _oauth_client(handler)
    identity = client.fetch_account_identity("long-lived-1")
    assert identity.account_id == "17841400"
    assert identity.username == "my_reel_account"
    client.close()


def test_fetch_account_identity_missing_id_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, json={"username": "x"}))
    with pytest.raises(TransientProviderError):
        client.fetch_account_identity("long-lived-1")
    client.close()


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_token_endpoint_auth_errors_are_not_retried(status_code: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status_code, json={"error": {"message": "bad"}})

    client = _oauth_client(handler)
    with pytest.raises(ProviderAuthError) as exc_info:
        client.exchange_code("bad-code", "v", "https://example.invalid/callback")
    assert FAKE_APP_SECRET not in str(exc_info.value)
    assert len(calls) == 1
    client.close()


def test_long_lived_exchange_401_is_auth_error() -> None:
    client = _oauth_client(lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        client.exchange_long_lived_token("short-lived-1")
    client.close()


def test_refresh_401_is_auth_error() -> None:
    client = _oauth_client(lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        client.refresh_long_lived_token("long-lived-1")
    client.close()


def test_token_endpoint_500_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(500))
    with pytest.raises(TransientProviderError):
        client.refresh_long_lived_token("long-lived-1")
    client.close()


def test_token_endpoint_malformed_json_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(TransientProviderError):
        client.refresh_long_lived_token("long-lived-1")
    client.close()


def test_token_endpoint_missing_access_token_field_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, json={"expires_in": 10}))
    with pytest.raises(TransientProviderError):
        client.refresh_long_lived_token("long-lived-1")
    client.close()
