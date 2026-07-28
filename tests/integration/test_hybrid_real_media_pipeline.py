"""Full hybrid pipeline with all three real adapters over contract
MockTransports (no external network -- wiring coverage, not a live E2E):
OpenAI-compatible LLM -> policy -> real Pexels-contract stock-media search/
download/validation/normalization -> OpenAI-compatible TTS (real validation/
normalization) -> real render (video-asset path) -> real ffprobe ->
REVIEW_REQUIRED.
"""
from __future__ import annotations

import hashlib
import io
import json
import wave
from pathlib import Path

import httpx

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Asset, Job, StageRun
from reel_harness.manifest.schema import Manifest, is_publish_eligible
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider
from reel_harness.providers.openai_compatible_tts import OpenAICompatibleTTSProvider
from reel_harness.providers.pexels_stock_media import PexelsStockMediaProvider
from reel_harness.worker.runner import ProviderBundle, run_job

DEPS = check_ffmpeg_available()
FFMPEG_PRESENT = DEPS.all_available

FAKE_LLM_KEY = "FAKE-HYBRID-MEDIA-LLM-KEY-00000000"
FAKE_TTS_KEY = "FAKE-HYBRID-MEDIA-TTS-KEY-00000000"
FAKE_ASSET_KEY = "FAKE-HYBRID-MEDIA-ASSET-KEY-000000"

SCRIPT_JSON = {
    "title": "3 ocean facts",
    "scenes": [
        {
            "voiceover": f"Hybrid media scene {i} voiceover.",
            "subtitle": f"Scene {i}",
            "visual_query": f"ocean waves {i}",
            "duration_hint_sec": 4.0,
        }
        for i in range(3)
    ],
}


def _wav_bytes(duration_sec: float = 1.2) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\x00\x00" * int(16000 * duration_sec))
    return buf.getvalue()


