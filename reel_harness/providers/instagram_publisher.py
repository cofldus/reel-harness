from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherCreatorNotEligibleError,
    PublisherPermissionDeniedError,
    PublisherPublishingLimitReachedError,
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

# Per the official Instagram Platform Content Publishing docs (checked
# 2026-07-29 -- see docs/PUBLISHING.md). Only the `upload_type=resumable`
# direct-upload path is implemented -- the `video_url`-hosted alternative
# would require this project to stand up a public HTTPS endpoint, which
# is explicitly out of scope this phase (see docs/PUBLISHING.md's "Two
# upload paths" section for the full reasoning).
ADAPTER_VERSION = "instagram-content-publishing-v1"

# Reels publishing is inherently public -- Instagram has no private/
# unlisted concept the way YouTube/TikTok do, so privacy_values has
# exactly one member and it's also the only "public" value: every publish
# requires the double-confirmation gate, with no restrictive default to
# fall back to.
CAPABILITIES = PublisherCapabilities(
    supports_direct_publish=True,
    supports_upload_only=False,
    supports_scheduled_publish=False,
    supports_public_privacy=True,
    supports_unlisted_privacy=False,
    supports_comments_control=False,
    supports_remix_control=False,
    supports_processing_poll=True,
    supports_remote_delete=False,
    requires_creator_info=True,
    requires_user_confirmation=True,
    privacy_values=frozenset({"PUBLIC"}),
    default_privacy="PUBLIC",
    public_privacy_values=frozenset({"PUBLIC"}),
)

# Per the ig-user/media reference: caption max 2200 chars, <=30 hashtags,
# <=20 @mentions.
MAX_CAPTION_LENGTH = 2200
MAX_HASHTAGS = 30
MAX_MENTIONS = 20

# Defense in depth only -- the caption is built exclusively from the
# manifest's own title/topic/attribution (never from a raw provider
# response, a local path, a job id, or any credential), so these patterns
# should never actually match in practice. Mirrors
# providers.tiktok_publisher's identical precaution.
_FORBIDDEN_CAPTION_PATTERNS = (
    re.compile(r"[A-Za-z]:\\"),  # a Windows local path (C:\...)
    re.compile(r"(?<![\w-])/jobs/[0-9a-fA-F-]{8,}"),  # this project's job artifact path shape
    re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),  # a UUID
    re.compile(r"(?i)(api[_-]?key|client[_-]?secret|access[_-]?token|refresh[_-]?token)\s*[:=]"),
    re.compile(r"(?i)x-amz-|signature=|expires=\d"),  # signed-URL query fragments
    re.compile(r"https?://127\.0\.0\.1"),
)
_HASHTAG_RE = re.compile(r"(?<!\w)#(\w+)")
_MENTION_RE = re.compile(r"(?<!\w)@(\w+)")


def build_caption(text: str) -> str:
    """Validates (never transforms) the Reels caption. Raises
    MetadataInvalidError on a length/hashtag/mention-count violation or a
    disallowed internal marker (job id / local path / secret / signed-URL
    fragment)."""
    if len(text) > MAX_CAPTION_LENGTH:
        raise MetadataInvalidError(f"instagram caption exceeds {MAX_CAPTION_LENGTH} characters")
    hashtag_count = len(_HASHTAG_RE.findall(text))
    if hashtag_count > MAX_HASHTAGS:
        raise MetadataInvalidError(
            f"instagram caption has {hashtag_count} hashtags, exceeding the {MAX_HASHTAGS} max"
        )
    mention_count = len(_MENTION_RE.findall(text))
    if mention_count > MAX_MENTIONS:
        raise MetadataInvalidError(
            f"instagram caption has {mention_count} @mentions, exceeding the {MAX_MENTIONS} max"
        )
    for pattern in _FORBIDDEN_CAPTION_PATTERNS:
        if pattern.search(text):
            raise MetadataInvalidError("instagram caption contains a disallowed internal marker")
    return text


@dataclass(frozen=True)
class InstagramReelsOptions:
    """Instagram-specific Reels fields that don't generalize to any other
    platform -- carried through providers.base.PublicationMetadata.
    platform_options (see its docstring and docs/PUBLISHING.md), never as
    extra fields on the common dataclass. `share_to_feed=False` (Reels-tab
    only, not also Feed) is the default -- the more conservative,
    lower-visibility choice, matching this project's "most restrictive
    default" rule wherever a platform offers one."""

    share_to_feed: bool = False
    thumb_offset_ms: int = 0
    cover_url: str | None = None
    collaborators: tuple[str, ...] = ()  # max 3, validated below
    location_id: str | None = None
    audio_name: str | None = None

    def as_platform_options(self) -> dict:
        return {
            "share_to_feed": self.share_to_feed,
            "thumb_offset_ms": self.thumb_offset_ms,
            "cover_url": self.cover_url,
            "collaborators": list(self.collaborators),
            "location_id": self.location_id,
            "audio_name": self.audio_name,
        }


