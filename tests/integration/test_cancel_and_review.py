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


def _ensure_review_required(session, db_job) -> bool:
    """Ensures the job is in REVIEW_REQUIRED, for whatever reason it got there.

    On a machine with ffmpeg/ffprobe (this one, as of this pass), `run_job`
    reaches REVIEW_REQUIRED for real -- this is a no-op and returns True. On a
    machine without them, RENDERING fails with BLOCKED_DEPENDENCY and this
    synthesizes the same state transitions a successful RENDER/VALIDATE pass
    would have produced, so the reject/approve *mechanics* under test stay
    covered either way, and returns False so callers know no real manifest
    exists yet.
    """
    if db_job.status == JobStatus.REVIEW_REQUIRED.value:
        return True
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
    return False


def test_reject_routes_back_to_the_requested_stage_and_resumes_for_real(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        _ensure_review_required(session, db_job)

    rejected = job_service.reject(job.id, reason="subtitle wording is off", regenerate_from_stage="SCRIPT")
    assert rejected.status == JobStatus.RETRY_WAIT.value
    assert rejected.retry_target_stage == "SCRIPT"
    assert rejected.attempt_number == 2

    # Resuming from that RETRY_WAIT must actually re-run SCRIPT for real. On a
    # machine with ffmpeg/ffprobe this re-runs the *entire* remaining pipeline
    # for real and reaches REVIEW_REQUIRED again; without them it re-blocks at
    # RENDER again -- either way nothing here is simulated.
    old_script = rejected.script
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.script is not None
        assert db_job.script == old_script  # FakeLLMProvider is deterministic for the same topic
        if db_job.status == JobStatus.REVIEW_REQUIRED.value:
            assert (storage.job_dir(job.id) / "final" / "final.mp4").exists()
        else:
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
        reached_for_real = _ensure_review_required(session, db_job)
        if not reached_for_real:
            # No ffmpeg on this machine: run_job() never got to write a real
            # manifest. Synthesize the same file it would have written, so
            # approve() has one to stamp, without claiming a real video was
            # produced.
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
    if reached_for_real:
        assert saved["final_video_checksum_sha256"] is not None
        assert saved["render"]["ffmpeg_version"] is not None
        assert saved["validation"]["has_audio_stream"] is True


def test_manual_retry_from_stage_requires_failed_status(job_service, channel) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with pytest.raises(InvalidActionError):
        job_service.retry_from_stage(job.id, stage="SCRIPT")
