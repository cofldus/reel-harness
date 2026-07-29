"""publisher.oauth_youtube: PKCE/state generation, authorization URL
building, token exchange/refresh and channel-identity contract tests
(httpx.MockTransport -- no external network), and a real loopback HTTP
round-trip against LoopbackCallbackServer (127.0.0.1 only, allowed by the
network-block fixture)."""
from __future__ import annotations

import socket
import threading
import urllib.parse

import httpx
import pytest

from reel_harness.core.errors import ProviderAuthError, TransientProviderError
from reel_harness.publisher.oauth_youtube import (
    AUTHORIZATION_ENDPOINT,
    LoopbackCallbackServer,
    OAuthCallbackError,
    YouTubeOAuthClient,
    build_authorization_url,
    generate_pkce,
    generate_state,
)

FAKE_CLIENT_ID = "fake-client-id.apps.googleusercontent.com"
FAKE_CLIENT_SECRET = "FAKE-OAUTH-CLIENT-SECRET-00000000"


def test_pkce_is_random_and_challenge_derives_from_verifier() -> None:
    a = generate_pkce()
    b = generate_pkce()
    assert a.verifier != b.verifier
    assert a.challenge != b.challenge
    assert a.method == "S256"
    # Deterministic re-derivation from the same verifier (SHA256 + base64url, no padding).
    import base64
    import hashlib

    expected = base64.urlsafe_b64encode(hashlib.sha256(a.verifier.encode()).digest()).rstrip(b"=").decode()
    assert a.challenge == expected


def test_state_is_random_and_url_safe() -> None:
    a = generate_state()
    b = generate_state()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_authorization_url_carries_pkce_state_and_minimal_scopes_never_the_secret() -> None:
    pkce = generate_pkce()
    state = generate_state()
    url = build_authorization_url(FAKE_CLIENT_ID, "http://127.0.0.1:12345", state, pkce)
    assert url.startswith(AUTHORIZATION_ENDPOINT)
    parsed = urllib.parse.urlsplit(url)
    params = urllib.parse.parse_qs(parsed.query)
    assert params["client_id"] == [FAKE_CLIENT_ID]
    assert params["code_challenge"] == [pkce.challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [state]
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]
    assert "youtube.upload" in params["scope"][0]
    assert "youtube.readonly" in params["scope"][0]
    assert "client_secret" not in url


def _oauth_client(handler) -> YouTubeOAuthClient:
    return YouTubeOAuthClient(FAKE_CLIENT_ID, FAKE_CLIENT_SECRET, transport=httpx.MockTransport(handler))


def test_exchange_code_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "authorization_code"
        assert body["code"] == "auth-code-1"
        assert body["client_secret"] == FAKE_CLIENT_SECRET
        return httpx.Response(200, json={
            "access_token": "access-1", "refresh_token": "refresh-1", "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/youtube.upload",
        })

    client = _oauth_client(handler)
    tokens = client.exchange_code("auth-code-1", "verifier-1", "http://127.0.0.1:1234")
    assert tokens.access_token == "access-1"
    assert tokens.refresh_token == "refresh-1"
    assert tokens.expires_in == 3600
    client.close()


def test_refresh_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["grant_type"] == "refresh_token"
        assert body["refresh_token"] == "refresh-1"
        return httpx.Response(200, json={"access_token": "access-2", "expires_in": 3600, "scope": "s"})

    client = _oauth_client(handler)
    tokens = client.refresh("refresh-1")
    assert tokens.access_token == "access-2"
    assert tokens.refresh_token is None
    client.close()


@pytest.mark.parametrize("status_code", [400, 401, 403])
def test_token_endpoint_auth_errors_are_not_retried(status_code: int) -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(status_code, json={"error": "invalid_grant"})

    client = _oauth_client(handler)
    with pytest.raises(ProviderAuthError) as exc_info:
        client.exchange_code("bad-code", "v", "http://127.0.0.1:1")
    assert FAKE_CLIENT_SECRET not in str(exc_info.value)
    assert len(calls) == 1
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


def test_fetch_channel_identity_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == "Bearer access-1"
        return httpx.Response(200, json={
            "items": [{"id": "UC-fake-1", "snippet": {"title": "My Channel"}}],
        })

    client = _oauth_client(handler)
    identity = client.fetch_channel_identity("access-1")
    assert identity.channel_id == "UC-fake-1"
    assert identity.title == "My Channel"
    client.close()


