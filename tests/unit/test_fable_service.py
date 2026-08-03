"""FableService lifecycle rules (core.fable_service): idempotent
creation, the stub adaptation's deterministic output, every review gate
requiring its own explicit approval, the adult-confirmation check at the
character gate, the SHOT_REVIEW -> GENERATING cost gate marking shots
READY, and single-selected-take enforcement."""
from __future__ import annotations

import pytest

from reel_harness.core.fable_service import FableProjectNotFoundError, FableService
from reel_harness.core.service import InvalidActionError
from reel_harness.db.cinematic_models import FableCharacter, FableTake
from reel_harness.storage.local import LocalFilesystemStorage
from tests.conftest import walk_casting


@pytest.fixture
def fable(session_factory, tmp_path):
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider

    return FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "fable_projects"),
        provider_snapshot={"cinematic_provider": "fake", "narrative_provider": "fake"},
        narrative_director=FakeNarrativeDirector(),
        reference_provider=FakeReferenceImageProvider(),
    )


def _create(fable: FableService, key: str = "story-1"):
    project, _ = fable.create_project(
        title="비 오는 밤", source_text="그날 밤, 그는 창밖을 바라보았다.", idempotency_key=key,
    )
    return project


def test_create_project_is_idempotent(fable) -> None:
    first, replay_first = fable.create_project(
        title="t", source_text="s", idempotency_key="same-key",
    )
    second, replay_second = fable.create_project(
        title="t", source_text="s", idempotency_key="same-key",
    )
    assert replay_first is False
    assert replay_second is True
    assert first.id == second.id


def test_create_project_rejects_empty_story_and_bad_aspect(fable) -> None:
    with pytest.raises(InvalidActionError):
        fable.create_project(title="t", source_text="   ", idempotency_key="k1")
    with pytest.raises(InvalidActionError):
        fable.create_project(title="t", source_text="s", idempotency_key="k2", aspect_ratio="4:3")


def test_create_project_pins_the_provider_snapshot(fable) -> None:
    project = _create(fable)
    assert project.provider_config["cinematic_provider"] == "fake"
    assert project.provider_config["narrative_provider"] == "fake"


def test_adapt_populates_bible_characters_scenes_shots(fable) -> None:
    project = _create(fable)
    adapted = fable.adapt_project(project.id)
    assert adapted.status == "STORY_REVIEW"
    assert adapted.story_bible is not None
    assert adapted.story_bible["prohibited_elements"] == ["real people", "minors", "explicit content"]

    shots = fable.project_shots(project.id)
    # Shot count follows the requested runtime now (one shot per
    # SHOT_SECONDS), rather than being a fixed number the fake
    # happened to emit -- the parser's craft layer rejects a plan
    # that does not fit the runtime it was asked for.
    assert len(shots) == 8
    assert all(shot.status == "PLANNED" for shot in shots)
    # Every shot carries exactly one camera movement, validated against the
    # grammar enum by the schema -- never a compound "pan and dolly".
    from reel_harness.core.cinematic_state import CameraMovement

    valid_movements = {m.value for m in CameraMovement}
    assert all(shot.camera_movement in valid_movements for shot in shots)


def test_review_gates_walk_in_order_and_never_skip(fable) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)

    # Approving shots before the earlier gates is an invalid transition.
    from reel_harness.core.cinematic_state import InvalidFableTransitionError

    with pytest.raises(InvalidFableTransitionError):
        fable.approve_shots(project.id)

    # Casting is a REAL stop (F3): approving the story lands in CASTING,
    # and CHARACTER_REVIEW is only reached by actually generating the
    # reference sheets.
    assert fable.approve_story(project.id).status == "CASTING"
    assert fable.generate_references(project.id).status == "CHARACTER_REVIEW"
    for character in fable.project_characters(project.id):
        fable.approve_reference(character.id)
    assert fable.approve_characters(project.id).status == "SHOT_REVIEW"
    generating = fable.approve_shots(project.id)
    assert generating.status == "GENERATING"
    assert all(shot.status == "READY" for shot in fable.project_shots(project.id))


def test_character_gate_refuses_unconfirmed_adult(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)

    with session_factory() as session:
        from sqlalchemy import select

        character = session.execute(
            select(FableCharacter).where(FableCharacter.project_id == project.id)
        ).scalar_one()
        character.adult_confirmed = False
        session.commit()

    with pytest.raises(InvalidActionError, match="virtual adult"):
        fable.approve_characters(project.id)


