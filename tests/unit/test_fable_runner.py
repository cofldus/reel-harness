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
from reel_harness.db.cinematic_models import FableShot, FableTake, StoryProject
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.pipeline.shot_prompt import prompt_fingerprint
from reel_harness.providers.base import CinematicCostEstimate
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_lease import lease_next_shot
from reel_harness.worker.fable_runner import compile_prompt_for_shot, run_shot
from tests.conftest import walk_casting

FFMPEG_PRESENT = check_ffmpeg_available().all_available


@pytest.fixture
def fable_env(session_factory, tmp_path):
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider

    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="runner-test")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)
    return session_factory, storage, project


def test_prompt_compilation_is_deterministic(fable_env) -> None:
    session_factory, _, project = fable_env
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        from reel_harness.db.cinematic_models import FableScene, StoryProject

        db_project = session.get(StoryProject, project.id)
        scene = session.get(FableScene, shot.scene_id)
        first = compile_prompt_for_shot(session, shot, scene, db_project)
        second = compile_prompt_for_shot(session, shot, scene, db_project)
        assert first == second
        assert prompt_fingerprint(first) == prompt_fingerprint(second)
        assert shot.camera_movement.replace("_", " ") in first  # grammar reaches the prompt


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


def test_budget_exhaustion_blocks_the_shot_for_review_never_failure(fable_env) -> None:
    """A shot the project cannot pay for stops BEFORE any provider call:
    REVIEW_REQUIRED (a human raises the limit or stops), never FAILED,
    and with no take row -- nothing was submitted, so nothing was
    charged."""
    session_factory, storage, project = fable_env
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = 0.001  # below any shot's fake price
        db_project.budget_currency = "FAKE"
        session.commit()

    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "BUDGET_EXCEEDED"

    with session_factory() as session:
        assert session.query(FableTake).filter(FableTake.shot_id == shot.id).count() == 0


def test_paid_provider_without_the_switch_blocks_the_shot_for_review(fable_env, monkeypatch) -> None:
    """The worker enforces the double gate itself: approval-time checks
    cannot bind a config change that happened after approval."""
    session_factory, storage, project = fable_env
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = 100.0
        db_project.budget_currency = "FAKE"
        session.commit()

    provider = FakeCinematicVideoProvider()
    monkeypatch.setattr(provider, "provider_id", "some-real-vendor")
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, allow_paid_generation=False,
        )
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "PAID_GENERATION_NOT_ALLOWED"

    with session_factory() as session:
        assert session.query(FableTake).filter(FableTake.shot_id == shot.id).count() == 0


def test_a_provider_that_cannot_quote_fails_the_shot_not_the_worker(fable_env) -> None:
    """Pricing runs before submission, and an unconfigured provider
    refuses to quote at all. That is an ordinary shot FAILURE with the
    provider's own code -- not a budget refusal, and never an exception
    escaping run_shot into the daemon loop."""
    from reel_harness.providers.registry import resolve_cinematic_video_for_snapshot

    session_factory, storage, _ = fable_env
    provider = resolve_cinematic_video_for_snapshot({"cinematic_provider": "nonexistent"}, None)
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)
        assert shot.status == "FAILED"
        assert shot.failure_code == "PROVIDER_NOT_CONFIGURED"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes a real mp4 via ffmpeg")
def test_completed_take_records_real_cost_and_accumulates_project_spend(fable_env) -> None:
    """Spend comes from the provider's REPORTED cost on the finished
    result, lands on the take, and moves the project total -- all in the
    same fenced commit that persists the take."""
    session_factory, storage, project = fable_env
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = 100.0
        db_project.budget_currency = "FAKE"
        session.commit()
        # Casting already spent on reference sheets, so the take's effect
        # is a DELTA on the running total, not the whole of it.
        spent_before = db_project.budget_spent_amount

    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        db_project = session.get(StoryProject, project.id)
        assert take.cost_amount is not None
        assert take.cost_currency == "FAKE"
        assert db_project.budget_spent_amount - spent_before == pytest.approx(take.cost_amount)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes a real mp4 via ffmpeg")
def test_spend_is_the_reported_cost_not_the_estimate(fable_env, monkeypatch) -> None:
    """The estimate authorizes the call; only the bill moves the total.
    A provider whose estimate and actual cost disagree must accumulate
    the actual one."""
    session_factory, storage, project = fable_env
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = 100.0
        db_project.budget_currency = "FAKE"
        session.commit()
        spent_before = db_project.budget_spent_amount

    provider = FakeCinematicVideoProvider()
    monkeypatch.setattr(
        provider, "estimate_cost",
        lambda request: CinematicCostEstimate(known=True, amount=99.0, currency="FAKE"),
    )
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(session, shot, provider, storage, lease_token=shot.lease_token, sleep=lambda _s: None)

    with session_factory() as session:
        take = session.query(FableTake).filter(FableTake.shot_id == shot.id).one()
        db_project = session.get(StoryProject, project.id)
        assert db_project.budget_spent_amount - spent_before == pytest.approx(take.cost_amount)
        assert db_project.budget_spent_amount - spent_before != 99.0


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
