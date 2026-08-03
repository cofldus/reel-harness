"""Multiple candidate takes per shot (F3).

Every property here is about MONEY or about not throwing away work
already paid for:

- N takes must be N genuinely different clips, or the operator is
  choosing between identical options at N times the price.
- The budget is checked per take, so a project that can afford two but
  not four stops after two instead of overspending or refusing outright.
- A failure on a later take never discards the earlier ones.
- A re-run after a crash replays; it does not buy a second batch.
"""
from __future__ import annotations

import pytest

from reel_harness.core.cinematic_state import SUPPORTED_TAKES_PER_SHOT
from reel_harness.core.fable_service import FableService
from reel_harness.core.service import InvalidActionError
from reel_harness.db.cinematic_models import FableTake, StoryProject
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_lease import lease_next_shot
from reel_harness.worker.fable_runner import (
    _seed_for_attempt,
    run_shot,
    takes_per_shot_for,
)
from tests.conftest import walk_casting

FFMPEG_PRESENT = check_ffmpeg_available().all_available


@pytest.fixture
def ready_shot(session_factory, tmp_path):
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = FableService(
        session_factory, storage=storage, narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )
    project, _ = fable.create_project(title="t", source_text="s", idempotency_key="takes-test")
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)
    return session_factory, storage, project, fable


# -- the count itself ----------------------------------------------------

def test_only_a_small_set_of_counts_is_allowed() -> None:
    """Each take is a paid generation, so "4" is a considered choice and
    "40" is a typo that would spend forty times the approved estimate."""
    assert SUPPORTED_TAKES_PER_SHOT == frozenset({1, 2, 4})


def test_a_project_override_beats_the_operator_default() -> None:
    project = StoryProject(idempotency_key="k", title="t", source_text="s", takes_per_shot=4)
    assert takes_per_shot_for(project, default=1) == 4


def test_an_unset_override_means_use_the_default_not_one() -> None:
    """NULL is "no statement", which is different from "one" -- every
    project created before takes were configurable reads as NULL."""
    project = StoryProject(idempotency_key="k", title="t", source_text="s", takes_per_shot=None)
    assert takes_per_shot_for(project, default=2) == 2


def test_an_unsupported_count_is_refused() -> None:
    project = StoryProject(idempotency_key="k", title="t", source_text="s", takes_per_shot=7)
    with pytest.raises(ValueError, match="takes_per_shot"):
        takes_per_shot_for(project, default=1)


def test_create_project_refuses_an_unsupported_override(session_factory, tmp_path) -> None:
    fable = FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "f"),
        narrative_director=FakeNarrativeDirector(),
    )
    with pytest.raises(InvalidActionError, match="takes_per_shot"):
        fable.create_project(title="t", source_text="s", idempotency_key="bad", takes_per_shot=3)


# -- seeds ---------------------------------------------------------------

def test_each_take_gets_a_distinct_seed() -> None:
    """N takes from one prompt with one seed are N copies of the same
    clip -- there would be nothing to choose between."""
    seeds = {_seed_for_attempt("fingerprint", n) for n in (1, 2, 3, 4)}
    assert len(seeds) == 4


def test_seeds_are_deterministic_so_a_replay_reproduces_them() -> None:
    """A re-run after a crash must reproduce the take it already paid for
    rather than buy a different one."""
    assert _seed_for_attempt("fp", 2) == _seed_for_attempt("fp", 2)


def test_seeds_stay_inside_int32() -> None:
    """Every surveyed provider's seed parameter is an int32; an
    overflowing value would be rejected or silently truncated."""
    for n in range(1, 10):
        assert 0 <= _seed_for_attempt("fp", n) < 2_147_483_647


# -- generation ----------------------------------------------------------

