from __future__ import annotations

from dataclasses import dataclass
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
    """Deterministic, provider-agnostic upload metadata built by
    publisher.metadata (see docs/PUBLISHING.md) -- never handed to an
    adapter unvalidated, since every field here becomes part of the actual
    upload request."""

    title: str
    description: str
    tags: list[str]
    category_id: str
    privacy_status: str  # "private" | "unlisted" | "public"
    made_for_kids: bool
    default_language: str | None = None


@dataclass
class UploadSessionHandle:
    """What a Publisher hands back after creating a resumable upload
    session. `session_reference` is a safe, opaque local identifier -- the
    real session URI (a bearer-style capability URL) is never part of this
    dataclass's persisted form; adapters keep it only in their own memory or
    behind the credential/secret backend (see docs/PUBLISHING.md)."""

    session_reference: str
    total_bytes: int
    chunk_size: int


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
    """Real vendor names (YouTube, ...) live only in the adapter module and
    the registry -- core.publish_service and worker.publish_runner depend
    only on this Protocol, mirroring LLMProvider/TTSProvider/
    StockMediaProvider. See docs/PUBLISHING.md for the resumable-upload
    contract this shape is built from."""

    provider_id: str

    def validate_configuration(self) -> None: ...

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle: ...

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult: ...

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None: ...

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult: ...
