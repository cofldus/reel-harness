from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.worker.lease import lease_next_job, recover_stale_jobs


def test_lease_next_job_claims_a_queued_job(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        assert leased is not None
        assert leased.id == job.id
        assert leased.locked_by == "worker-a"


def test_two_workers_racing_for_the_same_job_only_one_wins(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")

    with session_factory() as session_a, session_factory() as session_b:
        leased_a = lease_next_job(session_a, worker_id="worker-a")
        leased_b = lease_next_job(session_b, worker_id="worker-b")
        assert leased_a is not None
        assert leased_a.id == job.id
        assert leased_b is None  # already locked by worker-a


def test_lease_ignores_jobs_already_locked_by_another_worker(job_service, channel, session_factory) -> None:
    job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        first = lease_next_job(session, worker_id="worker-a")
        assert first is not None
        second = lease_next_job(session, worker_id="worker-b")
        assert second is None


def test_lease_picks_up_retry_wait_job_once_backoff_elapses(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(
            db_job, JobStatus.RETRY_WAIT,
            retry_target_stage="SCRIPT", next_retry_at=now - timedelta(seconds=1),
            failure_code="UPSTREAM_TRANSIENT", failure_summary="boom",
        )
        session.commit()

        leased = lease_next_job(session, worker_id="worker-a", now=now)
        assert leased is not None
        assert leased.id == job.id


def test_lease_skips_retry_wait_job_whose_backoff_has_not_elapsed(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(
            db_job, JobStatus.RETRY_WAIT,
            retry_target_stage="SCRIPT", next_retry_at=now + timedelta(seconds=60),
            failure_code="UPSTREAM_TRANSIENT", failure_summary="boom",
        )
        session.commit()

        leased = lease_next_job(session, worker_id="worker-a", now=now)
        assert leased is None


def test_recover_stale_jobs_moves_crashed_job_to_retry_wait(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        db_job.current_stage = "SCRIPT"
        db_job.locked_by = "dead-worker"
        db_job.heartbeat_at = now - timedelta(seconds=999)
        session.commit()

        recovered = recover_stale_jobs(session, lease_timeout_seconds=60, now=now)
        assert recovered == [job.id]

        refreshed = session.get(type(job), job.id)
        assert refreshed.status == JobStatus.RETRY_WAIT.value
        assert refreshed.retry_target_stage == "SCRIPT"
        assert refreshed.failure_code == "WORKER_CRASHED"
        assert refreshed.locked_by is None


def test_recover_stale_jobs_ignores_fresh_heartbeats(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        db_job.current_stage = "SCRIPT"
        db_job.locked_by = "alive-worker"
        db_job.heartbeat_at = now
        session.commit()

        recovered = recover_stale_jobs(session, lease_timeout_seconds=60, now=now)
        assert recovered == []