@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_generating_four_takes_produces_four_distinct_takes(ready_shot) -> None:
    session_factory, storage, _, _ = ready_shot
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=4,
        )
        assert shot.status == "REVIEW_REQUIRED"

    with session_factory() as session:
        takes = session.query(FableTake).filter(FableTake.shot_id == shot.id).all()
        assert len(takes) == 4
        assert sorted(t.attempt_number for t in takes) == [1, 2, 3, 4]
        assert len({t.generation_seed for t in takes}) == 4
        assert all(t.status == "DOWNLOADED" for t in takes)
        # Genuinely different media, not four copies of one clip.
        assert len({t.checksum_sha256 for t in takes}) == 4


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_every_take_counts_against_the_budget(ready_shot) -> None:
    session_factory, storage, project, _ = ready_shot
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = 100.0
        db_project.budget_currency = "FAKE"
        session.commit()
        spent_before = db_project.budget_spent_amount

    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=2,
        )

    with session_factory() as session:
        takes = session.query(FableTake).filter(FableTake.shot_id == shot.id).all()
        db_project = session.get(StoryProject, project.id)
        billed = sum(t.cost_amount for t in takes)
        assert db_project.budget_spent_amount - spent_before == pytest.approx(billed)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_a_budget_that_covers_two_takes_stops_after_two(ready_shot) -> None:
    """Partial candidates are useful. Refusing to produce any because
    four will not fit would waste the two the project could pay for."""
    session_factory, storage, project, _ = ready_shot
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        # Each fake take costs duration * 0.01; two fit, four do not.
        per_take = 0.02
        db_project.budget_limit_amount = db_project.budget_spent_amount + per_take * 2
        db_project.budget_currency = "FAKE"
        session.commit()

    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=4,
        )
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "BUDGET_EXCEEDED"

    with session_factory() as session:
        takes = session.query(FableTake).filter(FableTake.shot_id == shot.id).all()
        assert len(takes) == 2, "the affordable candidates were kept"
        assert all(t.status == "DOWNLOADED" for t in takes)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_a_later_failure_never_discards_the_earlier_takes(ready_shot) -> None:
    """A shot with one good take and one that broke is REVIEWABLE, not
    FAILED -- throwing the good one away would discard a generation the
    project already paid for."""
    session_factory, storage, _, _ = ready_shot
    provider = FakeCinematicVideoProvider()
    real_create = provider.create_generation
    calls = {"n": 0}

    def failing_after_first(request):
        calls["n"] += 1
        if calls["n"] > 1:
            from reel_harness.core.errors import TransientProviderError

            raise TransientProviderError("provider fell over on the second take")
        return real_create(request)

    provider.create_generation = failing_after_first

    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=4,
        )
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "UPSTREAM_TRANSIENT"

    with session_factory() as session:
        takes = session.query(FableTake).filter(
            FableTake.shot_id == shot.id, FableTake.status == "DOWNLOADED",
        ).all()
        assert len(takes) == 1


def test_a_failure_on_the_very_first_take_still_fails_the_shot(ready_shot) -> None:
    """Nothing was produced, so there is nothing to review."""
    session_factory, storage, _, _ = ready_shot
    provider = FakeCinematicVideoProvider(mode="timeout")
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=4,
        )
        assert shot.status == "FAILED"
        assert shot.failure_code == "UPSTREAM_TRANSIENT"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_rerunning_a_complete_batch_buys_nothing(ready_shot) -> None:
    """The replay guard: a crash between generation and status commit must
    not produce a second batch."""
    session_factory, storage, _, _ = ready_shot
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=2,
        )
        first_status = shot.status

    with session_factory() as session:
        db_shot = session.get(type(shot), shot.id)
        run_shot(
            session, db_shot, provider, storage, lease_token=db_shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=2,
        )
        assert db_shot.status == first_status

    with session_factory() as session:
        takes = session.query(FableTake).filter(FableTake.shot_id == shot.id).all()
        assert len(takes) == 2, "the second run replayed instead of buying more"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="fake provider materializes real mp4s via ffmpeg")
def test_selecting_one_take_retains_the_others(ready_shot) -> None:
    """Append-only retention: rejected candidates are kept, never deleted
    on selection."""
    session_factory, storage, _, fable = ready_shot
    provider = FakeCinematicVideoProvider()
    with session_factory() as session:
        shot = lease_next_shot(session, worker_id="w")
        run_shot(
            session, shot, provider, storage, lease_token=shot.lease_token,
            sleep=lambda _s: None, takes_per_shot=4,
        )

    takes = fable.shot_takes(shot.id)
    fable.select_take(takes[2].id)

    after = fable.shot_takes(shot.id)
    assert len(after) == 4
    assert [t.selected for t in after] == [False, False, True, False]
    assert all(t.media_path for t in after), "rejected takes keep their media"


# -- pricing -------------------------------------------------------------

def test_the_estimate_multiplies_by_the_projects_own_take_count(
    session_factory, tmp_path,
) -> None:
    """Pricing has to use the same number the worker will, or the budget
    gate approves a fraction of the real bill."""
    def build(takes):
        return FableService(
            session_factory, storage=LocalFilesystemStorage(tmp_path / f"f{takes}"),
            narrative_director=FakeNarrativeDirector(),
            reference_provider=FakeReferenceImageProvider(),
            cinematic_provider_resolver=lambda project: FakeCinematicVideoProvider(),
        ), takes

    fable, _ = build(1)
    single, _ = fable.create_project(
        title="t", source_text="s", idempotency_key="est-1", takes_per_shot=1,
    )
    quad, _ = fable.create_project(
        title="t", source_text="s", idempotency_key="est-4", takes_per_shot=4,
    )
    fable.adapt_project(single.id)
    fable.adapt_project(quad.id)

    one = fable.estimate_cost(single.id)
    four = fable.estimate_cost(quad.id)
    assert four.amount == pytest.approx(one.amount * 4)
