"""The bounded repair loop (core.adaptation_service) and the Fake
Narrative Director, plus the persistence/idempotency/crash-recovery
behavior FableService.adapt_project now owns.

The Fake director is exercised through the REAL parser and validators --
nothing is stubbed past the network boundary, so a passing test here
means the whole adaptation path works, not just its plumbing."""
from __future__ import annotations

import pytest

from reel_harness.core.adaptation_service import MAX_REPAIR_ATTEMPTS, run_adaptation
from reel_harness.core.fable_service import FableService
from reel_harness.core.service import InvalidActionError
from reel_harness.db.cinematic_models import FableCharacter, FableScene, StoryProject
from reel_harness.pipeline.adaptation_parser import AdaptationValidationError
from reel_harness.providers.base import AdaptationRequest
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector

SOURCE = (
    "그날 밤, 지우는 호텔 창밖의 비를 오래 바라보았다. "
    "마침내 그녀는 천천히 문 쪽으로 돌아섰다."
)


def _request(**overrides) -> AdaptationRequest:
    defaults = dict(
        source_text=SOURCE, language="ko", genre="drama", tone="quiet",
        target_duration_sec=60, aspect_ratio="9:16",
    )
    defaults.update(overrides)
    return AdaptationRequest(**defaults)


# -- repair loop -----------------------------------------------------------


def test_valid_first_attempt_makes_one_call() -> None:
    director = FakeNarrativeDirector()
    outcome = run_adaptation(director, _request())
    assert outcome.attempts == 1
    assert director.repair_calls == 0
    assert outcome.repair_errors == []
    assert len(outcome.adaptation.scenes) == 2


def test_invalid_first_attempt_repairs_and_feeds_errors_back() -> None:
    director = FakeNarrativeDirector(mode="invalid_once")
    outcome = run_adaptation(director, _request())
    assert outcome.attempts == 2
    assert director.repair_calls == 1
    # The errors that triggered the repair were passed to the director
    # verbatim -- that feedback IS the repair contract.
    assert outcome.repair_errors and director.last_errors == outcome.repair_errors[0]
    assert any("story_bible" in e or "characters" in e for e in director.last_errors)


def test_repair_budget_is_bounded_and_then_raises() -> None:
    director = FakeNarrativeDirector(mode="always_invalid")
    with pytest.raises(AdaptationValidationError):
        run_adaptation(director, _request())
    assert director.adapt_calls == 1
    assert director.repair_calls == MAX_REPAIR_ATTEMPTS  # never unbounded


def test_empty_response_fails_without_burning_the_repair_budget() -> None:
    class _EmptyDirector:
        provider_id = "empty"

        def __init__(self) -> None:
            self.repair_calls = 0

        def adapt_story(self, request):
            from reel_harness.providers.base import AdaptationResult

            return AdaptationResult(
                raw_text="   ", provider_id="empty", model_id="m", prompt_version="v",
            )

        def repair_adaptation(self, request, previous_raw, errors):  # pragma: no cover
            self.repair_calls += 1
            raise AssertionError("an empty response must not be repaired")

    director = _EmptyDirector()
    with pytest.raises(AdaptationValidationError, match="empty response"):
        run_adaptation(director, _request())
    assert director.repair_calls == 0


def test_transient_provider_error_propagates_for_stage_retry() -> None:
    from reel_harness.core.errors import TransientProviderError

    with pytest.raises(TransientProviderError):
        run_adaptation(FakeNarrativeDirector(mode="timeout"), _request())


def test_minor_character_is_rejected_by_the_real_validators() -> None:
    """The fake director's minor_character mode produces a document that
    is well-formed except for an under-age bracket -- proving the
    adult-only rule is enforced by the pipeline, not by the fake."""
    director = FakeNarrativeDirector(mode="minor_character")
    with pytest.raises(AdaptationValidationError) as excinfo:
        run_adaptation(director, _request())
    assert any("age_range" in e for e in excinfo.value.errors)


def test_fake_director_quotes_the_real_source_text() -> None:
    outcome = run_adaptation(FakeNarrativeDirector(), _request())
    for scene in outcome.adaptation.scenes:
        normalized_source = SOURCE.replace(" ", "")
        assert scene.source_beat.replace(" ", "") in normalized_source


# -- persistence / idempotency --------------------------------------------


