from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from pathlib import Path

from reel_harness.core.errors import DependencyError, TransientProviderError
from reel_harness.media import tts_audio
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.base import TTSResult


@dataclass
class DemoTTSStatus:
    available: bool
    detail: str


def check_demo_tts_available() -> DemoTTSStatus:
    """Read-only probe: can a local pyttsx3 engine actually be constructed on
    this machine? Never cached -- mirrors media.deps.check_ffmpeg_available's
    "always re-check, never assume it was there before" philosophy. Used by
    ops.preflight (as a WARN, not a FAIL -- Demo Mode is opt-in, unlike
    ffmpeg) and by tests to skip when no local TTS backend is installed."""
    try:
        import pyttsx3
    except ImportError:
        return DemoTTSStatus(False, "pyttsx3 is not installed (uv sync --extra demo)")
    try:
        engine = pyttsx3.init()
        voice_count = len(engine.getProperty("voices") or [])
        engine.stop()
    except Exception as exc:  # noqa: BLE001 - pyttsx3's failure shape varies by platform driver
        return DemoTTSStatus(False, f"local text-to-speech engine unavailable: {exc}")
    return DemoTTSStatus(True, f"{voice_count} local voice(s) available")


def _select_voice_id(engine, lang: str) -> str | None:
    """Best-effort match against the engine's installed voices by language
    prefix (e.g. "ko" for "ko-KR"). A local engine may simply not have a
    voice for the requested language installed (most default Windows/Linux
    installs only ship English) -- that is a real, documented limitation of
    Demo Mode, not something this function papers over: it falls back to the
    engine's own default voice rather than guessing or failing.

    Deliberately never matches against `voice.id`: on Windows/SAPI5 it's an
    opaque registry path (`...\\Voices\\Tokens\\TTS_MS_KO-KR_HEAMI_11.0`)
    whose fixed "Tokens" path segment spuriously contains "en", which would
    make every voice look like an English match regardless of its actual
    language -- found by actually running this against real installed
    voices, not assumed."""
    lang_prefix = (lang or "").split("-")[0].lower()
    if not lang_prefix:
        return None
    for voice in engine.getProperty("voices") or []:
        for language in voice.languages or []:
            code = language.decode("utf-8", "ignore") if isinstance(language, bytes) else str(language)
            if code.split("-")[0].lower() == lang_prefix:
                return voice.id
        if lang_prefix in (voice.name or "").lower():
            return voice.id
    return None


class DemoTTSProvider:
    """Demo Mode's TTS provider: real, audible speech synthesized entirely
    offline via pyttsx3 (SAPI5 on Windows, espeak/espeak-ng on Linux) -- no
    network call, no API key, unlike FakeTTSProvider's silent WAV. If the
    local engine is unavailable (pyttsx3 not installed, or its platform
    driver can't initialize -- e.g. Linux with no espeak-ng package), this
    raises DependencyError, the exact class already used when ffmpeg itself
    is missing (see pipeline.stages, providers.openai_compatible_tts) -- a
    demo job fails the same documented BLOCKED_DEPENDENCY way, never
    silently degrading to something else."""

    provider_id = "demo"

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        try:
            import pyttsx3
        except ImportError as exc:
            raise DependencyError(
                "pyttsx3 is not installed -- install the 'demo' extra "
                "(uv sync --extra demo) to use the demo tts provider"
            ) from exc
        try:
            engine = pyttsx3.init()
        except Exception as exc:  # noqa: BLE001 - pyttsx3's failure shape varies by platform driver
            raise DependencyError(
                f"local text-to-speech engine unavailable ({exc}) -- on Linux this usually "
                "means espeak/espeak-ng is not installed"
            ) from exc

        resolved_voice = _select_voice_id(engine, lang) or voice_id
        if resolved_voice:
            try:
                engine.setProperty("voice", resolved_voice)
            except Exception:  # noqa: BLE001 - fall back to the engine's default voice
                pass

        dest_dir.mkdir(parents=True, exist_ok=True)
        raw_path = dest_dir / f"source-{uuid.uuid4().hex}.wav"
        try:
            engine.save_to_file(text, str(raw_path))
            engine.runAndWait()
        finally:
            engine.stop()

        deps = check_ffmpeg_available()
        if not deps.ffmpeg_available:
            raw_path.unlink(missing_ok=True)
            raise DependencyError("ffmpeg executable not found (required to normalize demo tts audio)")
        assert deps.ffmpeg.path is not None

        output_path = dest_dir / "tts.wav"
        try:
            result = run(
                tts_audio.normalize_audio_argv(deps.ffmpeg.path, raw_path, output_path),
                timeout=60,
            )
            if result.returncode != 0:
                output_path.unlink(missing_ok=True)
                raise TransientProviderError(
                    f"demo tts audio failed normalization: ffmpeg exit {result.returncode}"
                )
            try:
                info = tts_audio.validate_wav(output_path)
            except ValueError as exc:
                output_path.unlink(missing_ok=True)
                raise TransientProviderError(f"demo tts audio failed validation: {exc}") from exc
        finally:
            raw_path.unlink(missing_ok=True)

        checksum = hashlib.sha256(output_path.read_bytes()).hexdigest()
        return TTSResult(
            audio_path=output_path,
            duration_sec=info.duration_sec,
            provider_id=self.provider_id,
            voice_id=resolved_voice or "demo-default-voice",
            checksum_sha256=checksum,
        )
