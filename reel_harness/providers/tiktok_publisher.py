from __future__ import annotations

import math
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherAppReviewRequiredError,
    PublisherCreatorNotEligibleError,
    PublisherPermissionDeniedError,
    PublisherPrivacyNotAllowedError,
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

# Per the official TikTok Content Posting API docs (checked 2026-07-29 -- see
# docs/PUBLISHING.md). Only Direct Post / FILE_UPLOAD is implemented -- no
# PULL_FROM_URL (no hosting available), no upload-only/inbox-draft mode
# (video.upload scope is never requested).
ADAPTER_VERSION = "tiktok-content-posting-api-v1"

# supports_public_privacy reflects what the API itself allows, not the
# unaudited-app runtime restriction that forces SELF_ONLY regardless of what
# was requested for most apps -- that's a per-account fact learned fresh from
# creator_info on every publish attempt and surfaced explicitly as
# APP_REVIEW_REQUIRED (see validate_publish_options below), never hidden and
# never baked into this static capability.
CAPABILITIES = PublisherCapabilities(
    supports_direct_publish=True,
    supports_upload_only=False,
    supports_scheduled_publish=False,
    supports_public_privacy=True,
    supports_unlisted_privacy=False,
    supports_comments_control=True,
    supports_remix_control=True,  # duet/stitch
    supports_processing_poll=True,
    supports_remote_delete=False,
    requires_creator_info=True,
    requires_user_confirmation=True,
    privacy_values=frozenset({
        "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY",
    }),
    default_privacy="SELF_ONLY",
    public_privacy_values=frozenset({"PUBLIC_TO_EVERYONE"}),
)

# The Direct Post `title` (post caption) limit per the official docs: 2200
# UTF-16 code units (not Python code points -- an astral character is one
# code point but a surrogate pair, i.e. 2 UTF-16 units).
MAX_POST_TEXT_UTF16_UNITS = 2200

# Defense in depth only -- the post text is built exclusively from the
# manifest's own title (never from a raw provider response, a local path, a
# job id, or any credential), so these patterns should never actually match
# in practice. See build_post_text.
_FORBIDDEN_POST_TEXT_PATTERNS = (
    re.compile(r"[A-Za-z]:\\"),  # a Windows local path (C:\...)
    re.compile(r"(?<![\w-])/jobs/[0-9a-fA-F-]{8,}"),  # this project's job artifact path shape
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),  # a UUID
    re.compile(r"(?i)(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[:=]"),
    re.compile(r"(?i)x-amz-|signature=|expires=\d"),  # signed-URL query fragments
    re.compile(r"https?://127\.0\.0\.1"),
)


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def build_post_text(title: str) -> str:
    """Validates (never transforms) the post caption. Raises
    MetadataInvalidError on a length violation or a disallowed internal
    marker (job id / local path / secret / signed-URL fragment)."""
    if _utf16_len(title) > MAX_POST_TEXT_UTF16_UNITS:
        raise MetadataInvalidError(
            f"tiktok post text exceeds {MAX_POST_TEXT_UTF16_UNITS} UTF-16 code units"
        )
    for pattern in _FORBIDDEN_POST_TEXT_PATTERNS:
        if pattern.search(title):
            raise MetadataInvalidError("tiktok post text contains a disallowed internal marker")
    return title


