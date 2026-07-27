"""Regression tests for resuming a job from a mid-pipeline stage in a fresh
worker/session (the audit BLOCKER-1 class of failures).

Every worker pass below opens a brand-new session and rebuilds inter-stage
context from the DB and job storage only -- nothing is carried over from the
process/session that ran the earlier stages. On a machine with real
ffmpeg/ffprobe (the completion-gate environment) the full resume paths run for
real; without them, each test asserts the honest FAILED outcome instead of
synthesizing success.
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from reel_harness.core.service import InvalidActionError
from reel_harness.core.state_machine import JobStatus, ReasonCode, apply_transition
from reel_harness.db.models import Job, StageRun
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.lease import find_orphaned_active_jobs
from reel_harness.worker.runner import run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _run_worker_pass(session_factory, job_id, channel, providers, storage) -> str:
    """One worker pass in a completely fresh session, like a restarted worker."""
    with session_factory() as session:
        db_job = session.get(Job, job_id)
        run_job(session, db_job, channel, providers, storage)
        return db_job.status


def _stage_attempts(session_factory, job_id) -> dict[str, list[tuple[int, str]]]:
    with session_factory() as session:
        rows = session.execute(
            select(StageRun.stage, StageRun.attempt, StageRun.status)
            .where(StageRun.job_id == job_id)
            .order_by(StageRun.started_at, StageRun.attempt),
        ).all()
    grouped: dict[str, list[tuple[int, str]]] = {}
    for stage, attempt, status in rows:
        grouped.setdefault(stage, []).append((attempt, status))
    return grouped


def _assert_no_orphans(session_factory) -> None:
    with session_factory() as session:
        assert find_orphaned_active_jobs(session) == []


def _final_path(storage, job_id):
    return storage.job_dir(job_id) / "final" / "final.mp4"


def _sha256(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _manifest(storage, job_id) -> dict:
    return json.loads(storage.read_bytes(job_id, "manifest.json"))


def test_reject_to_render_resumes_without_rerunning_earlier_stages(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="resume-render", topic="t")
    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)

    if not FFMPEG_PRESENT:
        # Honest outcome without ffmpeg: blocked at RENDER; REVIEW_REQUIRED (and
        # therefore reject) is unreachable. The RENDER resume path without
        # ffmpeg is covered by the manual-retry test below.
        assert status == JobStatus.FAILED.value
        _assert_no_orphans(session_factory)
        return

    assert status == JobStatus.REVIEW_REQUIRED.value
    rejected = job_service.reject(job.id, reason="colors are off", regenerate_from_stage="RENDER")
    assert rejected.status == JobStatus.RETRY_WAIT.value
    assert rejected.retry_target_stage == "RENDER"

    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    assert status == JobStatus.REVIEW_REQUIRED.value

    attempts = _stage_attempts(session_factory, job.id)
    for untouched in ("SCRIPT", "POLICY", "ASSET", "TTS"):
        assert [a for a, _ in attempts[untouched]] == [1], f"{untouched} must not re-run"
    assert [a for a, _ in attempts["RENDER"]] == [1, 2]
    assert [a for a, _ in attempts["VALIDATE"]] == [1, 2]
    assert all(s == "success" for _, s in attempts["RENDER"])

    manifest = _manifest(storage, job.id)
    assert manifest["final_video_checksum_sha256"] == _sha256(_final_path(storage, job.id))
    assert manifest["approval"]["decision"] is None  # no stale approval before re-review
    _assert_no_orphans(session_factory)


def test_reject_to_tts_resumes_without_rerunning_earlier_stages(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="resume-tts", topic="t")
    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)

    if not FFMPEG_PRESENT:
        assert status == JobStatus.FAILED.value
        _assert_no_orphans(session_factory)
        return

    assert status == JobStatus.REVIEW_REQUIRED.value
    job_service.reject(job.id, reason="voice pacing", regenerate_from_stage="TTS")

    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    assert status == JobStatus.REVIEW_REQUIRED.value

    attempts = _stage_attempts(session_factory, job.id)
    for untouched in ("SCRIPT", "POLICY", "ASSET"):
        assert [a for a, _ in attempts[untouched]] == [1], f"{untouched} must not re-run"
    assert [a for a, _ in attempts["TTS"]] == [1, 2]
    assert [a for a, _ in attempts["RENDER"]] == [1, 2]
    assert [a for a, _ in attempts["VALIDATE"]] == [1, 2]
    assert _manifest(storage, job.id)["final_video_checksum_sha256"] == _sha256(_final_path(storage, job.id))
    _assert_no_orphans(session_factory)


def test_reject_to_validate_revalidates_existing_video_without_rerendering(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="resume-validate", topic="t")
    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)

    if not FFMPEG_PRESENT:
        assert status == JobStatus.FAILED.value
        _assert_no_orphans(session_factory)
        return

    assert status == JobStatus.REVIEW_REQUIRED.value
    checksum_before = _sha256(_final_path(storage, job.id))
    job_service.reject(job.id, reason="double-check the encode", regenerate_from_stage="VALIDATE")

    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    assert status == JobStatus.REVIEW_REQUIRED.value

    attempts = _stage_attempts(session_factory, job.id)
    assert [a for a, _ in attempts["RENDER"]] == [1], "VALIDATE resume must not re-render"
    assert [a for a, _ in attempts["VALIDATE"]] == [1, 2]
    assert _sha256(_final_path(storage, job.id)) == checksum_before  # same file, re-probed
    assert _manifest(storage, job.id)["final_video_checksum_sha256"] == checksum_before
    _assert_no_orphans(session_factory)


def test_automatic_render_retry_resumes_from_render_in_a_new_session(
    job_service, channel, session_factory, storage, fake_providers, monkeypatch,
) -> None:
    """A real transient RENDER failure (the resolved "ffmpeg" is a binary that
    exits nonzero -- no mocked returncodes) must land in RETRY_WAIT targeting
    RENDER, and the next pass must resume from RENDER with restored context.
    A planted stale final.mp4 must not be mistaken for this run's output.
    """
    job, _ = job_service.create_job(channel.id, idempotency_key="auto-retry-render", topic="t")

    monkeypatch.setenv("REEL_HARNESS_FFMPEG_PATH", sys.executable)
    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    assert status == JobStatus.RETRY_WAIT.value

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        assert db_job.retry_target_stage == "RENDER"
        assert db_job.failure_code == "UPSTREAM_TRANSIENT"
        assert db_job.retry_count == 1
        assert db_job.next_retry_at is not None

    final_path = _final_path(storage, job.id)
    assert not final_path.exists(), "failed render must not leave a final.mp4 behind"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(b"stale output from some earlier attempt")

    monkeypatch.delenv("REEL_HARNESS_FFMPEG_PATH")
    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)

    if FFMPEG_PRESENT:
        assert status == JobStatus.REVIEW_REQUIRED.value
        assert final_path.read_bytes() != b"stale output from some earlier attempt"
        attempts = _stage_attempts(session_factory, job.id)
        assert [a for a, _ in attempts["RENDER"]] == [1, 2]
        assert [(a, s) for a, s in attempts["RENDER"]] == [(1, "failed"), (2, "success")]
        assert _manifest(storage, job.id)["final_video_checksum_sha256"] == _sha256(final_path)
    else:
        assert status == JobStatus.FAILED.value
        with session_factory() as session:
            assert session.get(Job, job.id).failure_code == "BLOCKED_DEPENDENCY"
    _assert_no_orphans(session_factory)


def test_manual_retry_of_failed_job_resumes_from_render(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="manual-retry-render", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        if db_job.status == JobStatus.REVIEW_REQUIRED.value:
            # ffmpeg present: synthesize the FAILED state a blocked machine
            # produces naturally, to exercise the operator-override path.
            apply_transition(
                db_job, JobStatus.RETRY_WAIT,
                retry_target_stage="RENDER", next_retry_at=datetime.now(UTC),
                failure_code="TEST_SETUP_ONLY", failure_summary="simulating a render failure",
            )
            apply_transition(
                db_job, JobStatus.FAILED,
                failure_code="BLOCKED_DEPENDENCY", failure_summary="simulated",
            )
        assert db_job.status == JobStatus.FAILED.value
        session.commit()

    retried = job_service.retry_from_stage(job.id, stage="RENDER")
    assert retried.status == JobStatus.RETRY_WAIT.value
    assert retried.retry_target_stage == "RENDER"

    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    if FFMPEG_PRESENT:
        assert status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session_factory, job.id)
        assert [a for a, _ in attempts["RENDER"]] == [1, 2]
        assert _manifest(storage, job.id)["final_video_checksum_sha256"] == _sha256(_final_path(storage, job.id))
    else:
        # Restoration itself succeeded (assets/tts were persisted); RENDER then
        # honestly re-blocks on the missing dependency. No crash either way.
        assert status == JobStatus.FAILED.value
        with session_factory() as session:
            assert session.get(Job, job.id).failure_code == "BLOCKED_DEPENDENCY"
    _assert_no_orphans(session_factory)


def test_invalid_retry_targets_are_rejected_at_the_service_boundary(
    job_service, channel, session_factory,
) -> None:
    reviewed, _ = job_service.create_job(channel.id, idempotency_key="bad-target-review", topic="t")
    failed, _ = job_service.create_job(channel.id, idempotency_key="bad-target-failed", topic="t")
    with session_factory() as session:
        db_reviewed = session.get(Job, reviewed.id)
        apply_transition(db_reviewed, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_reviewed, JobStatus.POLICY_CHECKING)
        apply_transition(
            db_reviewed, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.CONTENT_POLICY_REVIEW.value,
        )
        db_failed = session.get(Job, failed.id)
        apply_transition(db_failed, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_failed, JobStatus.FAILED, failure_code="X", failure_summary="x")
        session.commit()

    for bad_target in ("PUBLISH", "TOPIC", "", "NOT_A_STAGE"):
        with pytest.raises(InvalidActionError):
            job_service.reject(reviewed.id, reason="r", regenerate_from_stage=bad_target)
        with pytest.raises(InvalidActionError):
            job_service.retry_from_stage(failed.id, stage=bad_target)

    assert job_service.get_job(reviewed.id).status == JobStatus.REVIEW_REQUIRED.value
    assert job_service.get_job(failed.id).status == JobStatus.FAILED.value
    _assert_no_orphans(session_factory)


def test_worker_fails_cleanly_on_unsupported_persisted_resume_stage(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    """Defense in depth: even if a RETRY_WAIT row names an unsupported stage
    (older code, manual DB edits), the worker must fail the job explicitly
    instead of crashing or stranding it."""
    for bad_target, key in (("PUBLISH", "bad-resume-publish"), ("NOT_A_STAGE", "bad-resume-garbage")):
        job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
        with session_factory() as session:
            db_job = session.get(Job, job.id)
            apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
            apply_transition(
                db_job, JobStatus.RETRY_WAIT,
                retry_target_stage=bad_target, next_retry_at=datetime.now(UTC),
                failure_code="TEST_SETUP_ONLY", failure_summary="unsupported target",
            )
            session.commit()

        status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
        assert status == JobStatus.FAILED.value
        with session_factory() as session:
            refreshed = session.get(Job, job.id)
            assert refreshed.failure_code == "UNSUPPORTED_RESUME_STAGE"
            assert refreshed.locked_by is None
    _assert_no_orphans(session_factory)


def test_resume_with_missing_prerequisite_fails_explicitly(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    """A RENDER resume whose persisted assets were never recorded (or were
    deleted) must fail with MISSING_PREREQUISITE naming the gap -- not KeyError,
    and not an ACTIVE+unlocked job."""
    job, _ = job_service.create_job(channel.id, idempotency_key="missing-prereq", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "t", "scenes": [], "llm_provider_id": "fake",
                         "llm_model_id": "fake", "prompt_version": "v1"}
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(
            db_job, JobStatus.RETRY_WAIT,
            retry_target_stage="RENDER", next_retry_at=datetime.now(UTC),
            failure_code="TEST_SETUP_ONLY", failure_summary="no assets were ever persisted",
        )
        session.commit()

    status = _run_worker_pass(session_factory, job.id, channel, fake_providers, storage)
    assert status == JobStatus.FAILED.value
    with session_factory() as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.failure_code == "MISSING_PREREQUISITE"
        assert "assets" in (refreshed.failure_summary or "")
    _assert_no_orphans(session_factory)
