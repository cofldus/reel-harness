"""Contract tests for the YouTube resumable upload adapter (MockTransport --
no sockets, coexists with the network-block fixture; NOT a live YouTube
E2E). All tokens are obviously-fake placeholders."""
from __future__ import annotations

import httpx
import pytest

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherPermissionDeniedError,
    PublisherQuotaExceededError,
    TransientProviderError,
    UploadRejectedError,
    UploadSessionExpiredError,
)
from reel_harness.providers.base import PublicationMetadata, UploadSessionHandle
from reel_harness.providers.youtube_publisher import UPLOAD_ENDPOINT, YouTubePublisher

FAKE_TOKEN = "FAKE-YOUTUBE-ACCESS-TOKEN-000000000"
CHUNK_SIZE = 262144  # 256 KiB, the protocol minimum granularity


def _metadata(**overrides) -> PublicationMetadata:
    defaults: dict = dict(
        title="Test video", description="A test description", tags=["a", "b"],
        category_id="22", privacy_status="private", made_for_kids=False,
    )
    defaults.update(overrides)
    return PublicationMetadata(**defaults)


def _publisher(handler, **overrides) -> YouTubePublisher:
    defaults: dict = dict(
        access_token_provider=lambda: FAKE_TOKEN, chunk_size=CHUNK_SIZE,
        max_retries=2, retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return YouTubePublisher(transport=httpx.MockTransport(handler), **defaults)


def test_invalid_chunk_size_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="262144"):
        YouTubePublisher(access_token_provider=lambda: FAKE_TOKEN, chunk_size=1000)


def test_create_upload_session_success_returns_session_reference_from_location() -> None:
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["upload_length"] = request.headers.get("x-upload-content-length")
        seen["upload_type"] = request.headers.get("x-upload-content-type")
        seen["url"] = str(request.url)
        return httpx.Response(200, headers={"location": "https://upload.example.invalid/session/abc123"})

    publisher = _publisher(handler)
    session = publisher.create_upload_session(_metadata(), total_bytes=5_000_000, mime_type="video/mp4",
                                               correlation_id="corr-1")
    assert session.session_reference == "https://upload.example.invalid/session/abc123"
    assert session.total_bytes == 5_000_000
    assert seen["auth"] == f"Bearer {FAKE_TOKEN}"
    assert seen["upload_length"] == "5000000"
    assert seen["upload_type"] == "video/mp4"
    assert "uploadType=resumable" in seen["url"]


def test_create_upload_session_sends_correct_video_resource_body() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"location": "https://upload.example.invalid/s"})

    publisher = _publisher(handler)
    publisher.create_upload_session(
        _metadata(title="My Title", tags=["x", "y"], made_for_kids=True), total_bytes=1, mime_type="video/mp4",
        correlation_id="c",
    )
    body = captured["body"]
    assert body["snippet"]["title"] == "My Title"
    assert body["snippet"]["tags"] == ["x", "y"]
    assert body["status"]["privacyStatus"] == "private"
    assert body["status"]["selfDeclaredMadeForKids"] is True


def test_create_upload_session_missing_location_header_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200))
    with pytest.raises(TransientProviderError):
        publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")


def test_create_upload_session_401_raises_auth_error_not_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    publisher = _publisher(handler)
    with pytest.raises(ProviderAuthError) as exc_info:
        publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")
    assert len(calls) == 1
    assert FAKE_TOKEN not in str(exc_info.value)


def test_create_upload_session_403_quota_exceeded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "quotaExceeded"}]}})

    publisher = _publisher(handler)
    with pytest.raises(PublisherQuotaExceededError):
        publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")


def test_create_upload_session_403_permission_denied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"errors": [{"reason": "forbidden"}]}})

    publisher = _publisher(handler)
    with pytest.raises(PublisherPermissionDeniedError):
        publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")


def test_create_upload_session_429_honors_retry_after_then_succeeds() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, headers={"location": "https://upload.example.invalid/s"})

    publisher = _publisher(handler)
    session = publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")
    assert session.session_reference == "https://upload.example.invalid/s"
    assert len(calls) == 2


def test_create_upload_session_500_then_success() -> None:
    calls: list = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, headers={"location": "https://upload.example.invalid/s"})

    publisher = _publisher(handler)
    publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")
    assert len(calls) == 2


def test_create_upload_session_invalid_metadata_is_metadata_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"errors": [{"reason": "invalidVideoMetadata"}]}})

    publisher = _publisher(handler)
    with pytest.raises(MetadataInvalidError):
        publisher.create_upload_session(_metadata(), 1, "video/mp4", "c")


_SESSION = UploadSessionHandle(
    session_reference="https://upload.example.invalid/session/abc", total_bytes=1_000_000, chunk_size=CHUNK_SIZE,
)


