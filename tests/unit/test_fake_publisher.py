from __future__ import annotations

import pytest

from reel_harness.core.errors import TransientProviderError, UploadRejectedError
from reel_harness.providers.base import PublicationMetadata
from reel_harness.providers.fake_publisher import FakePublisher

_METADATA = PublicationMetadata(
    title="T", description="D", tags=["a"], category_id="22", privacy_status="private", made_for_kids=False,
)


def test_full_single_chunk_upload_completes() -> None:
    publisher = FakePublisher()
    session = publisher.create_upload_session(_METADATA, total_bytes=10, mime_type="video/mp4", correlation_id="c")
    result = publisher.upload_chunk(session, b"0123456789", start_byte=0, total_bytes=10)
    assert result.completed is True
    assert result.provider_video_id is not None

    status = publisher.get_processing_status(result.provider_video_id)
    assert status.processing_status == "succeeded"


def test_multi_chunk_upload_and_offset_query() -> None:
    publisher = FakePublisher()
    session = publisher.create_upload_session(_METADATA, total_bytes=10, mime_type="video/mp4", correlation_id="c")
    first = publisher.upload_chunk(session, b"01234", start_byte=0, total_bytes=10)
    assert first.completed is False
    assert publisher.query_upload_offset(session, total_bytes=10) == 5
    second = publisher.upload_chunk(session, b"56789", start_byte=5, total_bytes=10)
    assert second.completed is True
    assert publisher.query_upload_offset(session, total_bytes=10) is None


def test_empty_title_rejected_in_reject_metadata_mode() -> None:
    publisher = FakePublisher(mode="reject_metadata")
    empty_title = PublicationMetadata(
        title="", description="D", tags=[], category_id="22", privacy_status="private", made_for_kids=False,
    )
    with pytest.raises(UploadRejectedError):
        publisher.create_upload_session(empty_title, 10, "video/mp4", "c")


def test_timeout_mode_raises_transient() -> None:
    publisher = FakePublisher(mode="timeout")
    session = publisher.create_upload_session(_METADATA, 10, "video/mp4", "c")
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(session, b"x", 0, 10)


def test_fail_processing_mode() -> None:
    publisher = FakePublisher(mode="fail_processing")
    status = publisher.get_processing_status("any-id")
    assert status.processing_status == "failed"
    assert status.failure_reason == "fake_processing_failure"
