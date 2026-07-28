"""core.publish_eligibility: real re-verification against disk/DB, not a
manifest-only check. No network."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from reel_harness.core.publish_eligibility import check_publish_eligibility
from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Asset, Job
from reel_harness.manifest.schema import (
    ApprovalInfo,
    AssetInfo,
    LLMInfo,
    Manifest,
    RenderInfo,
    TTSInfo,
    ValidationInfo,
)
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _faststart_mp4_bytes(tmp_path) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / "eligible-final.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _eligible_asset_info(index: int = 0, checksum: str = "a" * 64) -> AssetInfo:
    return AssetInfo(
        scene_index=index, source_url="https://example.invalid/page", author="Creator",
        license_type="CC-BY-4.0", checksum_sha256=checksum,
        commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
    )


@pytest.fixture
def eligible_job(job_service, channel, session_factory, storage, tmp_path):
    """A COMPLETED job with a real faststart mp4, a matching manifest, and a
    matching current Asset DB row -- the actual happy path."""
    job, _ = job_service.create_job(channel.id, idempotency_key="elig-1", topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    asset_path = tmp_path / "asset0.mp4"
    asset_path.write_bytes(b"asset-bytes")
    asset_checksum = hashlib.sha256(b"asset-bytes").hexdigest()

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_transition(db_job, JobStatus.TTS_GENERATING)
        apply_transition(db_job, JobStatus.RENDERING)
        apply_transition(db_job, JobStatus.VALIDATING)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        apply_transition(db_job, JobStatus.READY)
        apply_transition(db_job, JobStatus.COMPLETED)
        session.add(Asset(
            job_id=job.id, scene_index=0, source_provider="pexels", local_path=str(asset_path),
            checksum_sha256=asset_checksum, mime_type="video/mp4", license_type="CC-BY-4.0",
            commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
        ))
        session.commit()

    manifest = Manifest(
        job_id=job.id, created_at=datetime.now(UTC), topic="t", script_title="T",
        llm=LLMInfo(provider_id="fake", model_id="m", prompt_version="v"),
        tts=TTSInfo(provider_id="fake", voice_id="v1"),
        assets=[_eligible_asset_info(checksum=asset_checksum)],
        render=RenderInfo(ffmpeg_version="8.0", width=320, height=240),
        validation=ValidationInfo(duration_sec=5.0, video_codec="h264", audio_codec="aac", has_audio_stream=True),
        final_video_checksum_sha256=checksum,
        approval=ApprovalInfo(decision="approve", decided_at=datetime.now(UTC)),
    )
    write_manifest(storage, job.id, manifest)
    return job, manifest, checksum


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")
def test_fully_eligible_job_passes_with_no_reasons(eligible_job, session_factory, storage) -> None:
    job, _, _ = eligible_job
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is True
    assert result.reasons == []
    assert result.final_video_checksum is not None


def test_missing_manifest_fails_closed(job_service, channel, session_factory, storage) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="elig-2", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "JOB_NOT_COMPLETED" in result.reasons
    assert "MANIFEST_MISSING" in result.reasons


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_checksum_mismatch_fails_closed(eligible_job, session_factory, storage) -> None:
    job, manifest, checksum = eligible_job
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.write_bytes(final_path.read_bytes() + b"\x00tampered")  # bytes changed after manifest write
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "FINAL_VIDEO_CHECKSUM_MISMATCH" in result.reasons


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_fake_license_fails_closed(eligible_job, session_factory, storage) -> None:
    job, manifest, checksum = eligible_job
    manifest.assets = [AssetInfo(
        scene_index=0, source_url="fake://x", author="A", license_type="FAKE_TEST_LICENSE",
        checksum_sha256="a" * 64, commercial_use_allowed=True, modification_allowed=True,
        attribution_text="x",
    )]
    write_manifest(storage, job.id, manifest)
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "ASSET_LICENSE_NOT_PUBLISHABLE" in result.reasons


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_missing_license_fails_closed(eligible_job, session_factory, storage) -> None:
    job, manifest, checksum = eligible_job
    manifest.assets = [AssetInfo(
        scene_index=0, source_url="https://x", author="A", license_type=None,
        checksum_sha256="a" * 64, commercial_use_allowed=True, modification_allowed=True,
        attribution_text="x",
    )]
    write_manifest(storage, job.id, manifest)
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "ASSET_LICENSE_NOT_PUBLISHABLE" in result.reasons


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_missing_technical_validation_fails_closed(eligible_job, session_factory, storage) -> None:
    job, manifest, checksum = eligible_job
    manifest.validation = ValidationInfo(
        duration_sec=5.0, video_codec=None, audio_codec="aac", has_audio_stream=True,
    )
    write_manifest(storage, job.id, manifest)
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "VIDEO_CODEC_NOT_SUPPORTED" in result.reasons


def test_manifest_unparseable_fails_closed(job_service, channel, session_factory, storage) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="elig-3", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_transition(db_job, JobStatus.TTS_GENERATING)
        apply_transition(db_job, JobStatus.RENDERING)
        apply_transition(db_job, JobStatus.VALIDATING)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        apply_transition(db_job, JobStatus.READY)
        apply_transition(db_job, JobStatus.COMPLETED)
        session.commit()
    storage.write_bytes_atomic(job.id, "manifest.json", b"not json")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "MANIFEST_UNPARSEABLE" in result.reasons


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_asset_file_missing_on_disk_fails_closed(eligible_job, session_factory, storage) -> None:
    job, manifest, checksum = eligible_job
    with session_factory() as session:
        row = session.query(Asset).filter(Asset.job_id == job.id).one()
        import os

        os.remove(row.local_path)
        db_job = session.get(Job, job.id)
        result = check_publish_eligibility(session, db_job, storage)
    assert result.eligible is False
    assert "ASSET_FILE_MISSING" in result.reasons
