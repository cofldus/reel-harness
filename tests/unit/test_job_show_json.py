"""job-show --json must emit exactly one parseable JSON document on stdout."""
from __future__ import annotations

import json

from reel_harness.cli import main as cli_main
from reel_harness.core.state_machine import JobStatus, ReasonCode, apply_transition
from reel_harness.db.models import Job


def _isolated_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)


def _make_review_required_job(tmp_path) -> str:
    from reel_harness.core.service import JobService
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    service = JobService(factory)
    channel = service.create_channel(name="c", niche="n", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="k1", topic="t")
    with factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.USER_APPROVAL_REQUIRED.value)
        session.commit()
    return job.id


def test_json_output_is_exactly_one_document_even_for_review_required(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolated_env(monkeypatch, tmp_path)
    job_id = _make_review_required_job(tmp_path)

    assert cli_main.main(["job-show", job_id, "--json"]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)  # would raise if anything but one JSON doc is on stdout
    assert payload["job_id"] == job_id
    assert payload["status"] == "REVIEW_REQUIRED"
    assert payload["preview_path"].endswith("final.mp4")
    assert payload["manifest_path"].endswith("manifest.json")


def test_human_output_keeps_hints_but_off_stdout_json_path(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolated_env(monkeypatch, tmp_path)
    job_id = _make_review_required_job(tmp_path)

    assert cli_main.main(["job-show", job_id]) == 0
    captured = capsys.readouterr()
    json.loads(captured.out)  # the job JSON block itself stays parseable
    assert "preview:" in captured.err
    assert "manifest:" in captured.err


def test_json_output_for_non_review_job_has_null_paths(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.core.service import JobService
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    _isolated_env(monkeypatch, tmp_path)
    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'cli.db').as_posix()}")
    init_db(engine)
    service = JobService(make_session_factory(engine))
    channel = service.create_channel(name="c", niche="n", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="k2", topic="t")

    assert cli_main.main(["job-show", job.id, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "QUEUED"
    assert payload["preview_path"] is None
    assert payload["manifest_path"] is None