@dataclass(frozen=True)
class TikTokPostOptions:
    """TikTok-specific Direct Post fields that don't generalize to any
    other platform -- carried through providers.base.PublicationMetadata.
    platform_options (see its docstring and docs/PUBLISHING.md), never as
    extra fields on the common dataclass. Every default is the most
    restrictive/safe choice -- comments/duet/stitch start disabled,
    every disclosure toggle starts False -- never silently enabled."""

    disable_comment: bool = True
    disable_duet: bool = True
    disable_stitch: bool = True
    video_cover_timestamp_ms: int = 0
    is_branded_content: bool = False  # brand_content_toggle -- promotes a third party's product/brand
    is_own_brand_content: bool = False  # brand_organic_toggle -- promotes the creator's own business
    is_ai_generated: bool = False  # is_aigc

    def as_platform_options(self) -> dict:
        return {
            "disable_comment": self.disable_comment,
            "disable_duet": self.disable_duet,
            "disable_stitch": self.disable_stitch,
            "video_cover_timestamp_ms": self.video_cover_timestamp_ms,
            "brand_content_toggle": self.is_branded_content,
            "brand_organic_toggle": self.is_own_brand_content,
            "is_aigc": self.is_ai_generated,
        }


def platform_options_from_dict(options: dict) -> TikTokPostOptions:
    """The inverse of TikTokPostOptions.as_platform_options -- reconstructs
    the typed dataclass from the generic platform_options dict (see
    providers.base.PublicationMetadata.platform_options). Shared by every
    caller that needs to go from the persisted/generic dict shape back to
    the typed dataclass (worker.publish_runner, core.publish_reconciliation)
    rather than each re-implementing this mapping."""
    return TikTokPostOptions(
        disable_comment=bool(options.get("disable_comment", True)),
        disable_duet=bool(options.get("disable_duet", True)),
        disable_stitch=bool(options.get("disable_stitch", True)),
        video_cover_timestamp_ms=int(options.get("video_cover_timestamp_ms", 0)),
        is_branded_content=bool(options.get("brand_content_toggle", False)),
        is_own_brand_content=bool(options.get("brand_organic_toggle", False)),
        is_ai_generated=bool(options.get("is_aigc", False)),
    )


def validate_publish_options(creator_info: CreatorInfo, privacy_status: str, options: TikTokPostOptions) -> None:
    """Called immediately before every publish attempt with a FRESHLY
    fetched CreatorInfo (never a cached/earlier one -- see
    TikTokPublisher.get_creator_info and docs/OPERATIONS.md). Rejects any
    unsupported privacy/interaction combination explicitly; never a silent
    fallback to a different option."""
    if privacy_status not in creator_info.allowed_privacy_values:
        if creator_info.allowed_privacy_values == frozenset({"SELF_ONLY"}) and privacy_status != "SELF_ONLY":
            # Matches TikTok's documented behavior for apps that have not
            # passed review: privacy_level_options only ever contains
            # SELF_ONLY. Surfaced distinctly from a plain "bad value" so the
            # operator knows an app review is what's actually needed.
            raise PublisherAppReviewRequiredError(
                "this tiktok app appears to be unaudited -- creator_info reports only SELF_ONLY as "
                f"an allowed privacy_level (requested {privacy_status!r}); see docs/PUBLISHING.md"
            )
        raise PublisherPrivacyNotAllowedError(
            f"privacy_status {privacy_status!r} is not in this account's allowed privacy_level_options "
            f"{sorted(creator_info.allowed_privacy_values)}"
        )
    if privacy_status == "SELF_ONLY" and options.is_branded_content:
        raise MetadataInvalidError(
            "tiktok branded content (brand_content_toggle) cannot be posted as SELF_ONLY -- per the "
            "official docs, branded content requires a non-private privacy_level"
        )
    if not creator_info.comments_configurable and not options.disable_comment:
        raise MetadataInvalidError(
            "this account's comments are disabled at the platform level (creator_info) -- "
            "disable_comment must stay at its default (disabled), it cannot be overridden"
        )
    if not creator_info.remix_configurable and not (options.disable_duet and options.disable_stitch):
        raise MetadataInvalidError(
            "this account's duet/stitch are disabled at the platform level (creator_info) -- "
            "disable_duet/disable_stitch must stay at their defaults (disabled)"
        )
    if creator_info.warnings:
        # TikTok's creator_info response does not currently document an
        # account-warnings field, so this is expected to always be empty in
        # practice -- kept as a forward-compatible/defense-in-depth check,
        # not something exercised by real TikTok responses today.
        raise PublisherCreatorNotEligibleError(
            "this account has active warnings reported by creator_info: " + "; ".join(creator_info.warnings)
        )


