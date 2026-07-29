from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from typing import Any

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherPermissionDeniedError,
    PublisherQuotaExceededError,
    PublisherRateLimitedError,
    TransientProviderError,
    UploadRejectedError,
    UploadSessionExpiredError,
)
from reel_harness.providers.base import (
    CreatorInfo,
    ProcessingStatusResult,
    PublicationMetadata,
    PublisherCapabilities,
    UploadChunkResult,
    UploadSessionHandle,
)

# Per the official resumable upload guide and videos.insert reference
# (checked 2026-07-28 -- see docs/PUBLISHING.md).
ADAPTER_VERSION = "youtube-data-api-v3-v1"
UPLOAD_ENDPOINT = "https://www.googleapis.com/upload/youtube/v3/videos"
VIDEOS_ENDPOINT = "https://www.googleapis.com/youtube/v3/videos"
CHUNK_GRANULARITY_BYTES = 262144  # 256 KiB -- every chunk but the last must be a multiple of this

# YouTube has no comment/duet/stitch-style controls in this API, no
# upload-only (inbox-draft) mode, and no scheduled publish in this
# adapter -- see docs/PUBLISHING.md's Phase 3C capability model.
CAPABILITIES = PublisherCapabilities(
    supports_direct_publish=True,
    supports_upload_only=False,
    supports_scheduled_publish=False,
    supports_public_privacy=True,
    supports_unlisted_privacy=True,
    supports_comments_control=False,
    supports_remix_control=False,
    supports_processing_poll=True,
    supports_remote_delete=False,
    requires_creator_info=False,
    requires_user_confirmation=False,
    privacy_values=frozenset({"private", "unlisted", "public"}),
    default_privacy="private",
    public_privacy_values=frozenset({"public"}),
)


