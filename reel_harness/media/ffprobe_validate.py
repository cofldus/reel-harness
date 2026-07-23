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
