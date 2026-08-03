"""Lease/fencing for Fable shots -- the third lease module, a deliberate
structural copy of worker.lease (jobs) and worker.publish_lease
(publications) rather than a generic abstraction over all three: the
entities' status vocabularies and recovery policies are intentionally
independent (see worker.lease and core.state_machine for the full
rationale, which applies here unchanged)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from reel_harness.core.cinematic_state import (
    FableProjectStatus,
    FableShotStatus,
    apply_project_transition,
    apply_shot_transition,
)
from reel_harness.db.cinematic_models import FableScene, FableShot, StoryProject
from reel_harness.db.models import new_uuid

# Statuses in which a shot MUST have a lease owner -- mirror of
# worker.policy.ACTIVE_STAGE_STATUSES for the shot machine.
ACTIVE_SHOT_STATUSES = frozenset({
    FableShotStatus.SUBMITTED, FableShotStatus.GENERATING,
    FableShotStatus.DOWNLOADING, FableShotStatus.VALIDATING,
})

# Crash-recovery retry budget for a shot (WORKER_CRASHED only -- provider
# failures are handled by the runner's own error mapping).
_SHOT_CRASH_MAX_RETRIES = 2


def lease_next_shot(session, worker_id: str, now: datetime | None = None) -> FableShot | None:
    """Atomically claims one READY shot. Guarded-UPDATE claim discipline
    identical to lease_next_job: the loser of a race sees rowcount == 0."""
    now = now or datetime.now(UTC)
    candidate_id = session.execute(
        select(FableShot.id)
        .where(FableShot.status == FableShotStatus.READY.value)
        .where(FableShot.locked_by.is_(None))
        .order_by(FableShot.created_at)
        .limit(1),
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    result = session.execute(
        update(FableShot)
        .where(FableShot.id == candidate_id, FableShot.locked_by.is_(None))
        .values(locked_by=worker_id, heartbeat_at=now, lease_token=new_uuid()),
    )
    session.commit()
    if result.rowcount == 0:
        return None
    return session.get(FableShot, candidate_id)


def assert_shot_lease(session, shot_id: str, lease_token: str | None, now: datetime | None = None) -> bool:
    """Fencing primitive -- see worker.lease.assert_lease for the full
    rationale, which applies here unchanged. Does NOT commit;
    lease_token=None (direct library/test invocation) passes."""
    if lease_token is None:
        return True
    now = now or datetime.now(UTC)
    result = session.execute(
        update(FableShot)
        .where(FableShot.id == shot_id, FableShot.lease_token == lease_token)
        .values(heartbeat_at=now),
    )
    return result.rowcount == 1


def release_shot_lease(session, shot: FableShot, lease_token: str | None = None) -> None:
    """Releases unless the shot is still ACTIVE (an ACTIVE shot must always
    have a lease owner -- stale recovery is what reclaims it, same
    invariant as release_lease). Token-guarded when a token is given."""
    if FableShotStatus(shot.status) in ACTIVE_SHOT_STATUSES:
        session.commit()
        return
    if lease_token is None:
        shot.locked_by = None
        shot.lease_token = None
        session.commit()
        return
    result = session.execute(
        update(FableShot)
        .where(FableShot.id == shot.id, FableShot.lease_token == lease_token)
        .values(locked_by=None, lease_token=None),
    )
    session.commit()
    if result.rowcount == 1:
        shot.locked_by = None
        shot.lease_token = None


def find_orphaned_active_shots(session) -> list[str]:
    """Forbidden-state detector, mirror of find_orphaned_active_jobs."""
    return list(
        session.execute(
            select(FableShot.id).where(
                FableShot.status.in_([s.value for s in ACTIVE_SHOT_STATUSES]),
                FableShot.locked_by.is_(None),
            ),
        ).scalars(),
    )


def recover_stale_shots(session, lease_timeout_seconds: int, now: datetime | None = None) -> list[str]:
    """Shots locked by a crashed worker (stale heartbeat) are failed with
    WORKER_CRASHED and -- while the crash-retry budget lasts -- immediately
    re-queued READY for the next worker. Both steps go through the shot
    state machine (ACTIVE -> FAILED -> READY are all legal edges)."""
    now = now or datetime.now(UTC)
    threshold = now - timedelta(seconds=lease_timeout_seconds)
    stale = session.execute(
        select(FableShot).where(
            FableShot.locked_by.is_not(None),
            FableShot.status.in_([s.value for s in ACTIVE_SHOT_STATUSES]),
        ),
    ).scalars().all()

    recovered: list[str] = []
    for shot in stale:
        if shot.heartbeat_at is not None and shot.heartbeat_at >= threshold:
            continue
        shot.locked_by = None
        shot.lease_token = None  # rotate: the crashed worker can never write again
        apply_shot_transition(
            shot, FableShotStatus.FAILED,
            failure_code="WORKER_CRASHED",
            failure_summary="worker crashed during shot generation",
        )
        if shot.retry_count < _SHOT_CRASH_MAX_RETRIES:
            shot.retry_count += 1
            apply_shot_transition(shot, FableShotStatus.READY)
        recovered.append(shot.id)
    session.commit()
    return recovered


def maybe_advance_project_after_generation(session, project_id: str) -> bool:
    """GENERATING -> TAKE_REVIEW once no shot remains in READY or an
    ACTIVE status (i.e. every shot is REVIEW_REQUIRED, SELECTED, REJECTED,
    or FAILED -- generation work is done and a human decision is next).
    Commits when it advances; returns whether it did."""
    project = session.get(StoryProject, project_id)
    if project is None or project.status != FableProjectStatus.GENERATING.value:
        return False
    pending_statuses = {FableShotStatus.READY.value} | {s.value for s in ACTIVE_SHOT_STATUSES}
    pending = session.execute(
        select(FableShot.id)
        .join(FableScene, FableShot.scene_id == FableScene.id)
        .where(FableScene.project_id == project_id, FableShot.status.in_(pending_statuses))
        .limit(1),
    ).scalar_one_or_none()
    if pending is not None:
        return False
    apply_project_transition(project, FableProjectStatus.TAKE_REVIEW)
    session.commit()
    return True
