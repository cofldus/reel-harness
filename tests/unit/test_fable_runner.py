"""run_shot's generation lifecycle (worker.fable_runner) against the fake
provider: the happy path produces a reviewable DOWNLOADED take, a
moderation block routes to REVIEW_REQUIRED with CONTENT_POLICY_REVIEW (a
human decision, never an automatic retry), provider failure maps to
UPSTREAM_TRANSIENT, and a fenced-out worker never publishes results.
The happy path needs real ffmpeg (the fake provider materializes a real
mp4) and is skipped where it's absent."""
from __future__ import annotations

import pytest

from reel_harness.core.fable_service import FableService
from reel_harness.db.cinematic_models import FableShot, FableTake
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_lease import lease_next_shot
from reel_harness.worker.fable_runner import compile_stub_prompt, prompt_fingerprint, run_shot

FFMPEG_PRESENT = check_ffmpeg_available().all_available


@pytest.fixture
def fable_env(session_factory, tmp_path):
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="runner-test")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)
    return session_factory, storage, project


def test_prompt_compilation_is_deterministic(fable_env) -> None:
    session_factory, _, project = fable_env
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        from reel_harness.db.cinematic_models import StoryProject

        db_project = session.get(StoryProject, project.id)
        first = compile_stub_prompt(shot, db_project)
        second = compile_stub_prompt(shot, db_project)
        assert first == second
        assert prompt_fingerprint(first) == prompt_fingerprint(second)
        assert "locked" in first  # camera grammar reaches the prompt


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes a real mp4 via ffmpeg")
def test_run_shot_happy_path_produces_reviewable_take(fable_env) -> None:
    session_factory, storage, project = fable_env
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "REVIEW_REQUIRED"

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        assert take.status == "DOWNLOADED"
        assert take.license == "FAKE_TEST_LICENSE"
        assert take.media_path and take.checksum_sha256
        assert take.provider_job_reference
        # Media landed under the PROJECT's storage tree, nowhere else.
        assert str(storage.job_dir(project.id)) in take.media_path


def test_run_shot_moderated_routes_to_review_not_retry(fable_env) -> None:
    session_factory, storage, _ = fable_env
    provider = FakeCinematicVideoProvider(mode="moderated")
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "CONTENT_POLICY_REVIEW"

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        assert take.status == "MODERATED"
        assert take.rejection_reasons["moderation"]


def test_run_shot_provider_failure_maps_to_upstream_transient(fable_env) -> None:
    session_factory, storage, _ = fable_env
    provider = FakeCinematicVideoProvider(mode="failed")
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "FAILED"
        assert shot.failure_code == "UPSTREAM_TRANSIENT"


def test_run_shot_submit_timeout_fails_shot_via_pipeline_error(fable_env) -> None:
    session_factory, storage, _ = fable_env
    provider = FakeCinematicVideoProvider(mode="timeout")
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "FAILED"
        assert shot.failure_code == "UPSTREAM_TRANSIENT"


def test_fenced_out_worker_stops_publishing(fable_env) -> None:
    """A worker whose token was rotated (takeover) must abandon at the
    first fenced commit -- the shot's committed state stays whatever the
    new owner writes, never the fenced worker's."""
    session_factory, storage, _ = fable_env
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        stolen_token = shot.lease_token

    # Takeover from a second session before the worker runs.
    with session_factory() as session:
        db_shot = session.get(FableShot, shot.id)
        db_shot.lease_token = "new-owner-token"
        session.commit()

    with session_factory() as session:
        db_shot = session.get(FableShot, shot.id)
        run_shot(session, db_shot, provider, storage, lease_token=stolen_token, sleep=lambda _s: None)

    with session_factory() as session:
        refreshed = session.get(FableShot, shot.id)
        assert refreshed.status == "READY"  # the fenced worker never committed a transition
        assert session.query(FableTake).filter(FableTake.shot_id == shot.id).count() == 0
