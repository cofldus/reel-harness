"""Fable F1 vertical slice, end to end and fully offline: create a
project from a story -> stub adaptation -> every review gate approved
explicitly -> the REAL FableDaemon (real lease/fencing/heartbeat path,
fake provider) generates a take per shot -> takes selected -> hard-cut
final render -> ffprobe-validated final.mp4 -> COMPLETED. Real ffmpeg
required (the fake provider materializes real clips); zero network.

This is the F1 completion bar: the full state walk driven through the
same code paths the web UI (F4) and the real provider (F5) will use --
no service internals bypassed except where a step's owner (the daemon)
is exercised directly."""
from __future__ import annotations

import pytest

from reel_harness.core.fable_service import FableService
from reel_harness.db.cinematic_models import FableTake
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
from reel_harness.media.runner import run
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_daemon import FableDaemon, FableDaemonConfig

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg for real clips")


def test_fable_vertical_slice_reaches_completed_final_film(session_factory, tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage,
        provider_snapshot={"cinematic_provider": "fake", "narrative_provider": "fake"},
        narrative_director=FakeNarrativeDirector(),
    )

    # 1. Create + adapt (stub) + walk every review gate explicitly.
    project, _ = fable.create_project(
        title="비 오는 밤", source_text="그날 밤, 그는 호텔 창밖의 비를 바라보다 천천히 뒤를 돌아보았다.",
        idempotency_key="fable-e2e",
    )
    assert fable.adapt_project(project.id).status == "STORY_REVIEW"
    assert fable.approve_story(project.id).status == "CHARACTER_REVIEW"
    assert fable.approve_characters(project.id).status == "SHOT_REVIEW"
    assert fable.approve_shots(project.id).status == "GENERATING"

    # 2. The REAL daemon generates every shot (fake provider), then exits
    # on idle -- and advances the project to TAKE_REVIEW itself.
    daemon = FableDaemon(
        session_factory, storage, lambda shot: FakeCinematicVideoProvider(),
        FableDaemonConfig(
            worker_id="fable-e2e-worker", poll_interval_seconds=0.05,
            lease_timeout_seconds=300, heartbeat_interval_seconds=0.5,
            idle_exit_after_seconds=0.5,
        ),
    )
    assert daemon.run() == 0
    assert daemon.shots_processed == 4
    assert fable.get_project(project.id).status == "TAKE_REVIEW"

    # 3. Every shot has exactly one reviewable take; select each.
    shots = fable.project_shots(project.id)
    assert [s.status for s in shots] == ["REVIEW_REQUIRED"] * 4
    for shot in shots:
        takes = fable.shot_takes(shot.id)
        assert len(takes) == 1
        assert takes[0].status == "DOWNLOADED"
        assert takes[0].license == "FAKE_TEST_LICENSE"
        fable.select_take(takes[0].id)

    assert fable.get_project(project.id).status == "EDITING"

    # 4. Final render: hard-cut concat of the selected takes, validated.
    final_path = fable.render_final(project.id)
    assert final_path.exists()
    assert fable.get_project(project.id).status == "FINAL_REVIEW"

    deps = check_ffmpeg_available()
    probe = run(build_ffprobe_argv(deps.ffprobe.path, final_path))
    assert probe.returncode == 0
    result = parse_ffprobe_output(probe.stdout)
    assert result.video_codec == "h264"
    assert result.width == 360 and result.height == 640  # 9:16 project
    assert result.has_audio_stream is True
    # 4 shots x ~2s each -- allow generous encoder tolerance, but it must
    # be a real multi-shot film, not a single clip.
    assert result.duration_sec > 5.0

    # 5. Final approval completes the project.
    assert fable.approve_final(project.id).status == "COMPLETED"

    # 6. Provenance: every selected take records provider/checksum, and no
    # artifact escaped the project's own storage tree.
    with session_factory() as session:
        takes = session.query(FableTake).filter(FableTake.selected.is_(True)).all()
        assert len(takes) == 4
        for take in takes:
            assert take.provider == "fake"
            assert take.checksum_sha256
            assert str(storage.job_dir(project.id)) in take.media_path


def test_fable_slice_survives_a_restart_between_generation_and_selection(
    session_factory, tmp_path,
) -> None:
    """Persistence across a simulated process restart: generation happens,
    then a brand-new service/daemon 'process' (same DB file) picks up
    review + selection + render -- nothing depends on in-memory state."""
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
    )
    project, _ = fable.create_project(
        title="t", source_text="짧은 이야기.", idempotency_key="fable-restart",
    )
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)

    daemon = FableDaemon(
        session_factory, storage, lambda shot: FakeCinematicVideoProvider(),
        FableDaemonConfig(
            worker_id="restart-worker", poll_interval_seconds=0.05,
            lease_timeout_seconds=300, heartbeat_interval_seconds=0.5,
            idle_exit_after_seconds=0.5,
        ),
    )
    assert daemon.run() == 0

    # "Restart": a brand-new FableService against the same session factory.
    restarted = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
    )
    assert restarted.get_project(project.id).status == "TAKE_REVIEW"
    for shot in restarted.project_shots(project.id):
        takes = restarted.shot_takes(shot.id)
        restarted.select_take(takes[0].id)
    final_path = restarted.render_final(project.id)
    assert final_path.exists()
