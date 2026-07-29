from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reel_harness.core.errors import (
    PipelineError,
    ProviderAuthError,
    PublisherQuotaExceededError,
    UploadSessionExpiredError,
)
from reel_harness.core.state_machine import (
    ALLOWED_PUBLICATION_TRANSITIONS,
    PublicationStatus,
    apply_publication_transition,
)
from reel_harness.db.models import Job, Publication, PublicationAuditEvent
from reel_harness.observability import redact
from reel_harness.pipeline.publish_metadata import build_publication_metadata, metadata_fingerprint
from reel_harness.providers.base import PublicationMetadata, Publisher, UploadSessionHandle
from reel_harness.publisher.journal import PublishJournal, safe_session_reference_hash
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_lease import PUBLICATION_RETRY_POLICY, assert_publication_lease

# Fallback chunk size if a publication's snapshot predates this field --
# must stay a multiple of 262144 (see providers.youtube_publisher).
_DEFAULT_CHUNK_SIZE = 8 * 262144


class PublicationLeaseLostSignal(Exception):
    """Internal worker outcome: this worker's lease on the publication was
    reclaimed. Mirrors worker.runner.LeaseLostSignal's role."""


class _SessionNotResumable(Exception):
    """Internal-only: the real session URI could not be recovered from the
    session store. Handled inside _upload_stage, never escapes it."""


@dataclass
class PublishBundle:
    publisher: Publisher
    session_store: UploadSessionStore
    journal: PublishJournal


def _persisted_error_text(text: object, limit: int) -> str:
    return (redact(str(text)) or "")[:limit]


def _now() -> datetime:
    return datetime.now(UTC)


def _audit(session, publication_id: str, event: str, detail: dict | None = None) -> None:
    session.add(PublicationAuditEvent(publication_id=publication_id, event=event, detail=detail))


def _chunk_size_for(publication: Publication) -> int:
    config = publication.publisher_config or {}
    size = config.get("youtube_chunk_size")
    return int(size) if isinstance(size, int) and size > 0 else _DEFAULT_CHUNK_SIZE


def run_publication(
    session, publication: Publication, storage, bundle: PublishBundle,
    channel_niche: str | None = None, lease_token: str | None = None,
) -> None:
    """Drives one leased publication forward as far as it safely can in this
    call: READY_TO_UPLOAD -> create session -> UPLOAD_SESSION_CREATED ->
    UPLOADING (chunked, resumable) -> UPLOAD_COMPLETED -> PROCESSING (initial
    poll only; a later publication-refresh call advances PROCESSING ->
    PUBLISHED once the provider finishes -- see cli publication-refresh).
    Every DB-visible mutation is fenced on lease_token exactly like
    worker.runner.run_job. Never raises (KeyboardInterrupt/SystemExit still
    propagate): a retryable PipelineError (e.g. a transient network error)
    routes to RETRY_WAIT with backoff, mirroring
    worker.runner._handle_pipeline_error; a non-retryable one or any other
    unexpected exception becomes an explicit FAILED, guaranteeing a
    publication is never left ACTIVE and unlocked.
    """
    try:
        _run_publication_impl(session, publication, storage, bundle, channel_niche, lease_token)
    except PublicationLeaseLostSignal:
        session.rollback()
    except PipelineError as error:
        _handle_pipeline_error(session, publication, error, lease_token)
    except Exception as error:  # noqa: BLE001 - safety net, see docstring
        _handle_unexpected_error(session, publication, error, lease_token)


