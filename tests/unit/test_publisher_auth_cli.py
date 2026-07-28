"""publisher-auth refusal paths (no network, no credentials configured)."""
from __future__ import annotations

from reel_harness.cli import main as cli_main


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'auth.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path / "secrets"))
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
