"""publisher-auth refusal paths (no network, no credentials configured), and
a full TikTok manual-paste OAuth flow against a mocked token endpoint."""
from __future__ import annotations

from reel_harness.cli import main as cli_main
from reel_harness.publisher.credentials import FileCredentialBackend
from reel_harness.publisher.secret_store import FileSecretStore

FAKE_CLIENT_SECRET = "FAKE-TIKTOK-CLI-CLIENT-SECRET-000000"


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    # A sibling of tmp_path (never inside the chdir'd cwd below) --
    # resolve_secret_dir refuses a credential dir inside the repo/cwd, and
    # some of these tests (the tiktok success flow) actually reach
    # credential_backend() rather than failing before that point.
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def test_publisher_auth_refuses_without_client_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publisher-auth", "youtube"]) == 2
    err = capsys.readouterr().err
    assert "provider configuration error" in err
    assert "REEL_HARNESS_YOUTUBE_CLIENT_ID" in err
    assert "Traceback" not in err


def test_publisher_auth_refuses_with_only_client_id(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "some-client-id")
    assert cli_main.main(["publisher-auth", "youtube"]) == 2
    err = capsys.readouterr().err
    assert "REEL_HARNESS_YOUTUBE_CLIENT_SECRET" in err


def test_publisher_auth_rejects_bad_chunk_size_at_startup(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CHUNK_SIZE", "1000")  # not a multiple of 262144
    assert cli_main.main(["publisher-auth", "youtube"]) == 2
    assert "262144" in capsys.readouterr().err


def test_publisher_auth_tiktok_refuses_without_client_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publisher-auth", "tiktok"]) == 2
    err = capsys.readouterr().err
    assert "provider configuration error" in err
    assert "REEL_HARNESS_TIKTOK_CLIENT_KEY" in err
    assert "REEL_HARNESS_TIKTOK_REDIRECT_URI" in err
    assert "Traceback" not in err


def test_publisher_auth_tiktok_refuses_without_redirect_uri(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "client-key-1")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", FAKE_CLIENT_SECRET)
    assert cli_main.main(["publisher-auth", "tiktok"]) == 2
    err = capsys.readouterr().err
    assert "REEL_HARNESS_TIKTOK_REDIRECT_URI" in err


def _isolate_tiktok(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "client-key-1")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", FAKE_CLIENT_SECRET)
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    monkeypatch.setattr("reel_harness.publisher.oauth_tiktok.generate_state", lambda: "fixed-state-1")


class _FakeTokens:
    access_token = "fake-access-token-should-never-print"
    refresh_token = "fake-refresh-token-should-never-print"
    expires_in = 86400
    refresh_expires_in = 31536000
    scope = "video.publish"
    open_id = "open-id-abc"


class _FakeTikTokOAuthClient:
    """Stands in for TikTokOAuthClient -- no real network, records the
    call so the test can assert the pasted code/verifier were forwarded
    correctly without ever touching httpx."""

    calls: list = []

    def __init__(self, client_key, client_secret, token_url, transport=None, **kwargs) -> None:
        assert client_secret == FAKE_CLIENT_SECRET

    def exchange_code(self, code, code_verifier, redirect_uri):
        _FakeTikTokOAuthClient.calls.append((code, code_verifier, redirect_uri))
        return _FakeTokens()

    def close(self) -> None:
        pass


def test_publisher_auth_tiktok_manual_paste_flow_saves_credential_and_never_leaks_secrets(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate_tiktok(monkeypatch, tmp_path)
    _FakeTikTokOAuthClient.calls = []
    monkeypatch.setattr("reel_harness.publisher.oauth_tiktok.TikTokOAuthClient", _FakeTikTokOAuthClient)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "https://example.invalid/callback?state=fixed-state-1&code=pasted-auth-code",
    )

    assert cli_main.main(["publisher-auth", "tiktok", "--account", "acct-1"]) == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert _FakeTikTokOAuthClient.calls == [
        ("pasted-auth-code", _FakeTikTokOAuthClient.calls[0][1], "https://example.invalid/callback"),
    ]

    import json as _json
    payload = _json.loads(out)
    assert payload["provider"] == "tiktok"
    assert payload["account_reference"] == "acct-1"
    assert payload["open_id"] == "open-id-abc"
    assert payload["has_refresh_token"] is True

    for leaked in (
        "fake-access-token-should-never-print", "fake-refresh-token-should-never-print",
        FAKE_CLIENT_SECRET, "pasted-auth-code",
    ):
        assert leaked not in out
        assert leaked not in err

    secret_dir = tmp_path.parent / f"{tmp_path.name}-secrets"
    backend = FileCredentialBackend(FileSecretStore(secret_dir, repo_root=tmp_path))
    saved = backend.get_credential("tiktok", "acct-1")
    assert saved is not None
    assert saved.access_token == "fake-access-token-should-never-print"
    assert saved.refresh_token == "fake-refresh-token-should-never-print"
    assert saved.channel_id == "open-id-abc"
    assert saved.refresh_expires_at is not None


def test_publisher_auth_tiktok_manual_paste_rejects_state_mismatch(monkeypatch, tmp_path, capsys) -> None:
    _isolate_tiktok(monkeypatch, tmp_path)
    monkeypatch.setattr("reel_harness.publisher.oauth_tiktok.TikTokOAuthClient", _FakeTikTokOAuthClient)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "https://example.invalid/callback?state=WRONG-STATE&code=pasted-auth-code",
    )
    assert cli_main.main(["publisher-auth", "tiktok"]) == 3
    assert "state_mismatch" in capsys.readouterr().err


