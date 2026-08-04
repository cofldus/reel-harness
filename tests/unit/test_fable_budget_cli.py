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


def test_casting_commands_walk_the_gate(monkeypatch, tmp_path, capsys) -> None:
    """fable-generate-references / fable-reference through the real CLI:
    casting is a stop, the sheet arrives unapproved, approving it opens
    the character gate."""
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-approve", project_id, "--step", "story"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "CASTING"

    assert cli_main.main(["fable-generate-references", project_id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "CHARACTER_REVIEW"
    character = payload["characters"][0]
    assert set(character["reference_images"]) == {
        "face", "three_quarter", "full_body", "wardrobe", "back",
    }
    assert character["reference_approved"] is False

    # The character gate refuses until the sheet is explicitly approved.
    assert cli_main.main(["fable-approve", project_id, "--step", "characters"]) == 2
    assert "no approved reference sheet" in capsys.readouterr().err

    assert cli_main.main(["fable-reference", character["character_id"]]) == 0
    assert json.loads(capsys.readouterr().out)["reference_approved"] is True
    assert cli_main.main(["fable-approve", project_id, "--step", "characters"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "SHOT_REVIEW"


def test_reference_reject_reopens_generation(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-approve", project_id, "--step", "story"]) == 0
    capsys.readouterr()
    assert cli_main.main(["fable-generate-references", project_id]) == 0
    character_id = json.loads(capsys.readouterr().out)["characters"][0]["character_id"]

    assert cli_main.main(["fable-reference", character_id]) == 0
    capsys.readouterr()
    assert cli_main.main(["fable-reference", character_id, "--reject"]) == 0
    assert json.loads(capsys.readouterr().out)["reference_approved"] is False


def test_reference_smoke_runs_against_a_free_tier(monkeypatch, tmp_path, capsys) -> None:
    """Against the fake tier it is a cheap wiring check -- and it reports
    what it does and does not prove, so a pasted result can never be read
    as more than it is."""
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["fable-reference-smoke"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["chained_reference_accepted"] is True
    assert payload["face_checksum_sha256"] != payload["chained_checksum_sha256"]
    assert any("Veo" in line for line in payload["does_not_prove"])


def test_reference_smoke_refuses_to_spend_without_confirmation(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setenv("REEL_HARNESS_REFERENCE_IMAGE_PROVIDER", "google")
    monkeypatch.setenv("REEL_HARNESS_GOOGLE_API_KEY", "test-key")
    assert cli_main.main(["fable-reference-smoke"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "NOT RUN"
    assert payload["images"] == 2
    assert payload["projected_cost"] is not None  # says what it would cost


def test_reference_smoke_keeps_output_when_asked(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    kept = tmp_path / "kept"
    assert cli_main.main(["fable-reference-smoke", "--keep-output", str(kept)]) == 0
    capsys.readouterr()
    assert sorted(p.name for p in kept.iterdir()) == ["face.png", "three_quarter.png"]


def test_status_reports_the_budget_block(monkeypatch, tmp_path, capsys) -> None:
    project_id = _adapted_project(monkeypatch, tmp_path, capsys)
    assert cli_main.main(["fable-budget", project_id, "--limit", "2.5", "--currency", "FAKE"]) == 0
    capsys.readouterr()
    assert cli_main.main(["fable-status", project_id]) == 0
    budget = json.loads(capsys.readouterr().out)["budget"]
    assert budget["limit_amount"] == 2.5
    assert budget["currency"] == "FAKE"
    assert budget["unpriced_take_count"] == 0
