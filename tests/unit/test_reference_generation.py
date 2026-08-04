"""Casting: the reference-sheet workflow (core.fable_service's
generate_references / approve_reference / reject_reference) and the
prompt vocabulary it is built on (pipeline.reference_prompt).

The property this whole feature exists for is CHAINING: the face is
generated from text, and every other view is generated with that face fed
back as a character reference. Generating the four independently yields
four different actors. Several tests here assert on the requests the
provider actually received, because that is the only place the chaining
is observable -- a fake image looks the same either way.
"""
from __future__ import annotations

import pytest

from reel_harness.core.fable_service import FableService
from reel_harness.core.service import InvalidActionError
from reel_harness.pipeline.reference_prompt import (
    REFERENCE_VIEWS,
    ReferenceView,
    compile_reference_prompt,
    reference_fingerprint,
)
from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector
from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
from reel_harness.storage.local import LocalFilesystemStorage


class _RecordingProvider(FakeReferenceImageProvider):
    """The fake provider, plus a log of every request it was handed."""

    def __init__(self, mode="ok") -> None:
        super().__init__(mode=mode)
        self.requests = []

    def generate_reference(self, request, dest_dir):
        self.requests.append(request)
        return super().generate_reference(request, dest_dir)


class _RefusingAfterFirstProvider(FakeReferenceImageProvider):
    """Succeeds on the face, then refuses -- the shape of a safety filter
    objecting to a full-body or wardrobe view of an otherwise fine
    character."""

    def generate_reference(self, request, dest_dir):
        from reel_harness.core.errors import ContentPolicyRefusedError

        if request.character_reference_paths:
            raise ContentPolicyRefusedError("safety filter refused this view")
        return super().generate_reference(request, dest_dir)


@pytest.fixture
def casting(session_factory, tmp_path):
    def build(provider=None, **kwargs):
        fable = FableService(
            session_factory, storage=LocalFilesystemStorage(tmp_path / "fable_projects"),
            narrative_director=FakeNarrativeDirector(),
            reference_provider=provider or FakeReferenceImageProvider(),
            **kwargs,
        )
        project, _ = fable.create_project(
            title="비 오는 밤", source_text="그날 밤, 그는 창밖을 바라보았다.",
            idempotency_key=f"casting-{id(provider)}",
        )
        fable.adapt_project(project.id)
        fable.approve_story(project.id)
        return fable, project.id

    return build


# -- the prompt vocabulary ----------------------------------------------

def test_face_is_the_first_view() -> None:
    """Load-bearing: everything else chains off index 0."""
    assert REFERENCE_VIEWS[0] is ReferenceView.FACE
    # Five now: BACK was added because films end with people walking
    # away, and every other view faces the camera.
    assert len(REFERENCE_VIEWS) == 5


def test_every_view_carries_the_same_fixed_identity(casting) -> None:
    """The reference sheet and the shot prompts must describe ONE actor,
    so both compilers inject the same fixed_identity fragment."""
    fable, project_id = casting()
    project = fable.get_project(project_id)
    character = fable.project_characters(project_id)[0]
    identity = character.bible["fixed_identity"]["face"]

    prompts = {v: compile_reference_prompt(v, character, project) for v in REFERENCE_VIEWS}
    assert all(identity in prompt for prompt in prompts.values())
    # ...while still being four DIFFERENT prompts (framing differs).
    assert len(set(prompts.values())) == 5


def test_every_view_states_the_adult_constraint(casting) -> None:
    fable, project_id = casting()
    project = fable.get_project(project_id)
    character = fable.project_characters(project_id)[0]
    for view in REFERENCE_VIEWS:
        assert "adult" in compile_reference_prompt(view, character, project)


def test_fingerprint_covers_the_whole_sheet(casting) -> None:
    """Sheet-level, not per-image: the views are chained, so a change that
    alters the face invalidates the three generated from it."""
    fable, project_id = casting()
    project = fable.get_project(project_id)
    character = fable.project_characters(project_id)[0]
    before = reference_fingerprint(character, project)
    assert before == reference_fingerprint(character, project)  # deterministic

    character.bible = {**character.bible, "fixed_identity": {"face": "totally different face"}}
    assert reference_fingerprint(character, project) != before


