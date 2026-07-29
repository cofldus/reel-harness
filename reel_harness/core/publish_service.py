from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reel_harness.core.publish_eligibility import EligibilityResult, check_publish_eligibility
from reel_harness.core.state_machine import PublicationStatus, apply_publication_transition
from reel_harness.db.models import Job, Publication, PublicationAuditEvent

PRIVACY_STATUSES = frozenset({"private", "unlisted", "public"})

# Statuses with no worker/poller actively attached, where a cancel can take
# effect immediately (mirrors core.service.JobService.request_cancel's
# `immediate` set). UPLOADING/UPLOAD_PAUSED are excluded on purpose -- an
# in-flight chunk upload is only interrupted at the worker's own next safe
# checkpoint, never yanked mid-request. UPLOAD_COMPLETED/PROCESSING ARE
# included: cancelling there is a purely local status change and never
# implies deleting anything already on the provider (see
# docs/OPERATIONS.md's cancellation policy -- remote deletion is always a
# separate, explicit action, not implemented in Phase 3A).
_IMMEDIATE_CANCEL_STATUSES = frozenset(PublicationStatus) - {
    PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED,
    PublicationStatus.UPLOADING, PublicationStatus.UPLOAD_PAUSED,
}


class PublicationNotFoundError(Exception):
    pass


class PublicationInvalidActionError(Exception):
    pass


class PublicationNotEligibleError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(f"job is not publish-eligible: {reasons}")