def _run_publication_impl(
    session, publication: Publication, storage, bundle: PublishBundle,
    channel_niche: str | None, lease_token: str | None,
) -> None:
    if publication.status == PublicationStatus.RETRY_WAIT.value:
        # Resolve back to whatever status this publication was actively in
        # when it was retried (an operator retry or stale-worker recovery),
        # mirroring worker.runner._start_stage's role for RETRY_WAIT jobs --
        # the dispatch chain below only ever matches concrete active statuses.
        if not assert_publication_lease(session, publication.id, lease_token):
            raise PublicationLeaseLostSignal
        assert publication.retry_target_status is not None  # required field of the RETRY_WAIT transition
        target = PublicationStatus(publication.retry_target_status)
        extra_fields = (
            {"upload_session_reference": publication.id}
            if target in (PublicationStatus.UPLOAD_SESSION_CREATED, PublicationStatus.UPLOADING)
            else {}
        )
        apply_publication_transition(publication, target, **extra_fields)
        if target == PublicationStatus.PROCESSING and publication.processing_started_at is None:
            publication.processing_started_at = _now()
        session.commit()

    job = session.get(Job, publication.job_id)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    total_bytes = final_path.stat().st_size

    if publication.status == PublicationStatus.READY_TO_UPLOAD.value:
        _create_session_stage(session, publication, job, storage, bundle, channel_niche, total_bytes, lease_token)

    if publication.status == PublicationStatus.UPLOAD_SESSION_CREATED.value:
        if not assert_publication_lease(session, publication.id, lease_token):
            raise PublicationLeaseLostSignal
        apply_publication_transition(
            publication, PublicationStatus.UPLOADING, upload_session_reference=publication.id,
        )
        session.commit()

    if publication.status == PublicationStatus.UPLOADING.value:
        _upload_stage(session, publication, final_path, bundle, total_bytes, lease_token)
        if publication.status != PublicationStatus.UPLOAD_COMPLETED.value:
            return  # cancelled mid-upload (lease-lost already raised inside)

    if publication.status == PublicationStatus.UPLOAD_COMPLETED.value:
        if not assert_publication_lease(session, publication.id, lease_token):
            raise PublicationLeaseLostSignal
        apply_publication_transition(publication, PublicationStatus.PROCESSING)
        publication.processing_started_at = _now()
        _audit(session, publication.id, "processing_started")
        session.commit()

    if publication.status == PublicationStatus.PROCESSING.value:
        _processing_stage(session, publication, bundle, lease_token)


def _create_session_stage(
    session, publication: Publication, job, storage, bundle: PublishBundle,
    channel_niche: str | None, total_bytes: int, lease_token: str | None,
) -> None:
    manifest = _load_manifest(storage, job.id)
    config = publication.publisher_config or {}
    metadata = build_publication_metadata(
        manifest,
        privacy_status=publication.privacy_status,
        category_id=config.get("youtube_category_id", "22"),
        made_for_kids=bool(config.get("youtube_made_for_kids", False)),
        channel_niche=channel_niche,
    )
    handle = bundle.publisher.create_upload_session(metadata, total_bytes, "video/mp4", str(uuid.uuid4()))
    bundle.session_store.set(publication.id, handle.session_reference)
    fingerprint = metadata_fingerprint(
        publication.provider, publication.account_reference, publication.job_id,
        publication.final_video_checksum, metadata,
    )
    bundle.journal.append(
        publication_id=publication.id, job_id=publication.job_id, provider=publication.provider,
        account_reference=publication.account_reference, final_video_checksum=publication.final_video_checksum,
        event="upload_session_created", timestamp=_now(),
        safe_session_hash=safe_session_reference_hash(handle.session_reference),
        detail={"total_bytes": total_bytes, "metadata_fingerprint": fingerprint},
    )

    if not assert_publication_lease(session, publication.id, lease_token):
        raise PublicationLeaseLostSignal
    publication.total_bytes = total_bytes
    publication.metadata_snapshot = _metadata_to_dict(metadata)
    publication.metadata_fingerprint = fingerprint
    apply_publication_transition(
        publication, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference=publication.id,
    )
    _audit(session, publication.id, "upload_session_created", {"total_bytes": total_bytes})
    session.commit()


def _metadata_to_dict(metadata: PublicationMetadata) -> dict:
    return {
        "title": metadata.title, "description": metadata.description, "tags": metadata.tags,
        "category_id": metadata.category_id, "privacy_status": metadata.privacy_status,
        "made_for_kids": metadata.made_for_kids, "platform_options": metadata.platform_options,
    }


