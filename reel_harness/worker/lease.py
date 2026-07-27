from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from reel_harness.core.state_machine import JobStatus, Stage, apply_transition
from reel_harness.db.models import Job, new_uuid
from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES, STAGE_RETRY_POLICY


def lease_next_job(session, worker_id: str, now: datetime | None = None) -> Job | None:
    """Atomically claims one QUEUED job, or one RETRY_WAIT job whose backoff has
    elapsed, for this worker. Returns None if nothing is ready or another worker
    won the race (rowcount == 0 on the guarded UPDATE).

    Every successful claim mints a fresh lease_token. All later writes for this
    job (heartbeats, stage commits, release) are guarded on that token, so a
    worker that loses its lease to stale recovery is fenced out at the DB level
    -- see assert_lease()."""
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
        .values(locked_by=worker_id, heartbeat_at=now, lease_token=new_uuid()),
    )
    session.commit()
    if result.rowcount == 0:
        return None
    return session.get(Job, candidate_id)


def heartbeat_lease(session, job_id: str, lease_token: str, now: datetime | None = None) -> bool:
    """Refreshes heartbeat_at -- but only if this worker still owns the lease
    (token match at the DB level). Returns False when the lease was reclaimed;
    the caller must stop treating the job as its own. Commits."""
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Job).where(Job.id == job_id, Job.lease_token == lease_token).values(heartbeat_at=now),
    )
    session.commit()
    return result.rowcount == 1


def assert_lease(session, job_id: str, lease_token: str | None, now: datetime | None = None) -> bool:
    """Fencing primitive, executed inside the caller's open transaction right
    before committing stage results or a status transition. The guarded UPDATE
    takes SQLite's write lock: either a takeover already committed (0 rows
    match -- the caller must roll back and abandon) or any takeover now blocks
    until the caller's commit finishes, so check-then-commit has no gap.
    Refreshes the heartbeat as a side effect. Does NOT commit.

    lease_token=None means the caller runs without lease fencing (direct
    library/test invocation); the check passes."""
    if lease_token is None:
        return True
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Job).where(Job.id == job_id, Job.lease_token == lease_token).values(heartbeat_at=now),
    )
    return result.rowcount == 1


def release_lease(session, job: Job, lease_token: str | None = None) -> None:
    """Releases the worker's lease -- unless the job is still in an ACTIVE stage
    status. Invariant: an ACTIVE job must always have a lease owner; a job whose
    lease is released must be terminal, RETRY_WAIT, or otherwise leaseable.
    Unlocking an ACTIVE job would strand it forever (lease_next_job only picks
    QUEUED/RETRY_WAIT and recover_stale_jobs only looks at locked jobs), so if a
    caller ever reaches here with an ACTIVE status -- e.g. a crash path that
    failed to record a final state -- the lease is kept and recover_stale_jobs
    reclaims the job once the heartbeat goes stale.

    When a lease_token is passed, the release is additionally guarded on it: a
    worker whose lease was reclaimed (token rotated) cannot release the new
    owner's lease."""
    if JobStatus(job.status) in ACTIVE_STAGE_STATUSES:
        session.commit()
        return
    if lease_token is None:
        job.locked_by = None
        job.lease_token = None
        session.commit()
        return
    result = session.execute(
        update(Job)
        .where(Job.id == job.id, Job.lease_token == lease_token)
        .values(locked_by=None, lease_token=None),
    )
    session.commit()
    if result.rowcount == 1:
        # Mirror the DB change on the in-memory object (without expiring it) so
        # callers can keep using the instance after the session closes.
        job.locked_by = None
        job.lease_token = None


def find_orphaned_active_jobs(session) -> list[str]:
    """Forbidden-state detector: returns ids of jobs persisted in an ACTIVE
    stage status with no lease owner. Outside of a transaction in flight, this
    must always be empty -- such a job can never be leased or recovered."""
    return list(
        session.execute(
            select(Job.id).where(
                Job.status.in_([s.value for s in ACTIVE_STAGE_STATUSES]),
                Job.locked_by.is_(None),
            ),
        ).scalars(),
    )


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
    job.lease_token = None  # rotate: the crashed worker's token can never match again
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