class PublicationService:
    """Used by both the CLI and the FastAPI layer, mirroring
    core.service.JobService's role for jobs. Every publish starts here:
    eligibility is re-verified fresh against disk/DB on every call (see
    core.publish_eligibility), never trusted from an earlier check or from
    the job's own in-memory state."""

    def __init__(self, session_factory, storage) -> None:
        self._session_factory = session_factory
        self._storage = storage

    def check_eligibility(self, job_id: str) -> EligibilityResult:
        """Read-only: used by publish-job --dry-run and the API without
        creating anything."""
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise PublicationNotFoundError(job_id)
            return check_publish_eligibility(session, job, self._storage)

    def create_publication(
        self, job_id: str, provider: str, account_reference: str,
        publisher_snapshot: dict | None = None,
        privacy_status: str = "private",
        confirm_public_upload: bool = False,
        public_upload_enabled: bool = False,
    ) -> tuple[Publication, EligibilityResult]:
        """Returns (publication, eligibility). Idempotent: a second call with
        the same (provider, account_reference, job_id, final_video_checksum)
        -- including the race where two callers hit the DB unique
        constraint at the same time -- returns the existing row rather than
        creating a duplicate upload target. Raises PublicationNotEligibleError
        (with structured reasons) before creating anything if the job fails
        the fresh eligibility re-check."""
        if privacy_status not in PRIVACY_STATUSES:
            raise PublicationInvalidActionError(
                f"unknown privacy_status {privacy_status!r} (allowed: {sorted(PRIVACY_STATUSES)})"
            )
        if privacy_status == "public" and not (confirm_public_upload and public_upload_enabled):
            raise PublicationInvalidActionError(
                "public uploads require both --confirm-public-upload and the "
                "REEL_HARNESS_ALLOW_PUBLIC_UPLOAD feature flag -- private is always available "
                "without extra confirmation"
            )

        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise PublicationNotFoundError(job_id)

            eligibility = check_publish_eligibility(session, job, self._storage)
            if not eligibility.eligible:
                raise PublicationNotEligibleError(eligibility.reasons)
            assert eligibility.final_video_checksum is not None  # guaranteed by eligible=True

            existing = self._find_existing(
                session, provider, account_reference, job_id, eligibility.final_video_checksum,
            )
            if existing is not None:
                session.expunge(existing)
                return existing, eligibility

            idempotency_key = (
                f"{provider}:{account_reference}:{job_id}:{eligibility.final_video_checksum}"
            )
            publication = Publication(
                job_id=job_id, provider=provider, account_reference=account_reference,
                privacy_status=privacy_status, idempotency_key=idempotency_key,
                final_video_checksum=eligibility.final_video_checksum,
                publisher_config=dict(publisher_snapshot) if publisher_snapshot else None,
            )
            session.add(publication)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = self._find_existing(
                    session, provider, account_reference, job_id, eligibility.final_video_checksum,
                )
                assert existing is not None  # the constraint that just fired guarantees this
                session.expunge(existing)
                return existing, eligibility

            apply_publication_transition(publication, PublicationStatus.ELIGIBILITY_CHECKING)
            session.add(PublicationAuditEvent(
                publication_id=publication.id, event="eligibility_checked", detail={"eligible": True},
            ))
            apply_publication_transition(publication, PublicationStatus.READY_TO_UPLOAD)
            session.add(PublicationAuditEvent(
                publication_id=publication.id, event="publication_created",
                detail={"provider": provider, "privacy_status": privacy_status},
            ))
            session.commit()
            session.refresh(publication)
            session.expunge(publication)
            return publication, eligibility

    def get_publication(self, publication_id: str) -> Publication:
        with self._session_factory() as session:
            pub = self._require(session, publication_id)
            session.expunge(pub)
            return pub

    def list_publications(
        self, job_id: str | None = None, status: str | None = None, *,
        provider: str | None = None, account_reference: str | None = None,
        statuses: list[str] | None = None,
        created_after: object = None, created_before: object = None,
    ) -> list[Publication]:
        """`status` filters to exactly one status; `statuses` (used by e.g.
        `publication-list --failed-only`) filters to any of a set -- passing
        both is redundant, `status` wins if both are given."""
        with self._session_factory() as session:
            stmt = select(Publication)
            if job_id:
                stmt = stmt.where(Publication.job_id == job_id)
            if status:
                stmt = stmt.where(Publication.status == status)
            elif statuses:
                stmt = stmt.where(Publication.status.in_(statuses))
            if provider:
                stmt = stmt.where(Publication.provider == provider)
            if account_reference:
                stmt = stmt.where(Publication.account_reference == account_reference)
            if created_after is not None:
                stmt = stmt.where(Publication.created_at >= created_after)
            if created_before is not None:
                stmt = stmt.where(Publication.created_at <= created_before)
            rows = list(session.execute(stmt.order_by(Publication.created_at.desc())).scalars().all())
            for row in rows:
                session.expunge(row)
            return rows

    def cancel_publication(self, publication_id: str) -> Publication:
        """Cancels a publication. States with no active upload in flight
        transition to CANCELLED immediately (including UPLOAD_COMPLETED/
        PROCESSING, as pure local bookkeeping -- see the module docstring
        above); UPLOADING/UPLOAD_PAUSED only get cancel_requested set, honored
        by the worker at its next chunk boundary. PUBLISHED/CANCELLED refuse."""
        with self._session_factory() as session:
            pub = self._require(session, publication_id)
            current = PublicationStatus(pub.status)
            if current in (PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED):
                raise PublicationInvalidActionError(
                    f"cannot cancel a publication in terminal status {pub.status}"
                )
            pub.cancel_requested = True
            if current in _IMMEDIATE_CANCEL_STATUSES and pub.locked_by is None:
                apply_publication_transition(pub, PublicationStatus.CANCELLED)
            session.add(PublicationAuditEvent(
                publication_id=pub.id, event="publication_cancelled",
                detail={"previous_status": current.value, "immediate": pub.status == "CANCELLED"},
            ))
            session.commit()
            session.refresh(pub)
            session.expunge(pub)
            return pub

    @staticmethod
    def _find_existing(
        session, provider: str, account_reference: str, job_id: str, checksum: str,
    ) -> Publication | None:
        return session.execute(
            select(Publication).where(
                Publication.provider == provider, Publication.account_reference == account_reference,
                Publication.job_id == job_id, Publication.final_video_checksum == checksum,
            ),
        ).scalar_one_or_none()

    @staticmethod
    def _require(session, publication_id: str) -> Publication:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise PublicationNotFoundError(publication_id)
        return pub
