from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

import reel_harness
from reel_harness._version import __version__ as version_module_value


def test_version_is_pep440_final_release() -> None:
    # "0.1.0", not "0.1.0.0" or "v0.1.0" -- exactly one format used
    # consistently everywhere a version string appears (see docs/STATUS.md).
    assert version_module_value == "0.1.0"


def test_package_version_matches_version_module() -> None:
    assert reel_harness.__version__ == version_module_value


def test_pyproject_version_matches_version_module() -> None:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    assert data["project"]["version"] == version_module_value


def test_cli_version_flag_prints_version_and_exits_zero_without_appcontext(capsys, monkeypatch) -> None:
    """--version must never require a working DB/storage/provider config --
    argparse's version action exits before main() ever constructs an
    AppContext. Proven here by pointing every AppContext dependency at a
    deliberately broken path and confirming --version still succeeds."""
    monkeypatch.setenv("REEL_HARNESS_DATABASE_URL", "sqlite:////definitely/not/a/real/path/db.sqlite")
    from reel_harness.cli.main import main

    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert f"reel-harness {version_module_value}" in captured.out
