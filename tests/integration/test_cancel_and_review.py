from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from reel_harness.core.service import InvalidActionError
from reel_harness.core.state_machine import JobStatus, ReasonCode, apply_transition
from reel_harness.manifest.schema import LLMInfo, Manifest, TTSInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.worker.runner import run_job


def test_cancel_requested_is_honored_before_the_next_stage(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    job_service.request_cancel(job.id)

    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.status == JobStatus.CANCELLED.value


def test_cannot_cancel_an_already_cancelled_job(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    job_service.request_cancel(job.id)
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.status == JobStatus.CANCELLED.value

    with pytest.raises(InvalidActionError):
        job_service.request_cancel(job.id)


def _drive_to_review_required(session, db_job) -> None:
    """Puts a job into REVIEW_REQUIRED without a real ffmpeg render.

    On a machine without ffmpeg/ffprobe, `run_job` legitimately cannot reach
    REVIEW_REQUIRED for real (RENDERING fails with BLOCKED_DEPENDENCY -- see
    docs/STATUS.md). The reject/approve *mechanics* being tested here
    (JobService.reject/approve, RETRY_WAIT bookkeeping, manifest approval
    update) are independent of whether the video actually rendered, so this
    helper synthesizes the same state transitions a successful RENDER/VALIDATE
    pass would have produced, instead of skipping coverage of those mechanics
    whenever ffmpeg happens to be missing.
    """
    assert db_job.status == JobStatus.FAILED.value
    assert db_job.failure_code == "BLOCKED_DEPENDENCY"
    apply_transition(
        db_job, JobStatus.RETRY_WAIT,
        retry_target_stage="RENDER", next_retry_at=datetime.now(UTC),
        failure_code="TEST_SETUP_ONLY", failure_summary="simulating a completed render for this test",
    )
    apply_transition(db_job, JobStatus.RENDERING)
    apply_transition(db_job, JobStatus.VALIDATING)
    apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.USER_APPROVAL_REQUIRED.value)
    session.commit()


def test_reject_routes_back_to_the_requested_stage_and_resumes_for_real(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        _drive_to_review_required(session, db_job)

    rejected = job_service.reject(job.id, reason="subtitle wording is off", regenerate_from_stage="SCRIPT")
    assert rejected.status == JobStatus.RETRY_WAIT.value
    assert rejected.retry_target_stage == "SCRIPT"
    assert rejected.attempt_number == 2

    # Resuming from that RETRY_WAIT must actually re-run SCRIPT for real
    # (this part needs no ffmpeg at all, so it is not simulated).
    old_script = rejected.script
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.script is not None
        assert db_job.script == old_script  # FakeLLMProvider is deterministic for the same topic
        # blocked at RENDER again, for real, exactly like the first pass
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.current_stage == "RENDER"
        assert db_job.failure_code == "BLOCKED_DEPENDENCY"


def test_approve_requires_review_required_status(job_service, channel) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with pytest.raises(InvalidActionError):
        job_service.approve(job.id)


def test_approve_transitions_review_required_to_completed_and_stamps_manifest(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        _drive_to_review_required(session, db_job)
        # A real run_job() would have written this manifest right before
        # REVIEW_REQUIRED; synthesize the same file so approve() has one to
        # stamp, without claiming a real video was produced.
        manifest = Manifest(
            job_id=db_job.id, created_at=db_job.created_at, topic=db_job.topic,
            script_title=db_job.script["title"],
            llm=LLMInfo(provider_id="fake", model_id="fake-deterministic-v1", prompt_version="fake-script-v1"),
            tts=TTSInfo(provider_id="fake", voice_id="fake-voice-1"),
            assets=[],
        )
        write_manifest(storage, db_job.id, manifest)

    approved = job_service.approve(job.id)
    assert approved.status == JobStatus.COMPLETED.value

    saved = json.loads(storage.read_bytes(job.id, "manifest.json"))
    assert saved["approval"]["decision"] == "approve"
    assert saved["approval"]["decided_at"] is not None


def test_manual_retry_from_stage_requires_failed_status(job_service, channel) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with pytest.raises(InvalidActionError):
        job_service.retry_from_stage(job.id, stage="SCRIPT")