def test_publisher_auth_tiktok_manual_paste_reports_authorization_error(monkeypatch, tmp_path, capsys) -> None:
    _isolate_tiktok(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "https://example.invalid/callback?error=access_denied",
    )
    assert cli_main.main(["publisher-auth", "tiktok"]) == 3
    assert "access_denied" in capsys.readouterr().err


FAKE_INSTAGRAM_APP_SECRET = "FAKE-INSTAGRAM-CLI-APP-SECRET-000000"


def test_publisher_auth_instagram_refuses_without_client_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publisher-auth", "instagram"]) == 2
    err = capsys.readouterr().err
    assert "provider configuration error" in err
    assert "REEL_HARNESS_INSTAGRAM_APP_ID" in err
    assert "REEL_HARNESS_INSTAGRAM_REDIRECT_URI" in err
    assert "Traceback" not in err


def test_publisher_auth_instagram_refuses_without_redirect_uri(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "app-id-1")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", FAKE_INSTAGRAM_APP_SECRET)
    assert cli_main.main(["publisher-auth", "instagram"]) == 2
    err = capsys.readouterr().err
    assert "REEL_HARNESS_INSTAGRAM_REDIRECT_URI" in err


def _isolate_instagram(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "app-id-1")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", FAKE_INSTAGRAM_APP_SECRET)
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "https://example.invalid/callback")
    monkeypatch.setattr("webbrowser.open", lambda url: False)
    monkeypatch.setattr("reel_harness.publisher.oauth_instagram.generate_state", lambda: "fixed-state-1")


class _FakeInstagramTokens:
    def __init__(self, access_token) -> None:
        self.access_token = access_token
        self.user_id = "17841400"
        self.expires_in = 5184000


class _FakeInstagramIdentity:
    account_id = "17841400"
    username = "my_reel_account"


class _FakeInstagramOAuthClient:
    """Stands in for InstagramOAuthClient -- no real network, records the
    call so the test can assert the pasted code/verifier were forwarded
    correctly without ever touching httpx."""

    calls: list = []

    def __init__(self, app_id, app_secret, token_url, graph_url, transport=None, **kwargs) -> None:
        assert app_secret == FAKE_INSTAGRAM_APP_SECRET

    def exchange_code(self, code, code_verifier, redirect_uri):
        _FakeInstagramOAuthClient.calls.append((code, code_verifier, redirect_uri))
        return _FakeInstagramTokens("fake-short-lived-token-should-never-print")

    def exchange_long_lived_token(self, short_lived_access_token):
        assert short_lived_access_token == "fake-short-lived-token-should-never-print"
        return _FakeInstagramTokens("fake-long-lived-token-should-never-print")

    def fetch_account_identity(self, access_token):
        assert access_token == "fake-long-lived-token-should-never-print"
        return _FakeInstagramIdentity()

    def close(self) -> None:
        pass


def test_publisher_auth_instagram_manual_paste_flow_saves_credential_and_never_leaks_secrets(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate_instagram(monkeypatch, tmp_path)
    _FakeInstagramOAuthClient.calls = []
    monkeypatch.setattr("reel_harness.publisher.oauth_instagram.InstagramOAuthClient", _FakeInstagramOAuthClient)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "https://example.invalid/callback?state=fixed-state-1&code=pasted-auth-code",
    )

    assert cli_main.main(["publisher-auth", "instagram", "--account", "acct-1"]) == 0
    captured = capsys.readouterr()
    out, err = captured.out, captured.err

    assert _FakeInstagramOAuthClient.calls == [
        ("pasted-auth-code", _FakeInstagramOAuthClient.calls[0][1], "https://example.invalid/callback"),
    ]

    import json as _json
    payload = _json.loads(out)
    assert payload["provider"] == "instagram"
    assert payload["account_reference"] == "acct-1"
    assert payload["account_id"] == "17841400"
    assert payload["username"] == "my_reel_account"

    for leaked in (
        "fake-short-lived-token-should-never-print", "fake-long-lived-token-should-never-print",
        FAKE_INSTAGRAM_APP_SECRET, "pasted-auth-code",
    ):
        assert leaked not in out
        assert leaked not in err

    secret_dir = tmp_path.parent / f"{tmp_path.name}-secrets"
    backend = FileCredentialBackend(FileSecretStore(secret_dir, repo_root=tmp_path))
    saved = backend.get_credential("instagram", "acct-1")
    assert saved is not None
    assert saved.access_token == "fake-long-lived-token-should-never-print"
    assert saved.refresh_token is None  # instagram refreshes the access token itself -- see oauth_instagram
    assert saved.channel_id == "17841400"
    assert saved.channel_title == "my_reel_account"


def test_publisher_auth_instagram_manual_paste_rejects_state_mismatch(monkeypatch, tmp_path, capsys) -> None:
    _isolate_instagram(monkeypatch, tmp_path)
    monkeypatch.setattr("reel_harness.publisher.oauth_instagram.InstagramOAuthClient", _FakeInstagramOAuthClient)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": "https://example.invalid/callback?state=WRONG-STATE&code=pasted-auth-code",
    )
    assert cli_main.main(["publisher-auth", "instagram"]) == 3
    assert "state_mismatch" in capsys.readouterr().err


def test_publisher_auth_instagram_manual_paste_reports_authorization_error(monkeypatch, tmp_path, capsys) -> None:
    _isolate_instagram(monkeypatch, tmp_path)
    monkeypatch.setattr(
        "builtins.input", lambda prompt="": "https://example.invalid/callback?error=access_denied",
    )
    assert cli_main.main(["publisher-auth", "instagram"]) == 3
    assert "access_denied" in capsys.readouterr().err