@pytest.fixture
def fable(session_factory):
    return FableService(
        session_factory,
        provider_snapshot={"narrative_provider": "fake"},
        narrative_director=FakeNarrativeDirector(),
    )


def _create(fable: FableService, key: str = "adapt-1") -> StoryProject:
    project, _ = fable.create_project(title="t", source_text=SOURCE, idempotency_key=key)
    return project


def test_adaptation_persists_every_entity_and_the_fingerprint(fable, session_factory) -> None:
    project = _create(fable)
    adapted = fable.adapt_project(project.id)
    assert adapted.status == "STORY_REVIEW"
    assert adapted.adaptation_fingerprint

    with session_factory() as session:
        characters = session.query(FableCharacter).filter(
            FableCharacter.project_id == project.id,
        ).all()
        assert len(characters) == 1
        assert characters[0].adult_confirmed is True  # set from the validated schema
        assert characters[0].bible["fixed_identity"]  # compiler input is present

        scenes = session.query(FableScene).filter(FableScene.project_id == project.id).all()
        assert len(scenes) == 2
        assert scenes[0].continuity_notes["source_beat"]  # fidelity anchor persisted

    shots = fable.project_shots(project.id)
    # Shot count follows the requested runtime now (one shot per
    # SHOT_SECONDS), rather than being a fixed number the fake
    # happened to emit -- the parser's craft layer rejects a plan
    # that does not fit the runtime it was asked for.
    assert len(shots) == 8
    assert all(shot.duration_sec and shot.subject and shot.action for shot in shots)


def test_reruns_with_the_same_input_replay_without_a_second_call(fable) -> None:
    director = FakeNarrativeDirector()
    project = _create(fable)
    fable.adapt_project(project.id, director=director)
    assert director.adapt_calls == 1

    replayed = fable.adapt_project(project.id, director=director)
    assert replayed.status == "STORY_REVIEW"
    assert director.adapt_calls == 1  # no second paid call


def test_changed_input_after_adaptation_is_refused(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.source_text = "완전히 다른 이야기가 여기에 있다."
        session.commit()

    with pytest.raises(InvalidActionError, match="already adapted"):
        fable.adapt_project(project.id)


def test_crash_during_adaptation_is_resumable(fable, session_factory) -> None:
    """A crash after the ADAPTING commit leaves no children -- re-running
    adapt_project resumes rather than refusing."""
    project = _create(fable)
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.status = "ADAPTING"  # simulate the crashed state
        session.commit()

    resumed = fable.adapt_project(project.id)
    assert resumed.status == "STORY_REVIEW"
    assert len(fable.project_shots(project.id)) == 8


def test_missing_director_is_refused_explicitly(session_factory) -> None:
    service = FableService(session_factory)  # no director, no resolver
    project, _ = service.create_project(title="t", source_text=SOURCE, idempotency_key="no-director")
    with pytest.raises(InvalidActionError, match="narrative director"):
        service.adapt_project(project.id)


def test_re_adaptation_after_story_rejection_replaces_children(fable, session_factory) -> None:
    project = _create(fable)
    fable.adapt_project(project.id)
    first_shot_ids = {s.id for s in fable.project_shots(project.id)}

    # Reject back to ADAPTING (the real edge the review UI uses), then
    # change the source so the fingerprint differs.
    with session_factory() as session:
        db_project = session.get(StoryProject, project.id)
        db_project.status = "ADAPTING"
        db_project.source_text = "다른 밤, 그는 문을 열고 밖으로 나갔다."
        session.commit()

    fable.adapt_project(project.id)
    second_shot_ids = {s.id for s in fable.project_shots(project.id)}
    assert len(second_shot_ids) == 8
    assert not (first_shot_ids & second_shot_ids)  # old children replaced, not appended


def test_re_adaptation_refuses_when_takes_already_exist(fable, session_factory) -> None:
    from reel_harness.db.cinematic_models import FableTake

    project = _create(fable)
    fable.adapt_project(project.id)
    shots = fable.project_shots(project.id)
    with session_factory() as session:
        session.add(FableTake(
            shot_id=shots[0].id, provider="fake", prompt_fingerprint="fp", attempt_number=1,
        ))
        db_project = session.get(StoryProject, project.id)
        db_project.status = "ADAPTING"
        db_project.source_text = "또 다른 이야기."
        session.commit()

    with pytest.raises(InvalidActionError, match="takes"):
        fable.adapt_project(project.id)
