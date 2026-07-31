"""Fable domain models (db.cinematic_models): tables are created by the
standard init_db() path (v8 = new tables only, no ALTER migration), the
uniqueness constraints that serve as idempotency/duplicate-generation
guards actually enforce, and rows round-trip through a real SQLite
session with the same UTCDateTime/UUID conventions as the rest of the
schema."""
from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError

from reel_harness.db.cinematic_models import (
    FableCharacter,
    FableScene,
    FableShot,
    FableTake,
    StoryProject,
)
from reel_harness.db.schema import SCHEMA_VERSION

_FABLE_TABLES = {
    "fable_projects", "fable_characters", "fable_locations",
    "fable_scenes", "fable_shots", "fable_takes",
}


def test_schema_version_is_9() -> None:
    assert SCHEMA_VERSION == 9


def test_init_db_creates_all_fable_tables(engine) -> None:
    existing = set(sa_inspect(engine).get_table_names())
    assert _FABLE_TABLES <= existing


def _make_project(session, key: str = "fable-key") -> StoryProject:
    project = StoryProject(idempotency_key=key, title="비 오는 밤", source_text="어느 밤...")
    session.add(project)
    session.flush()
    return project


def test_project_round_trip_defaults(session_factory) -> None:
    with session_factory() as session:
        project = _make_project(session)
        session.commit()
        project_id = project.id

    with session_factory() as session:
        loaded = session.get(StoryProject, project_id)
        assert loaded is not None
        assert loaded.status == "DRAFT"
        assert loaded.aspect_ratio == "9:16"
        assert loaded.target_duration_sec == 60
        assert loaded.created_at.tzinfo is not None  # UTCDateTime convention


def test_project_idempotency_key_is_unique(session_factory) -> None:
    with session_factory() as session:
        _make_project(session, key="dup")
        session.commit()
    with session_factory() as session:
        with pytest.raises(IntegrityError):
            _make_project(session, key="dup")  # flush() issues the INSERT
            session.commit()


def test_scene_order_unique_per_project(session_factory) -> None:
    with session_factory() as session:
        project = _make_project(session)
        session.add(FableScene(project_id=project.id, scene_order=1))
        session.commit()
    with session_factory() as session:
        session.add(FableScene(project_id=project.id, scene_order=1))
        with pytest.raises(IntegrityError):
            session.commit()


def test_take_duplicate_generation_guard(session_factory) -> None:
    """(shot_id, prompt_fingerprint, attempt_number) unique constraint IS
    the duplicate-generation guard -- a retry that lost the provider
    response hits this instead of paying for a second generation."""
    with session_factory() as session:
        project = _make_project(session)
        scene = FableScene(project_id=project.id, scene_order=1)
        session.add(scene)
        session.flush()
        shot = FableShot(scene_id=scene.id, shot_order=1)
        session.add(shot)
        session.flush()
        session.add(FableTake(
            shot_id=shot.id, provider="fake", prompt_fingerprint="abc123", attempt_number=1,
        ))
        session.commit()
        shot_id = shot.id

    with session_factory() as session:
        session.add(FableTake(
            shot_id=shot_id, provider="fake", prompt_fingerprint="abc123", attempt_number=1,
        ))
        with pytest.raises(IntegrityError):
            session.commit()

    # A new attempt_number is a legitimate new generation, not a duplicate.
    with session_factory() as session:
        session.add(FableTake(
            shot_id=shot_id, provider="fake", prompt_fingerprint="abc123", attempt_number=2,
        ))
        session.commit()


def test_shot_carries_lease_columns(session_factory) -> None:
    with session_factory() as session:
        project = _make_project(session)
        scene = FableScene(project_id=project.id, scene_order=1)
        session.add(scene)
        session.flush()
        shot = FableShot(scene_id=scene.id, shot_order=1)
        session.add(shot)
        session.commit()
        assert shot.locked_by is None
        assert shot.lease_token is None
        assert shot.heartbeat_at is None
        assert shot.status == "PLANNED"


def test_character_defaults_to_unconfirmed_and_unapproved(session_factory) -> None:
    """adult_confirmed and reference_approved both default False -- the
    service layer must flip them explicitly; nothing is born approved."""
    with session_factory() as session:
        project = _make_project(session)
        character = FableCharacter(project_id=project.id, name="지우")
        session.add(character)
        session.commit()
        assert character.adult_confirmed is False
        assert character.reference_approved is False