class YouTubePublisher:
    """Adapter for the YouTube Data API v3's resumable upload protocol
    (https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol,
    checked 2026-07-28 -- see docs/PUBLISHING.md for the full research this
    is built from). Plain httpx/REST, no Google client SDK: the protocol is
    a documented HTTP contract with no YouTube-specific transport
    requirement, the same reasoning behind every other adapter in this
    project.

    `access_token_provider` is called fresh before every request rather than
    the token being passed once at construction: a resumable upload session
    for a large file can outlive a single OAuth access token's ~1 hour
    lifetime, so the caller (worker.publish_runner) is expected to refresh
    via the CredentialBackend and hand back a live token on each call.

    UploadSessionHandle.session_reference IS the real resumable-session URI
    (a bearer-style capability URL) for the lifetime of this adapter
    instance/process -- it is never returned to a caller that would persist
    it verbatim. worker.publish_runner is responsible for translating it
    through publisher.session_store.UploadSessionStore into the opaque
    reference actually written to Publication.upload_session_reference; see
    docs/PUBLISHING.md.
    """

    provider_id = "youtube"
    capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        chunk_size: int,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        transport: Any = None,
    ) -> None:
        self.validate_configuration_static(chunk_size)
        import httpx

        self._access_token_provider = access_token_provider
        self._chunk_size = chunk_size
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=60.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def validate_configuration_static(chunk_size: int) -> None:
        if chunk_size <= 0 or chunk_size % CHUNK_GRANULARITY_BYTES != 0:
            raise ValueError(
                f"youtube upload chunk size must be a positive multiple of "
                f"{CHUNK_GRANULARITY_BYTES} bytes (256 KiB), got {chunk_size}"
            )

    def validate_configuration(self) -> None:
        self.validate_configuration_static(self._chunk_size)

    def get_creator_info(self) -> CreatorInfo | None:
        return None

    # -- session creation -------------------------------------------------

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle:
        body = _video_resource(metadata)
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Length": str(total_bytes),
            "X-Upload-Content-Type": mime_type,
            "X-Request-ID": correlation_id,
        }
        response = self._request_with_retry(
            "POST", UPLOAD_ENDPOINT, params={"uploadType": "resumable", "part": "snippet,status"},
            json=body, headers=headers,
        )
        if response.status_code != 200:
            _raise_for_status(response, context="session creation")
        location = response.headers.get("location")
        if not location:
            raise TransientProviderError(
                "resumable session creation succeeded but no Location header was returned"
            )
        return UploadSessionHandle(
            session_reference=location, total_bytes=total_bytes, chunk_size=self._chunk_size,
        )

    # -- chunk upload / resume ---------------------------------------------

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult:
        end_byte = start_byte + len(chunk) - 1
        headers = {
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start_byte}-{end_byte}/{total_bytes}",
        }
        try:
            response = self._client.put(session.session_reference, content=chunk, headers=headers)
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"chunk upload timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"chunk upload transport error ({type(exc).__name__})") from exc

        request_id = response.headers.get("x-guploader-uploadid")
        if response.status_code == 308:
            confirmed = _parse_range_upper_bound(response.headers.get("range"))
            bytes_uploaded = confirmed + 1 if confirmed is not None else start_byte
            return UploadChunkResult(bytes_uploaded=bytes_uploaded, completed=False, request_id=request_id)
        if response.status_code in (200, 201):
            video_id = _extract_video_id(response)
            if not video_id:
                raise TransientProviderError("upload completion response did not include a video id")
            return UploadChunkResult(
                bytes_uploaded=total_bytes, completed=True, provider_video_id=video_id,
                publication_url=_watch_url(video_id), request_id=request_id,
            )
        if response.status_code == 404:
            raise UploadSessionExpiredError(f"resumable upload session no longer valid (request_id={request_id})")
        if response.status_code in (500, 502, 503, 504):
            raise TransientProviderError(f"chunk upload returned HTTP {response.status_code}, retryable")
        if response.status_code in (400, 403):
            raise UploadRejectedError(
                f"upload rejected by the provider (HTTP {response.status_code}, request_id={request_id})"
            )
        raise TransientProviderError(f"chunk upload returned unexpected HTTP {response.status_code}")

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None:
        """Per the resumable upload protocol: PUT with Content-Range:
        bytes */TOTAL and an empty body. Returns the next byte offset to
        send, or None if the provider reports the upload as already
        complete (caller should treat this as done, not resume)."""
        headers = {"Content-Length": "0", "Content-Range": f"bytes */{total_bytes}"}
        try:
            response = self._client.put(session.session_reference, content=b"", headers=headers)
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"offset query timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"offset query transport error ({type(exc).__name__})") from exc

        if response.status_code == 308:
            confirmed = _parse_range_upper_bound(response.headers.get("range"))
            return confirmed + 1 if confirmed is not None else 0
        if response.status_code in (200, 201):
            return None  # already complete
        if response.status_code == 404:
            raise UploadSessionExpiredError("resumable upload session no longer valid")
        raise TransientProviderError(f"offset query returned unexpected HTTP {response.status_code}")

    # -- processing status --------------------------------------------------

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        response = self._request_with_retry(
            "GET", VIDEOS_ENDPOINT, params={"part": "status,processingDetails", "id": provider_video_id},
        )
        if response.status_code != 200:
            _raise_for_status(response, context="processing status")
        try:
            payload = response.json()
            items = payload["items"]
        except (ValueError, KeyError) as exc:
            raise TransientProviderError("processing status response was malformed") from exc
        if not items:
            raise TransientProviderError(f"no video found for id {provider_video_id!r}")
        item = items[0]
        status = item.get("status", {})
        processing = item.get("processingDetails", {})
        upload_status = status.get("uploadStatus")
        processing_status = processing.get("processingStatus")

        if upload_status in ("failed", "rejected"):
            resolved = "failed"
            failure_reason = status.get("failureReason") or status.get("rejectionReason")
        elif processing_status == "failed":
            resolved = "failed"
            failure_reason = processing.get("processingFailureReason")
        elif processing_status in ("processing", None) and upload_status == "uploaded":
            resolved = "processing"
            failure_reason = None
        elif processing_status == "succeeded" or upload_status == "processed":
            resolved = "succeeded"
            failure_reason = None
        elif processing_status == "terminated":
            resolved = "terminated"
            failure_reason = None
        else:
            resolved = "processing"
            failure_reason = None

        return ProcessingStatusResult(
            processing_status=resolved,
            privacy_status=status.get("privacyStatus"),
            publication_url=_watch_url(provider_video_id) if resolved == "succeeded" else None,
            failure_reason=failure_reason,
            request_id=response.headers.get("x-request-id"),
        )

    # -- shared request plumbing --------------------------------------------

    def _request_with_retry(self, method: str, url: str, **kwargs: Any):
        attempts = self._max_retries + 1
        last_transient: Exception | None = None
        retry_after: float | None = None
        for attempt in range(attempts):
            if attempt > 0:
                delay = max(self._retry_backoff_seconds * attempt, retry_after or 0.0)
                retry_after = None
                time.sleep(min(delay, 30.0))
            headers = kwargs.pop("headers", {}) or {}
            headers["Authorization"] = f"Bearer {self._access_token_provider()}"
            correlation_id = headers.setdefault("X-Request-ID", str(uuid.uuid4()))
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"youtube api request timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"youtube api transport error ({type(exc).__name__})")
                continue

            if response.status_code in (401, 403):
                reason = _error_reason(response)
                if reason == "quotaExceeded":
                    raise PublisherQuotaExceededError(f"youtube api quota exceeded (request_id={correlation_id})")
                if response.status_code == 401 or reason in ("forbidden", "insufficientPermissions"):
                    if response.status_code == 401:
                        raise ProviderAuthError(
                            f"youtube api rejected the access token (request_id={correlation_id})"
                        )
                    raise PublisherPermissionDeniedError(
                        f"youtube api permission denied: {reason} (request_id={correlation_id})"
                    )
                raise PublisherPermissionDeniedError(f"youtube api returned HTTP 403: {reason}")
            if response.status_code == 429:
                raw_retry_after = response.headers.get("retry-after")
                try:
                    retry_after = max(0.0, float(raw_retry_after)) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                last_transient = PublisherRateLimitedError(
                    f"youtube api rate limited (request_id={correlation_id})"
                )
                continue
            if response.status_code >= 500:
                last_transient = TransientProviderError(f"youtube api returned HTTP {response.status_code}")
                continue
            return response

        assert last_transient is not None
        raise TransientProviderError(f"youtube api request failed after {attempts} attempts: {last_transient}")


