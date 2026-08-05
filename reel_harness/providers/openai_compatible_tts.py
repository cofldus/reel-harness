from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from reel_harness.config import TTS_SPEED_RANGE, TTS_SUPPORTED_FORMATS
from reel_harness.core.errors import (
    DependencyError,
    ProviderAuthError,
    TransientProviderError,
)
from reel_harness.media import tts_audio
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.base import TTSResult

ADAPTER_VERSION = "openai-compat-tts-v1"

# Hard ceiling on a single scene's audio download -- a runaway response is a
# provider fault, not something to buffer without bounds.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class OpenAICompatibleTTSProvider:
    """Adapter for any OpenAI-compatible /audio/speech endpoint.

    Vendor-neutral: the concrete vendor is chosen entirely by configuration
    (tts_base_url + tts_model + tts_voice + tts_api_key); the endpoint shape is
    isolated to this class and the pipeline depends only on the TTS Protocol.

    Safety contract mirrors the LLM adapter: the API key lives only in the
    Authorization header and never appears in exception messages, logs,
    results, or files; response bodies are never logged or persisted (only the
    validated audio itself, a checksum, and the provider request id).

    A 2xx HTTP status is NOT success: the received bytes must pass a container
    signature check, survive normalization to canonical PCM WAV through real
    ffmpeg, and the normalized file must pass the audio acceptance policy
    (duration/sample-rate/channels) before a result is returned. Corrupt,
    empty, oversized, or wrong-content-type bodies received with a 2xx status
    are classified as transient provider faults (bounded retries apply -- each
    retry re-bills the scene; see docs/OPERATIONS.md). The original provider
    container is deleted after successful normalization; only the canonical
    WAV is kept and checksummed.
    """

    provider_id = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        voice: str,
        audio_format: str = "wav",
        speed: float = 1.0,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        max_audio_bytes: int = MAX_AUDIO_BYTES,
        transport: Any = None,
    ) -> None:
        if not base_url:
            raise ValueError("tts_base_url is required for the openai-compatible tts provider")
        if not model:
            raise ValueError("tts_model is required for the openai-compatible tts provider")
        if not voice:
            raise ValueError("tts_voice is required for the openai-compatible tts provider")
        if not api_key:
            raise ValueError("tts_api_key is required for the openai-compatible tts provider")
        if audio_format not in TTS_SUPPORTED_FORMATS:
            raise ValueError(
                f"unsupported tts format {audio_format!r} "
                f"(supported: {', '.join(sorted(TTS_SUPPORTED_FORMATS))})"
            )
        low, high = TTS_SPEED_RANGE
        if not (low <= speed <= high):
            raise ValueError(f"tts speed {speed} outside [{low}, {high}]")
        import httpx

        self.model_id = model
        self.voice_id = voice
        self.audio_format = audio_format
        self.speed = speed
        self.last_latency_ms: float | None = None
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_audio_bytes = max_audio_bytes
        self._httpx = httpx
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        # The CALLER's voice wins when it names one, and the configured
        # voice is the fallback.
        #
        # This used to be the other way round, which was right when every
        # job had one narrator: the pipeline passed a placeholder and the
        # operator's setting pinned it. Fable casts a different voice per
        # character, decided from the adaptation, and under the old
        # precedence every character in a film came out sounding
        # identical -- the exact problem synthesising dialogue was meant
        # to solve.
        #
        # Safe for the short-form path, which passes the provider's own
        # voice_id straight back (see pipeline.stages), so the result
        # there is unchanged either way.
        voice = voice_id or self.voice_id
        started = time.monotonic()
        audio_bytes, request_id = self._fetch(text, voice)
        self.last_latency_ms = (time.monotonic() - started) * 1000

        self._check_signature(audio_bytes)

        deps = check_ffmpeg_available()
        if not deps.ffmpeg_available:
            raise DependencyError("ffmpeg executable not found (required to normalize tts audio)")
        assert deps.ffmpeg.path is not None

        dest_dir.mkdir(parents=True, exist_ok=True)
        source_path = dest_dir / f"source-{uuid.uuid4().hex}.{self.audio_format}"
        output_path = dest_dir / "tts.wav"
        source_path.write_bytes(audio_bytes)
        try:
            result = run(
                tts_audio.normalize_audio_argv(deps.ffmpeg.path, source_path, output_path),
                timeout=60,
            )
            if result.returncode != 0:
                output_path.unlink(missing_ok=True)
                raise TransientProviderError(
                    f"tts audio failed normalization (corrupt or undecodable "
                    f"{self.audio_format}): ffmpeg exit {result.returncode}"
                )
            try:
                info = tts_audio.validate_wav(output_path)
            except ValueError as exc:
                output_path.unlink(missing_ok=True)
                raise TransientProviderError(f"tts audio failed validation: {exc}") from exc
        finally:
            source_path.unlink(missing_ok=True)

        checksum = hashlib.sha256(output_path.read_bytes()).hexdigest()
        return TTSResult(
            audio_path=output_path,
            duration_sec=info.duration_sec,
            provider_id=self.provider_id,
            voice_id=voice,
            checksum_sha256=checksum,
            request_id=request_id,
        )

    def _check_signature(self, data: bytes) -> None:
        if self.audio_format == "wav" and not tts_audio.looks_like_wav(data):
            raise TransientProviderError("tts response is not a WAV container (bad signature)")
        if self.audio_format == "mp3" and not tts_audio.looks_like_mp3(data):
            raise TransientProviderError("tts response is not an MP3 stream (bad signature)")

    def _fetch(self, text: str, voice: str) -> tuple[bytes, str | None]:
        payload = {
            "model": self.model_id,
            "input": text,
            "voice": voice,
            "response_format": self.audio_format,
            "speed": self.speed,
        }
        correlation_id = str(uuid.uuid4())
        attempts = self._max_retries + 1
        last_transient: Exception | None = None
        retry_after: float | None = None
        for attempt in range(attempts):
            if attempt > 0:
                delay = max(self._retry_backoff_seconds * attempt, retry_after or 0.0)
                retry_after = None
                time.sleep(min(delay, 30.0))
            try:
                response = self._client.post(
                    "/audio/speech", json=payload, headers={"X-Request-ID": correlation_id},
                )
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"tts request timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"tts transport error ({type(exc).__name__})")
                continue

            request_id = response.headers.get("x-request-id")
            if response.status_code in (401, 403):
                raise ProviderAuthError(
                    f"tts endpoint rejected the configured credential "
                    f"(HTTP {response.status_code}, request_id={request_id})"
                )
            if response.status_code == 429 or response.status_code >= 500:
                raw_retry_after = response.headers.get("retry-after")
                try:
                    retry_after = max(0.0, float(raw_retry_after)) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                last_transient = TransientProviderError(
                    f"tts endpoint returned HTTP {response.status_code} (request_id={request_id})"
                )
                continue
            if response.status_code != 200:
                raise TransientProviderError(
                    f"tts endpoint returned unexpected HTTP {response.status_code} "
                    f"(request_id={request_id})"
                )

            content_type = response.headers.get("content-type", "").lower()
            if "text/html" in content_type or "application/json" in content_type:
                # An error page or JSON error delivered with a 2xx: never log
                # or persist the body itself.
                last_transient = TransientProviderError(
                    f"tts endpoint returned non-audio content type {content_type!r} "
                    f"(request_id={request_id})"
                )
                continue
            body = response.content
            if not body:
                last_transient = TransientProviderError(
                    f"tts endpoint returned an empty body (request_id={request_id})"
                )
                continue
            if len(body) > self._max_audio_bytes:
                last_transient = TransientProviderError(
                    f"tts response exceeded the {self._max_audio_bytes} byte limit "
                    f"(request_id={request_id})"
                )
                continue
            return body, request_id

        assert last_transient is not None
        raise TransientProviderError(
            f"tts request failed after {attempts} attempts: {last_transient}"
        )