def platform_options_from_dict(options: dict) -> InstagramReelsOptions:
    """The inverse of InstagramReelsOptions.as_platform_options -- mirrors
    providers.tiktok_publisher.platform_options_from_dict's role."""
    return InstagramReelsOptions(
        share_to_feed=bool(options.get("share_to_feed", False)),
        thumb_offset_ms=int(options.get("thumb_offset_ms", 0)),
        cover_url=options.get("cover_url"),
        collaborators=tuple(options.get("collaborators") or ()),
        location_id=options.get("location_id"),
        audio_name=options.get("audio_name"),
    )


def validate_publish_options(account_info: CreatorInfo, options: InstagramReelsOptions) -> None:
    """Called immediately before every publish attempt with a FRESHLY
    fetched account info (never a cached/earlier one -- see
    InstagramPublisher.get_creator_info). Rejects an unsupported
    combination explicitly; never a silent fallback to a different
    option."""
    if len(options.collaborators) > 3:
        raise MetadataInvalidError(
            f"instagram reels allow at most 3 collaborators, got {len(options.collaborators)}"
        )
    if account_info.warnings:
        for warning in account_info.warnings:
            if warning == "publishing_limit_reached":
                raise PublisherPublishingLimitReachedError(
                    "this account has reached Instagram's documented 100 API-published-posts-per-"
                    "24-hour limit -- see GET /{ig-user-id}/content_publishing_limit"
                )
        raise PublisherCreatorNotEligibleError(
            "this account is not currently eligible to publish: " + "; ".join(account_info.warnings)
        )


