"""Hybrid pipeline with BOTH real adapters over contract MockTransports (no
external network -- wiring coverage, not a live E2E): OpenAI-compatible LLM ->
policy -> fake assets -> OpenAI-compatible TTS (real validation/normalization
through real ffmpeg) -> real render -> real ffprobe -> REVIEW_REQUIRED.
"""
from __future__ import annotations

import hashlib
import io
import json
import wave

import httpx

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job, StageRun
from reel_harness.manifest.schema import Manifest, is_publish_eligible
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider
from reel_harness.providers.openai_compatible_tts import OpenAICompatibleTTSProvider
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available

FAKE_LLM_KEY = "FAKE-HYBRID-LLM-KEY-000000000000"
FAKE_TTS_KEY = "FAKE-HYBRID-TTS-KEY-000000000000"

SCRIPT_JSON = {
    "title": "3 fridge mistakes",
    "scenes": [
        {
            "voiceover": f"Hybrid tts scene {i} voiceover.",
            "subtitle": f"Scene {i}",
            "visual_query": f"fridge {i}",
            "duration_hint_sec": 4.0,
        }
        for i in range(3)
    ],
}


def _wav_bytes(duration_sec: float = 1.2) -> bytes:
    # 3 scenes x 1.2s keeps the final video inside the production validation
    # policy's minimum duration.
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * duration_sec))
    return buf.getvalue()


def _bundle(tts_calls: list | None = None) -> ProviderBundle:
    def llm_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "llm-req-1",
                "choices": [{"message": {"role": "assistant", "content": json.dumps(SCRIPT_JSON)}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            },
            headers={"x-request-id": "llm-req-1"},
        )

    def tts_handler(request: httpx.Request) -> httpx.Response:
        if tts_calls is not None:
            tts_calls.append(json.loads(request.content))
        return httpx.Response(
            200, content=_wav_bytes(),
            headers={"content-type": "audio/wav", "x-request-id": "tts-req-1"},
        )

    llm = OpenAICompatibleLLMProvider(
        base_url="https://llm.example.invalid/v1", model="hybrid-llm", api_key=FAKE_LLM_KEY,
        max_retries=0, retry_backoff_seconds=0.0, transport=httpx.MockTransport(llm_handler),
    )
    tts = OpenAICompatibleTTSProvider(
        base_url="https://tts.example.invalid/v1", model="hybrid-tts", api_key=FAKE_TTS_KEY,
        voice="hybrid-voice", audio_format="wav",
        max_retries=0, retry_backoff_seconds=0.0, transport=httpx.MockTransport(tts_handler),
    )
    return ProviderBundle(llm=llm, tts=tts, stock_media=FakeStockMediaProvider())


def _stage_attempts(session, job_id) -> dict[str, list[int]]:
    from sqlalchemy import select

    rows = session.execute(
        select(StageRun.stage, StageRun.attempt).where(StageRun.job_id == job_id)
        .order_by(StageRun.started_at),
    ).all()
    grouped: dict[str, list[int]] = {}
    for stage, attempt in rows:
        grouped.setdefault(stage, []).append(attempt)
    return grouped


def test_hybrid_real_llm_and_tts_reach_review_with_full_metadata(
    job_service, channel, session_factory, storage,
) -> None:
    tts_calls: list = []
    job, _ = job_service.create_job(channel.id, idempotency_key="hyb-tts-1", topic="fridge tips")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tts_calls), storage)

        if not FFMPEG_PRESENT:
            # Without ffmpeg the real TTS adapter cannot normalize: honest
            # BLOCKED_DEPENDENCY at the TTS stage, nothing synthesized as success.
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"
            return
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value

    assert all(call["voice"] == "hybrid-voice" for call in tts_calls)
    assert len(tts_calls) == 3

    manifest_raw = storage.read_bytes(job.id, "manifest.json")
    for key in (FAKE_LLM_KEY, FAKE_TTS_KEY):
        assert key.encode() not in manifest_raw
    manifest = Manifest.model_validate_json(manifest_raw)
    assert manifest.llm.provider_id == "openai-compatible"
    assert manifest.llm.model_id == "hybrid-llm"
    assert manifest.tts.provider_id == "openai-compatible"
    assert manifest.tts.model_id == "hybrid-tts"
    assert manifest.tts.voice_id == "hybrid-voice"
    assert manifest.validation.video_codec == "h264"
    assert manifest.validation.audio_codec == "aac"
    assert manifest.validation.has_audio_stream is True
    assert manifest.final_video_checksum_sha256 is not None
    assert manifest.approval.decision is None

    # Per-scene TTS checksums describe the actual normalized files on disk.
    assert len(manifest.tts_audio) == 3
    for entry in manifest.tts_audio:
        scene_wav = storage.job_dir(job.id) / "tts" / f"scene_{entry.scene_index}" / "tts.wav"
        assert hashlib.sha256(scene_wav.read_bytes()).hexdigest() == entry.checksum_sha256
        assert entry.duration_sec and entry.duration_sec > 0

    assert is_publish_eligible(manifest) is False  # fake asset licenses stay non-publishable


def test_reject_from_tts_reruns_tts_and_reject_from_render_does_not(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="hyb-tts-2", topic="fridge tips")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(), storage)
        if db_job.status != JobStatus.REVIEW_REQUIRED.value:
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"
            return

    # Reject back to TTS: TTS/RENDER/VALIDATE re-run, earlier stages do not,
    # and the same pinned voice keeps being used through the retry.
    tts_calls: list = []
    job_service.reject(job.id, reason="voice pacing", regenerate_from_stage="TTS")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tts_calls), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session, job.id)
        assert attempts["TTS"] == [1, 2]
        assert attempts["SCRIPT"] == [1]
        assert attempts["ASSET"] == [1]
    assert all(call["voice"] == "hybrid-voice" for call in tts_calls)

    # Reject back to RENDER: TTS must NOT re-run (no new tts requests at all).
    render_calls: list = []
    job_service.reject(job.id, reason="colors", regenerate_from_stage="RENDER")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(render_calls), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session, job.id)
        assert attempts["TTS"] == [1, 2], "RENDER reject must not re-run TTS"
        assert attempts["RENDER"] == [1, 2, 3]
    assert render_calls == [], "no TTS request may be made when resuming from RENDER"

    manifest = Manifest.model_validate_json(storage.read_bytes(job.id, "manifest.json"))
    assert manifest.tts.voice_id == "hybrid-voice"
    assert len(manifest.tts_audio) == 3
