"""Web UI OAuth connect/callback/disconnect routes
(reel_harness/web/router.py's publisher_connect/publisher_oauth_callback/
publisher_disconnect). Every OAuth token exchange here is monkeypatched with
a fake *OAuthClient class -- exactly like every existing oauth_*.py contract
test in this project uses httpx.MockTransport -- so no test in this file
ever makes a real network call to Google/TikTok/Meta."""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _make_ctx(tmp_path, **settings_overrides) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'oauth-web-test.db'}",
        jobs_dir=tmp_path / "jobs",
        # Never the real ~/.reel-harness/credentials default -- every OAuth
        # credential/pending-flow write in this file must stay confined to
        # tmp_path, exactly like database_url/jobs_dir above.
        credential_dir=tmp_path / "credentials",
        app_api_key="a-real-non-placeholder-test-key",
        **settings_overrides,
    )
    return AppContext(settings=settings)


def _csrf_token_from(client: TestClient) -> str:
    page = client.get("/publisher-accounts")
    match = _CSRF_INPUT_RE.search(page.text)
    assert match, "no csrf_token hidden field found on /publisher-accounts"
    return match.group(1)


class _FakeYouTubeOAuthClient:
    def __init__(self, client_id: str, client_secret: str) -> None:
        self.client_id = client_id
        self.client_secret = client_secret

    def close(self) -> None:
        pass

    def exchange_code(self, code: str, code_verifier: str, redirect_uri: str):
        from reel_harness.publisher.oauth_youtube import TokenResponse

        assert code == "fake-auth-code"
        assert code_verifier  # a real PKCE verifier was generated and threaded through
        return TokenResponse(
            access_token="fake-yt-access-token", refresh_token="fake-yt-refresh-token",
            expires_in=3600, scope="https://www.googleapis.com/auth/youtube.upload",
        )

    def fetch_channel_identity(self, access_token: str):
        from reel_harness.publisher.oauth_youtube import ChannelIdentity

        return ChannelIdentity(channel_id="UC-fake-channel", title="Fake Channel")


