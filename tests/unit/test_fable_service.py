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


@pytest.fixture
def fable(session_factory, tmp_path):
    return FableService(
        session_factory, storage=LocalFilesystemStorage(tmp_path / "fable_projects"),
        provider_snapshot={"cinematic_provider": "fake"},
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
    assert project.provider_config == {"cinematic_provider": "fake"}


def test_adapt_populates_bible_characters_scenes_shots(fable) -> None:
    project = _create(fable)
    adapted = fable.adapt_project(project.id)
    assert adapted.status == "STORY_REVIEW"
    assert adapted.story_bible is not None
    assert adapted.story_bible["prohibited_elements"] == ["real people", "minors", "explicit content"]

    shots = fable.project_shots(project.id)
    assert len(shots) == 4
    assert all(shot.status == "PLANNED" for shot in shots)
    assert all(shot.camera_movement == "locked" for shot in shots)  # one movement per shot


def test_review_gates_walk_in_order_and_never_skip(fable) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)

    # Approving shots before the earlier gates is an invalid transition.
    from reel_harness.core.cinematic_state import InvalidFableTransitionError

    with pytest.raises(InvalidFableTransitionError):
        fable.approve_shots(project.id)

    assert fable.approve_story(project.id).status == "CHARACTER_REVIEW"
    assert fable.approve_characters(project.id).status == "SHOT_REVIEW"
    generating = fable.approve_shots(project.id)
    assert generating.status == "GENERATING"
    assert all(shot.status == "READY" for shot in fable.project_shots(project.id))


def test_character_gate_refuses_unconfirmed_adult(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    fable.approve_story(project.id)

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