def _metadata_from_snapshot(publication: Publication) -> PublicationMetadata:
    snapshot = publication.metadata_snapshot or {}
    return PublicationMetadata(
        title=snapshot.get("title", "Untitled"), description=snapshot.get("description", ""),
        tags=snapshot.get("tags", []), category_id=snapshot.get("category_id", "22"),
        privacy_status=snapshot.get("privacy_status", publication.privacy_status),
        made_for_kids=bool(snapshot.get("made_for_kids", False)),
        platform_options=dict(snapshot.get("platform_options") or {}),
    )


def _resolve_session_handle(
    publication: Publication, bundle: PublishBundle, total_bytes: int,
) -> UploadSessionHandle:
    real_uri = bundle.session_store.get(publication.id)
    if real_uri is None:
        # Documented safe fallback (see docs/PUBLISHING.md): the real session
        # URI lives only in the (repository-external) session store, never
        # the DB, so if it's missing here -- different machine, secret store
        # cleared, or a crash before it was ever written -- there is no URI
        # to guess at. Starting a brand-new session is the only safe
        # recourse; it never risks reusing or leaking a stale capability URL.
        raise _SessionNotResumable
    return UploadSessionHandle(
        session_reference=real_uri, total_bytes=total_bytes, chunk_size=_chunk_size_for(publication),
    )


def _start_new_session(
    session, publication: Publication, bundle: PublishBundle, total_bytes: int,
) -> UploadSessionHandle:
    """Starts a fresh resumable session from the pinned metadata snapshot,
    from byte 0. Reached both when no prior session can be resumed at all
    (_SessionNotResumable) and when the provider reports the previous
    session as expired (UploadSessionExpiredError) -- self-healing within
    one run_publication call rather than bouncing through RETRY_WAIT for
    something this recoverable."""
    metadata = _metadata_from_snapshot(publication)
    handle = bundle.publisher.create_upload_session(metadata, total_bytes, "video/mp4", str(uuid.uuid4()))
    bundle.session_store.set(publication.id, handle.session_reference)
    bundle.journal.append(
        publication_id=publication.id, job_id=publication.job_id, provider=publication.provider,
        account_reference=publication.account_reference, final_video_checksum=publication.final_video_checksum,
        event="upload_session_created", timestamp=_now(),
        safe_session_hash=safe_session_reference_hash(handle.session_reference),
        detail={"total_bytes": total_bytes, "resumed": True},
    )
    publication.bytes_uploaded = 0
    _audit(session, publication.id, "upload_session_created", {"total_bytes": total_bytes, "resumed": True})
    return handle


def _upload_stage(
    session, publication: Publication, final_path: Path, bundle: PublishBundle,
    total_bytes: int, lease_token: str | None,
) -> None:
    handle: UploadSessionHandle | None
    try:
        handle = _resolve_session_handle(publication, bundle, total_bytes)
        offset = bundle.publisher.query_upload_offset(handle, total_bytes)
        start_byte = total_bytes if offset is None else offset
        if offset is not None and offset > 0:
            _audit(session, publication.id, "upload_resumed", {"start_byte": offset})
    except (_SessionNotResumable, UploadSessionExpiredError):
        handle = None
        start_byte = 0

    with open(final_path, "rb") as file_handle:
        while start_byte < total_bytes:
            if publication.cancel_requested:
                if not assert_publication_lease(session, publication.id, lease_token):
                    raise PublicationLeaseLostSignal
                apply_publication_transition(publication, PublicationStatus.CANCELLED)
                _audit(session, publication.id, "publication_cancelled", {"during": "UPLOADING"})
                session.commit()
                return

            if handle is None:
                handle = _start_new_session(session, publication, bundle, total_bytes)
                session.commit()

            file_handle.seek(start_byte)
            chunk = file_handle.read(handle.chunk_size)
            try:
                result = bundle.publisher.upload_chunk(handle, chunk, start_byte, total_bytes)
            except UploadSessionExpiredError:
                handle = None  # self-heal: fresh session next iteration, same start_byte
                continue
            start_byte = result.bytes_uploaded

            if not assert_publication_lease(session, publication.id, lease_token):
                raise PublicationLeaseLostSignal
            publication.bytes_uploaded = start_byte
            _audit(session, publication.id, "chunk_uploaded", {
                "bytes_uploaded": start_byte, "total_bytes": total_bytes,
            })

            if result.completed:
                # Durable journal write FIRST, before any DB mutation: this
                # is the exact instant a real crash (process killed right
                # here) could otherwise lose the fact that the provider
                # already has the finished video, causing a naive resume to
                # re-upload it as a duplicate. A journal write that returns
                # successfully is fsync'd -- see publisher.journal.
                bundle.journal.append(
                    publication_id=publication.id, job_id=publication.job_id, provider=publication.provider,
                    account_reference=publication.account_reference,
                    final_video_checksum=publication.final_video_checksum,
                    event="upload_completed", timestamp=_now(),
                    provider_video_id=result.provider_video_id, provider_request_id=result.request_id,
                )
                publication.provider_video_id = result.provider_video_id
                publication.publication_url = result.publication_url
                apply_publication_transition(publication, PublicationStatus.UPLOAD_COMPLETED)
                bundle.session_store.delete(publication.id)
                _audit(session, publication.id, "upload_completed", {
                    "provider_video_id": result.provider_video_id,
                })
                session.commit()
                return
            session.commit()  # persist progress after every chunk -- a crash resumes near where it left off