def _mp4_bytes(tmp_path: Path, seed: int) -> bytes:
    """A real, ffprobe-parseable portrait MP4 built with the project's own
    ffmpeg (there is no stdlib equivalent to `wave` for video)."""
    out = tmp_path / f"stock-source-{seed}.mp4"
    argv = [
        str(DEPS.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=640x1136:rate=25",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _videos_page(scene_index: int) -> dict:
    vid = 9000 + scene_index
    return {
        "page": 1, "per_page": 15,
        "videos": [{
            "id": vid, "width": 1080, "height": 1920, "duration": 8,
            "url": f"https://www.pexels.com/video/ocean-waves-{vid}/",
            "user": {"id": 1, "name": "Ocean Films", "url": "https://www.pexels.com/@ocean-films"},
            "video_files": [{
                "id": vid, "quality": "hd", "file_type": "video/mp4", "width": 1080, "height": 1920,
                "fps": 25.0, "link": f"https://videos.pexels.com/video-files/{vid}/{vid}-hd.mp4",
            }],
        }],
    }


def _bundle(
    tmp_path: Path, tts_calls: list | None = None, asset_search_calls: list | None = None,
) -> ProviderBundle:
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
            200, content=_wav_bytes(), headers={"content-type": "audio/wav", "x-request-id": "tts-req-1"},
        )

    # One real video body per possible scene id, generated once and reused
    # across every search/download call this test makes (fresh ffmpeg encodes
    # for every scene index keep it deterministic and avoids inter-test state).
    video_bodies = {9000 + i: _mp4_bytes(tmp_path, i) for i in range(3)}

    def asset_router(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "api.pexels.com" in url:
            if asset_search_calls is not None:
                asset_search_calls.append(url)
            query = httpx.QueryParams(request.url.query)
            scene_index = int(query.get("query", "ocean waves 0").rsplit(" ", 1)[-1])
            return httpx.Response(200, json=_videos_page(scene_index), headers={"x-request-id": "asset-req-1"})
        # Download host (videos.pexels.com/video-files/{id}/...).
        vid = int(url.rsplit("/", 2)[-2])
        return httpx.Response(200, content=video_bodies[vid], headers={"content-type": "video/mp4"})

    llm = OpenAICompatibleLLMProvider(
        base_url="https://llm.example.invalid/v1", model="hybrid-media-llm", api_key=FAKE_LLM_KEY,
        max_retries=0, retry_backoff_seconds=0.0, transport=httpx.MockTransport(llm_handler),
    )
    tts = OpenAICompatibleTTSProvider(
        base_url="https://tts.example.invalid/v1", model="hybrid-media-tts", api_key=FAKE_TTS_KEY,
        voice="hybrid-media-voice", audio_format="wav",
        max_retries=0, retry_backoff_seconds=0.0, transport=httpx.MockTransport(tts_handler),
    )
    stock_media = PexelsStockMediaProvider(
        api_key=FAKE_ASSET_KEY, max_retries=0, retry_backoff_seconds=0.0,
        transport=httpx.MockTransport(asset_router),
    )
    return ProviderBundle(llm=llm, tts=tts, stock_media=stock_media)


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


def test_full_hybrid_media_pipeline_reaches_review_with_real_license_metadata(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    tts_calls: list = []
    asset_calls: list = []
    job, _ = job_service.create_job(channel.id, idempotency_key="hyb-media-1", topic="ocean facts")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path, tts_calls, asset_calls), storage)

        if not FFMPEG_PRESENT:
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"
            return
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value

    assert len(asset_calls) == 3  # one search per scene
    assert len(tts_calls) == 3

    manifest_raw = storage.read_bytes(job.id, "manifest.json")
    for key in (FAKE_LLM_KEY, FAKE_TTS_KEY, FAKE_ASSET_KEY):
        assert key.encode() not in manifest_raw
    # The CDN file-download link is used only transiently to fetch bytes and
    # must never reach the manifest -- only the stable page URL does.
    assert b"videos.pexels.com" not in manifest_raw

    manifest = Manifest.model_validate_json(manifest_raw)
    assert manifest.llm.provider_id == "openai-compatible"
    assert manifest.tts.provider_id == "openai-compatible"
    assert manifest.validation.video_codec == "h264"
    assert manifest.validation.audio_codec == "aac"
    assert manifest.validation.has_audio_stream is True
    assert manifest.final_video_checksum_sha256 is not None
    assert manifest.approval.decision is None

    assert len(manifest.assets) == 3
    for asset_info in manifest.assets:
        assert asset_info.license_type == "PEXELS_LICENSE"
        assert asset_info.commercial_use_allowed is True
        assert asset_info.modification_allowed is True
        assert asset_info.attribution_text and "Ocean Films" in asset_info.attribution_text
        assert asset_info.width and asset_info.height

    # Real per-scene asset checksums describe the actual normalized files.
    with session_factory() as session:
        rows = session.execute(
            Asset.__table__.select().where(Asset.job_id == job.id, Asset.is_current.is_(True))
            .order_by(Asset.scene_index),
        ).fetchall()
    assert len(rows) == 3
    for row in rows:
        on_disk = Path(row.local_path)
        assert on_disk.is_file()
        assert hashlib.sha256(on_disk.read_bytes()).hexdigest() == row.checksum_sha256
        assert row.mime_type == "video/mp4"

    # Not publish-eligible before approval; eligible once approved, because
    # every field is real (unlike a fake-license job).
    assert is_publish_eligible(manifest) is False
    approved = job_service.approve(job.id)
    assert approved.status == JobStatus.COMPLETED.value
    approved_manifest = Manifest.model_validate_json(storage.read_bytes(job.id, "manifest.json"))
    assert is_publish_eligible(approved_manifest) is True


def test_reject_from_asset_reruns_search_reject_from_tts_does_not(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="hyb-media-2", topic="ocean facts")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path), storage)
        if db_job.status != JobStatus.REVIEW_REQUIRED.value:
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"
            return

    # Reject back to ASSET: ASSET/TTS/RENDER/VALIDATE re-run; SCRIPT does not.
    asset_calls: list = []
    job_service.reject(job.id, reason="wrong footage", regenerate_from_stage="ASSET")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path, asset_search_calls=asset_calls), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session, job.id)
        assert attempts["ASSET"] == [1, 2]
        assert attempts["SCRIPT"] == [1]
    assert len(asset_calls) == 3

    # Reject back to TTS: ASSET must NOT re-run (no new search calls at all).
    asset_calls_2: list = []
    job_service.reject(job.id, reason="pacing", regenerate_from_stage="TTS")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path, asset_search_calls=asset_calls_2), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session, job.id)
        assert attempts["ASSET"] == [1, 2], "TTS reject must not re-run ASSET"
        # TTS attempt 2 already happened as part of the ASSET-triggered
        # resume above (ASSET -> TTS -> RENDER -> VALIDATE); this reject
        # triggers attempt 3.
        assert attempts["TTS"] == [1, 2, 3]
    assert asset_calls_2 == [], "no asset search may happen when resuming from TTS"

    # Asset provenance history: both ASSET attempts are still on record.
    with session_factory() as session:
        all_rows = session.execute(
            Asset.__table__.select().where(Asset.job_id == job.id),
        ).fetchall()
    assert len(all_rows) == 6
    assert sum(1 for r in all_rows if r.is_current) == 3


def test_reject_from_render_calls_no_provider_at_all(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="hyb-media-3", topic="ocean facts")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path), storage)
        if db_job.status != JobStatus.REVIEW_REQUIRED.value:
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"
            return

    tts_calls: list = []
    asset_calls: list = []
    job_service.reject(job.id, reason="color grading", regenerate_from_stage="RENDER")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, _bundle(tmp_path, tts_calls, asset_calls), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        attempts = _stage_attempts(session, job.id)
        assert attempts["RENDER"] == [1, 2]
        assert "ASSET" not in attempts or attempts["ASSET"] == [1]
        assert "TTS" not in attempts or attempts["TTS"] == [1]
    assert tts_calls == []
    assert asset_calls == []


def test_snapshot_pins_provider_and_voice_across_retry_even_if_env_changes(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """A job's LLM/TTS/asset provider snapshot is pinned at creation via
    job_service's provider_snapshot -- retries must keep using the pinned
    voice/provider regardless of what a *different* ProviderBundle instance
    is passed to run_job (simulating a worker restart with new provider
    objects)."""
    from reel_harness.providers.registry import provider_snapshot

    snapshot = provider_snapshot(None)  # fake for all three, since no real settings configured
    from reel_harness.core.service import JobService

    service = JobService(session_factory, storage=storage, provider_snapshot=snapshot)
    local_channel = service.create_channel(name="c2", niche="n", language="en")
    job, _ = service.create_job(local_channel.id, idempotency_key="hyb-media-4", topic="t")

    with session_factory() as session:
        stored = session.get(Job, job.id).provider_config
        assert stored["asset_provider"] == "fake"
        assert stored["llm_provider"] == "fake"
        assert stored["tts_provider"] == "fake"
