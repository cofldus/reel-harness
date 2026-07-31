"""Fable shot lease/fencing (worker.fable_lease) -- the third lease
module, held to the same standard as worker.lease's and
worker.publish_lease's tests: guarded-UPDATE claims, token fencing,
ACTIVE-shots-keep-their-lease, crash recovery through the state machine,
and the GENERATING -> TAKE_REVIEW project advance."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reel_harness.core.fable_service import FableService
from reel_harness.db.cinematic_models import FableShot
from reel_harness.worker.fable_lease import (
    assert_shot_lease,
    find_orphaned_active_shots,
    lease_next_shot,
    maybe_advance_project_after_generation,
    recover_stale_shots,
    release_shot_lease,
)


def _ready_project(session_factory):
    """A project with 4 READY shots, walked through the real gates."""
    fable = FableService(session_factory)
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="lease-test")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)
    return project


def test_lease_next_shot_claims_ready_and_mints_token(session_factory) -> None:
    project = _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-a")
        assert shot is not None
        assert shot.locked_by == "fable-a"
        assert shot.lease_token is not None
        assert shot.heartbeat_at is not None
    assert project.id  # fixture sanity


def test_second_worker_cannot_lease_a_locked_shot_twice(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        first = lease_next_shot(session, worker_id="fable-a")
        second = lease_next_shot(session, worker_id="fable-b")
        # 4 shots exist, so worker B gets a DIFFERENT shot, never A's.
        assert first is not None and second is not None
        assert first.id != second.id


def test_assert_shot_lease_fences_a_rotated_token(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-a")
        original_token = shot.lease_token
        # Simulate takeover: token rotated by recovery.
        shot.lease_token = "rotated-by-recovery"
        session.commit()
        assert assert_shot_lease(session, shot.id, original_token) is False
        assert assert_shot_lease(session, shot.id, "rotated-by-recovery") is True
        assert assert_shot_lease(session, shot.id, None) is True  # unfenced test invocation


def test_release_keeps_lease_while_shot_is_active(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-a")
        token = shot.lease_token
        shot.status = "GENERATING"  # ACTIVE
        session.commit()
        release_shot_lease(session, shot, lease_token=token)
        refreshed = session.get(FableShot, shot.id)
        assert refreshed.locked_by == "fable-a"  # invariant: ACTIVE keeps its owner


def test_release_clears_lease_for_non_active_shot(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-a")
        token = shot.lease_token
        shot.status = "REVIEW_REQUIRED"
        session.commit()
        release_shot_lease(session, shot, lease_token=token)
        refreshed = session.get(FableShot, shot.id)
        assert refreshed.locked_by is None
        assert refreshed.lease_token is None


def test_recover_stale_shots_requeues_within_crash_budget(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-crashed")
        shot.status = "GENERATING"
        shot.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()
        shot_id = shot.id

    with session_factory() as session:
        recovered = recover_stale_shots(session, lease_timeout_seconds=300)
        assert shot_id in recovered
        refreshed = session.get(FableShot, shot_id)
        assert refreshed.status == "READY"  # re-queued, crash budget not exhausted
        assert refreshed.locked_by is None
        assert refreshed.retry_count == 1
        assert refreshed.failure_code == "WORKER_CRASHED"


def test_recover_stale_shots_fails_after_budget_exhausted(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-crashed")
        shot.status = "GENERATING"
        shot.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
        shot.retry_count = 2  # budget already spent
        session.commit()
        shot_id = shot.id

    with session_factory() as session:
        recover_stale_shots(session, lease_timeout_seconds=300)
        refreshed = session.get(FableShot, shot_id)
        assert refreshed.status == "FAILED"
        assert refreshed.failure_code == "WORKER_CRASHED"


def test_healthy_heartbeat_prevents_recovery(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-healthy")
        shot.status = "GENERATING"
        shot.heartbeat_at = datetime.now(UTC)
        session.commit()

    with session_factory() as session:
        assert recover_stale_shots(session, lease_timeout_seconds=300) == []


def test_find_orphaned_active_shots_detects_forbidden_state(session_factory) -> None:
    _ready_project(session_factory)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="fable-a")
        shot.status = "GENERATING"
        shot.locked_by = None  # forbidden: ACTIVE with no owner
        session.commit()
        assert shot.id in find_orphaned_active_shots(session)


def test_project_advances_to_take_review_only_when_no_shot_is_pending(session_factory) -> None:
    project = _ready_project(session_factory)
    with session_factory() as session:
        # Shots are all READY -- generation not done, no advance.
        assert maybe_advance_project_after_generation(session, project.id) is False

    fable = FableService(session_factory)
    with session_factory() as session:
        for shot in fable.project_shots(project.id):
            db_shot = session.get(FableShot, shot.id)
            db_shot.status = "REVIEW_REQUIRED"
        session.commit()

    with session_factory() as session:
        assert maybe_advance_project_after_generation(session, project.id) is True
        from reel_harness.db.cinematic_models import StoryProject

        assert session.get(StoryProject, project.id).status == "TAKE_REVIEW"
        # Idempotent: a second call on an already-advanced project is a no-op.
        assert maybe_advance_project_after_generation(session, project.id) is False
