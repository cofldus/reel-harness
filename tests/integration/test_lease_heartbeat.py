"""Lease token, guarded heartbeat, and fencing primitives -- single-worker
scope. Multi-worker takeover scenarios live in test_multi_worker.py.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Job
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.heartbeat import LeaseHeartbeat
from reel_harness.worker.lease import (
    assert_lease,
    heartbeat_lease,
    lease_next_job,
    recover_stale_jobs,
    release_lease,
)
from reel_harness.worker.runner import run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def test_lease_mints_a_token_and_heartbeat_is_token_guarded(
    job_service, channel, session_factory,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        assert leased is not None
        token = leased.lease_token
        assert token, "every lease acquisition must mint a lease token"
        before = leased.heartbeat_at

        assert heartbeat_lease(session, job.id, token) is True
        session.expire(leased)
        assert leased.heartbeat_at >= before

        assert heartbeat_lease(session, job.id, "not-the-token") is False


def test_recovery_rotates_the_token_so_the_old_owner_is_fenced_out(
    job_service, channel, session_factory,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a", now=now)
        old_token = leased.lease_token
        apply_transition(leased, JobStatus.SCRIPT_GENERATING)
        leased.current_stage = "SCRIPT"
        leased.heartbeat_at = now - timedelta(seconds=999)
        session.commit()

        recovered = recover_stale_jobs(session, lease_timeout_seconds=60, now=now)
        assert recovered == [job.id]
        session.expire(leased)
        assert leased.lease_token is None
        assert heartbeat_lease(session, job.id, old_token) is False
        assert assert_lease(session, job.id, old_token) is False
        session.rollback()


def test_release_with_wrong_token_keeps_the_current_lease(
    job_service, channel, session_factory,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        token = leased.lease_token

        release_lease(session, leased, lease_token="stale-token-from-before")
        session.expire(leased)
        assert leased.locked_by == "worker-a"
        assert leased.lease_token == token

        release_lease(session, leased, lease_token=token)
        assert leased.locked_by is None
        assert leased.lease_token is None


def test_heartbeat_thread_beats_stops_cleanly_and_detects_loss(
    job_service, channel, session_factory,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        token = leased.lease_token
        first_beat = leased.heartbeat_at

    heartbeat = LeaseHeartbeat(session_factory, job.id, token, interval_seconds=0.05)
    with heartbeat:
        deadline = time.monotonic() + 5.0
        while heartbeat.beat_count == 0 and time.monotonic() < deadline:
            time.sleep(0.02)
    assert heartbeat.beat_count >= 1
    assert heartbeat.is_running is False, "stop() must join the thread"
    assert heartbeat.error_count == 0
    with session_factory() as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.heartbeat_at > first_beat

    # Rotate the token out from under a second heartbeat: it must set
    # lease_lost and exit on its own.
    with session_factory() as session:
        session.execute(
            Job.__table__.update().where(Job.id == job.id).values(lease_token="taken-over"),
        )
        session.commit()
    lost = LeaseHeartbeat(session_factory, job.id, token, interval_seconds=0.05)
    with lost:
        assert lost.lease_lost.wait(timeout=5.0)
    assert lost.is_running is False


def test_single_worker_pipeline_with_fencing_still_completes(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    """Regression: the fenced commit path must not change single-worker
    behavior -- same terminal states as before, lease released afterwards."""
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        token = leased.lease_token
        run_job(session, leased, channel, fake_providers, storage, lease_token=token)
        if FFMPEG_PRESENT:
            assert leased.status == JobStatus.REVIEW_REQUIRED.value
        else:
            assert leased.status == JobStatus.FAILED.value
            assert leased.failure_code == "BLOCKED_DEPENDENCY"
        release_lease(session, leased, lease_token=token)
        session.expire(leased)
        assert leased.locked_by is None


def test_worker_with_stale_token_cannot_touch_the_job_at_all(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    """A worker holding a reclaimed token is fenced out at the very first stage
    commit: no status change, no StageRun rows, no exception."""
    from sqlalchemy import select

    from reel_harness.db.models import StageRun

    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.lease_token = "current-owner-token"
        db_job.locked_by = "worker-b"
        session.commit()

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage, lease_token="reclaimed-old-token")
        session.expire(db_job)
        assert db_job.status == JobStatus.QUEUED.value  # untouched
        assert db_job.lease_token == "current-owner-token"
        runs = session.execute(select(StageRun).where(StageRun.job_id == job.id)).scalars().all()
        assert runs == []
