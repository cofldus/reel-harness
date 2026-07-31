"""fable-budget / fable-estimate CLI surface, driven through the real
`main()` entry point against a real (fake-provider) project -- no
network, no cost.

The properties worth holding at this layer: reporting a budget never
changes one, estimating never approves or spends anything, and a refusal
comes back as a non-zero exit with the reason on stderr rather than a
traceback.
"""
from __future__ import annotations

import json

from reel_harness.cli import main as cli_main


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'fable-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_FABLE_PROJECTS_DIR", str(tmp_path / "fable_projects"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def _adapted_project(monkeypatch, tmp_path, capsys) -> str:
    """Creates and adapts a project through the CLI itself, so these tests
    exercise the same entry point they assert on. A helper rather than a
    fixture: capsys belongs to the test, and sharing it with a fixture
    that also drains it makes the capture ordering ambiguous."""
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main([
        "fable-create", "--title", "t", "--story", "그날 밤, 그는 창밖을 바라보았다.",
        "--idempotency-key", "cli-budget",
    ]) == 0
    created = json.loads(capsys.readouterr().out)
    assert cli_main.main(["fable-adapt", created["project_id"]]) == 0
    capsys.readouterr()
    return created["project_id"]


def test_budget_without_flags_only_reports(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-budget", project_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["limit_amount"] is None
    assert payload["spent_amount"] == 0.0
    assert payload["paid_generation_enabled"] is False


def test_budget_set_then_report_then_clear(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-budget", project_id, "--limit", "5.0", "--currency", "FAKE"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["limit_amount"] == 5.0
    assert payload["currency"] == "FAKE"
    assert payload["remaining_amount"] == 5.0

    assert cli_main.main(["fable-budget", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["limit_amount"] == 5.0  # report changed nothing

    assert cli_main.main(["fable-budget", project_id, "--clear"]) == 0
    assert json.loads(capsys.readouterr().out)["limit_amount"] is None


def test_budget_rejects_a_limit_without_a_currency(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    exit_code = cli_main.main(["fable-budget", project_id, "--limit", "5.0"])
    assert exit_code == 2
    assert "currency" in capsys.readouterr().err


def test_budget_unknown_project_exits_one(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["fable-budget", "00000000-0000-0000-0000-000000000000"]) == 1
    assert "not found" in capsys.readouterr().err


def test_estimate_prices_the_project_without_approving_it(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-estimate", project_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["known"] is True
    assert payload["currency"] == "FAKE"
    assert payload["amount"] > 0
    assert payload["shot_count"] > 0

    assert cli_main.main(["fable-status", project_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["status"] == "STORY_REVIEW"  # estimating advanced nothing
    assert status["budget"]["spent_amount"] == 0.0


def test_status_reports_the_budget_block(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-budget", project_id, "--limit", "2.5", "--currency", "FAKE"]) == 0
    capsys.readouterr()
    assert cli_main.main(["fable-status", project_id]) == 0
    budget = json.loads(capsys.readouterr().out)["budget"]
    assert budget["limit_amount"] == 2.5
    assert budget["currency"] == "FAKE"
    assert budget["unpriced_take_count"] == 0
