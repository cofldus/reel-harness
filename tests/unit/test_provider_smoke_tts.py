"""provider-smoke tts refusal paths (no network, no credentials)."""
from __future__ import annotations

from reel_harness.cli import main as cli_main


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)  # no repo .env, no accidental repo DB writes


def test_smoke_tts_refuses_when_provider_is_fake(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_TTS_PROVIDER", "fake")
    assert cli_main.main(["provider-smoke", "tts"]) == 2
    err = capsys.readouterr().err
    assert "nothing to smoke" in err
    assert "Traceback" not in err


def test_smoke_tts_refuses_clearly_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_TTS_PROVIDER", "openai_compatible")
    assert cli_main.main(["provider-smoke", "tts"]) == 2
    err = capsys.readouterr().err
    assert "provider configuration error" in err
    assert "REEL_HARNESS_TTS" in err
    assert "Traceback" not in err


def test_smoke_tts_rejects_invalid_format_at_startup(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_TTS_FORMAT", "ogg")
    assert cli_main.main(["provider-smoke", "tts"]) == 2
    assert "unsupported tts format" in capsys.readouterr().err