# TikTok's post-status `status` values mapped to this project's generic
# ProcessingStatusResult.processing_status vocabulary (see providers.base).
# Any status not in this map -- including one this adapter doesn't yet know
# about -- resolves to "processing" (keep polling), never "succeeded": only
# an explicit, recognized PUBLISH_COMPLETE ever completes a publication.
_PROCESSING_STATUS_MAP = {
    "PROCESSING_UPLOAD": "processing",
    "PROCESSING_DOWNLOAD": "processing",  # PULL_FROM_URL only -- unused (FILE_UPLOAD only)
    "PUBLISH_COMPLETE": "succeeded",
    "FAILED": "failed",
    # TikTok routed the post to the user's TikTok inbox as a draft instead
    # of actually publishing it (e.g. the account lacks Direct Post
    # permission) -- a genuine terminal outcome for this project (an actual
    # published post is always what's wanted, never a draft sitting in
    # someone's inbox), so it's mapped to "failed" rather than polled
    # forever waiting for a PUBLISH_COMPLETE that will never arrive.
    "SEND_TO_USER_INBOX": "failed",
}


def _parse_envelope(response: Any, *, context: str) -> dict:
    """TikTok's `{data, error}` response envelope can carry a failure even
    on HTTP 200 -- `error.code != "ok"` (and != None, for endpoints that
    omit it on success) is never treated as success. The specific
    non-auth error-code vocabulary for the Content Posting API endpoints
    is not confirmed against primary docs in this session, so anything
    unrecognized is classified as TransientProviderError (retryable) --
    the same conservative default providers.youtube_publisher uses for an
    unrecognized YouTube error reason."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise TransientProviderError(
            f"{context}: response was not valid JSON (HTTP {response.status_code})"
        ) from exc
    error = payload.get("error") or {}
    error_code = error.get("code")
    if error_code not in (None, "ok"):
        if error_code in ("access_token_invalid", "scope_not_authorized"):
            raise ProviderAuthError(f"{context}: {error_code}")
        raise TransientProviderError(f"{context}: {error_code}")
    if response.status_code != 200:
        raise TransientProviderError(f"{context}: unexpected HTTP {response.status_code}")
    return payload


class TikTokPublisher:
    """Adapter for TikTok's Content Posting API (Direct Post / FILE_UPLOAD
    only -- see docs/PUBLISHING.md for the full research this is built
    from, checked 2026-07-29). Plain httpx/REST, no TikTok SDK, mirroring
    every other adapter in this project.

    `access_token_provider` is called fresh before every request, exactly
    like YouTubePublisher -- the caller (providers.registry, wired in a
    later commit) is responsible for refreshing via the CredentialBackend
    and handing back a live token on each call; this class never reads a
    credential or refreshes a token itself.

    Unlike YouTube, the durable identifier (`publish_id`) is known
    immediately from create_upload_session's response, surfaced via
    UploadSessionHandle.provider_reference -- see its docstring in
    providers.base. query_upload_offset always raises
    UploadSessionExpiredError (no documented way to query a session's
    confirmed offset -- see docs/PUBLISHING.md); a fresh session is
    started instead of guessing."""

    provider_id = "tiktok"
    capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        base_url: str,
        chunk_size: int = 10 * 1024 * 1024,
        connect_timeout: float = 10.0,
        read_timeout: float = 30.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        transport: Any = None,
    ) -> None:
        import httpx

        self._access_token_provider = access_token_provider
        self._base_url = base_url.rstrip("/")
        self._chunk_size = chunk_size
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def validate_configuration(self) -> None:
        return None  # nothing left to statically validate beyond config.py's own checks

    # -- creator info -------------------------------------------------------

    def get_creator_info(self) -> CreatorInfo | None:
        """Always a fresh network call -- never memoized on this instance.
        See providers.base.Publisher's docstring and docs/OPERATIONS.md:
        an old creator_info snapshot must never be trusted for a publish
        decision, since privacy_level_options/comment_disabled/etc. can
        change between calls (most importantly, the moment an app passes
        TikTok's review)."""
        response = self._request_with_retry(
            "POST", f"{self._base_url}/v2/post/publish/creator_info/query/", json={},
        )
        payload = _parse_envelope(response, context="creator info")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise TransientProviderError("creator info response missing 'data'")
        try:
            privacy_options = frozenset(data["privacy_level_options"])
            comment_disabled = bool(data.get("comment_disabled", True))
            duet_disabled = bool(data.get("duet_disabled", True))
            stitch_disabled = bool(data.get("stitch_disabled", True))
            max_duration = data.get("max_video_post_duration_sec")
            return CreatorInfo(
                account_identifier=str(data.get("creator_username", "")),
                display_name=str(data.get("creator_nickname") or data.get("creator_username", "")),
                allowed_privacy_values=privacy_options,
                comments_configurable=not comment_disabled,
                remix_configurable=not (duet_disabled or stitch_disabled),
                max_post_duration_sec=float(max_duration) if max_duration is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise TransientProviderError("creator info response missing required fields") from exc

    # -- session creation -----------------------------------------------------

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle:
        post_text = build_post_text(metadata.title)
        options = metadata.platform_options or {}
        total_chunk_count = max(1, math.ceil(total_bytes / self._chunk_size))
        body = {
            "post_info": {
                "title": post_text,
                "privacy_level": metadata.privacy_status,
                "disable_duet": bool(options.get("disable_duet", True)),
                "disable_stitch": bool(options.get("disable_stitch", True)),
                "disable_comment": bool(options.get("disable_comment", True)),
                "video_cover_timestamp_ms": int(options.get("video_cover_timestamp_ms", 0)),
                "brand_content_toggle": bool(options.get("brand_content_toggle", False)),
                "brand_organic_toggle": bool(options.get("brand_organic_toggle", False)),
                "is_aigc": bool(options.get("is_aigc", False)),
            },
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": total_bytes,
                "chunk_size": self._chunk_size,
                "total_chunk_count": total_chunk_count,
            },
        }
        response = self._request_with_retry(
            "POST", f"{self._base_url}/v2/post/publish/video/init/", json=body,
            headers={"X-Request-ID": correlation_id},
        )
        payload = _parse_envelope(response, context="upload session creation")
        data = payload.get("data") or {}
        publish_id = data.get("publish_id")
        upload_url = data.get("upload_url")
        if not publish_id or not upload_url:
            raise TransientProviderError("upload session creation response missing publish_id/upload_url")
        return UploadSessionHandle(
            session_reference=upload_url, total_bytes=total_bytes, chunk_size=self._chunk_size,
            provider_reference=publish_id,
        )

    # -- chunk upload ---------------------------------------------------------

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult:
        end_byte = start_byte + len(chunk) - 1
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(chunk)),
            "Content-Range": f"bytes {start_byte}-{end_byte}/{total_bytes}",
        }
        try:
            response = self._client.put(session.session_reference, content=chunk, headers=headers)
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"chunk upload timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"chunk upload transport error ({type(exc).__name__})") from exc

        # The official docs do not document a distinct per-chunk "partial"
        # status code the way YouTube's protocol documents 308 (see
        # docs/PUBLISHING.md's "not specified" section) -- completion is
        # instead derived purely from client-side byte accounting against
        # total_bytes, which this adapter already needs regardless since
        # the durable publish_id (provider_video_id) is known upfront from
        # create_upload_session, never from this response.
        if 200 <= response.status_code < 300:
            completed = (end_byte + 1) >= total_bytes
            return UploadChunkResult(
                bytes_uploaded=total_bytes if completed else end_byte + 1,
                completed=completed,
                provider_video_id=session.provider_reference if completed else None,
            )
        if response.status_code == 404:
            raise UploadSessionExpiredError("tiktok upload session no longer valid")
        if response.status_code in (500, 502, 503, 504):
            raise TransientProviderError(f"chunk upload returned HTTP {response.status_code}, retryable")
        if response.status_code in (400, 403):
            raise UploadRejectedError(f"upload rejected by the provider (HTTP {response.status_code})")
        raise TransientProviderError(f"chunk upload returned unexpected HTTP {response.status_code}")

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None:
        """TikTok's docs do not confirm a way to query a resumable upload
        session's already-accepted byte offset (unlike YouTube's documented
        `Content-Range: bytes */TOTAL` convention -- see docs/PUBLISHING.md's
        "not specified" section). Treating every resume attempt as expired
        is the safe, conservative choice: worker.publish_runner already
        starts a brand-new session from byte 0 on UploadSessionExpiredError
        (the same path YouTube's genuinely-expired sessions use), so this
        never risks re-sending bytes at a guessed/wrong offset or silently
        skipping bytes TikTok never actually received -- at the cost of a
        full re-upload after any interruption rather than a true resume."""
        raise UploadSessionExpiredError(
            "tiktok upload sessions cannot be safely resumed (no documented offset-query endpoint) "
            "-- starting a new session from byte 0"
        )

    # -- processing status ------------------------------------------------------

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        response = self._request_with_retry(
            "POST", f"{self._base_url}/v2/post/publish/status/fetch/", json={"publish_id": provider_video_id},
        )
        payload = _parse_envelope(response, context="processing status")
        data = payload.get("data") or {}
        raw_status = str(data.get("status") or "")
        resolved = _PROCESSING_STATUS_MAP.get(raw_status, "processing")  # unknown -> keep polling, never success

        failure_reason = None
        if resolved == "failed":
            failure_reason = data.get("fail_reason") or (
                "sent_to_inbox_not_published" if raw_status == "SEND_TO_USER_INBOX" else raw_status
            )
        return ProcessingStatusResult(
            processing_status=resolved,
            privacy_status=None,  # the status-fetch response does not echo privacy_level back
            # Never fabricated: the response doesn't carry enough account
            # info (e.g. the creator's handle) to build a public watch URL
            # from this adapter alone -- see docs/PUBLISHING.md. The durable
            # reference is provider_video_id (publish_id), already
            # persisted on Publication.provider_video_id.
            publication_url=None,
            failure_reason=failure_reason,
            request_id=response.headers.get("x-tt-logid"),
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
            headers.setdefault("Content-Type", "application/json; charset=UTF-8")
            correlation_id = headers.setdefault("X-Request-ID", str(uuid.uuid4()))
            try:
                response = self._client.request(method, url, headers=headers, **kwargs)
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"tiktok api request timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"tiktok api transport error ({type(exc).__name__})")
                continue

            if response.status_code == 401:
                raise ProviderAuthError(f"tiktok api rejected the access token (request_id={correlation_id})")
            if response.status_code == 403:
                raise PublisherPermissionDeniedError(f"tiktok api permission denied (request_id={correlation_id})")
            if response.status_code == 429:
                raw_retry_after = response.headers.get("retry-after")
                try:
                    retry_after = max(0.0, float(raw_retry_after)) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                last_transient = PublisherRateLimitedError(
                    f"tiktok api rate limited (request_id={correlation_id})"
                )
                continue
            if response.status_code >= 500:
                last_transient = TransientProviderError(f"tiktok api returned HTTP {response.status_code}")
                continue
            return response

        assert last_transient is not None
        raise TransientProviderError(f"tiktok api request failed after {attempts} attempts: {last_transient}")
