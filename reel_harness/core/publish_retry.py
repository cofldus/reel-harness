from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from reel_harness.core.publish_eligibility import check_publish_eligibility
from reel_harness.core.state_machine import (
    PUBLICATION_ACTIVE_STATUSES,
    PublicationStatus,
    apply_publication_transition,
)
from reel_harness.db.models import Job, Publication, PublicationAuditEvent
from reel_harness.pipeline.publish_metadata import build_publication_metadata, metadata_fingerprint

# The only stage names publication-retry accepts for --from-stage; map onto
# the concrete status the publication is set to resume from. "UPLOAD" always
# means "resume/restart the resumable session" (worker.publish_runner's own
# offset-check decides whether that resumes mid-file or starts over), never
# a guarantee of byte-level resume by itself.
RETRY_STAGES = frozenset({"SESSION", "UPLOAD", "PROCESSING"})

_STAGE_TO_STATUS = {
    "SESSION": PublicationStatus.READY_TO_UPLOAD,
    "UPLOAD": PublicationStatus.UPLOADING,
    "PROCESSING": PublicationStatus.PROCESSING,
}

# Only these statuses ever accept a retry. PUBLISHED/CANCELLED/REVIEW_REQUIRED
# are refused with a specific reason each; any PUBLICATION_ACTIVE_STATUSES
# member is refused too -- an active-looking publication needs
# publication-reconcile first (it might just be a crashed worker that
# hasn't been reclaimed yet), never a blind retry.
_RETRYABLE_STATUSES = frozenset({
    PublicationStatus.FAILED, PublicationStatus.AUTH_REQUIRED,
    PublicationStatus.QUOTA_BLOCKED, PublicationStatus.RETRY_WAIT,
})


class PublicationRetryError(Exception):
    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__("; ".join(reasons))


@dataclass
class RetryResult:
    publication_id: str
    resumed_from_status: str
    target_status: str

    def to_dict(self) -> dict:
        return {
            "publication_id": self.publication_id, "resumed_from_status": self.resumed_from_status,
            "target_status": self.target_status,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _auto_target(publication: Publication) -> PublicationStatus:
    """Picks the least-wasteful safe resume point when --from-stage is not
    given: if the provider already confirmed a video, only processing needs
    retrying; if a session was at least created, resume the upload (the
    worker's own offset check decides how much of it, if any, needs
    re-sending); otherwise start from scratch."""
    if publication.provider_video_id:
        return PublicationStatus.PROCESSING
    if publication.upload_session_reference:
        return PublicationStatus.UPLOADING
    return PublicationStatus.READY_TO_UPLOAD


def retry_publication(session, publication: Publication, storage, from_stage: str | None = None) -> RetryResult:
    """Manually retries a stuck publication (FAILED / AUTH_REQUIRED /
    QUOTA_BLOCKED / RETRY_WAIT). Never uploads anything itself -- it only
    repositions the publication so the next publisher-run/-run-once cycle
    picks it up; the actual upload/resume/processing-poll logic is entirely
    worker.publish_runner's, unchanged.

    AUTH_REQUIRED and QUOTA_BLOCKED are allowed to retry immediately on the
    operator's say-so rather than this function verifying the credential is
    now valid or the quota window has passed -- neither can be confirmed
    without a real network call, which retrying itself is not supposed to
    make. If the underlying problem isn't actually fixed yet, the very next
    attempt lands right back in the same status; this is self-correcting
    and never risks a duplicate upload, just an occasionally wasted cycle.

    Before allowing ANY retry, re-verifies: (1) the job is still publish-
    eligible (approval/manifest/asset-license state may have drifted since
    this publication was created), and (2) if a metadata fingerprint was
    already recorded, that recomputing it from the current manifest/config
    still matches -- if either check fails, the retry is refused and a new
    publication should be created instead of resuming this one.
    """
    current = PublicationStatus(publication.status)

    if current == PublicationStatus.PUBLISHED:
        raise PublicationRetryError(["publication is already PUBLISHED"])
    if current == PublicationStatus.CANCELLED:
        raise PublicationRetryError(["publication is CANCELLED -- create a new publication instead"])
    if current == PublicationStatus.REVIEW_REQUIRED:
        raise PublicationRetryError([
            "publication is REVIEW_REQUIRED -- fix the underlying eligibility issue and create a new publication",
        ])
    if current in PUBLICATION_ACTIVE_STATUSES:
        raise PublicationRetryError([
            f"publication is {current.value} (still active) -- run publication-reconcile first to confirm its "
            "real state before retrying",
        ])
    if current not in _RETRYABLE_STATUSES:
        raise PublicationRetryError([f"publication status {current.value} is not retryable"])

    if from_stage is not None:
        if from_stage not in RETRY_STAGES:
            raise PublicationRetryError([f"unknown --from-stage {from_stage!r} (allowed: {sorted(RETRY_STAGES)})"])
        target = _STAGE_TO_STATUS[from_stage]
        if target == PublicationStatus.PROCESSING and not publication.provider_video_id:
            raise PublicationRetryError(["--from-stage PROCESSING requires an already-known provider_video_id"])
    else:
        target = _auto_target(publication)

    job = session.get(Job, publication.job_id)
    eligibility = check_publish_eligibility(session, job, storage)
    if not eligibility.eligible:
        raise PublicationRetryError(eligibility.reasons)

    if publication.metadata_fingerprint and eligibility.manifest is not None:
        config = publication.publisher_config or {}
        metadata = build_publication_metadata(
            eligibility.manifest, privacy_status=publication.privacy_status,
            category_id=config.get("youtube_category_id", "22"),
            made_for_kids=bool(config.get("youtube_made_for_kids", False)),
        )
        assert eligibility.final_video_checksum is not None  # guaranteed by eligible=True
        fresh_fingerprint = metadata_fingerprint(
            publication.provider, publication.account_reference, publication.job_id,
            eligibility.final_video_checksum, metadata,
        )
        if fresh_fingerprint != publication.metadata_fingerprint:
            raise PublicationRetryError([
                "the metadata that would be sent no longer matches what this publication originally recorded -- "
                "refusing to retry; create a new publication instead",
            ])

    if current == PublicationStatus.RETRY_WAIT:
        # Already waiting on a timer -- bring it forward to now instead of
        # tearing down and recreating the wait.
        publication.next_retry_at = _now()
        if from_stage is not None:
            publication.retry_target_status = target.value
        assert publication.retry_target_status is not None  # required field of the RETRY_WAIT status itself
        resolved_target = publication.retry_target_status
    else:
        apply_publication_transition(
            publication, PublicationStatus.RETRY_WAIT,
            retry_target_status=target.value, next_retry_at=_now(),
            failure_code=publication.failure_code or "MANUAL_RETRY",
            failure_summary=publication.failure_summary or "manual retry requested",
        )
        resolved_target = target.value

    session.add(PublicationAuditEvent(
        publication_id=publication.id, event="publication_retried",
        detail={"from_status": current.value, "target_status": resolved_target, "from_stage": from_stage},
    ))
    session.commit()
    return RetryResult(publication.id, current.value, resolved_target)
