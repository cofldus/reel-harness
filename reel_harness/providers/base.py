from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ChannelContext:
    channel_id: str
    niche: str
    language: str
    style_preset: dict


@dataclass
class TopicResult:
    topic: str
    provider_id: str
    model_id: str
    # Optional provider-side correlation id. Never contains the request/response
    # body or any credential.
    request_id: str | None = None


@dataclass
class ScriptResult:
    raw_text: str
    provider_id: str
    model_id: str
    prompt_version: str
    request_id: str | None = None
    # Token usage metadata as reported by the provider (prompt/completion/total
    # counts) -- cost accounting only, never message content.
    usage: dict | None = None


@dataclass
class TTSResult:
    audio_path: Path
    duration_sec: float
    provider_id: str
    voice_id: str
    # Checksum of the (normalized) audio actually written to audio_path, and
    # the provider-side correlation id. Never credentials or request bodies.
    checksum_sha256: str | None = None
    request_id: str | None = None


@dataclass
class MediaCandidate:
    candidate_id: str
    source_url: str
    author: str | None
    license_type: str | None
    license_url: str | None
    # Phase 2D: real-provider search/selection metadata. Every field has a
    # default so Fake-provider call sites and older tests keep constructing
    # this without changes; a real adapter fills all of them.
    provider_id: str = ""
    download_url: str = ""
    creator_url: str | None = None
    commercial_use_allowed: bool = False
    modification_allowed: bool = False
    attribution_text: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    fps: float | None = None
    file_size_bytes: int | None = None
    content_type: str | None = None
    # Position in the provider's own result ordering (0 = top result) -- used
    # as a selection-score input and, combined with candidate_id, keeps
    # selection deterministic.
    provider_rank: int = 0
    provider_request_id: str | None = None


@dataclass
class LocalAssetResult:
    local_path: Path
    checksum_sha256: str
    mime_type: str
    source_url: str
    author: str | None
    license_type: str | None
    # Phase 2D: provenance/license metadata carried from the selected
    # MediaCandidate through to the Asset DB row and manifest. Defaults keep
    # Fake-provider call sites unchanged.
    provider_id: str = ""
    provider_asset_id: str = ""
    source_page_url: str | None = None
    creator_url: str | None = None
    commercial_use_allowed: bool = False
    modification_allowed: bool = False
    attribution_text: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None
    fps: float | None = None
    request_id: str | None = None


@dataclass
class PublicationMetadata:
    """Deterministic upload metadata built by pipeline.publish_metadata (see
    docs/PUBLISHING.md) -- never handed to an adapter unvalidated, since
    every field here becomes part of the actual upload/post request.

    `title`/`description`/`tags`/`category_id`/`made_for_kids`/
    `default_language` are the fields every current adapter uses in some
    form (YouTube uses all of them; a text-only platform like TikTok uses
    `title` as the post caption and ignores `category_id`/`made_for_kids`).
    `privacy_status` is validated against the specific provider's own
    `PublisherCapabilities.privacy_values` (see below), not a fixed global
    enum -- different platforms have entirely different privacy vocabularies
    (YouTube: private/unlisted/public; TikTok: SELF_ONLY/PUBLIC_TO_EVERYONE/
    MUTUAL_FOLLOW_FRIENDS/FOLLOWER_OF_CREATOR).

    `platform_options` is a deliberately generic passthrough dict for
    fields that exist on ONE platform's API and don't generalize (TikTok's
    disable_comment/disable_duet/disable_stitch/video_cover_timestamp_ms/
    brand_content_toggle/brand_organic_toggle/is_aigc, for instance) --
    see core.publish_service and docs/PUBLISHING.md. Never contains a
    credential, a local path, or a raw provider response."""

    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy_status: str
    made_for_kids: bool
    default_language: str | None = None
    platform_options: dict = field(default_factory=dict)


@dataclass
class UploadSessionHandle:
    """What a Publisher hands back after creating a resumable upload
    session. `session_reference` is a safe, opaque local identifier -- the
    real session URI (a bearer-style capability URL) is never part of this
    dataclass's persisted form; adapters keep it only in their own memory or
    behind the credential/secret backend (see docs/PUBLISHING.md).

    `provider_reference` is set only for adapters where the provider hands
    back a durable identifier at SESSION-creation time, before any bytes
    are uploaded (TikTok's `publish_id`, known immediately from the init
    response) -- as opposed to YouTube, where the durable video id is only
    known once the upload completes. When set, callers persist it onto
    `Publication.provider_video_id` right away, closing an even earlier
    version of the "provider succeeded, DB commit lost" crash-recovery gap
    (see core.publish_reconciliation) for providers structured this way."""

    session_reference: str
    total_bytes: int
    chunk_size: int
    provider_reference: str | None = None