def test_fetch_channel_identity_no_channels_is_transient() -> None:
    client = _oauth_client(lambda r: httpx.Response(200, json={"items": []}))
    with pytest.raises(TransientProviderError):
        client.fetch_channel_identity("access-1")
    client.close()


def test_fetch_channel_identity_401_is_auth_error() -> None:
    client = _oauth_client(lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        client.fetch_channel_identity("bad-token")
    client.close()


def _raw_get(port: int, path: str, timeout: float = 5.0) -> int:
    """A real HTTP GET over a real loopback TCP socket, bypassing httpx (whose
    httpcore backend calls the module-level socket.create_connection(), which
    tests/conftest.py's network-block fixture blocks unconditionally --
    unlike socket.socket().connect(), which it explicitly allows for
    loopback). Returns the parsed HTTP status code."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(("127.0.0.1", port))
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode())
        chunks = []
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
        except TimeoutError:
            pass
        status_line = b"".join(chunks).split(b"\r\n", 1)[0].decode(errors="replace")
        return int(status_line.split(" ")[1]) if " " in status_line else 0
    finally:
        sock.close()


def test_loopback_server_accepts_matching_state_and_returns_code() -> None:
    server = LoopbackCallbackServer(expected_state="state-xyz", timeout_seconds=10)
    results: dict = {}

    def _client_request() -> None:
        results["status"] = _raw_get(server.port, "/?state=state-xyz&code=the-code")

    thread = threading.Thread(target=_client_request)
    thread.start()
    code = server.wait_for_code()
    thread.join(timeout=10)

    assert code == "the-code"
    assert results["status"] == 200


def test_loopback_server_rejects_state_mismatch() -> None:
    server = LoopbackCallbackServer(expected_state="expected-state", timeout_seconds=10)
    thread = threading.Thread(target=lambda: _raw_get(server.port, "/?state=wrong-state&code=x"))
    thread.start()
    with pytest.raises(OAuthCallbackError, match="state_mismatch"):
        server.wait_for_code()
    thread.join(timeout=10)


def test_loopback_server_reports_error_param() -> None:
    server = LoopbackCallbackServer(expected_state="s", timeout_seconds=10)
    thread = threading.Thread(target=lambda: _raw_get(server.port, "/?state=s&error=access_denied"))
    thread.start()
    with pytest.raises(OAuthCallbackError, match="access_denied"):
        server.wait_for_code()
    thread.join(timeout=10)


def test_loopback_server_missing_code_reported() -> None:
    server = LoopbackCallbackServer(expected_state="s", timeout_seconds=10)
    thread = threading.Thread(target=lambda: _raw_get(server.port, "/?state=s"))
    thread.start()
    with pytest.raises(OAuthCallbackError, match="missing_code"):
        server.wait_for_code()
    thread.join(timeout=10)


def test_loopback_server_times_out_with_no_request() -> None:
    server = LoopbackCallbackServer(expected_state="s", timeout_seconds=0.3)
    with pytest.raises(OAuthCallbackError, match="timeout"):
        server.wait_for_code()


def test_loopback_server_redirect_uri_is_loopback_only() -> None:
    server = LoopbackCallbackServer(expected_state="s", timeout_seconds=1)
    assert server.redirect_uri.startswith("http://127.0.0.1:")
    threading.Thread(target=lambda: _raw_get(server.port, "/?state=s&code=x")).start()
    server.wait_for_code()


def test_loopback_server_can_bind_an_exact_pre_registered_port() -> None:
    """A provider (TikTok) that requires an exact pre-registered redirect_uri
    -- unlike Google's "any 127.0.0.1:PORT is accepted" installed-app flow
    -- needs the listener bound to that specific port, not an OS-assigned
    ephemeral one."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()

    server = LoopbackCallbackServer(expected_state="s", timeout_seconds=5, port=free_port)
    assert server.port == free_port
    threading.Thread(target=lambda: _raw_get(server.port, "/?state=s&code=fixed-port-code")).start()
    assert server.wait_for_code() == "fixed-port-code"
