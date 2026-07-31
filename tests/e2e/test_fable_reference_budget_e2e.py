"""Fable F3 end to end, fully offline: a story becomes a cast with
approved reference sheets, a budget that actually binds, several
candidate takes per shot, and a final film -- with every real gate
crossed and every charge accounted for.

This is F3's completion bar. What F1's vertical slice was for the state
walk, this is for the two things F3 added: casting is real work that a
human approves, and money is tracked rather than assumed.

The assertion that matters most is the last one: the project's running
total equals the sum of its own line items. A budget nobody can audit
against what was actually generated is a number to be suspicious of.
"""
from __future__ import annotations

import pytest

from reel_harness.core.cost_service import recorded_spend
from reel_harness.core.fable_service import FableService
from reel_harness.core.service import InvalidActionError
from reel_harness.db.cinematic_models import FableTake, StoryProject
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.pipeline.reference_prompt import REFERENCE_VIEWS
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.fable_daemon import FableDaemon, FableDaemonConfig

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg for real clips")

STORY = "그날 밤, 그는 호텔 창밖의 비를 오래 바라보다 천천히 뒤를 돌아보았다."


def _service(session_factory, storage, **kwargs):
    return FableService(
        session_factory, storage=storage,
        provider_snapshot={
            "cinematic_provider": "fake", "narrative_provider": "fake",
            "reference_image_provider": "fake",
        },
        narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
        cinematic_provider_resolver=lambda project: FakeCinematicVideoProvider(),
        **kwargs,
    )


def test_fable_f3_slice_casts_budgets_and_films(session_factory, tmp_path) -> None:
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = _service(session_factory, storage)

    # 1. Create with a per-project take count and adapt.
    project, _ = fable.create_project(
        title="비 오는 밤", source_text=STORY, idempotency_key="f3-e2e", takes_per_shot=2,
    )
    assert fable.adapt_project(project.id).status == "STORY_REVIEW"

    # 2. Casting is a REAL stop: approving the story lands there and goes
    # no further on its own.
    assert fable.approve_story(project.id).status == "CASTING"

    # 3. A budget that cannot cover the cast refuses BEFORE spending
    # anything -- affording half a cast is not affording it.
    fable.set_budget(project.id, 0.01, "FAKE")
    with pytest.raises(InvalidActionError, match="budget exhausted"):
        fable.generate_references(project.id)
    assert fable.budget_status(project.id).spent_amount == 0.0
    assert fable.get_project(project.id).status == "CASTING"

    # 4. With a real budget, casting produces a complete sheet per
    # character and stops at the review gate, unapproved.
    fable.set_budget(project.id, 50.0, "FAKE")
    assert fable.generate_references(project.id).status == "CHARACTER_REVIEW"
    characters = fable.project_characters(project.id)
    assert characters
    for character in characters:
        assert set(character.reference_images) == {v.value for v in REFERENCE_VIEWS}
        assert character.reference_image_path == character.reference_images["face"]
        assert character.reference_approved is False
        assert character.reference_cost_amount > 0

    # 5. The character gate refuses until every sheet is approved.
    with pytest.raises(InvalidActionError, match="no approved reference sheet"):
        fable.approve_characters(project.id)
    for character in characters:
        fable.approve_reference(character.id)
    assert fable.approve_characters(project.id).status == "SHOT_REVIEW"

    # 6. The cost gate prices the whole plan at the project's OWN take
    # count before any shot becomes claimable.
    estimate = fable.estimate_cost(project.id)
    assert estimate.known is True
    assert estimate.currency == "FAKE"
    assert fable.approve_shots(project.id).status == "GENERATING"

    # 7. The REAL daemon generates two candidate takes per shot.
    daemon = FableDaemon(
        session_factory, storage, lambda shot: FakeCinematicVideoProvider(),
        FableDaemonConfig(
            worker_id="f3-e2e-worker", poll_interval_seconds=0.05,
            lease_timeout_seconds=300, heartbeat_interval_seconds=0.5,
            idle_exit_after_seconds=0.5, takes_per_shot=2,
        ),
    )
    assert daemon.run() == 0
    assert fable.get_project(project.id).status == "TAKE_REVIEW"

    shots = fable.project_shots(project.id)
    for shot in shots:
        takes = fable.shot_takes(shot.id)
        assert len(takes) == 2, "the project's own takes_per_shot was honored"
        assert len({t.generation_seed for t in takes}) == 2, "distinct seeds, not two copies"
        assert all(t.status == "DOWNLOADED" for t in takes)

    # 8. Selecting one take per shot retains the rejected siblings.
    for shot in shots:
        takes = fable.shot_takes(shot.id)
        fable.select_take(takes[0].id)
        after = fable.shot_takes(shot.id)
        assert len(after) == 2
        assert [t.selected for t in after] == [True, False]
        assert all(t.media_path for t in after)

    assert fable.get_project(project.id).status == "EDITING"

    # 9. The final film is cut from the SELECTED takes only.
    final_path = fable.render_final(project.id)
    assert final_path.exists()
    assert fable.approve_final(project.id).status == "COMPLETED"

    # 10. The books balance: the running total equals the sum of its own
    # line items (reference sheets + takes), and the budget held.
    status = fable.budget_status(project.id)
    with session_factory() as session:
        audited, currency, unpriced = recorded_spend(session, project.id)
    assert audited == pytest.approx(status.spent_amount), (
        "the running total must equal what its line items actually say"
    )
    assert currency == "FAKE"
    assert unpriced == 0
    assert status.spent_amount <= status.limit_amount
    assert status.remaining_amount == pytest.approx(50.0 - status.spent_amount)

    # And the spend is genuinely made of BOTH kinds of charge.
    with session_factory() as session:
        take_total = sum(
            t.cost_amount or 0.0
            for t in session.query(FableTake).all()
        )
    reference_total = sum(c.reference_cost_amount or 0.0 for c in fable.project_characters(project.id))
    assert take_total > 0 and reference_total > 0
    assert audited == pytest.approx(take_total + reference_total)