def test_upload_chunk_incomplete_returns_308_range_and_not_completed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-range"] == f"bytes 0-{CHUNK_SIZE - 1}/1000000"
        return httpx.Response(308, headers={"range": f"bytes=0-{CHUNK_SIZE - 1}"})

    publisher = _publisher(handler)
    result = publisher.upload_chunk(_SESSION, b"x" * CHUNK_SIZE, start_byte=0, total_bytes=1_000_000)
    assert result.completed is False
    assert result.bytes_uploaded == CHUNK_SIZE


def test_upload_chunk_final_chunk_completes_and_returns_video_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "video-xyz"})

    publisher = _publisher(handler)
    result = publisher.upload_chunk(_SESSION, b"tail-bytes", start_byte=999_990, total_bytes=1_000_000)
    assert result.completed is True
    assert result.provider_video_id == "video-xyz"
    assert result.publication_url == "https://www.youtube.com/watch?v=video-xyz"


def test_upload_chunk_completion_without_video_id_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={}))
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(_SESSION, b"x", 0, 1)


def test_upload_chunk_404_is_session_expired() -> None:
    publisher = _publisher(lambda r: httpx.Response(404))
    with pytest.raises(UploadSessionExpiredError):
        publisher.upload_chunk(_SESSION, b"x", 0, 1_000_000)


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_upload_chunk_5xx_is_transient_and_interrupted(status_code: int) -> None:
    publisher = _publisher(lambda r: httpx.Response(status_code))
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(_SESSION, b"x", 0, 1_000_000)


def test_upload_chunk_rejected_content() -> None:
    publisher = _publisher(lambda r: httpx.Response(400))
    with pytest.raises(UploadRejectedError):
        publisher.upload_chunk(_SESSION, b"x", 0, 1_000_000)


def test_query_upload_offset_partial() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-range"] == "bytes */1000000"
        assert request.headers["content-length"] == "0"
        return httpx.Response(308, headers={"range": "bytes=0-499999"})

    publisher = _publisher(handler)
    offset = publisher.query_upload_offset(_SESSION, total_bytes=1_000_000)
    assert offset == 500_000


def test_query_upload_offset_nothing_received_yet() -> None:
    publisher = _publisher(lambda r: httpx.Response(308))  # no Range header at all
    offset = publisher.query_upload_offset(_SESSION, total_bytes=1_000_000)
    assert offset == 0


def test_query_upload_offset_already_complete_returns_none() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"id": "v1"}))
    assert publisher.query_upload_offset(_SESSION, total_bytes=1_000_000) is None


def test_query_upload_offset_expired_session() -> None:
    publisher = _publisher(lambda r: httpx.Response(404))
    with pytest.raises(UploadSessionExpiredError):
        publisher.query_upload_offset(_SESSION, total_bytes=1_000_000)


def test_get_processing_status_succeeded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "videos" in str(request.url)
        return httpx.Response(200, json={"items": [{
            "status": {"uploadStatus": "processed", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "succeeded"},
        }]})

    publisher = _publisher(handler)
    result = publisher.get_processing_status("video-1")
    assert result.processing_status == "succeeded"
    assert result.privacy_status == "private"
    assert result.publication_url == "https://www.youtube.com/watch?v=video-1"


def test_get_processing_status_still_processing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "status": {"uploadStatus": "uploaded", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "processing"},
        }]})

    publisher = _publisher(handler)
    result = publisher.get_processing_status("video-1")
    assert result.processing_status == "processing"
    assert result.publication_url is None


def test_get_processing_status_failed_reports_reason() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": [{
            "status": {"uploadStatus": "failed", "failureReason": "codec", "privacyStatus": "private"},
            "processingDetails": {},
        }]})

    publisher = _publisher(handler)
    result = publisher.get_processing_status("video-1")
    assert result.processing_status == "failed"
    assert result.failure_reason == "codec"


def test_get_processing_status_no_video_found_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"items": []}))
    with pytest.raises(TransientProviderError):
        publisher.get_processing_status("does-not-exist")


def test_access_token_provider_called_fresh_on_every_request() -> None:
    tokens = iter(["token-1", "token-2"])
    seen_tokens = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_tokens.append(request.headers.get("authorization"))
        return httpx.Response(200, headers={"location": "https://upload.example.invalid/s"})

    publisher = _publisher(handler, access_token_provider=lambda: next(tokens))
    publisher.create_upload_session(_metadata(), 1, "video/mp4", "c1")
    publisher.create_upload_session(_metadata(), 1, "video/mp4", "c2")
    assert seen_tokens == ["Bearer token-1", "Bearer token-2"]


def test_upload_endpoint_constant_matches_official_docs() -> None:
    assert UPLOAD_ENDPOINT == "https://www.googleapis.com/upload/youtube/v3/videos"
