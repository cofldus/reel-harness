"""reel-harness live-verify: local-mode (no configured credentials) CLI
behavior only -- a real upload-test path needs live credentials, exactly
like provider-smoke's own live-smoke path, so it is exercised only up to
its NOT_CONFIGURED / never-attempted-without-confirmation refusal here."""
from __future__ import annotations

import json

from reel_harness.cli import main as cli_main
from reel_harness.media.deps import check_ffmpeg_available


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'lv.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    deps = check_ffmpeg_available()
    if deps.ffmpeg.path:
        monkeypatch.setenv("REEL_HARNESS_FFMPEG_PATH", str(deps.ffmpeg.path))
    if deps.ffprobe.path:
        monkeypatch.setenv("REEL_HARNESS_FFPROBE_PATH", str(deps.ffprobe.path))
    monkeypatch.chdir(tmp_path)


def test_live_verify_default_checks_all_three_providers(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["live-verify", "--json"])
    captured = capsys.readouterr()
    # The startup config-fingerprint log line goes to stderr (see
    # observability.configure_logging), so stdout is pure JSON.
    payload = json.loads(captured.out)
    assert set(payload["providers"]) == {"youtube", "tiktok", "instagram"}
    assert len(payload["records"]) == 3
    assert all(r["outcome"] == "NOT_CONFIGURED" for r in payload["records"])
    assert exit_code == 0  # NOT_CONFIGURED does not fail the whole sweep


def test_live_verify_scoped_to_one_provider(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["live-verify", "--tiktok", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["providers"] == ["tiktok"]
    assert len(payload["records"]) == 1


def test_live_verify_never_uploads_without_upload_tests_flag(monkeypatch, tmp_path, capsys) -> None:
    """Even with a platform confirm flag, omitting --upload-tests must
    never attempt an upload -- only the read-only sweep runs."""
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["live-verify", "--instagram", "--confirm-instagram-public", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert all(r["verification_type"] == "read_only" for r in payload["records"])


def test_live_verify_never_uploads_without_platform_confirm_flag(monkeypatch, tmp_path, capsys) -> None:
    """--upload-tests alone (no per-platform confirm) must never attempt
    an upload for that platform."""
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["live-verify", "--instagram", "--upload-tests", "--json"])
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert all(r["verification_type"] == "read_only" for r in payload["records"])


def test_live_verify_human_readable_output(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["live-verify", "--youtube"])
    captured = capsys.readouterr()
    assert "NOT_CONFIGURED" in captured.out
    assert "youtube" in captured.out


def test_live_verify_persists_records_to_append_only_log(monkeypatch, tmp_path) -> None:
    _isolate(monkeypatch, tmp_path)
    cli_main.main(["live-verify", "--youtube", "--json"])
    cli_main.main(["live-verify", "--youtube", "--json"])
    from reel_harness.ops.live_verify import LiveVerificationLog
    from reel_harness.publisher.secret_store import FileSecretStore

    secret_store = FileSecretStore(tmp_path.parent / f"{tmp_path.name}-secrets")
    log = LiveVerificationLog(secret_store.root_dir / "live_verification")
    records = log.read_all()
    assert len(records) == 2  # one per live-verify invocation, never overwritten
