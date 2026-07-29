from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    ProviderNotConfiguredError,
    PublisherAppReviewRequiredError,
    PublisherCreatorNotEligibleError,
    PublisherPrivacyNotAllowedError,
    TransientProviderError,
    UploadSessionExpiredError,
)
from reel_harness.core.state_machine import PublicationStatus, apply_publication_transition
from reel_harness.db.models import Publication, PublicationAuditEvent
from reel_harness.observability import redact
from reel_harness.providers.base import UploadSessionHandle
from reel_harness.worker.publish_lease import lease_publication_for_reconcile
from reel_harness.worker.publish_runner import _DEFAULT_CHUNK_SIZE

# Every outcome publication-reconcile can report. Deliberately conservative:
# any state this module cannot positively confirm lands in
# manual_review_required or ambiguous_remote_state rather than guessing --
# see the module docstring below and docs/PUBLISHING.md.
#
# This vocabulary is intentionally SHARED across every provider rather than
# forked per-provider (YouTube and TikTok both use it) -- the framework
# reuse this project favors over parallel per-provider reconciliation
# modules. TikTok-flavored concepts from docs/PUBLISHING.md map onto these
# existing names rather than duplicating them under new literals:
# "upload_initialized"/"publish_submitted"/"upload_complete_not_published"
# -> already_consistent (the granular remote status -- e.g.
# processing_status=PROCESSING_UPLOAD -- is already in `reasons`);
# "remote_post_found"/"remote_post_missing" -> recovered_remote_video/
# remote_video_missing; "session_expired" -> upload_session_expired (which
# TikTok publications always reach, since TikTokPublisher.query_upload_offset
# always raises it -- see providers.tiktok_publisher). app_review_required
# is the one genuinely new outcome TikTok needed: proactively surfacing an
# unaudited-app block on a stuck, not-yet-uploaded publication, which no
# other provider's reconciliation needed before.
RECONCILE_OUTCOMES = frozenset({
    "already_consistent",
    "recovered_remote_video",
    "upload_incomplete",
    "upload_session_expired",
    "remote_video_missing",
    "credentials_unavailable",
    "manual_review_required",
    "ambiguous_remote_state",
    "app_review_required",
})

_TERMINAL = frozenset({PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED})


@dataclass
class ReconciliationResult:
    outcome: str
    publication_id: str
    reasons: list[str] = field(default_factory=list)
    provider_video_id: str | None = None

    def __post_init__(self) -> None:
        assert self.outcome in RECONCILE_OUTCOMES, f"unknown reconcile outcome: {self.outcome!r}"

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome, "publication_id": self.publication_id,
            "reasons": list(self.reasons), "provider_video_id": self.provider_video_id,
        }


def _now() -> datetime:
    return datetime.now(UTC)


def _result(outcome: str, publication_id: str, reasons: list[str], provider_video_id: str | None = None):
    return ReconciliationResult(
        outcome=outcome, publication_id=publication_id, reasons=reasons, provider_video_id=provider_video_id,
    )


def _chunk_size_for(publication: Publication) -> int:
    config = publication.publisher_config or {}
    size = config.get("youtube_chunk_size") or config.get("tiktok_chunk_size")
    return int(size) if isinstance(size, int) and size > 0 else _DEFAULT_CHUNK_SIZE