# -- generation ----------------------------------------------------------

def test_generate_references_chains_every_view_off_the_face(casting) -> None:
    provider = _RecordingProvider()
    fable, project_id = casting(provider)
    fable.generate_references(project_id)

    per_character = len(REFERENCE_VIEWS)
    characters = fable.project_characters(project_id)
    assert len(provider.requests) == per_character * len(characters)

    first = provider.requests[0]
    assert first.character_reference_paths == [], "the face is generated from text alone"
    face_path = str(fable.project_characters(project_id)[0].reference_images["face"])
    for request in provider.requests[1:per_character]:
        assert [str(p) for p in request.character_reference_paths] == [face_path], (
            "every later view must chain off the FACE, not off its predecessor "
            "and not off nothing"
        )


def test_generate_references_stops_at_character_review(casting) -> None:
    fable, project_id = casting()
    assert fable.get_project(project_id).status == "CASTING"
    assert fable.generate_references(project_id).status == "CHARACTER_REVIEW"
    for character in fable.project_characters(project_id):
        assert set(character.reference_images) == {v.value for v in REFERENCE_VIEWS}
        assert character.reference_image_path == character.reference_images["face"]
        assert character.reference_approved is False  # generation is not approval


def test_generate_references_refuses_outside_casting(casting) -> None:
    fable, project_id = casting()
    fable.generate_references(project_id)
    with pytest.raises(InvalidActionError, match="not CASTING"):
        fable.generate_references(project_id)


def test_unchanged_rerun_is_a_replay_not_four_more_paid_calls(casting, session_factory) -> None:
    from reel_harness.core.cinematic_state import FableProjectStatus
    from reel_harness.db.cinematic_models import StoryProject

    provider = _RecordingProvider()
    fable, project_id = casting(provider)
    fable.generate_references(project_id)
    first_count = len(provider.requests)

    # Put the project back in CASTING the way a character rejection would.
    with session_factory() as session:
        project = session.get(StoryProject, project_id)
        project.status = FableProjectStatus.CASTING.value
        session.commit()

    fable.generate_references(project_id)
    assert len(provider.requests) == first_count, "an unchanged sheet must not be regenerated"


def test_reject_clears_the_fingerprint_so_the_next_run_regenerates(casting, session_factory) -> None:
    from reel_harness.core.cinematic_state import FableProjectStatus
    from reel_harness.db.cinematic_models import StoryProject

    provider = _RecordingProvider()
    fable, project_id = casting(provider)
    fable.generate_references(project_id)
    first_count = len(provider.requests)

    character = fable.project_characters(project_id)[0]
    fable.approve_reference(character.id)
    rejected = fable.reject_reference(character.id)
    assert rejected.reference_approved is False
    assert rejected.reference_fingerprint is None
    # The images stay on disk: they were paid for, and deleting them would
    # destroy the evidence of what was rejected.
    assert rejected.reference_images

    with session_factory() as session:
        project = session.get(StoryProject, project_id)
        project.status = FableProjectStatus.CASTING.value
        session.commit()
    fable.generate_references(project_id)
    assert len(provider.requests) > first_count


def test_regeneration_revokes_a_previous_approval(casting, session_factory) -> None:
    """A re-run means the operator is looking at a different actor than
    the one they approved, so the approval cannot carry over."""
    from reel_harness.core.cinematic_state import FableProjectStatus
    from reel_harness.db.cinematic_models import FableCharacter, StoryProject

    fable, project_id = casting()
    fable.generate_references(project_id)
    character = fable.project_characters(project_id)[0]
    fable.approve_reference(character.id)

    with session_factory() as session:
        session.get(StoryProject, project_id).status = FableProjectStatus.CASTING.value
        db_character = session.get(FableCharacter, character.id)
        db_character.bible = {"fixed_identity": {"face": "a different face entirely"}}
        session.commit()

    fable.generate_references(project_id)
    assert fable.project_characters(project_id)[0].reference_approved is False


