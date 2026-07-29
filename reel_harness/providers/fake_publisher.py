from __future__ import annotations

import hashlib
import uuid
from typing import Literal

from reel_harness.core.errors import TransientProviderError, UploadRejectedError, UploadSessionExpiredError
from reel_harness.providers.base import (
    CreatorInfo,
    ProcessingStatusResult,
    PublicationMetadata,
    PublisherCapabilities,
    UploadChunkResult,
    UploadSessionHandle,
)

FakeMode = Literal["ok", "reject_metadata", "fail_processing", "timeout"]

# Mirrors YouTube's real capabilities (see providers.youtube_publisher) --
# FakePublisher stands in for YouTube in most of this project's tests.
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


class FakePublisher:
    """Stand-in for a real Publisher (YouTube-like). Deterministic,
    in-memory, no network -- mirrors FakeLLMProvider/FakeTTSProvider/
    FakeStockMediaProvider's role for the publisher worker's tests."""

    provider_id = "fake"
    capabilities = CAPABILITIES

    def __init__(self, mode: FakeMode = "ok") -> None:
        self.mode = mode
        self._sessions: dict[str, dict] = {}
        self._videos: dict[str, bytes] = {}

    def validate_configuration(self) -> None:
        pass

    def get_creator_info(self) -> CreatorInfo | None:
        return None

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle:
        if self.mode == "reject_metadata" and not metadata.title:
            raise UploadRejectedError("fake: empty title rejected")
        ref = f"fake-session-{uuid.uuid4().hex}"
        self._sessions[ref] = {"bytes": bytearray(), "total_bytes": total_bytes}
        return UploadSessionHandle(session_reference=ref, total_bytes=total_bytes, chunk_size=262144)

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult:
        if self.mode == "timeout":
            raise TransientProviderError("fake: simulated timeout")
        state = self._sessions.get(session.session_reference)
        if state is None:
            raise UploadSessionExpiredError("fake: unknown session (simulated expiry)")
        buf: bytearray = state["bytes"]
        if start_byte > len(buf):
            raise TransientProviderError("fake: chunk out of order")
        buf[start_byte:start_byte + len(chunk)] = chunk
        if len(buf) >= total_bytes:
            video_id = hashlib.sha256(bytes(buf)).hexdigest()[:16]
            self._videos[video_id] = bytes(buf)
            return UploadChunkResult(
                bytes_uploaded=total_bytes, completed=True, provider_video_id=video_id,
                publication_url=f"fake://video/{video_id}",
            )
        return UploadChunkResult(bytes_uploaded=len(buf), completed=False)

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None:
        """Mirrors the real adapter's contract: an unknown session raises
        UploadSessionExpiredError (never conflated with "already complete",
        which is a genuine None return only for a session this instance
        actually knows about and has fully received)."""
        state = self._sessions.get(session.session_reference)
        if state is None:
            raise UploadSessionExpiredError("fake: unknown session (simulated expiry)")
        uploaded = len(state["bytes"])
        return None if uploaded >= total_bytes else uploaded

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        if self.mode == "fail_processing":
            return ProcessingStatusResult(processing_status="failed", failure_reason="fake_processing_failure")
        return ProcessingStatusResult(
            processing_status="succeeded", privacy_status="private",
            publication_url=f"fake://video/{provider_video_id}",
        )