class InstagramPublisher:
    """Adapter for Instagram's Content Publishing API (Reels via
    `upload_type=resumable` direct upload only -- see docs/PUBLISHING.md
    for the full research this is built from, checked 2026-07-29). Plain
    httpx/REST, no Meta SDK, mirroring every other adapter in this
    project.

    `access_token_provider` is called fresh before every request, exactly
    like YouTubePublisher/TikTokPublisher -- the caller (providers.registry,
    wired in a later commit) is responsible for refreshing via the
    CredentialBackend and handing back a live token on each call; this
    class never reads a credential or refreshes a token itself.

    Only get_creator_info is implemented so far -- create_upload_session/
    upload_chunk/query_upload_offset/get_processing_status land with the
    container-publisher adapter in a later commit (see docs/PUBLISHING.md's
    Phase 3D commit plan)."""

    provider_id = "instagram"
    capabilities = CAPABILITIES

    def __init__(
        self,
        *,
        access_token_provider: Callable[[], str],
        graph_url: str,
        api_version: str,
        account_id: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        transport: Any = None,
    ) -> None:
        import httpx

        self._access_token_provider = access_token_provider
        self._graph_url = graph_url.rstrip("/")
        self._api_version = api_version
        self._account_id = account_id
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._httpx = httpx
        self._client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=60.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def validate_configuration(self) -> None:
        return None  # nothing left to statically validate beyond config.py's own checks

    def _versioned_url(self, path: str) -> str:
        return f"{self._graph_url}/{self._api_version}/{path}"

    # -- account info ---------------------------------------------------------

    def get_creator_info(self) -> CreatorInfo | None:
        """Always a fresh network call -- never memoized on this instance.
        See providers.base.Publisher's docstring and docs/OPERATIONS.md:
        an old account snapshot must never be trusted for a publish
        decision, since the account's professional status and publishing
        quota usage can both change between calls. Two real requests:
        basic account identity, and the current publishing-limit usage."""
        identity = self._request_with_retry(
            "GET", self._versioned_url(self._account_id),
            params={"fields": "id,username,account_type"},
        )
        identity_payload = _parse_graph_response(identity, context="account info")

        limit_response = self._request_with_retry(
            "GET", self._versioned_url(f"{self._account_id}/content_publishing_limit"),
        )
        limit_payload = _parse_graph_response(limit_response, context="publishing limit")

        warnings: list[str] = []
        account_type = identity_payload.get("account_type")
        if account_type not in (None, "BUSINESS", "MEDIA_CREATOR"):
            warnings.append(f"account_type={account_type!r} is not a Reels-eligible professional account type")

        try:
            usage_entries = limit_payload.get("data") or []
            quota_usage = usage_entries[0]["quota_usage"] if usage_entries else 0
            quota_total = usage_entries[0].get("config", {}).get("quota_total", 100) if usage_entries else 100
        except (KeyError, TypeError, IndexError) as exc:
            raise TransientProviderError("publishing limit response missing required fields") from exc
        if quota_usage >= quota_total:
            warnings.append("publishing_limit_reached")

        return CreatorInfo(
            account_identifier=str(identity_payload.get("id", self._account_id)),
            display_name=str(identity_payload.get("username", "")),
            allowed_privacy_values=frozenset({"PUBLIC"}),
            comments_configurable=False,
            remix_configurable=False,
            max_post_duration_sec=900.0,  # documented static limit, not account-specific
            warnings=warnings,
        )

    # -- container creation ---------------------------------------------------

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle:
        caption = build_caption(metadata.title)
        options = metadata.platform_options or {}
        body: dict[str, Any] = {
            "media_type": "REELS", "upload_type": "resumable", "caption": caption,
            "share_to_feed": bool(options.get("share_to_feed", False)),
        }
        if options.get("cover_url"):
            body["cover_url"] = options["cover_url"]
        else:
            body["thumb_offset"] = int(options.get("thumb_offset_ms", 0))
        collaborators = options.get("collaborators") or []
        if collaborators:
            body["collaborators"] = collaborators
        if options.get("location_id"):
            body["location_id"] = options["location_id"]
        if options.get("audio_name"):
            body["audio_name"] = options["audio_name"]

        response = self._request_with_retry(
            "POST", self._versioned_url(f"{self._account_id}/media"), data=body,
        )
        payload = _parse_graph_response(response, context="container creation")
        container_id = payload.get("id")
        if not container_id:
            raise TransientProviderError("container creation response missing id")
        # This project only ever sends the whole file in one request (see
        # docs/PUBLISHING.md -- multi-chunk resumability is not confirmed
        # against primary docs), so chunk_size=total_bytes makes
        # worker.publish_runner's generic chunking loop call upload_chunk
        # exactly once with the entire file.
        upload_url = f"https://rupload.facebook.com/ig-api-upload/{self._api_version}/{container_id}"
        return UploadSessionHandle(
            session_reference=upload_url, total_bytes=total_bytes, chunk_size=total_bytes,
            provider_reference=container_id,
        )

    # -- upload -----------------------------------------------------------

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult:
        """A single whole-file POST to rupload.facebook.com -- see
        create_upload_session's docstring for why chunk_size=total_bytes
        guarantees this is only ever called once per session. Uses header-
        based auth (`Authorization: OAuth ...`), a real, documented
        difference from graph.instagram.com's query-param access_token
        convention used by every other call this adapter makes."""
        headers = {
            "Authorization": f"OAuth {self._access_token_provider()}",
            "offset": "0", "file_size": str(total_bytes),
            "Content-Type": "application/octet-stream",
        }
        try:
            response = self._client.post(session.session_reference, content=chunk, headers=headers)
        except self._httpx.TimeoutException as exc:
            raise TransientProviderError(f"chunk upload timed out ({type(exc).__name__})") from exc
        except self._httpx.HTTPError as exc:
            raise TransientProviderError(f"chunk upload transport error ({type(exc).__name__})") from exc

        if response.status_code == 404:
            raise UploadSessionExpiredError("instagram upload container no longer valid")
        if response.status_code in (500, 502, 503, 504):
            raise TransientProviderError(f"chunk upload returned HTTP {response.status_code}, retryable")
        try:
            payload = response.json()
        except ValueError as exc:
            raise TransientProviderError("upload response was not valid JSON") from exc

        if payload.get("success") is True:
            return UploadChunkResult(
                bytes_uploaded=total_bytes, completed=True, provider_video_id=session.provider_reference,
            )
        debug = payload.get("debug_info") or {}
        if debug.get("retriable"):
            raise TransientProviderError(f"instagram upload failed (retriable): {debug.get('message')}")
        raise UploadRejectedError(f"instagram upload rejected: {debug.get('message', 'unknown error')}")

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None:
        """Instagram's docs do not confirm a way to query a resumable
        upload session's already-accepted byte offset -- the documented
        example is a single whole-file POST with offset=0, not a genuine
        multi-request resumable sequence (see docs/PUBLISHING.md's "not
        specified" section). Treating every resume attempt as expired is
        the safe, conservative choice, the same one
        providers.tiktok_publisher.query_upload_offset makes for its own
        unconfirmed offset-query gap -- worker.publish_runner already
        starts a brand-new session from byte 0 on
        UploadSessionExpiredError."""
        raise UploadSessionExpiredError(
            "instagram upload sessions cannot be safely resumed (no documented offset-query endpoint) "
            "-- starting a new container from byte 0"
        )

    # -- processing status + publish ------------------------------------------

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        """`provider_video_id` here is the CONTAINER id (see
        create_upload_session), not yet Instagram's real media id --
        Instagram's flow needs an explicit `media_publish` call once the
        container reaches FINISHED, unlike YouTube/TikTok where upload
        completion alone is the whole story. That publish call happens
        HERE, transparently, the first time this method observes
        FINISHED -- never on every poll (a `PUBLISHED` container status
        on a later poll means a previous call to this method already
        published it, and is never re-published)."""
        container_id = provider_video_id
        status_response = self._request_with_retry(
            "GET", self._versioned_url(container_id), params={"fields": "status_code"},
        )
        payload = _parse_graph_response(status_response, context="container status")
        status_code = payload.get("status_code")

        if status_code == "ERROR":
            return ProcessingStatusResult(processing_status="failed", failure_reason="container_error")
        if status_code == "EXPIRED":
            return ProcessingStatusResult(processing_status="failed", failure_reason="container_expired")
        if status_code == "PUBLISHED":
            # Already published by an earlier call to this method (or a
            # crash happened between that publish succeeding and this
            # process recording it) -- never re-publish. The real media
            # id/permalink from that earlier call may be unrecoverable
            # here; never fabricated -- see docs/PUBLISHING.md.
            return ProcessingStatusResult(processing_status="succeeded", publication_url=None)
        if status_code == "FINISHED":
            publish_response = self._request_with_retry(
                "POST", self._versioned_url(f"{self._account_id}/media_publish"),
                data={"creation_id": container_id},
            )
            publish_payload = _parse_graph_response(publish_response, context="media publish")
            media_id = publish_payload.get("id")
            if not media_id:
                raise TransientProviderError("media_publish response missing media id")
            return ProcessingStatusResult(
                processing_status="succeeded", publication_url=self._fetch_permalink(media_id),
            )
        # IN_PROGRESS, or anything this adapter doesn't recognize -> keep
        # polling, never treated as success.
        return ProcessingStatusResult(processing_status="processing")

    def _fetch_permalink(self, media_id: str) -> str | None:
        """Best-effort only -- the publish itself already succeeded by
        the time this is called, so a failure here is never fatal to the
        publication, only to knowing its public URL right away."""
        try:
            response = self._request_with_retry(
                "GET", self._versioned_url(media_id), params={"fields": "permalink"},
            )
            payload = _parse_graph_response(response, context="media permalink")
            permalink = payload.get("permalink")
            return str(permalink) if permalink else None
        except (ProviderAuthError, PublisherPermissionDeniedError, TransientProviderError):
            return None

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
            params = kwargs.pop("params", {}) or {}
            params["access_token"] = self._access_token_provider()
            correlation_id = str(uuid.uuid4())
            try:
                response = self._client.request(method, url, params=params, **kwargs)
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"instagram api request timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"instagram api transport error ({type(exc).__name__})")
                continue

            if response.status_code == 401:
                raise ProviderAuthError(f"instagram api rejected the access token (request_id={correlation_id})")
            if response.status_code == 403:
                raise PublisherPermissionDeniedError(
                    f"instagram api permission denied (request_id={correlation_id})"
                )
            if response.status_code == 429:
                raw_retry_after = response.headers.get("retry-after")
                try:
                    retry_after = max(0.0, float(raw_retry_after)) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                last_transient = PublisherRateLimitedError(
                    f"instagram api rate limited (request_id={correlation_id})"
                )
                continue
            if response.status_code >= 500:
                last_transient = TransientProviderError(f"instagram api returned HTTP {response.status_code}")
                continue
            return response

        assert last_transient is not None
        raise TransientProviderError(f"instagram api request failed after {attempts} attempts: {last_transient}")


def _parse_graph_response(response: Any, *, context: str) -> dict:
    """Meta's general Graph API error envelope is
    `{"error": {"message", "type", "code", "error_subcode", "fbtrace_id"}}`
    -- an `error` key present at all means failure, regardless of HTTP
    status (mirrors providers.tiktok_publisher._parse_envelope's same
    defensive parsing for TikTok's own envelope shape)."""
    try:
        payload = response.json()
    except ValueError as exc:
        raise TransientProviderError(
            f"{context}: response was not valid JSON (HTTP {response.status_code})"
        ) from exc
    error = payload.get("error")
    if isinstance(error, dict):
        error_type = error.get("type")
        if error_type in ("OAuthException",):
            raise ProviderAuthError(f"{context}: {error.get('message', error_type)}")
        raise TransientProviderError(f"{context}: {error.get('message', error_type or 'unknown error')}")
    if response.status_code != 200:
        raise TransientProviderError(f"{context}: unexpected HTTP {response.status_code}")
    return payload