def reconcile_publication(session, publication: Publication, bundle) -> ReconciliationResult:
    """Determines whether a publication's local DB state actually matches
    reality at the provider, and repairs it ONLY when that can be confirmed
    via a read-only provider call -- never by guessing, and never by
    starting a new upload itself (that remains publication-retry's job,
    once this call reports it's safe to).

    The core problem this closes: a real upload can succeed at the
    provider (a provider_video_id is minted) in the same instant a worker
    process crashes, before the DB transaction that would record
    provider_video_id and advance the status ever commits. Without this
    check, the next attempt could re-upload the same video as a duplicate.
    The durable journal (publisher.journal, written before that same DB
    commit -- see worker.publish_runner._upload_stage) is the primary
    defense; this function is what actually reads it back and confirms it
    against the provider before trusting it.
    """
    pub_id = publication.id
    status = PublicationStatus(publication.status)

    if status in _TERMINAL:
        return _result("already_consistent", pub_id, ["terminal status, nothing to reconcile"],
                       publication.provider_video_id)

    if publication.provider_video_id:
        # A video id is already on record -- regardless of exact status,
        # confirm it still resolves before trusting it further.
        try:
            remote = bundle.publisher.get_processing_status(publication.provider_video_id)
        except (ProviderAuthError, ProviderNotConfiguredError) as exc:
            return _result("credentials_unavailable", pub_id,
                           [f"cannot confirm existing provider_video_id: {(redact(str(exc)) or '')[:200]}"])
        except TransientProviderError:
            # A PREVIOUSLY confirmed video id no longer resolving is a
            # concrete, actionable finding, not mere uncertainty.
            return _result(
                "remote_video_missing", pub_id,
                [f"provider_video_id={publication.provider_video_id} no longer resolves at the provider"],
                publication.provider_video_id,
            )
        return _result("already_consistent", pub_id,
                       [f"confirmed remotely: processing_status={remote.processing_status}"],
                       publication.provider_video_id)

    if not lease_publication_for_reconcile(session, pub_id, worker_id=f"reconcile-{pub_id}"):
        return _result("manual_review_required", pub_id,
                       ["publication is currently locked by an active worker -- try again once it releases"])

    try:
        return _reconcile_unlocked_no_known_video(session, publication, bundle)
    finally:
        # Reconciliation never keeps a publication leased for a subsequent
        # worker step -- it always hands the row back, regardless of the
        # status it ends up in (unlike release_publication_lease, which
        # deliberately keeps an ACTIVE-status job's lease for the worker
        # still driving it -- reconcile is never that worker).
        publication.locked_by = None
        publication.lease_token = None
        session.commit()


def _reconcile_unlocked_no_known_video(session, publication: Publication, bundle) -> ReconciliationResult:
    pub_id = publication.id
    journal = bundle.journal
    events = journal.read_events(pub_id)
    completed = [e for e in events if e.get("event") == "upload_completed" and e.get("provider_video_id")]

    if completed:
        candidate = completed[-1]["provider_video_id"]
        try:
            remote = bundle.publisher.get_processing_status(candidate)
        except (ProviderAuthError, ProviderNotConfiguredError) as exc:
            return _result("credentials_unavailable", pub_id,
                           [f"cannot confirm journal-recovered video id: {(redact(str(exc)) or '')[:200]}"])
        except TransientProviderError:
            return _result("manual_review_required", pub_id, [
                f"durable journal recorded provider_video_id={candidate} for this publication, but it could not "
                "be confirmed remotely -- a human should check the account's uploads before proceeding",
            ])
        return _repair_from_confirmed_video(session, publication, candidate, remote)

    real_uri = bundle.session_store.get(pub_id)
    total_bytes = publication.total_bytes
    if real_uri is None or total_bytes is None:
        if publication.provider == "tiktok":
            app_review_result = _check_tiktok_app_review_block(publication, bundle)
            if app_review_result is not None:
                return app_review_result
        return _result("upload_incomplete", pub_id,
                       ["no resumable session reference on record locally -- safe to start a fresh session"])

    handle = UploadSessionHandle(session_reference=real_uri, total_bytes=total_bytes,
                                  chunk_size=_chunk_size_for(publication))
    try:
        offset = bundle.publisher.query_upload_offset(handle, total_bytes)
    except UploadSessionExpiredError:
        bundle.session_store.delete(pub_id)
        return _result("upload_session_expired", pub_id,
                       ["the provider reports this resumable session as no longer valid; "
                        "a new session will be created on the next retry"])
    except (ProviderAuthError, ProviderNotConfiguredError) as exc:
        return _result("credentials_unavailable", pub_id, [(redact(str(exc)) or "")[:200]])
    except TransientProviderError as exc:
        detail = (redact(str(exc)) or "")[:200]
        return _result("manual_review_required", pub_id,
                       [f"transient error querying the provider's upload offset: {detail}"])

    if offset is None:
        # The provider believes the file is fully received, but there is
        # neither a durable-journal record nor a known provider_video_id --
        # genuinely ambiguous. Never auto-start a new upload here: per
        # policy that could create a duplicate if the provider is right.
        return _result("ambiguous_remote_state", pub_id, [
            "the provider reports this upload session as already complete, but no provider_video_id is known "
            "locally and the durable journal has no matching record -- confirm via the channel's own uploads "
            "(YouTube Studio, or channels.list -> uploads playlist -> playlistItems.list; see docs/PUBLISHING.md) "
            "before retrying",
        ])
    if offset >= total_bytes:
        return _result(
            "ambiguous_remote_state", pub_id,
            ["provider-confirmed offset reached the full size but no completion response was ever seen"],
        )
    return _result("upload_incomplete", pub_id, [f"{offset}/{total_bytes} bytes confirmed by the provider"])


