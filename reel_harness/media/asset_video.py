from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reel_harness.media.ffprobe_validate import ValidationResult

# Canonical pipeline scene-clip format: what every downloaded stock-video
# asset is normalized to before rendering, mirroring media.tts_audio's
# canonical-WAV policy. Scaling/cropping to the final render resolution still
# happens once, at RENDER time -- exactly as it always has for image assets
# -- so normalization here only fixes container/codec/framerate/audio, never
# resolution (avoiding a second lossy scale pass).
CANONICAL_VIDEO_CODEC = "h264"
CANONICAL_PIX_FMT = "yuv420p"
CANONICAL_FPS = 30


@dataclass(frozen=True)
class AssetVideoPolicy:
    """Acceptance policy for a single downloaded stock-video asset, checked
    against real ffprobe output before normalization is even attempted."""

    min_width: int = 480
    min_height: int = 480
    min_duration_sec: float = 0.5
    max_duration_sec: float = 120.0


DEFAULT_ASSET_VIDEO_POLICY = AssetVideoPolicy()


def validate_asset_video(info: ValidationResult, policy: AssetVideoPolicy | None = None) -> None:
    """Real-video acceptance check on already-parsed ffprobe output. Raises
    ValueError naming every violated condition; returns None on success."""
    policy = policy or DEFAULT_ASSET_VIDEO_POLICY
    failures: list[str] = []
    if info.width < policy.min_width or info.height < policy.min_height:
        failures.append(
            f"resolution {info.width}x{info.height} below minimum "
            f"{policy.min_width}x{policy.min_height}"
        )
    if info.duration_sec <= 0:
        failures.append("duration is zero")
    elif not (policy.min_duration_sec <= info.duration_sec <= policy.max_duration_sec):
        failures.append(
            f"duration {info.duration_sec:.2f}s outside "
            f"[{policy.min_duration_sec}, {policy.max_duration_sec}]s"
        )
    if failures:
        raise ValueError("; ".join(failures))


def normalize_asset_video_argv(
    ffmpeg_path: Path, source_path: Path, output_path: Path, fps: int = CANONICAL_FPS,
) -> list[str]:
    """argv (list form, never a shell string) that strips any audio the
    original stock clip carries (the final render's only audio is the TTS
    track -- see docs/OPERATIONS.md), forces a stable frame rate, and
    transcodes to canonical H.264/yuv420p. ffmpeg's default autorotate
    behavior (on by default since ffmpeg 4.4) resolves any rotation display
    matrix into the output pixels, so no explicit transpose filter is needed.
    ffmpeg failing on the input doubles as the corruption check."""
    return [
        str(ffmpeg_path), "-y",
        "-i", str(source_path),
        "-an",
        "-r", str(fps),
        "-c:v", CANONICAL_VIDEO_CODEC,
        "-pix_fmt", CANONICAL_PIX_FMT,
        str(output_path),
    ]