# -- refusals ------------------------------------------------------------

def test_a_safety_refusal_records_the_reason_and_still_reaches_the_gate(casting) -> None:
    """An uncertain content-policy outcome is a human decision, so the
    project still reaches CHARACTER_REVIEW with the refusal recorded --
    it never auto-fails the project."""
    fable, project_id = casting(_RefusingAfterFirstProvider())
    project = fable.generate_references(project_id)
    assert project.status == "CHARACTER_REVIEW"

    character = fable.project_characters(project_id)[0]
    assert character.reference_failure_code == "CONTENT_POLICY_REVIEW"
    assert character.reference_failure_summary
    # The face WAS generated and paid for, so it is recorded rather than
    # discarded -- hiding it would make the spend unauditable.
    assert set(character.reference_images) == {"face"}
    assert character.reference_fingerprint is None  # an incomplete sheet is not a done sheet


def test_an_incomplete_sheet_cannot_be_approved(casting) -> None:
    fable, project_id = casting(_RefusingAfterFirstProvider())
    fable.generate_references(project_id)
    character = fable.project_characters(project_id)[0]
    with pytest.raises(InvalidActionError, match="incomplete reference sheet"):
        fable.approve_reference(character.id)


def test_character_gate_refuses_an_unapproved_sheet(casting) -> None:
    fable, project_id = casting()
    fable.generate_references(project_id)
    with pytest.raises(InvalidActionError, match="no approved reference sheet"):
        fable.approve_characters(project_id)

    for character in fable.project_characters(project_id):
        fable.approve_reference(character.id)
    assert fable.approve_characters(project_id).status == "SHOT_REVIEW"


# -- cost ----------------------------------------------------------------

def test_reference_spend_is_recorded_per_character_and_audits(casting, session_factory) -> None:
    """Reference images spend real money, so they are a line item -- a
    spend audit that only counted takes would under-report every project
    that generated a cast."""
    from reel_harness.core.cost_service import recorded_spend

    fable, project_id = casting()
    fable.generate_references(project_id)

    characters = fable.project_characters(project_id)
    expected = 0.01 * len(REFERENCE_VIEWS) * len(characters)
    for character in characters:
        assert character.reference_cost_amount == pytest.approx(0.01 * len(REFERENCE_VIEWS))
        assert character.reference_cost_currency == "FAKE"

    status = fable.budget_status(project_id)
    assert status.spent_amount == pytest.approx(expected)
    with session_factory() as session:
        audited, currency, _ = recorded_spend(session, project_id)
    assert audited == pytest.approx(expected)
    assert currency == "FAKE"


def test_casting_refuses_when_the_whole_cast_will_not_fit_the_budget(casting) -> None:
    """A partially-generated cast is worth nothing, so affording half of
    it is not affording it -- the check is over the whole cast, up front."""
    fable, project_id = casting()
    fable.set_budget(project_id, 0.01, "FAKE")  # one image, not one sheet
    with pytest.raises(InvalidActionError, match="budget exhausted"):
        fable.generate_references(project_id)
    assert fable.get_project(project_id).status == "CASTING"
    assert fable.budget_status(project_id).spent_amount == 0.0


def test_casting_refuses_a_paid_provider_without_the_switch(casting) -> None:
    provider = FakeReferenceImageProvider()
    provider.provider_id = "some-real-vendor"
    fable, project_id = casting(provider, allow_paid_generation=False)
    fable.set_budget(project_id, 100.0, "FAKE")
    with pytest.raises(InvalidActionError, match="ALLOW_PAID_GENERATION"):
        fable.generate_references(project_id)


# -- reusing an already-approved actor ------------------------------------


def _approved_project(casting, key_suffix: str):
    """A project whose single character has an approved reference sheet --
    the state that makes an actor reusable."""
    fable, project_id = casting()
    fable.generate_references(project_id)
    for character in fable.project_characters(project_id):
        fable.approve_reference(character.id)
    return fable, project_id