def _repair_from_confirmed_video(session, publication: Publication, video_id: str, remote) -> ReconciliationResult:
    publication.provider_video_id = video_id
    if publication.total_bytes is not None:
        publication.bytes_uploaded = publication.total_bytes

    if publication.status == PublicationStatus.UPLOADING.value:
        apply_publication_transition(publication, PublicationStatus.UPLOAD_COMPLETED)
    if publication.status == PublicationStatus.UPLOAD_COMPLETED.value:
        apply_publication_transition(publication, PublicationStatus.PROCESSING)

    reasons = [f"recovered provider_video_id={video_id} from the durable journal, confirmed via a read-only "
               "processing-status check"]
    if remote.processing_status == "succeeded":
        publication.publication_url = remote.publication_url or publication.publication_url
        publication.processing_completed_at = _now()
        apply_publication_transition(publication, PublicationStatus.PUBLISHED, published_at=_now())
    elif remote.processing_status in ("failed", "terminated"):
        apply_publication_transition(
            publication, PublicationStatus.FAILED, failure_code="PROCESSING_FAILED",
            failure_summary=(redact(remote.failure_reason or remote.processing_status) or "")[:500],
        )
    # else "processing": leave at PROCESSING; a later publication-refresh re-polls.

    session.add(PublicationAuditEvent(
        publication_id=publication.id, event="publication_reconciled", detail={
            "provider_video_id": video_id, "resolved_status": publication.status,
        },
    ))
    session.commit()
    return _result("recovered_remote_video", publication.id, reasons, video_id)


def _tiktok_options_from_snapshot(publication: Publication):
    from reel_harness.providers.tiktok_publisher import TikTokPostOptions

    snapshot = publication.metadata_snapshot or {}
    options = snapshot.get("platform_options") or {}
    return TikTokPostOptions(
        disable_comment=bool(options.get("disable_comment", True)),
        disable_duet=bool(options.get("disable_duet", True)),
        disable_stitch=bool(options.get("disable_stitch", True)),
        video_cover_timestamp_ms=int(options.get("video_cover_timestamp_ms", 0)),
        is_branded_content=bool(options.get("brand_content_toggle", False)),
        is_own_brand_content=bool(options.get("brand_organic_toggle", False)),
        is_ai_generated=bool(options.get("is_aigc", False)),
    )


def _check_tiktok_app_review_block(publication: Publication, bundle) -> ReconciliationResult | None:
    """A TikTok publication that never even created an upload session is
    often stuck for a reason a plain 'upload_incomplete' doesn't explain:
    the app hasn't passed TikTok's review yet, so every attempt at the
    publication's configured (non-SELF_ONLY) privacy_status will keep
    failing validation before any network upload even starts. This is a
    read-only, proactive check (get_creator_info is always safe -- never
    mutates anything) -- returns None (defer to the generic
    upload_incomplete outcome) when nothing app-review-related explains
    the stall, so it never masks a genuinely ordinary "just hasn't been
    retried yet" case."""
    from reel_harness.providers.tiktok_publisher import validate_publish_options

    pub_id = publication.id
    try:
        creator_info = bundle.publisher.get_creator_info()
    except (ProviderAuthError, ProviderNotConfiguredError) as exc:
        return _result("credentials_unavailable", pub_id,
                       [f"cannot check creator_info before reconciling: {(redact(str(exc)) or '')[:200]}"])
    except TransientProviderError:
        return None  # a transient creator_info hiccup should never block reconciliation itself

    if creator_info is None:
        return None
    options = _tiktok_options_from_snapshot(publication)
    try:
        validate_publish_options(creator_info, publication.privacy_status, options)
    except PublisherAppReviewRequiredError as exc:
        return _result("app_review_required", pub_id, [(redact(str(exc)) or "")[:300]])
    except (PublisherPrivacyNotAllowedError, MetadataInvalidError, PublisherCreatorNotEligibleError) as exc:
        return _result("manual_review_required", pub_id,
                       [f"this publication's configured options are no longer valid for the account: "
                        f"{(redact(str(exc)) or '')[:300]}"])
    return None
