"""publisher-doctor / publisher-account-list / -show / -remove: local-mode
diagnostics with no configured OAuth client or credential, and with a
pre-seeded credential. No network (--check-remote is exercised only in its
NOT-CONFIGURED refusal path here; a real remote check needs live
credentials -- see provider-smoke's own live-smoke path)."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from reel_harness.cli import main as cli_main
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.publisher.credentials import FileCredentialBackend, OAuthCredential
from reel_harness.publisher.secret_store import FileSecretStore


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'doctor.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    # Outside tmp_path on purpose -- once chdir'd into tmp_path below it IS
    # "the repository" as far as resolve_secret_dir is concerned.
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    # Pin the real ffmpeg/ffprobe paths (resolved from the ACTUAL project
    # root, before chdir) so publisher-doctor's ffmpeg/ffprobe checks reflect
    # real availability rather than "not found relative to this tmp cwd".
    deps = check_ffmpeg_available()
    if deps.ffmpeg.path:
        monkeypatch.setenv("REEL_HARNESS_FFMPEG_PATH", str(deps.ffmpeg.path))
    if deps.ffprobe.path:
        monkeypatch.setenv("REEL_HARNESS_FFPROBE_PATH", str(deps.ffprobe.path))
    monkeypatch.chdir(tmp_path)


def _credential_dir(tmp_path):
    return tmp_path.parent / f"{tmp_path.name}-secrets"


def _seed_credential(tmp_path, **overrides) -> None:
    store = FileSecretStore(_credential_dir(tmp_path), repo_root=tmp_path.parent / "unrelated-repo")
    backend = FileCredentialBackend(store)
    defaults = dict(
        access_token="fake-access-token-000000000000",
        refresh_token="fake-refresh-token-000000000000",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/youtube.upload",
        provider="youtube", account_reference="default",
        channel_id="UC-fake", channel_title="Fake Channel",
        created_at=datetime.now(UTC) - timedelta(days=1),
    )
    defaults.update(overrides)
    backend.save_credential(OAuthCredential(**defaults))


def _seed_tiktok_credential(tmp_path, **overrides) -> None:
    defaults = dict(
        provider="tiktok", scope="video.publish", channel_id="tiktok-open-id", channel_title=None,
    )
    defaults.update(overrides)
    _seed_credential(tmp_path, **defaults)


def _seed_instagram_credential(tmp_path, **overrides) -> None:
    defaults = dict(
        provider="instagram", refresh_token=None, scope="instagram_business_content_publish",
        channel_id="17841400", channel_title="my_reel_account",
    )
    defaults.update(overrides)
    _seed_credential(tmp_path, **defaults)


def test_doctor_reports_not_configured_with_no_client_and_no_credential(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "youtube", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["overall"] == "NOT_CONFIGURED"
    names = {c["name"] for c in payload["checks"]}
    assert "oauth_client_config" in names
    assert "account_credential" in names
    oauth_check = next(c for c in payload["checks"] if c["name"] == "oauth_client_config")
    assert oauth_check["status"] == "NOT_CONFIGURED"


def test_doctor_never_prints_a_secret(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "test-client-secret-value")
    _seed_credential(tmp_path, access_token="super-secret-access-token-xyz")
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "youtube", "--json"])
    out = capsys.readouterr().out
    assert "super-secret-access-token-xyz" not in out
    assert "test-client-secret-value" not in out


def test_doctor_passes_with_configured_client_and_fresh_credential(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "test-client-secret")
    _seed_credential(tmp_path)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "youtube", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "PASS"


def test_doctor_reports_warn_when_refresh_token_missing(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "test-client-secret")
    _seed_credential(tmp_path, refresh_token=None)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "youtube", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "WARN"


def test_doctor_reports_fail_for_an_invalid_credential(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "test-client-secret")
    _seed_credential(tmp_path, invalid=True, last_refresh_error="invalid_grant")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "youtube", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["overall"] == "FAIL"
    invalid_check = next(c for c in payload["checks"] if c["name"] == "credential_valid")
    assert "invalid_grant" in invalid_check["detail"]


def test_doctor_check_remote_not_run_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "youtube", "--check-remote", "--json"])
    payload = json.loads(capsys.readouterr().out)
    remote_checks = {c["name"]: c for c in payload["checks"] if c["name"].startswith("remote_")}
    assert remote_checks["remote_token_refresh"]["status"] == "NOT_CONFIGURED"
    assert "NOT RUN" in remote_checks["remote_token_refresh"]["detail"]
    assert "NOT RUN" in remote_checks["remote_channel_identity"]["detail"]


def test_tiktok_doctor_reports_not_configured_with_no_client_and_no_credential(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "tiktok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["overall"] == "NOT_CONFIGURED"
    names = {c["name"] for c in payload["checks"]}
    assert "oauth_client_config" in names
    assert "account_credential" in names


def test_tiktok_doctor_never_prints_a_secret(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "test-client-key")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "test-client-secret-value")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    _seed_tiktok_credential(tmp_path, access_token="super-secret-access-token-xyz")
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "tiktok", "--json"])
    out = capsys.readouterr().out
    assert "super-secret-access-token-xyz" not in out
    assert "test-client-secret-value" not in out


def test_tiktok_doctor_passes_with_configured_client_and_fresh_credential(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "test-client-key")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    _seed_tiktok_credential(tmp_path)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "tiktok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "PASS"
    scope_check = next(c for c in payload["checks"] if c["name"] == "required_scope_granted")
    assert scope_check["status"] == "PASS"


def test_tiktok_doctor_reports_warn_when_refresh_token_missing(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "test-client-key")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    _seed_tiktok_credential(tmp_path, refresh_token=None)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "tiktok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "WARN"


def test_tiktok_doctor_reports_fail_for_an_invalid_credential(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "test-client-key")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    _seed_tiktok_credential(tmp_path, invalid=True, last_refresh_error="invalid_grant")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "tiktok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["overall"] == "FAIL"
    invalid_check = next(c for c in payload["checks"] if c["name"] == "credential_valid")
    assert "invalid_grant" in invalid_check["detail"]


def test_tiktok_doctor_reports_fail_for_an_expired_refresh_token(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_KEY", "test-client-key")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("REEL_HARNESS_TIKTOK_REDIRECT_URI", "https://example.invalid/callback")
    _seed_tiktok_credential(tmp_path, refresh_expires_at=datetime.now(UTC) - timedelta(days=1))
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "tiktok", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["overall"] == "FAIL"
    refresh_check = next(c for c in payload["checks"] if c["name"] == "refresh_token_expiry")
    assert refresh_check["status"] == "FAIL"


def test_tiktok_doctor_check_remote_not_run_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "tiktok", "--check-remote", "--json"])
    payload = json.loads(capsys.readouterr().out)
    remote_checks = {c["name"]: c for c in payload["checks"] if c["name"].startswith("remote_")}
    assert remote_checks["remote_token_refresh"]["status"] == "NOT_CONFIGURED"
    assert "NOT RUN" in remote_checks["remote_token_refresh"]["detail"]
    assert "NOT RUN" in remote_checks["remote_creator_info"]["detail"]
    app_review_check = next(c for c in payload["checks"] if c["name"] == "app_review_status")
    assert app_review_check["status"] == "NOT_CONFIGURED"


def test_instagram_doctor_reports_not_configured_with_no_client_and_no_credential(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "instagram", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["overall"] == "NOT_CONFIGURED"
    names = {c["name"] for c in payload["checks"]}
    assert "oauth_client_config" in names
    assert "account_credential" in names


def test_instagram_doctor_never_prints_a_secret(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "test-app-id")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", "test-app-secret-value")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "https://example.invalid/callback")
    _seed_instagram_credential(tmp_path, access_token="super-secret-access-token-xyz")
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "instagram", "--json"])
    out = capsys.readouterr().out
    assert "super-secret-access-token-xyz" not in out
    assert "test-app-secret-value" not in out


def test_instagram_doctor_passes_with_configured_client_and_fresh_credential(
    monkeypatch, tmp_path, capsys,
) -> None:
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "test-app-id")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "https://example.invalid/callback")
    _seed_instagram_credential(tmp_path)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "instagram", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["overall"] == "PASS"
    account_check = next(c for c in payload["checks"] if c["name"] == "account_credential")
    assert "17841400" in account_check["detail"]


def test_instagram_doctor_reports_fail_for_an_invalid_credential(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_ID", "test-app-id")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_APP_SECRET", "test-app-secret")
    monkeypatch.setenv("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "https://example.invalid/callback")
    _seed_instagram_credential(tmp_path, invalid=True, last_refresh_error="token too young to refresh")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-doctor", "instagram", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["overall"] == "FAIL"
    invalid_check = next(c for c in payload["checks"] if c["name"] == "credential_valid")
    assert "token too young to refresh" in invalid_check["detail"]


def test_instagram_doctor_check_remote_not_run_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publisher-doctor", "instagram", "--check-remote", "--json"])
    payload = json.loads(capsys.readouterr().out)
    remote_checks = {c["name"]: c for c in payload["checks"] if c["name"].startswith("remote_")}
    assert remote_checks["remote_token_refresh"]["status"] == "NOT_CONFIGURED"
    assert "NOT RUN" in remote_checks["remote_token_refresh"]["detail"]
    assert "NOT RUN" in remote_checks["remote_account_info"]["detail"]
    eligibility_check = next(c for c in payload["checks"] if c["name"] == "account_eligibility_status")
    assert eligibility_check["status"] == "NOT_CONFIGURED"


def test_account_list_is_empty_with_nothing_saved(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-list"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["accounts"] == []


def test_account_list_shows_saved_accounts_without_tokens(monkeypatch, tmp_path, capsys) -> None:
    _seed_credential(tmp_path, account_reference="acct-a", access_token="secret-token-a")
    _seed_credential(tmp_path, account_reference="acct-b", access_token="secret-token-b")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-list"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert exit_code == 0
    aliases = [a["account_reference"] for a in payload["accounts"]]
    assert aliases == ["acct-a", "acct-b"]
    assert "secret-token-a" not in out
    assert "secret-token-b" not in out


def test_account_show_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publisher-account-show", "does-not-exist"]) == 2
    assert "no saved credential" in capsys.readouterr().err


def test_account_show_reports_safe_metadata_only(monkeypatch, tmp_path, capsys) -> None:
    _seed_credential(tmp_path, access_token="super-secret-token", refresh_token="super-secret-refresh")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-show", "default"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert exit_code == 0
    assert payload["channel_id"] == "UC-fake"
    assert payload["has_refresh_token"] is True
    assert "super-secret-token" not in out
    assert "super-secret-refresh" not in out
    assert "access_token" not in payload
    assert "refresh_token" not in payload


def test_account_remove_refuses_without_confirm(monkeypatch, tmp_path, capsys) -> None:
    _seed_credential(tmp_path)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-remove", "default"])
    assert exit_code == 2
    assert "does NOT revoke remote authorization" in capsys.readouterr().err
    assert cli_main.main(["publisher-account-show", "default"]) == 0  # still there


def test_account_remove_with_confirm_deletes_local_credential_only(monkeypatch, tmp_path, capsys) -> None:
    _seed_credential(tmp_path)
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-remove", "default", "--confirm"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["removed"] is True
    assert cli_main.main(["publisher-account-show", "default"]) == 2


def test_account_remove_missing_alias_with_confirm(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-account-remove", "does-not-exist", "--confirm"])
    assert exit_code == 2
    assert "no saved credential" in capsys.readouterr().err