@dataclass(frozen=True)
class PublisherCapabilities:
    """What one Publisher adapter actually supports -- checked by
    core.publish_service/CLI/API instead of scattering
    `if provider == "youtube"`/`if provider == "tiktok"` conditionals
    through domain code (see docs/PUBLISHING.md's Phase 3C capability
    model). A platform that doesn't support something a caller asked for
    gets an explicit validation error, never a silent fallback to a
    different option.

    `privacy_values` is the full set of privacy strings this provider's API
    defines; `public_privacy_values` is the subset "public-like" enough to
    require the double-confirmation gate (an explicit --confirm-public-upload
    flag AND the REEL_HARNESS_ALLOW_PUBLIC_UPLOAD feature flag) --
    `supports_public_privacy`/`supports_unlisted_privacy` are coarser
    booleans some callers (e.g. a quick capability check) may prefer over
    inspecting the full sets."""

    supports_direct_publish: bool
    supports_upload_only: bool
    supports_scheduled_publish: bool
    supports_public_privacy: bool
    supports_unlisted_privacy: bool
    supports_comments_control: bool
    supports_remix_control: bool  # duet/stitch-style "build on this" controls
    supports_processing_poll: bool
    supports_remote_delete: bool
    requires_creator_info: bool
    requires_user_confirmation: bool
    privacy_values: frozenset[str]
    default_privacy: str
    public_privacy_values: frozenset[str]


@dataclass
class CreatorInfo:
    """Safe, non-secret account/creator state fetched fresh immediately
    before a publish attempt (never trusted from an earlier snapshot -- see
    docs/OPERATIONS.md). Generic across platforms that have this concept
    (TikTok's `creator_info` query); a platform without one (YouTube) never
    constructs this and `requires_creator_info=False` in its capabilities.
    Never contains a token or any credential."""

    account_identifier: str
    display_name: str
    allowed_privacy_values: frozenset[str]
    comments_configurable: bool
    remix_configurable: bool
    max_post_duration_sec: float | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class UploadChunkResult:
    """Result of one PUT chunk (or an upload-completing final chunk).
    `provider_video_id` is set only once the provider has actually created
    the video resource (i.e. `completed=True`)."""

    bytes_uploaded: int
    completed: bool
    provider_video_id: str | None = None
    publication_url: str | None = None
    request_id: str | None = None


@dataclass
class ProcessingStatusResult:
    processing_status: str  # "processing" | "succeeded" | "failed" | "terminated"
    privacy_status: str | None = None
    publication_url: str | None = None
    failure_reason: str | None = None
    request_id: str | None = None


@dataclass(frozen=True)
class CinematicCapabilities:
    """What one cinematic video adapter actually supports -- checked by
    the Fable service/UI instead of scattering vendor conditionals through
    domain code, mirroring PublisherCapabilities. A capability the
    provider's OFFICIAL docs don't confirm is False/absent here, and the
    feature is hidden rather than imitated (Fable spec rule: never fake an
    unsupported provider feature)."""

    text_to_video: bool
    image_to_video: bool
    first_frame: bool
    last_frame: bool
    character_reference: bool
    multiple_references: bool
    video_reference: bool
    native_audio: bool
    lip_sync: bool
    supports_seed: bool
    supports_negative_prompt: bool
    supported_durations_sec: frozenset[float]
    supported_aspect_ratios: frozenset[str]
    supported_resolutions: frozenset[str]
    max_concurrent_jobs: int | None


@dataclass
class CinematicGenerationRequest:
    """One shot-generation request, provider-agnostic. The compiled
    prompt text arrives here already assembled (F2's ShotPromptCompiler
    owns prompt construction); reference images are local paths whose
    upload/encoding is the adapter's concern. Never contains credentials."""

    prompt: str
    duration_sec: float
    aspect_ratio: str
    resolution: str
    reference_image_paths: list[Path] = field(default_factory=list)
    first_frame_path: Path | None = None
    negative_prompt: str | None = None
    seed: int | None = None
    # Deterministic idempotency identity: project + shot + take attempt +
    # prompt/reference fingerprints -- the adapter includes it in provider
    # correlation metadata where supported, and callers use it to avoid
    # duplicate paid generations (see db.cinematic_models.FableTake).
    correlation_id: str = ""


