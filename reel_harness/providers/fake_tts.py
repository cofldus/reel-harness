from __future__ import annotations

import struct
import wave
from pathlib import Path
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import TTSResult

SAMPLE_RATE = 16000

FakeMode = Literal["ok", "timeout"]


class FakeTTSProvider:
    """Writes a deterministic silent WAV file instead of calling a real TTS API.

    Duration is derived from the input text length so that longer voiceover lines
    produce longer clips, which is enough for the render/validate stages to have
    something duration-consistent to work with.
    """

    provider_id = "fake"

    def __init__(self, mode: FakeMode = "ok") -> None:
        self.mode = mode

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        if self.mode == "timeout":
            raise TransientProviderError("fake TTS timed out")

        duration_sec = max(1.5, round(len(text) / 15, 2))
        n_samples = int(SAMPLE_RATE * duration_sec)
        dest_dir.mkdir(parents=True, exist_ok=True)
        audio_path = dest_dir / "tts.wav"
        with wave.open(str(audio_path), "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(struct.pack("<h", 0) * n_samples)
        return TTSResult(
            audio_path=audio_path,
            duration_sec=duration_sec,
            provider_id=self.provider_id,
            voice_id=voice_id,
        )