def _processing_poll_config(publication: Publication) -> tuple[float, float]:
    config = publication.publisher_config or {}
    interval = config.get("processing_poll_interval_seconds", 30.0)
    max_duration = config.get("processing_max_duration_seconds", 3600.0)
    return float(interval), float(max_duration)


def _processing_stage(session, publication: Publication, bundle: PublishBundle, lease_token: str | None) -> None:
    assert publication.provider_video_id is not None  # guaranteed by UPLOAD_COMPLETED's precondition
    poll_interval, max_duration = _processing_poll_config(publication)

    if publication.processing_started_at is not None:
        elapsed = (_now() - publication.processing_started_at).total_seconds()
        if elapsed > max_duration:
            # A local timeout, never a provider-reported failure -- no
            # request is made. The video may still finish processing on
            # YouTube's side; publication-reconcile can confirm that later.
            apply_publication_transition(
                publication, PublicationStatus.FAILED,
                failure_code="PROCESSING_TIMEOUT",
                failure_summary=f"processing exceeded the local max duration ({max_duration:.0f}s)",
            )
            _audit(session, publication.id, "publication_failed", {"reason": "PROCESSING_TIMEOUT"})
            session.commit()
            return

    status = bundle.publisher.get_processing_status(publication.provider_video_id)

    if not assert_publication_lease(session, publication.id, lease_token):
        raise PublicationLeaseLostSignal
    publication.processing_poll_count += 1

    if status.processing_status == "succeeded":
        bundle.journal.append(
            publication_id=publication.id, job_id=publication.job_id, provider=publication.provider,
            account_reference=publication.account_reference, final_video_checksum=publication.final_video_checksum,
            event="processing_completed", timestamp=_now(),
            provider_video_id=publication.provider_video_id, detail={"outcome": "succeeded"},
        )
        publication.publication_url = status.publication_url or publication.publication_url
        publication.processing_completed_at = _now()
        publication.next_poll_at = None
        apply_publication_transition(publication, PublicationStatus.PUBLISHED, published_at=_now())
        _audit(session, publication.id, "processing_completed", {"outcome": "succeeded"})
    elif status.processing_status in ("failed", "terminated"):
        publication.next_poll_at = None
        apply_publication_transition(
            publication, PublicationStatus.FAILED,
            failure_code="PROCESSING_FAILED",
            failure_summary=_persisted_error_text(status.failure_reason or status.processing_status, 500),
        )
        _audit(session, publication.id, "publication_failed", {"reason": status.failure_reason})
    else:
        # Still processing -- don't poll again until the interval elapses,
        # so a busy processing poller doesn't hammer the provider.
        publication.next_poll_at = _now() + timedelta(seconds=poll_interval)
    session.commit()


