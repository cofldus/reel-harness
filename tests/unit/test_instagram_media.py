"""providers.instagram_media: local pre-upload validation against
Instagram's documented Reels video limits. No network, no ffprobe call --
reuses facts already known (manifest duration, file size)."""
from __future__ import annotations

import pytest

from reel_harness.core.errors import VideoTooLargeError, VideoTooLongError
from reel_harness.providers.instagram_media import (
    MAX_DURATION_SECONDS,
    MAX_FILE_SIZE_BYTES,
    MIN_DURATION_SECONDS,
    validate_video_for_reels,
)


def test_accepts_a_normal_short_form_video() -> None:
    validate_video_for_reels(duration_sec=20.0, file_size_bytes=5_000_000)


def test_accepts_exactly_the_minimum_duration() -> None:
    validate_video_for_reels(duration_sec=MIN_DURATION_SECONDS, file_size_bytes=1_000_000)


def test_accepts_exactly_the_maximum_duration() -> None:
    validate_video_for_reels(duration_sec=MAX_DURATION_SECONDS, file_size_bytes=1_000_000)


def test_rejects_too_short() -> None:
    with pytest.raises(VideoTooLongError, match="minimum"):
        validate_video_for_reels(duration_sec=MIN_DURATION_SECONDS - 0.01, file_size_bytes=1_000_000)


def test_rejects_too_long() -> None:
    with pytest.raises(VideoTooLongError, match="maximum"):
        validate_video_for_reels(duration_sec=MAX_DURATION_SECONDS + 1, file_size_bytes=1_000_000)


def test_accepts_exactly_the_maximum_file_size() -> None:
    validate_video_for_reels(duration_sec=20.0, file_size_bytes=MAX_FILE_SIZE_BYTES)


def test_rejects_too_large() -> None:
    with pytest.raises(VideoTooLargeError, match="300"):
        validate_video_for_reels(duration_sec=20.0, file_size_bytes=MAX_FILE_SIZE_BYTES + 1)


def test_file_size_checked_even_without_a_known_duration() -> None:
    """An older manifest schema or a pinned snapshot might not carry
    duration_sec -- the file-size check never depends on ffprobe, so it
    always applies regardless."""
    with pytest.raises(VideoTooLargeError):
        validate_video_for_reels(duration_sec=None, file_size_bytes=MAX_FILE_SIZE_BYTES + 1)


def test_unknown_duration_skips_the_duration_check_rather_than_guessing() -> None:
    validate_video_for_reels(duration_sec=None, file_size_bytes=1_000_000)


def test_error_codes_match_the_documented_taxonomy() -> None:
    with pytest.raises(VideoTooLongError) as exc_info:
        validate_video_for_reels(duration_sec=0.5, file_size_bytes=1_000_000)
    assert exc_info.value.code == "VIDEO_TOO_LONG"
    assert exc_info.value.retryable is False

    with pytest.raises(VideoTooLargeError) as exc_info:
        validate_video_for_reels(duration_sec=20.0, file_size_bytes=MAX_FILE_SIZE_BYTES + 1)
    assert exc_info.value.code == "VIDEO_TOO_LARGE"
    assert exc_info.value.retryable is False
