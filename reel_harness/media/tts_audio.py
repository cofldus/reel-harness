from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

# Canonical pipeline audio: what every real-TTS result is normalized to before
# rendering, so the ffmpeg render path always consumes the same input shape.
CANONICAL_SAMPLE_RATE = 44100
CANONICAL_CHANNELS = 1
CANONICAL_CODEC = "pcm_s16le"


@dataclass(frozen=True)
class AudioPolicy:
    """Acceptance policy for a single scene's TTS audio."""

    max_duration_sec: float = 120.0
    min_sample_rate: int = 8000
    max_sample_rate: int = 48000
    allowed_channels: tuple[int, ...] = (1, 2)


DEFAULT_AUDIO_POLICY = AudioPolicy()


@dataclass
class WavInfo:
    duration_sec: float
    sample_rate: int
    channels: int


def looks_like_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def looks_like_mp3(data: bytes) -> bool:
    if len(data) < 3:
        return False
    if data[:3] == b"ID3":
        return True
    return data[0] == 0xFF and (data[1] & 0xE0) == 0xE0  # MPEG frame sync


def wav_info(path: Path) -> WavInfo:
    """Parses a WAV header with the standard library; raises wave.Error/EOFError
    on a corrupt or non-WAV file."""
    with wave.open(str(path), "rb") as handle:
        frames = handle.getnframes()
        rate = handle.getframerate()
        channels = handle.getnchannels()
    return WavInfo(
        duration_sec=frames / float(rate) if rate else 0.0,
        sample_rate=rate,
        channels=channels,
    )


def validate_wav(path: Path, policy: AudioPolicy | None = None) -> WavInfo:
    """Real-audio acceptance check: parseable WAV with an audible stream inside
    the policy's duration/sample-rate/channel bounds. Returns the parsed info;
    raises ValueError naming every violated condition."""
    policy = policy or DEFAULT_AUDIO_POLICY
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("audio file is missing or empty")
    try:
        info = wav_info(path)
    except (wave.Error, EOFError, OSError) as exc:
        raise ValueError(f"not a parseable WAV file: {exc}") from exc

    failures: list[str] = []
    if info.duration_sec <= 0:
        failures.append("duration is zero")
    elif info.duration_sec > policy.max_duration_sec:
        failures.append(f"duration {info.duration_sec:.2f}s exceeds {policy.max_duration_sec}s")
    if not (policy.min_sample_rate <= info.sample_rate <= policy.max_sample_rate):
        failures.append(f"sample rate {info.sample_rate} outside "
                        f"[{policy.min_sample_rate}, {policy.max_sample_rate}]")
    if info.channels not in policy.allowed_channels:
        failures.append(f"channel count {info.channels} not in {policy.allowed_channels}")
    if failures:
        raise ValueError("; ".join(failures))
    return info


def normalize_audio_argv(
    ffmpeg_path: Path, source_path: Path, output_path: Path,
    sample_rate: int = CANONICAL_SAMPLE_RATE, channels: int = CANONICAL_CHANNELS,
) -> list[str]:
    """argv (list form, never a shell string) converting any provider audio to
    canonical PCM WAV. ffmpeg failing on the input doubles as the corruption
    check for non-WAV containers."""
    return [
        str(ffmpeg_path), "-y",
        "-i", str(source_path),
        "-vn",
        "-ac", str(channels),
        "-ar", str(sample_rate),
        "-c:a", CANONICAL_CODEC,
        str(output_path),
    ]
