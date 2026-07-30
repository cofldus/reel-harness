from __future__ import annotations

import wave

import pytest

from reel_harness.core.errors import DependencyError
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.demo_tts import DemoTTSProvider, check_demo_tts_available

_DEMO_TTS_STATUS = check_demo_tts_available()
_FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(
    not (_DEMO_TTS_STATUS.available and _FFMPEG_PRESENT),
    reason=f"requires a local TTS engine and ffmpeg: {_DEMO_TTS_STATUS.detail}",
)


def _wav_is_silent(path) -> bool:
    with wave.open(str(path), "rb") as handle:
        frames = handle.readframes(handle.getnframes())
    return frames == b"\x00" * len(frames)


def test_demo_tts_produces_real_non_silent_audio(tmp_path) -> None:
    """Unlike FakeTTSProvider's deliberately silent WAV, Demo Mode's whole
    point is real, audible speech -- confirm the produced audio actually has
    non-zero signal, not just a technically-valid WAV container."""
    provider = DemoTTSProvider()
    result = provider.synthesize(
        "Hello, this is a demo mode text to speech test.", voice_id="v", lang="en-US", dest_dir=tmp_path,
    )
    assert result.provider_id == "demo"
    assert result.audio_path.is_file()
    assert result.duration_sec > 0
    assert result.checksum_sha256 is not None
    assert not _wav_is_silent(result.audio_path)


def test_demo_tts_selects_a_matching_voice_when_available(tmp_path) -> None:
    provider = DemoTTSProvider()
    result_en = provider.synthesize("Hello there.", voice_id="v", lang="en-US", dest_dir=tmp_path / "en")
    result_ko = provider.synthesize("안녕하세요.", voice_id="v", lang="ko-KR", dest_dir=tmp_path / "ko")
    # Voice selection is best-effort (may fall back to the default voice if
    # no matching language is installed) -- this only asserts both calls
    # succeed and report SOME voice, not that they differ.
    assert result_en.voice_id
    assert result_ko.voice_id


def test_demo_tts_duration_scales_with_text_length(tmp_path) -> None:
    provider = DemoTTSProvider()
    short = provider.synthesize("Hi.", voice_id="v", lang="en-US", dest_dir=tmp_path / "short")
    long_text = "This is a much longer piece of narration text than the very short one before it."
    long = provider.synthesize(long_text, voice_id="v", lang="en-US", dest_dir=tmp_path / "long")
    assert long.duration_sec > short.duration_sec


def test_demo_tts_missing_engine_raises_dependency_error(tmp_path, monkeypatch) -> None:
    """Mirrors ffmpeg-missing's BLOCKED_DEPENDENCY handling -- a demo job
    must fail the same documented way when the local engine truly isn't
    available, never silently degrade."""
    class _BrokenModule:
        @staticmethod
        def init():
            raise RuntimeError("no speech driver for this platform")

    original_import = __import__

    def _fake_import(name, *args, **kwargs):
        if name == "pyttsx3":
            return _BrokenModule()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", _fake_import)
    provider = DemoTTSProvider()
    with pytest.raises(DependencyError):
        provider.synthesize("text", voice_id="v", lang="en-US", dest_dir=tmp_path)
