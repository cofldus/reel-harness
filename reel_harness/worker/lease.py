from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from reel_harness.core.state_machine import JobStatus, Stage, apply_transition
from reel_harness.db.models import Job
from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES, STAGE_RETRY_POLICY


def lease_next_job(session, worker_id: str, now: datetime | None = None) -> Job | None:
    """Atomically claims one QUEUED job, or one RETRY_WAIT job whose backoff has
    elapsed, for this worker. Returns None if nothing is ready or another worker
    won the race (rowcount == 0 on the guarded UPDATE)."""
    now = now or datetime.now(UTC)
    candidate_id = session.execute(
        select(Job.id)
        .where(
            (Job.status == JobStatus.QUEUED.value)
            | ((Job.status == JobStatus.RETRY_WAIT.value) & (Job.next_retry_at <= now)),
        )
        .where(Job.locked_by.is_(None))
        .order_by(Job.created_at)
        .limit(1),
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    result = session.execute(
        update(Job)
        .where(Job.id == candidate_id, Job.locked_by.is_(None))
        .values(locked_by=worker_id, heartbeat_at=now),
    )
    session.commit()
    if result.rowcount == 0:
        return None
    return session.get(Job, candidate_id)


def release_lease(session, job: Job) -> None:
    job.locked_by = None
    session.commit()


def recover_stale_jobs(session, lease_timeout_seconds: int, now: datetime | None = None) -> list[str]:
    """Finds jobs locked by a worker whose heartbeat is older than the lease
    timeout (i.e. that worker crashed mid-stage) and routes them back to
    RETRY_WAIT/FAILED via the normal retry-count bookkeeping."""
    now = now or datetime.now(UTC)
    threshold = now - timedelta(seconds=lease_timeout_seconds)
    stale_jobs = session.execute(
        select(Job).where(
            Job.locked_by.is_not(None),
            Job.status.in_([s.value for s in ACTIVE_STAGE_STATUSES]),
        ),
    ).scalars().all()

    recovered: list[str] = []
    for job in stale_jobs:
        if job.heartbeat_at is not None and job.heartbeat_at >= threshold:
            continue
        _recover_job(job, now)
        recovered.append(job.id)
    session.commit()
    return recovered


def _recover_job(job: Job, now: datetime) -> None:
    stage = Stage(job.current_stage) if job.current_stage else Stage.SCRIPT
    max_retries, backoffs = STAGE_RETRY_POLICY.get(stage, (0, []))
    job.locked_by = None
    if job.retry_count >= max_retries:
        apply_transition(
            job, JobStatus.FAILED,
            failure_code="WORKER_CRASHED",
            failure_summary=f"worker crashed during {stage.value}, retries exhausted",
        )
        return
    delay = backoffs[min(job.retry_count, len(backoffs) - 1)] if backoffs else 30
    job.retry_count += 1
    apply_transition(
        job, JobStatus.RETRY_WAIT,
        retry_target_stage=stage.value,
        next_retry_at=now + timedelta(seconds=delay),
        failure_code="WORKER_CRASHED",
        failure_summary=f"worker crashed during {stage.value}",
    )
