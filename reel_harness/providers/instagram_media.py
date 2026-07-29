from __future__ import annotations

from reel_harness.core.errors import VideoTooLargeError, VideoTooLongError

# Per the official ig-user/media reference (checked 2026-07-29 -- see
# docs/PUBLISHING.md). Unlike TikTok's Content Posting API (whose chunk/
# file-size limits were never confirmed against primary docs -- see
# providers.tiktok_publisher), Instagram's Reels limits ARE documented,
# so this project checks them locally before ever attempting an upload,
# rather than only discovering a rejection from the live API.
MIN_DURATION_SECONDS = 3.0
MAX_DURATION_SECONDS = 15 * 60.0  # 15 minutes
MAX_FILE_SIZE_BYTES = 300 * 1024 * 1024  # 300 MB


def validate_video_for_reels(duration_sec: float | None, file_size_bytes: int) -> None:
    """Raises VideoTooLongError/VideoTooLargeError with a clear reason if
    the video falls outside Instagram's documented Reels limits. Reuses
    facts the render pipeline already validated and persisted onto the
    manifest (ValidationInfo.duration_sec) and the final file's own byte
    size -- never re-runs ffprobe, since this is exactly the same
    duration ffprobe already confirmed at VALIDATING time (see
    core.publish_eligibility). `duration_sec=None` (an older manifest
    schema, or a pinned snapshot predating this field) skips the duration
    check rather than guessing -- the file-size check still applies
    unconditionally since it never depends on ffprobe."""
    if file_size_bytes > MAX_FILE_SIZE_BYTES:
        raise VideoTooLargeError(
            f"final video is {file_size_bytes} bytes, exceeding Instagram's documented "
            f"{MAX_FILE_SIZE_BYTES} byte (300 MB) maximum for Reels"
        )
    if duration_sec is None:
        return
    if duration_sec < MIN_DURATION_SECONDS:
        raise VideoTooLongError(
            f"final video is {duration_sec:.2f}s, below Instagram's documented "
            f"{MIN_DURATION_SECONDS}s minimum for Reels"
        )
    if duration_sec > MAX_DURATION_SECONDS:
        raise VideoTooLongError(
            f"final video is {duration_sec:.2f}s, exceeding Instagram's documented "
            f"{MAX_DURATION_SECONDS:.0f}s (15 minute) maximum for Reels"
        )
