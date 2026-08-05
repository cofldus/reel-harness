"""Contract tests for the OpenAI-compatible TTS adapter (MockTransport -- no
sockets, coexists with the network-block fixture; NOT a live TTS E2E).
All keys are obviously-fake placeholders.
"""
from __future__ import annotations

import io
import wave
from pathlib import Path

import httpx
import pytest

from reel_harness.core.errors import DependencyError, ProviderAuthError, TransientProviderError
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.tts_audio import CANONICAL_CHANNELS, CANONICAL_SAMPLE_RATE, wav_info
from reel_harness.providers.openai_compatible_tts import OpenAICompatibleTTSProvider

FFMPEG_PRESENT = check_ffmpeg_available().all_available

FAKE_KEY = "FAKE-TTS-ADAPTER-KEY-000000000000"


def _wav_bytes(duration_sec: float = 0.4, rate: int = 16000) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\x00\x00" * int(rate * duration_sec))
    return buf.getvalue()


def _provider(handler, **overrides) -> OpenAICompatibleTTSProvider:
    defaults = dict(
        base_url="https://tts.example.invalid/v1",
        model="tts-test-model",
        api_key=FAKE_KEY,
        voice="test-voice",
        audio_format="wav",
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return OpenAICompatibleTTSProvider(transport=httpx.MockTransport(handler), **defaults)


def _audio_response(body: bytes, content_type: str = "audio/wav", request_id: str = "tts-req-1"):
    return httpx.Response(
        200, content=body, headers={"content-type": content_type, "x-request-id": request_id},
    )


def _synthesize(provider, tmp_path: Path):
    # Empty, so the configured voice is what gets used. The caller's
    # voice now wins when it names one -- Fable casts a voice per
    # character and the operator's single setting must not override it.
    return provider.synthesize("hello from the contract test", "", "en", tmp_path / "scene_0")


def test_successful_wav_synthesis_normalizes_validates_and_checksums(tmp_path) -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["correlation"] = request.headers.get("x-request-id")
        import json

        seen["payload"] = json.loads(request.content)
        return _audio_response(_wav_bytes())

    provider = _provider(handler)
    if not FFMPEG_PRESENT:
        with pytest.raises(DependencyError):
            _synthesize(provider, tmp_path)
        return

    result = _synthesize(provider, tmp_path)
    assert result.provider_id == "openai-compatible"
    assert result.voice_id == "test-voice", "configured voice is the fallback when none is named"
    assert result.request_id == "tts-req-1"
    assert result.duration_sec > 0
    assert result.audio_path.name == "tts.wav"

    import hashlib

    assert result.checksum_sha256 == hashlib.sha256(result.audio_path.read_bytes()).hexdigest()
    info = wav_info(result.audio_path)
    assert info.sample_rate == CANONICAL_SAMPLE_RATE
    assert info.channels == CANONICAL_CHANNELS
    # Original provider container is deleted after normalization; only tts.wav remains.
    leftovers = [p.name for p in result.audio_path.parent.iterdir() if p.name != "tts.wav"]
    assert leftovers == []
    assert seen["auth"] == f"Bearer {FAKE_KEY}"
    assert seen["correlation"]
    assert seen["payload"]["voice"] == "test-voice"
    assert seen["payload"]["response_format"] == "wav"


def test_successful_mp3_synthesis_is_normalized_to_canonical_wav(tmp_path) -> None:
    if not FFMPEG_PRESENT:
        # Honest branch: without ffmpeg no mp3 fixture can exist and the
        # adapter must fail with the explicit dependency error.
        def blocked_handler(request: httpx.Request) -> httpx.Response:
            return _audio_response(b"ID3" + b"\x00" * 64, content_type="audio/mpeg")

        with pytest.raises(DependencyError):
            _synthesize(_provider(blocked_handler, audio_format="mp3"), tmp_path)
        return
    # Encode a real mp3 with the real toolchain so the adapter decodes real data.
    deps = check_ffmpeg_available()
    src_wav = tmp_path / "src.wav"
    src_wav.write_bytes(_wav_bytes())
    mp3_path = tmp_path / "src.mp3"
    from reel_harness.media.runner import run

    encode = run([str(deps.ffmpeg.path), "-y", "-i", str(src_wav), str(mp3_path)], timeout=30)
    assert encode.returncode == 0
    mp3_bytes = mp3_path.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return _audio_response(mp3_bytes, content_type="audio/mpeg")

    result = _synthesize(_provider(handler, audio_format="mp3"), tmp_path)
    info = wav_info(result.audio_path)
    assert info.sample_rate == CANONICAL_SAMPLE_RATE
    assert result.duration_sec > 0


@pytest.mark.parametrize("status", [401, 403])
def test_auth_errors_are_non_retryable_and_leak_no_key(tmp_path, status) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(status, json={"error": "bad key"})

    with pytest.raises(ProviderAuthError) as excinfo:
        _synthesize(_provider(handler), tmp_path)
    assert calls["n"] == 1
    assert FAKE_KEY not in str(excinfo.value)


def test_429_with_retry_after_then_success(tmp_path) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return _audio_response(_wav_bytes())

    provider = _provider(handler)
    if FFMPEG_PRESENT:
        _synthesize(provider, tmp_path)
    else:
        with pytest.raises(DependencyError):
            _synthesize(provider, tmp_path)
    assert calls["n"] == 2


def test_server_error_then_success_and_exhaustion_counts(tmp_path) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(502)
        return _audio_response(_wav_bytes())

    provider = _provider(flaky)
    if FFMPEG_PRESENT:
        _synthesize(provider, tmp_path)
        assert calls["n"] == 2

    always = {"n": 0}

    def broken(request: httpx.Request) -> httpx.Response:
        always["n"] += 1
        return httpx.Response(503)

    with pytest.raises(TransientProviderError):
        _synthesize(_provider(broken), tmp_path / "b")
    assert always["n"] == 3  # max_retries=2 -> 3 attempts


def test_timeout_maps_to_transient(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated")

    with pytest.raises(TransientProviderError):
        _synthesize(_provider(handler), tmp_path)


def test_empty_body_and_oversized_body_are_transient(tmp_path) -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return _audio_response(b"")

    with pytest.raises(TransientProviderError, match="empty body|failed after"):
        _synthesize(_provider(empty), tmp_path)

    def huge(request: httpx.Request) -> httpx.Response:
        return _audio_response(_wav_bytes())

    with pytest.raises(TransientProviderError, match="byte limit|failed after"):
        _synthesize(_provider(huge, max_audio_bytes=16), tmp_path / "b")


def test_corrupt_audio_signature_is_rejected(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _audio_response(b"\x00\x01this is not audio at all")

    with pytest.raises(TransientProviderError, match="signature"):
        _synthesize(_provider(handler), tmp_path)


def test_json_error_body_and_html_page_with_200_are_rejected_without_body_leak(tmp_path) -> None:
    def json_handler(request: httpx.Request) -> httpx.Response:
        return _audio_response(b'{"error": "secret-detail-must-not-leak"}', content_type="application/json")

    with pytest.raises(TransientProviderError) as excinfo:
        _synthesize(_provider(json_handler), tmp_path)
    assert "secret-detail-must-not-leak" not in str(excinfo.value)

    def html_handler(request: httpx.Request) -> httpx.Response:
        return _audio_response(b"<html>gateway error</html>", content_type="text/html")

    with pytest.raises(TransientProviderError) as excinfo2:
        _synthesize(_provider(html_handler), tmp_path / "b")
    assert "gateway error" not in str(excinfo2.value)


def test_configuration_is_validated_up_front() -> None:
    base = dict(base_url="https://x.invalid", model="m", api_key=FAKE_KEY, voice="v")
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**{**base, "base_url": ""})
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**{**base, "model": ""})
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**{**base, "voice": ""})
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**{**base, "api_key": ""})
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**base, audio_format="ogg")
    with pytest.raises(ValueError):
        OpenAICompatibleTTSProvider(**base, speed=9.0)


def test_no_file_written_on_synthesis_ever_contains_the_key(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _audio_response(_wav_bytes())

    provider = _provider(handler)
    if not FFMPEG_PRESENT:
        return
    _synthesize(provider, tmp_path)
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert FAKE_KEY.encode() not in path.read_bytes()


def _capture_voice(seen: dict):
    def handler(request):
        import json as _json

        seen.update(_json.loads(request.content))
        return _audio_response(_wav_bytes())

    return handler


def test_the_caller_s_voice_wins_over_the_configured_default(tmp_path) -> None:
    """Fable casts a voice per character. Under the old precedence the
    operator's single configured voice overrode all of them and every
    character in a film sounded identical -- the exact problem that
    synthesising dialogue was meant to solve."""
    seen: dict = {}
    provider = _provider(_capture_voice(seen), voice="onyx")
    provider.synthesize("대사", "nova", "ko", tmp_path / "a")
    assert seen["voice"] == "nova"


def test_the_configured_voice_is_the_fallback_not_the_override(tmp_path) -> None:
    seen: dict = {}
    provider = _provider(_capture_voice(seen), voice="onyx")
    provider.synthesize("대사", "", "ko", tmp_path / "b")
    assert seen["voice"] == "onyx"