@dataclass
class CinematicGenerationHandle:
    """What create_generation returns. `provider_job_reference` is the
    provider's own job/operation id (safe to persist); signed URLs and
    tokens never appear here."""

    provider_job_reference: str
    provider_id: str
    request_id: str | None = None


@dataclass
class CinematicGenerationStatus:
    """Polling result. `state` is normalized across providers:
    "generating" | "succeeded" | "failed" | "moderated" | "cancelled".
    "moderated" is deliberately distinct from "failed": a moderation
    block routes to REVIEW_REQUIRED (a human decision), never a blind
    retry of the same prompt."""

    state: str
    failure_reason: str | None = None
    moderation_reason: str | None = None
    request_id: str | None = None


@dataclass
class CinematicVideoResult:
    """A downloaded, locally-persisted generated clip -- same shape
    discipline as TTSResult: local path + real measured properties +
    provenance, never a raw provider response."""

    video_path: Path
    duration_sec: float
    provider_id: str
    model_id: str
    license: str
    checksum_sha256: str | None = None
    generation_seed: int | None = None
    request_id: str | None = None
    # Cost as officially reported/derivable, or None when the provider's
    # docs give no per-generation figure -- never an invented estimate.
    cost_amount: float | None = None
    cost_currency: str | None = None


@dataclass
class CinematicCostEstimate:
    """Pre-generation estimate. `known` is False when official pricing
    can't be applied to this request -- callers show "unknown"/credits,
    never a guessed number (Fable spec rule)."""

    known: bool
    amount: float | None = None
    currency: str | None = None
    detail: str | None = None


class CinematicVideoProvider(Protocol):
    """Async-generation contract (submit -> poll -> download), shaped
    after the real APIs surveyed for Fable (all are job-submit + poll,
    none synchronous). Vendor names live only in adapters and the
    registry, per this project's provider discipline."""

    provider_id: str
    capabilities: CinematicCapabilities

    def validate_request(self, request: CinematicGenerationRequest) -> None: ...

    def estimate_cost(self, request: CinematicGenerationRequest) -> CinematicCostEstimate: ...

    def create_generation(self, request: CinematicGenerationRequest) -> CinematicGenerationHandle: ...

    def get_generation_status(self, handle: CinematicGenerationHandle) -> CinematicGenerationStatus: ...

    def cancel_generation(self, handle: CinematicGenerationHandle) -> None: ...

    def download_result(
        self, handle: CinematicGenerationHandle, dest_dir: Path,
    ) -> CinematicVideoResult: ...


class LLMProvider(Protocol):
    provider_id: str

    def generate_topic(self, ctx: ChannelContext) -> TopicResult: ...
    def generate_script(self, topic: str, ctx: ChannelContext) -> ScriptResult: ...


class TTSProvider(Protocol):
    provider_id: str

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult: ...


class StockMediaProvider(Protocol):
    provider_id: str

    def search(
        self, query: str, orientation: str, min_duration: float,
        *,
        max_duration: float | None = None,
        min_width: int | None = None,
        min_height: int | None = None,
        per_page: int = 15,
        page: int = 1,
        safe_search: bool = True,
        exclude_provider_asset_ids: frozenset[str] = frozenset(),
    ) -> list[MediaCandidate]: ...
    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult: ...


class Publisher(Protocol):
    """Real vendor names (YouTube, TikTok, ...) live only in the adapter
    module and the registry -- core.publish_service and
    worker.publish_runner depend only on this Protocol, mirroring
    LLMProvider/TTSProvider/StockMediaProvider. See docs/PUBLISHING.md for
    the resumable-upload contract this shape is built from.

    `capabilities` is a plain attribute (every adapter sets it once, from
    its module-level `CAPABILITIES` constant -- see
    providers.registry.provider_capabilities for the credential-free
    lookup used by validation that must work even before an adapter
    instance can be constructed). `get_creator_info` returns `None` for any
    adapter whose `capabilities.requires_creator_info` is `False`
    (YouTube); an adapter that DOES require it always re-fetches fresh,
    never reusing an earlier result."""

    provider_id: str
    capabilities: PublisherCapabilities

    def validate_configuration(self) -> None: ...

    def get_creator_info(self) -> CreatorInfo | None: ...

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle: ...

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult: ...

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None: ...

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult: ...