def test_select_take_enforces_single_selection(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    fable.approve_shots(project.id)

    shots = fable.project_shots(project.id)
    shot = shots[0]
    # Simulate the worker having produced two reviewable takes.
    with session_factory() as session:
        db_shot = session.get(type(shot), shot.id)
        db_shot.status = "REVIEW_REQUIRED"
        session.add_all([
            FableTake(shot_id=shot.id, provider="fake", prompt_fingerprint="fp", attempt_number=1,
                      status="DOWNLOADED", media_path="/tmp/a.mp4"),
            FableTake(shot_id=shot.id, provider="fake", prompt_fingerprint="fp", attempt_number=2,
                      status="DOWNLOADED", media_path="/tmp/b.mp4"),
        ])
        session.commit()

    takes = fable.shot_takes(shot.id)
    fable.select_take(takes[0].id)
    with session_factory() as session:
        db_shot = session.get(type(shot), shot.id)
        db_shot.status = "REVIEW_REQUIRED"  # allow re-selection for the test
        session.commit()
    fable.select_take(takes[1].id)

    refreshed = fable.shot_takes(shot.id)
    assert [t.selected for t in refreshed] == [False, True]  # exactly one selected


def test_select_take_refuses_undownloaded_take(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    shots = fable.project_shots(project.id)
    with session_factory() as session:
        take = FableTake(shot_id=shots[0].id, provider="fake", prompt_fingerprint="fp", attempt_number=1)
        session.add(take)
        session.commit()
        take_id = take.id
    with pytest.raises(InvalidActionError, match="not downloadable"):
        fable.select_take(take_id)


# -- budget / cost gate --------------------------------------------------

@pytest.fixture
def priced_fable(session_factory, tmp_path):
    """A FableService that can actually price a project: a resolver
    returning the fake cinematic adapter, mirroring how AppContext wires
    cinematic_provider_for_project."""
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector

    def build(*, allow_paid_generation=False, provider=None):
        provider = provider or FakeCinematicVideoProvider()
        from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider

        return FableService(
            session_factory, storage=LocalFilesystemStorage(tmp_path / "fable_projects"),
            provider_snapshot={"cinematic_provider": "fake", "narrative_provider": "fake"},
            narrative_director=FakeNarrativeDirector(),
            cinematic_provider_resolver=lambda project: provider,
            allow_paid_generation=allow_paid_generation,
            reference_provider=FakeReferenceImageProvider(),
        )

    return build


def _adapted_to_shot_review(fable: FableService, key: str = "budget-1") -> str:
    project = _create(fable, key)
    fable.adapt_project(project.id)
    fable.approve_story(project.id)
    walk_casting(fable, project.id)
    fable.approve_characters(project.id)
    return project.id


def test_set_budget_requires_a_positive_amount_and_a_currency(priced_fable) -> None:
    fable = priced_fable()
    project = _create(fable)
    with pytest.raises(InvalidActionError, match="positive"):
        fable.set_budget(project.id, 0.0, "FAKE")
    with pytest.raises(InvalidActionError, match="currency"):
        fable.set_budget(project.id, 5.0, None)


def test_set_budget_refuses_a_limit_below_what_was_already_spent(priced_fable, session_factory) -> None:
    """Lowering a limit under the existing spend would read as a promise
    the money comes back."""
    from reel_harness.db.cinematic_models import StoryProject

    fable = priced_fable()
    project = _create(fable)
    fable.set_budget(project.id, 10.0, "FAKE")
    with session_factory() as session:
        session.get(StoryProject, project.id).budget_spent_amount = 4.0
        session.commit()

    with pytest.raises(InvalidActionError, match="already spent"):
        fable.set_budget(project.id, 2.0, "FAKE")
    fable.set_budget(project.id, 6.0, "FAKE")  # above the spend is fine


def test_clearing_a_budget_keeps_the_recorded_spend(priced_fable, session_factory) -> None:
    from reel_harness.db.cinematic_models import StoryProject

    fable = priced_fable()
    project = _create(fable)
    fable.set_budget(project.id, 10.0, "FAKE")
    with session_factory() as session:
        session.get(StoryProject, project.id).budget_spent_amount = 4.0
        session.commit()

    fable.set_budget(project.id, None)
    status = fable.budget_status(project.id)
    assert status.limit_amount is None
    assert status.spent_amount == 4.0  # clearing a ceiling un-spends nothing


def test_approve_shots_refuses_when_the_estimate_exceeds_the_budget(priced_fable) -> None:
    """The cost gate is literal: shots never become claimable when the
    project cannot pay for all of them."""
    fable = priced_fable()
    project_id = _adapted_to_shot_review(fable)
    # Casting already spent real money on the reference sheets, so the
    # limit has to sit ABOVE that and below spend + the shot estimate.
    spent = fable.budget_status(project_id).spent_amount
    assert spent > 0, "casting should have recorded reference spend"
    fable.set_budget(project_id, spent + 0.001, "FAKE")
    with pytest.raises(InvalidActionError, match="budget exhausted"):
        fable.approve_shots(project_id)
    # Nothing moved: still at the gate, still no claimable shot.
    assert fable.get_project(project_id).status == "SHOT_REVIEW"
    assert all(shot.status == "PLANNED" for shot in fable.project_shots(project_id))


def test_approve_shots_passes_within_budget(priced_fable) -> None:
    fable = priced_fable()
    project_id = _adapted_to_shot_review(fable)
    fable.set_budget(project_id, 100.0, "FAKE")
    assert fable.approve_shots(project_id).status == "GENERATING"


def test_approve_shots_refuses_a_paid_provider_without_a_project_budget(priced_fable) -> None:
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider

    provider = FakeCinematicVideoProvider()
    provider.provider_id = "some-real-vendor"
    fable = priced_fable(allow_paid_generation=True, provider=provider)
    project_id = _adapted_to_shot_review(fable)
    with pytest.raises(InvalidActionError, match="no budget limit"):
        fable.approve_shots(project_id)


def test_approve_shots_refuses_a_paid_provider_without_the_global_switch(priced_fable) -> None:
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider

    provider = FakeCinematicVideoProvider()
    provider.provider_id = "some-real-vendor"
    fable = priced_fable(allow_paid_generation=False, provider=provider)
    project_id = _adapted_to_shot_review(fable)
    fable.set_budget(project_id, 100.0, "FAKE")
    with pytest.raises(InvalidActionError, match="ALLOW_PAID_GENERATION"):
        fable.approve_shots(project_id)


def test_free_provider_needs_no_budget_at_all(priced_fable) -> None:
    """Requiring a budget to run the offline fake tier would be ceremony,
    not safety -- the whole F1/F2 offline slice must keep working."""
    fable = priced_fable()
    project_id = _adapted_to_shot_review(fable)
    assert fable.approve_shots(project_id).status == "GENERATING"


def test_unpriceable_project_with_a_budget_is_refused_at_the_gate(priced_fable) -> None:
    """A limit is in force and the cost cannot be established -- approving
    would authorize an unbounded charge."""
    from reel_harness.providers.base import CinematicCostEstimate
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider

    provider = FakeCinematicVideoProvider()
    provider.estimate_cost = lambda request: CinematicCostEstimate(known=False, detail="no price")
    fable = priced_fable(provider=provider)
    project_id = _adapted_to_shot_review(fable)
    fable.set_budget(project_id, 100.0, "FAKE")
    with pytest.raises(InvalidActionError, match="cannot be established"):
        fable.approve_shots(project_id)


def test_estimate_cost_is_read_only(priced_fable) -> None:
    fable = priced_fable()
    project_id = _adapted_to_shot_review(fable)
    before = fable.get_project(project_id).status
    spent_before = fable.budget_status(project_id).spent_amount
    estimate = fable.estimate_cost(project_id)
    assert estimate.known is True
    assert fable.get_project(project_id).status == before
    # Pricing moves nothing -- an estimate is not a charge.
    assert fable.budget_status(project_id).spent_amount == spent_before


def test_get_project_raises_for_unknown_id(fable) -> None:
    with pytest.raises(FableProjectNotFoundError):
        fable.get_project("00000000-0000-0000-0000-000000000000")


def test_render_final_requires_editing_status(fable) -> None:
    project = _create(fable)
    with pytest.raises(InvalidActionError, match="EDITING"):
        fable.render_final(project.id)


def test_cancel_from_any_active_state(fable) -> None:
    project = _create(fable)
    assert fable.cancel_project(project.id).status == "CANCELLED"