def _video_resource(metadata: PublicationMetadata) -> dict:
    snippet: dict[str, Any] = {
        "title": metadata.title, "description": metadata.description,
        "tags": list(metadata.tags), "categoryId": metadata.category_id,
    }
    if metadata.default_language:
        snippet["defaultLanguage"] = metadata.default_language
    return {
        "snippet": snippet,
        "status": {
            "privacyStatus": metadata.privacy_status,
            "selfDeclaredMadeForKids": metadata.made_for_kids,
        },
    }


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _parse_range_upper_bound(range_header: str | None) -> int | None:
    """Parses a "bytes=0-999999" style Range header into its upper bound
    (999999). Returns None if the header is absent (nothing received yet)."""
    if not range_header or "-" not in range_header:
        return None
    try:
        return int(range_header.split("-")[-1])
    except ValueError:
        return None


def _extract_video_id(response: Any) -> str | None:
    try:
        payload = response.json()
        return payload.get("id")
    except ValueError:
        return None


def _error_reason(response: Any) -> str | None:
    """Extracts error.errors[0].reason from the standard YouTube Data API
    error JSON shape (see docs/PUBLISHING.md) -- never logs or returns the
    full body."""
    try:
        payload = response.json()
        errors = payload.get("error", {}).get("errors", [])
        return errors[0].get("reason") if errors else None
    except ValueError:
        return None


def _raise_for_status(response: Any, *, context: str) -> None:
    reason = _error_reason(response)
    if response.status_code in (400, 422):
        if reason in ("invalidVideoMetadata", "mediaBodyRequired"):
            raise MetadataInvalidError(f"{context}: invalid metadata ({reason})")
        raise TransientProviderError(f"{context} returned HTTP {response.status_code}: {reason}")
    raise TransientProviderError(f"{context} returned unexpected HTTP {response.status_code}: {reason}")
