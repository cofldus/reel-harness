from __future__ import annotations

import pytest

from reel_harness.core.state_machine import JobStatus
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.runner import run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def test_vertical_slice_reaches_review_or_blocked_dependency(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="vslice-1", topic="how to fold a burrito")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)

        if FFMPEG_PRESENT:
            assert db_job.status == JobStatus.REVIEW_REQUIRED.value
            assert (storage.job_dir(job.id) / "manifest.json").exists()
            assert (storage.job_dir(job.id) / "final" / "final.mp4").exists()
        else:
            # Honest outcome on a machine without ffmpeg/ffprobe (true as of this
            # Phase 0/1 run -- see docs/STATUS.md): topic-skip -> script -> policy
            # -> asset -> tts all run for real against Fake providers, then the
            # pipeline fails fast and non-retryably at RENDERING instead of
            # pretending to have produced a video.
            assert db_job.status == JobStatus.FAILED.value
            assert db_job.current_stage == "RENDER"
            assert db_job.failure_code == "BLOCKED_DEPENDENCY"


def test_vertical_slice_persists_script_and_assets_before_render(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="vslice-2", topic="3 tips for sourdough")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.script is not None
        assert len(db_job.script["scenes"]) >= 3

    assets_dir = storage.job_dir(job.id) / "assets"
    assert assets_dir.exists()
    assert any(assets_dir.rglob("*.png"))
    tts_dir = storage.job_dir(job.id) / "tts"
    assert any(tts_dir.rglob("*.wav"))


def test_manual_retry_from_render_after_dependency_block(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    if FFMPEG_PRESENT:
        pytest.skip("only meaningful on a machine without ffmpeg/ffprobe")

    job, _ = job_service.create_job(channel.id, idempotency_key="vslice-3", topic="topic")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.status == JobStatus.FAILED.value

    retried = job_service.retry_from_stage(job.id, stage="RENDER")
    assert retried.status == JobStatus.RETRY_WAIT.value
    assert retried.retry_target_stage == "RENDER"