def _load_manifest(storage, job_id: str):
    from reel_harness.manifest.schema import Manifest

    raw = storage.read_bytes(job_id, "manifest.json")
    return Manifest.model_validate_json(raw)


def _handle_pipeline_error(
    session, publication: Publication, error: PipelineError, lease_token: str | None,
) -> None:
    """Mirrors worker.runner._handle_pipeline_error's retry/fail branch, plus
    two publish-specific landing states an operator can act on directly
    instead of a generic FAILED: an auth rejection lands in AUTH_REQUIRED
    (re-run publisher-auth, then retry) and a quota error lands in
    QUOTA_BLOCKED (wait for the provider's reset, then retry) -- neither
    auto-retries on its own, since retrying immediately cannot fix either
    condition. Any other retryable error backs off into RETRY_WAIT (bounded
    by PUBLICATION_RETRY_POLICY); anything else, or an error from a status
    with no RETRY_WAIT path at all, goes straight to FAILED."""
    try:
        session.rollback()
    except Exception:  # pragma: no cover - session teardown must not mask the outcome
        pass

    current = PublicationStatus(publication.status)
    summary = _persisted_error_text(error, 500)
    landing_status: PublicationStatus | None = None
    if isinstance(error, ProviderAuthError):
        landing_status = PublicationStatus.AUTH_REQUIRED
    elif isinstance(error, PublisherQuotaExceededError):
        landing_status = PublicationStatus.QUOTA_BLOCKED

    if landing_status is not None and landing_status in ALLOWED_PUBLICATION_TRANSITIONS.get(current, set()):
        apply_publication_transition(
            publication, landing_status, failure_code=error.code, failure_summary=summary,
        )
    else:
        can_retry_wait = PublicationStatus.RETRY_WAIT in ALLOWED_PUBLICATION_TRANSITIONS.get(current, set())
        max_retries, backoffs = PUBLICATION_RETRY_POLICY
        if not error.retryable or not can_retry_wait or publication.retry_count >= max_retries:
            apply_publication_transition(
                publication, PublicationStatus.FAILED, failure_code=error.code, failure_summary=summary,
            )
        else:
            delay = backoffs[min(publication.retry_count, len(backoffs) - 1)]
            publication.retry_count += 1
            apply_publication_transition(
                publication, PublicationStatus.RETRY_WAIT,
                retry_target_status=current.value, next_retry_at=_now() + timedelta(seconds=delay),
                failure_code=error.code, failure_summary=summary,
            )

    if not assert_publication_lease(session, publication.id, lease_token):
        session.rollback()
        return
    _audit(session, publication.id, "publication_failed", {"error_code": error.code, "retryable": error.retryable})
    session.commit()


def _handle_unexpected_error(
    session, publication: Publication, error: Exception, lease_token: str | None,
) -> None:
    """Last-resort safety boundary for non-PipelineError exceptions, mirroring
    worker.runner._handle_unexpected_error: guarantees a publication is never
    persisted ACTIVE and unlocked. The summary carries only the exception
    type and a short redacted message -- no traceback, no request bodies."""
    try:
        session.rollback()
    except Exception:  # pragma: no cover - session teardown must not mask the outcome
        pass

    summary = f"unexpected {type(error).__name__}: {_persisted_error_text(error, 300)}"
    current = PublicationStatus(publication.status)
    if PublicationStatus.FAILED in ALLOWED_PUBLICATION_TRANSITIONS.get(current, set()):
        apply_publication_transition(
            publication, PublicationStatus.FAILED,
            failure_code="UNEXPECTED_PUBLISH_ERROR", failure_summary=summary,
        )
    else:
        # e.g. still READY_TO_UPLOAD (leaseable again) -- record the cause
        # without forcing an invalid transition.
        publication.failure_code = "UNEXPECTED_PUBLISH_ERROR"
        publication.failure_summary = summary

    if not assert_publication_lease(session, publication.id, lease_token):
        session.rollback()
        return
    _audit(session, publication.id, "publication_failed", {"error": type(error).__name__})
    session.commit()