def test_publisher_accounts_page_shows_youtube_not_configured(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/publisher-accounts")
        assert response.status_code == 200
        assert "미설정" in response.text
    finally:
        app.dependency_overrides.clear()


def test_connect_without_csrf_token_is_rejected(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).post(
            "/publisher-accounts/youtube/connect", data={"account_reference": "default"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_connect_refuses_when_oauth_client_not_configured(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)  # no youtube client configured
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        response = client.post(
            "/publisher-accounts/youtube/connect",
            data={"account_reference": "default", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_connect_unknown_provider_404s(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        response = client.post(
            "/publisher-accounts/facebook/connect",
            data={"account_reference": "default", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_connect_redirects_to_the_real_authorization_url_with_state_and_pkce(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        response = client.post(
            "/publisher-accounts/youtube/connect",
            data={"account_reference": "default", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert response.status_code == 303
        location = response.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?")
        assert "client_id=client-1" in location
        assert "code_challenge=" in location
        assert "state=" in location
        assert "redirect_uri=" in location
        assert "publisher-accounts%2Fyoutube%2Fcallback" in location or "publisher-accounts/youtube/callback" in (
            location
        )
    finally:
        app.dependency_overrides.clear()


def test_callback_with_valid_state_exchanges_and_saves_credential(tmp_path, monkeypatch) -> None:
    import reel_harness.publisher.oauth_youtube as oauth_youtube_module

    monkeypatch.setattr(oauth_youtube_module, "YouTubeOAuthClient", _FakeYouTubeOAuthClient)

    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        connect_response = client.post(
            "/publisher-accounts/youtube/connect",
            data={"account_reference": "my-account", "csrf_token": csrf_token},
            follow_redirects=False,
        )
        location = connect_response.headers["location"]
        state = re.search(r"state=([^&]+)", location).group(1)

        # No CSRF cookie/header at all on this request -- this IS the real
        # security model (state param, not the double-submit cookie); a
        # SameSite=Strict cookie wouldn't even be sent on a genuine
        # cross-site redirect from the provider, so this must succeed.
        anonymous_client = TestClient(app)
        callback_response = anonymous_client.get(
            f"/publisher-accounts/youtube/callback?code=fake-auth-code&state={state}", follow_redirects=False,
        )
        assert callback_response.status_code == 303
        assert callback_response.headers["location"] == "/publisher-accounts?connected=youtube"

        saved = ctx.credential_backend().get_credential("youtube", "my-account")
        assert saved is not None
        assert saved.access_token == "fake-yt-access-token"
        assert saved.channel_title == "Fake Channel"
    finally:
        app.dependency_overrides.clear()


def test_callback_state_is_single_use(tmp_path, monkeypatch) -> None:
    import reel_harness.publisher.oauth_youtube as oauth_youtube_module

    monkeypatch.setattr(oauth_youtube_module, "YouTubeOAuthClient", _FakeYouTubeOAuthClient)

    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        connect_response = client.post(
            "/publisher-accounts/youtube/connect",
            data={"account_reference": "default", "csrf_token": csrf_token}, follow_redirects=False,
        )
        state = re.search(r"state=([^&]+)", connect_response.headers["location"]).group(1)

        first = client.get(
            f"/publisher-accounts/youtube/callback?code=fake-auth-code&state={state}", follow_redirects=False,
        )
        assert first.headers["location"] == "/publisher-accounts?connected=youtube"

        second = client.get(
            f"/publisher-accounts/youtube/callback?code=fake-auth-code&state={state}", follow_redirects=False,
        )
        assert "error=" in second.headers["location"]
    finally:
        app.dependency_overrides.clear()


def test_callback_expired_state_is_rejected(tmp_path, monkeypatch) -> None:
    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        # A state that was never created at all behaves identically to an
        # expired one from the route's perspective (both are "not found").
        response = TestClient(app).get(
            "/publisher-accounts/youtube/callback?code=fake-auth-code&state=never-created-state",
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]

        assert ctx.credential_backend().get_credential("youtube", "default") is None
    finally:
        app.dependency_overrides.clear()


def test_callback_provider_error_query_param_is_handled_gracefully(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get(
            "/publisher-accounts/youtube/callback?error=access_denied", follow_redirects=False,
        )
        assert response.status_code == 303
        assert "error=" in response.headers["location"]
    finally:
        app.dependency_overrides.clear()


def test_disconnect_without_csrf_is_rejected(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).post(
            "/publisher-accounts/youtube/disconnect", data={"account_reference": "default"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_disconnect_removes_the_saved_credential(tmp_path) -> None:
    from reel_harness.publisher.credentials import OAuthCredential

    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    ctx.credential_backend().save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="s", provider="youtube", account_reference="default",
    ))
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        response = client.post(
            "/publisher-accounts/youtube/disconnect",
            data={"account_reference": "default", "csrf_token": csrf_token}, follow_redirects=False,
        )
        assert response.status_code == 303
        assert ctx.credential_backend().get_credential("youtube", "default") is None
    finally:
        app.dependency_overrides.clear()


def test_publisher_accounts_page_lists_connected_account_after_connect_flow(tmp_path, monkeypatch) -> None:
    import reel_harness.publisher.oauth_youtube as oauth_youtube_module

    monkeypatch.setattr(oauth_youtube_module, "YouTubeOAuthClient", _FakeYouTubeOAuthClient)

    ctx = _make_ctx(tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client)
        connect_response = client.post(
            "/publisher-accounts/youtube/connect",
            data={"account_reference": "default", "csrf_token": csrf_token}, follow_redirects=False,
        )
        state = re.search(r"state=([^&]+)", connect_response.headers["location"]).group(1)
        client.get(f"/publisher-accounts/youtube/callback?code=fake-auth-code&state={state}")

        page = client.get("/publisher-accounts")
        assert "Fake Channel" in page.text
        assert "fake-yt-access-token" not in page.text
        assert "fake-yt-refresh-token" not in page.text
    finally:
        app.dependency_overrides.clear()
