from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from reel_harness.core.state_machine import (
    ALLOWED_PUBLICATION_TRANSITIONS,
    PUBLICATION_ACTIVE_STATUSES,
    PublicationStatus,
    apply_publication_transition,
)
from reel_harness.db.models import Publication, new_uuid

# (max_retries, backoff_seconds_by_attempt) for a publisher-worker crash mid-
# upload/processing. Mirrors worker.policy.STAGE_RETRY_POLICY's shape and
# reasoning for the render pipeline.
PUBLICATION_RETRY_POLICY: tuple[int, list[int]] = (5, [30, 60, 120, 300, 600])

# Statuses a publisher worker may lease: idle-and-ready plus any status left
# behind by a crashed worker that stale recovery has since unlocked.
_LEASABLE_STATUSES = frozenset({
    PublicationStatus.READY_TO_UPLOAD, PublicationStatus.UPLOAD_SESSION_CREATED, PublicationStatus.UPLOADING,
})


def lease_next_publication(session, worker_id: str, now: datetime | None = None) -> Publication | None:
    """Atomically claims one leasable Publication (see _LEASABLE_STATUSES) or
    one RETRY_WAIT publication whose backoff has elapsed. Mirrors
    worker.lease.lease_next_job's fencing discipline exactly: every
    successful claim mints a fresh lease_token that later writes are guarded
    on."""
    now = now or datetime.now(UTC)
    leasable_values = [s.value for s in _LEASABLE_STATUSES]
    candidate_id = session.execute(
        select(Publication.id)
        .where(
            Publication.status.in_(leasable_values)
            | ((Publication.status == PublicationStatus.RETRY_WAIT.value) & (Publication.next_retry_at <= now)),
        )
        .where(Publication.locked_by.is_(None))
        .order_by(Publication.created_at)
        .limit(1),
    ).scalar_one_or_none()
    if candidate_id is None:
        return None

    result = session.execute(
        update(Publication)
        .where(Publication.id == candidate_id, Publication.locked_by.is_(None))
        .values(locked_by=worker_id, heartbeat_at=now, lease_token=new_uuid()),
    )
    session.commit()
    if result.rowcount == 0:
        return None
    return session.get(Publication, candidate_id)


def lease_specific_publication(session, publication_id: str, worker_id: str, now: datetime | None = None) -> bool:
    """Like lease_next_publication, but targets one specific publication
    (used by `publication-refresh` and its API/CLI equivalents to poke a
    single publication out of turn) instead of picking the oldest ready one.
    Only succeeds if that publication is currently in PROCESSING and
    unlocked -- refresh is for re-polling an in-flight upload's processing
    status, not for jumping the queue on other stages. Returns whether the
    lease was acquired; on True, the row's lease_token has been rotated,
    exactly like lease_next_publication."""
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Publication)
        .where(
            Publication.id == publication_id, Publication.locked_by.is_(None),
            Publication.status == PublicationStatus.PROCESSING.value,
        )
        .values(locked_by=worker_id, heartbeat_at=now, lease_token=new_uuid()),
    )
    session.commit()
    return result.rowcount == 1


def lease_publication_for_reconcile(
    session, publication_id: str, worker_id: str, now: datetime | None = None,
) -> bool:
    """Like lease_specific_publication, but for `publication-reconcile`:
    succeeds for ANY unlocked status (reconciliation's whole purpose is
    examining publications that may be stuck in an unusual state after a
    crash, not just PROCESSING). Refuses only if the row is currently
    locked by an active worker -- reconciliation must never race a worker
    that is still genuinely making progress on the same publication."""
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Publication)
        .where(Publication.id == publication_id, Publication.locked_by.is_(None))
        .values(locked_by=worker_id, heartbeat_at=now, lease_token=new_uuid()),
    )
    session.commit()
    return result.rowcount == 1


def heartbeat_publication_lease(
    session, publication_id: str, lease_token: str, now: datetime | None = None,
) -> bool:
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Publication).where(Publication.id == publication_id, Publication.lease_token == lease_token)
        .values(heartbeat_at=now),
    )
    session.commit()
    return result.rowcount == 1


def assert_publication_lease(
    session, publication_id: str, lease_token: str | None, now: datetime | None = None,
) -> bool:
    """Fencing primitive -- see worker.lease.assert_lease for the full
    rationale (the guarded UPDATE takes SQLite's write lock, closing the
    check-then-commit gap). Does NOT commit."""
    if lease_token is None:
        return True
    now = now or datetime.now(UTC)
    result = session.execute(
        update(Publication).where(Publication.id == publication_id, Publication.lease_token == lease_token)
        .values(heartbeat_at=now),
    )
    return result.rowcount == 1


def release_publication_lease(session, publication: Publication, lease_token: str | None = None) -> None:
    """Mirrors worker.lease.release_lease: a publication left in an ACTIVE
    status keeps its lease (recover_stale_publications reclaims it once the
    heartbeat goes stale) rather than being unlocked while still "in
    flight"."""
    if PublicationStatus(publication.status) in PUBLICATION_ACTIVE_STATUSES:
        session.commit()
        return
    if lease_token is None:
        publication.locked_by = None
        publication.lease_token = None
        session.commit()
        return
    result = session.execute(
        update(Publication)
        .where(Publication.id == publication.id, Publication.lease_token == lease_token)
        .values(locked_by=None, lease_token=None),
    )
    session.commit()
    if result.rowcount == 1:
        publication.locked_by = None
        publication.lease_token = None


def find_orphaned_active_publications(session) -> list[str]:
    return list(
        session.execute(
            select(Publication.id).where(
                Publication.status.in_([s.value for s in PUBLICATION_ACTIVE_STATUSES]),
                Publication.locked_by.is_(None),
            ),
        ).scalars(),
    )


def recover_stale_publications(session, lease_timeout_seconds: int, now: datetime | None = None) -> list[str]:
    now = now or datetime.now(UTC)
    threshold = now - timedelta(seconds=lease_timeout_seconds)
    stale = session.execute(
        select(Publication).where(
            Publication.locked_by.is_not(None),
            Publication.status.in_([s.value for s in PUBLICATION_ACTIVE_STATUSES]),
        ),
    ).scalars().all()

    recovered: list[str] = []
    for pub in stale:
        if pub.heartbeat_at is not None and pub.heartbeat_at >= threshold:
            continue
        _recover_publication(pub, now)
        recovered.append(pub.id)
    session.commit()
    return recovered


def _recover_publication(pub: Publication, now: datetime) -> None:
    max_retries, backoffs = PUBLICATION_RETRY_POLICY
    current = PublicationStatus(pub.status)
    pub.locked_by = None
    pub.lease_token = None  # rotate: the crashed worker's token can never match again

    can_retry = PublicationStatus.RETRY_WAIT in ALLOWED_PUBLICATION_TRANSITIONS.get(current, set())
    if not can_retry or pub.retry_count >= max_retries:
        apply_publication_transition(
            pub, PublicationStatus.FAILED,
            failure_code="WORKER_CRASHED", failure_summary=f"publisher worker crashed during {pub.status}",
        )
        return
    delay = backoffs[min(pub.retry_count, len(backoffs) - 1)]
    pub.retry_count += 1
    apply_publication_transition(
        pub, PublicationStatus.RETRY_WAIT,
        retry_target_status=current.value,
        next_retry_at=now + timedelta(seconds=delay),
        failure_code="WORKER_CRASHED", failure_summary=f"publisher worker crashed during {pub.status}",
    )
