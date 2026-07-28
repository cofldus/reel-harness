"""REVIEW_REQUIRED (and other idle-state) cancellation: immediate transition,
no dangling flags, artifacts preserved, and no further actions possible."""
from __future__ import annotations

import json

import pytest

from reel_harness.core.service import InvalidActionError
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job
from reel_harness.manifest.schema import Manifest, is_publish_eligible
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.lease import find_orphaned_active_jobs, lease_next_job
from reel_harness.worker.runner import run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _run_to_review(job_service, channel, session_factory, storage, fake_providers, key) -> tuple[str, bool]:
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        return job.id, db_job.status == JobStatus.REVIEW_REQUIRED.value


def test_review_required_cancel_transitions_immediately(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job_id, reached = _run_to_review(job_service, channel, session_factory, storage, fake_providers, "c1")
    if not reached:
        # Without ffmpeg the job is FAILED (terminal): cancel is refused, which
        # is the designed behavior for terminal states.
        with pytest.raises(InvalidActionError):
            job_service.request_cancel(job_id)
        return

    cancelled = job_service.request_cancel(job_id)
    assert cancelled.status == JobStatus.CANCELLED.value, "no worker is attached -- must not linger as a flag"

    # Artifacts are preserved for post-mortem; the manifest still carries no
    # approval and can never become publish-eligible.
    final_path = storage.job_dir(job_id) / "final" / "final.mp4"
    assert final_path.exists()
    manifest = Manifest.model_validate_json(storage.read_bytes(job_id, "manifest.json"))
    assert manifest.approval.decision is None
    assert is_publish_eligible(manifest) is False

    # No further review actions, no retry, no re-cancel.
    with pytest.raises(InvalidActionError):
        job_service.approve(job_id)
    with pytest.raises(InvalidActionError):
        job_service.reject(job_id, reason="r", regenerate_from_stage="SCRIPT")
    with pytest.raises(InvalidActionError):
        job_service.retry_from_stage(job_id, stage="RENDER")
    with pytest.raises(InvalidActionError):
        job_service.request_cancel(job_id)

    # Workers can never lease it again and no forbidden state remains.
    with session_factory() as session:
        assert lease_next_job(session, worker_id="w1") is None
        assert find_orphaned_active_jobs(session) == []


def test_queued_and_retry_wait_cancel_transition_immediately_when_unleased(
    job_service, channel, session_factory,
) -> None:
    from datetime import UTC, datetime

    from reel_harness.core.state_machine import apply_transition

    queued, _ = job_service.create_job(channel.id, idempotency_key="c2", topic="t")
    assert job_service.request_cancel(queued.id).status == JobStatus.CANCELLED.value

    waiting, _ = job_service.create_job(channel.id, idempotency_key="c3", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, waiting.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(
            db_job, JobStatus.RETRY_WAIT, retry_target_stage="SCRIPT",
            next_retry_at=datetime.now(UTC), failure_code="X", failure_summary="x",
        )
        session.commit()
    assert job_service.request_cancel(waiting.id).status == JobStatus.CANCELLED.value


def test_leased_job_cancel_still_defers_to_the_worker_boundary(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="c4", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        assert leased is not None

    flagged = job_service.request_cancel(job.id)
    assert flagged.status == JobStatus.QUEUED.value, "a leased job must not be yanked out from under its worker"
    assert flagged.cancel_requested is True

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage, lease_token=db_job.lease_token)
        assert db_job.status == JobStatus.CANCELLED.value


def test_api_and_cli_share_the_same_cancel_path(monkeypatch, tmp_path, capsys) -> None:
    from fastapi.testclient import TestClient

    from reel_harness.api.app import app, get_context
    from reel_harness.bootstrap import AppContext
    from reel_harness.cli import main as cli_main
    from reel_harness.config import Settings
    from reel_harness.core.state_machine import ReasonCode, apply_transition

    ctx = AppContext(settings=Settings(
        database_url=f"sqlite:///{tmp_path / 'cancel.db'}",
        jobs_dir=tmp_path / "jobs", app_api_key="fake-test-api-key",
    ))
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")

    def make_review_job(key: str) -> str:
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key=key, topic="t")
        with ctx.session_factory() as session:
            db_job = session.get(Job, job.id)
            apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
            apply_transition(db_job, JobStatus.POLICY_CHECKING)
            apply_transition(
                db_job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.USER_APPROVAL_REQUIRED.value,
            )
            session.commit()
        return job.id

    api_job = make_review_job("api-cancel")
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).post(
            f"/v1/jobs/{api_job}/cancel", headers={"Authorization": "Bearer fake-test-api-key"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == JobStatus.CANCELLED.value
    finally:
        app.dependency_overrides.clear()

    cli_job = make_review_job("cli-cancel")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'cancel.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.chdir(tmp_path)
    assert cli_main.main(["job-cancel", cli_job]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == JobStatus.CANCELLED.value
