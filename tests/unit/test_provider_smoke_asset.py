"""provider-smoke asset refusal paths (no network, no credentials)."""
from __future__ import annotations

from reel_harness.cli import main as cli_main


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'smoke.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)  # no repo .env, no accidental repo DB writes


def test_smoke_asset_refuses_when_provider_is_fake(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_ASSET_PROVIDER", "fake")
    assert cli_main.main(["provider-smoke", "asset"]) == 2
    err = capsys.readouterr().err
    assert "nothing to smoke" in err
    assert "Traceback" not in err


def test_smoke_asset_refuses_clearly_without_credentials(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_ASSET_PROVIDER", "pexels")
    assert cli_main.main(["provider-smoke", "asset"]) == 2
    err = capsys.readouterr().err
    assert "provider configuration error" in err
    assert "REEL_HARNESS_ASSET" in err
    assert "Traceback" not in err


def test_smoke_asset_rejects_bad_orientation_at_startup(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_ASSET_ORIENTATION", "diagonal")
    assert cli_main.main(["provider-smoke", "asset"]) == 2
    assert "orientation" in capsys.readouterr().err
