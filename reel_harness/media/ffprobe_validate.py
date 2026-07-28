from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ValidationResult:
    width: int
    height: int
    duration_sec: float
    video_codec: str
    has_audio_stream: bool
    audio_codec: str | None


@dataclass(frozen=True)
class ValidationPolicy:
    """Production-safe defaults: every pipeline render (including the fast
    360x640 fake-provider path) must be H.264/AAC with an audio stream, a sane
    short-form duration, and a faststart-ordered mp4. Loosen per call site only
    with an explicit policy object."""

    video_codec: str = "h264"
    audio_codec: str = "aac"
    min_duration_sec: float = 3.0
    max_duration_sec: float = 180.0
    require_faststart: bool = True


DEFAULT_VALIDATION_POLICY = ValidationPolicy()


def has_faststart(video_path: Path) -> bool:
    """True if the moov atom precedes mdat -- the file-level signature of
    `-movflags +faststart` actually taking effect (playback can start before
    the whole file downloads)."""
    data = video_path.read_bytes()
    moov_pos = data.find(b"moov")
    mdat_pos = data.find(b"mdat")
    return moov_pos != -1 and mdat_pos != -1 and moov_pos < mdat_pos


def check_against_policy(
    validation: ValidationResult, expected_width: int, expected_height: int, policy: ValidationPolicy,
) -> list[str]:
    """Returns a structured list of failed conditions (empty = pass). Callers
    join these into the TECHNICAL_VALIDATION_FAILED summary so the operator
    sees every violated condition, not just the first."""
    failures: list[str] = []
    if (validation.width, validation.height) != (expected_width, expected_height):
        failures.append(
            f"resolution {validation.width}x{validation.height}, "
            f"expected {expected_width}x{expected_height}"
        )
    if validation.video_codec != policy.video_codec:
        failures.append(f"video codec {validation.video_codec!r}, expected {policy.video_codec!r}")
    if not validation.has_audio_stream:
        failures.append("no audio stream")
    elif validation.audio_codec != policy.audio_codec:
        failures.append(f"audio codec {validation.audio_codec!r}, expected {policy.audio_codec!r}")
    if not (policy.min_duration_sec <= validation.duration_sec <= policy.max_duration_sec):
        failures.append(
            f"duration {validation.duration_sec:.3f}s outside "
            f"[{policy.min_duration_sec}, {policy.max_duration_sec}]s"
        )
    return failures


def build_ffprobe_argv(ffprobe_path: Path, video_path: Path) -> list[str]:
    return [
        str(ffprobe_path), "-v", "error",
        "-print_format", "json",
        "-show_format", "-show_streams",
        str(video_path),
    ]


def parse_ffprobe_output(stdout: str) -> ValidationResult:
    payload = json.loads(stdout)
    streams = payload.get("streams", [])
    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if video_stream is None:
        raise ValueError("no video stream found in ffprobe output")
    return ValidationResult(
        width=int(video_stream["width"]),
        height=int(video_stream["height"]),
        duration_sec=float(payload["format"]["duration"]),
        video_codec=video_stream["codec_name"],
        has_audio_stream=audio_stream is not None,
        audio_codec=audio_stream["codec_name"] if audio_stream else None,
    )