def test_only_approved_complete_sheets_are_offered_for_reuse(casting) -> None:
    """An unapproved sheet is a candidate nobody accepted yet; offering it
    would spread a face no one signed off on across projects."""
    fable, project_id = casting()
    fable.generate_references(project_id)
    assert fable.reusable_characters() == []  # generated, not yet approved

    for character in fable.project_characters(project_id):
        fable.approve_reference(character.id)
    reusable = fable.reusable_characters()
    assert reusable
    assert all(c.reference_approved and c.reference_images for c in reusable)


def test_reuse_excludes_the_asking_project(casting) -> None:
    fable, project_id = _approved_project(casting, "self")
    assert fable.reusable_characters(exclude_project_id=project_id) == []


def test_reusing_an_actor_copies_identity_but_not_approval(casting, session_factory) -> None:
    """The new project inherits the face and the bible, but must approve
    the actor for ITSELF -- and the reuse costs nothing, so inheriting the
    original's price would double-count it."""
    fable, source_project = _approved_project(casting, "source")
    source = fable.project_characters(source_project)[0]

    target, _ = fable.create_project(
        title="다른 이야기", source_text="다른 밤, 그는 문을 열었다.", idempotency_key="reuse-target",
    )
    fable.adapt_project(target.id)
    fable.approve_story(target.id)  # -> CASTING

    cast = fable.reuse_character(target.id, source.id)
    assert cast.project_id == target.id
    assert cast.reference_images == source.reference_images  # same stills
    assert cast.bible == source.bible
    assert cast.adult_confirmed is True
    assert cast.reference_approved is False  # this film approves for itself
    assert cast.reference_cost_amount is None  # nothing was generated here

    # The source project is untouched.
    assert fable.project_characters(source_project)[0].reference_approved is True


def test_reuse_is_refused_outside_the_casting_gate(casting) -> None:
    """Swapping the cast after the character gate was approved would
    change the actors the storyboard was written against."""
    fable, source_project = _approved_project(casting, "gate")
    source = fable.project_characters(source_project)[0]

    target, _ = fable.create_project(
        title="t", source_text="다른 밤, 그는 문을 열었다.", idempotency_key="reuse-gate",
    )
    fable.adapt_project(target.id)  # still STORY_REVIEW, not CASTING
    with pytest.raises(InvalidActionError, match="CASTING"):
        fable.reuse_character(target.id, source.id)


def test_reuse_refuses_an_unapproved_source(casting) -> None:
    fable, project_id = casting()
    fable.generate_references(project_id)
    source = fable.project_characters(project_id)[0]

    target, _ = fable.create_project(
        title="t", source_text="다른 밤, 그는 문을 열었다.", idempotency_key="reuse-unapproved",
    )
    fable.adapt_project(target.id)
    fable.approve_story(target.id)
    with pytest.raises(InvalidActionError, match="approved"):
        fable.reuse_character(target.id, source.id)


def test_reuse_replaces_a_same_named_character_rather_than_duplicating(casting) -> None:
    """The adaptation already invented a character; casting a real actor
    into that role must fill the existing row, not add a second one."""
    fable, source_project = _approved_project(casting, "dup")
    source = fable.project_characters(source_project)[0]

    target, _ = fable.create_project(
        title="t", source_text="다른 밤, 그는 문을 열었다.", idempotency_key="reuse-dup",
    )
    fable.adapt_project(target.id)
    fable.approve_story(target.id)
    before = len(fable.project_characters(target.id))

    # Rename the target's character to match, so the names collide.
    from reel_harness.db.cinematic_models import FableCharacter

    with fable._session_factory() as session:
        existing = session.query(FableCharacter).filter(
            FableCharacter.project_id == target.id,
        ).first()
        existing.name = source.name
        session.commit()

    fable.reuse_character(target.id, source.id)
    assert len(fable.project_characters(target.id)) == before