def test_budget_exhaustion_mid_generation_reviews_rather_than_fails(
    session_factory, tmp_path,
) -> None:
    """A project that runs out of money part-way through generation leaves
    every shot reviewable or untouched -- never FAILED, and never with a
    charge the budget did not allow."""
    storage = LocalFilesystemStorage(tmp_path / "fable_projects")
    fable = _service(session_factory, storage)
    project, _ = fable.create_project(
        title="t", source_text=STORY, idempotency_key="f3-e2e-broke",
    )
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    fable.set_budget(project.id, 50.0, "FAKE")
    fable.generate_references(project.id)
    for character in fable.project_characters(project.id):
        fable.approve_reference(character.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)

    # Now cut the budget to just above what casting already spent, so the
    # first shot cannot be paid for.
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.budget_limit_amount = db_project.budget_spent_amount + 0.001
        session.commit()

    daemon = FableDaemon(
        session_factory, storage, lambda shot: FakeCinematicVideoProvider(),
        FableDaemonConfig(
            worker_id="f3-broke-worker", poll_interval_seconds=0.05,
            lease_timeout_seconds=300, heartbeat_interval_seconds=0.5,
            idle_exit_after_seconds=0.5,
        ),
    )
    assert daemon.run() == 0

    shots = fable.project_shots(project.id)
    assert shots
    for shot in shots:
        assert shot.status == "REVIEW_REQUIRED"
        assert shot.failure_code == "BUDGET_EXCEEDED"
        assert fable.shot_takes(shot.id) == [], "nothing was submitted, so nothing was charged"

    status = fable.budget_status(project.id)
    assert status.spent_amount <= status.limit_amount

    # Raising the limit re-queues the blocked shots through the same path
    # a rejected take uses -- no special recovery mechanism for money.
    fable.set_budget(project.id, 50.0, "FAKE")
    with session_factory() as session:
        from reel_harness.core.cinematic_state import FableShotStatus, apply_shot_transition
        from reel_harness.db.cinematic_models import FableShot

        for shot in session.query(FableShot).all():
            apply_shot_transition(shot, FableShotStatus.READY)
        session.commit()

    assert daemon.run() == 0
    for shot in fable.project_shots(project.id):
        assert shot.status == "REVIEW_REQUIRED"
        assert len(fable.shot_takes(shot.id)) == 1
