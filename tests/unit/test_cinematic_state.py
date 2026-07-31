"""Fable project/shot state machines (core.cinematic_state) -- the third
state-machine pair, tested to the same standard as Job's and
Publication's: every allowed edge references real statuses, review gates
never advance without an explicit action, FAILED always carries its
bookkeeping fields, terminal states are terminal."""
from __future__ import annotations

import pytest

from reel_harness.core.cinematic_state import (
    ALLOWED_PROJECT_TRANSITIONS,
    ALLOWED_SHOT_TRANSITIONS,
    PROJECT_REVIEW_STATUSES,
    PROJECT_TERMINAL_STATUSES,
    FableProjectStatus,
    FableShotStatus,
    InvalidFableTransitionError,
    MissingFableTransitionFieldsError,
    apply_project_transition,
    apply_shot_transition,
    check_project_transition,
    check_shot_transition,
)


class _FakeProject:
    def __init__(self, status: str = "DRAFT") -> None:
        self.status = status
        self.failure_code = None
        self.failure_summary = None


class _FakeShot:
    def __init__(self, status: str = "PLANNED") -> None:
        self.status = status
        self.failure_code = None
        self.failure_summary = None
        self.next_retry_at = None


def test_every_project_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_PROJECT_TRANSITIONS) == set(FableProjectStatus)


def test_every_shot_status_has_a_transition_entry() -> None:
    assert set(ALLOWED_SHOT_TRANSITIONS) == set(FableShotStatus)


def test_project_terminal_statuses_have_no_outgoing_edges() -> None:
    for status in PROJECT_TERMINAL_STATUSES:
        assert ALLOWED_PROJECT_TRANSITIONS[status] == set()


def test_project_happy_path_walks_every_review_gate() -> None:
    project = _FakeProject()
    path = [
        FableProjectStatus.ADAPTING, FableProjectStatus.STORY_REVIEW,
        FableProjectStatus.CASTING, FableProjectStatus.CHARACTER_REVIEW,
        FableProjectStatus.STORYBOARDING, FableProjectStatus.SHOT_REVIEW,
        FableProjectStatus.GENERATING, FableProjectStatus.TAKE_REVIEW,
        FableProjectStatus.EDITING, FableProjectStatus.FINAL_REVIEW,
        FableProjectStatus.COMPLETED,
    ]
    for status in path:
        apply_project_transition(project, status)
    assert project.status == "COMPLETED"


def test_generating_is_only_reachable_from_approved_shot_review_or_regeneration() -> None:
    """The cost gate: paid generation may start only from SHOT_REVIEW
    approval, TAKE_REVIEW regeneration, or a FAILED manual retry -- never
    from any earlier automatic phase."""
    sources = {
        status for status, targets in ALLOWED_PROJECT_TRANSITIONS.items()
        if FableProjectStatus.GENERATING in targets
    }
    assert sources == {
        FableProjectStatus.SHOT_REVIEW, FableProjectStatus.TAKE_REVIEW, FableProjectStatus.FAILED,
    }


def test_review_states_never_advance_to_another_automatic_phase_directly() -> None:
    """A review gate's forward edges must lead to the NEXT user-approved
    phase, and each review state must also allow going backward
    (rejection) or cancellation -- but never skip ahead past the next
    phase (e.g. STORY_REVIEW can never jump straight to GENERATING)."""
    assert FableProjectStatus.GENERATING not in ALLOWED_PROJECT_TRANSITIONS[FableProjectStatus.STORY_REVIEW]
    assert FableProjectStatus.EDITING not in ALLOWED_PROJECT_TRANSITIONS[FableProjectStatus.SHOT_REVIEW]
    assert FableProjectStatus.COMPLETED not in ALLOWED_PROJECT_TRANSITIONS[FableProjectStatus.TAKE_REVIEW]
    for review_status in PROJECT_REVIEW_STATUSES:
        assert FableProjectStatus.CANCELLED in ALLOWED_PROJECT_TRANSITIONS[review_status]


def test_project_failed_requires_bookkeeping_fields() -> None:
    with pytest.raises(MissingFableTransitionFieldsError):
        check_project_transition(FableProjectStatus.ADAPTING, FableProjectStatus.FAILED, {})
    # With the fields present it passes.
    check_project_transition(
        FableProjectStatus.ADAPTING, FableProjectStatus.FAILED,
        {"failure_code": "SCHEMA_INVALID", "failure_summary": "boom"},
    )


def test_project_invalid_transition_raises() -> None:
    with pytest.raises(InvalidFableTransitionError):
        check_project_transition(FableProjectStatus.DRAFT, FableProjectStatus.GENERATING, {})


def test_apply_project_transition_sets_fields() -> None:
    project = _FakeProject(status="ADAPTING")
    apply_project_transition(
        project, FableProjectStatus.FAILED,
        failure_code="SCHEMA_INVALID", failure_summary="parse failed",
    )
    assert project.status == "FAILED"
    assert project.failure_code == "SCHEMA_INVALID"


def test_shot_happy_path() -> None:
    shot = _FakeShot()
    for status in [
        FableShotStatus.READY, FableShotStatus.SUBMITTED, FableShotStatus.GENERATING,
        FableShotStatus.DOWNLOADING, FableShotStatus.VALIDATING,
        FableShotStatus.REVIEW_REQUIRED, FableShotStatus.SELECTED,
    ]:
        apply_shot_transition(shot, status)
    assert shot.status == "SELECTED"


def test_shot_selected_is_terminal() -> None:
    assert ALLOWED_SHOT_TRANSITIONS[FableShotStatus.SELECTED] == set()


def test_shot_failure_requires_bookkeeping_and_allows_manual_retry() -> None:
    with pytest.raises(MissingFableTransitionFieldsError):
        check_shot_transition(FableShotStatus.GENERATING, FableShotStatus.FAILED, {})
    shot = _FakeShot(status="GENERATING")
    apply_shot_transition(
        shot, FableShotStatus.FAILED,
        failure_code="UPSTREAM_TRANSIENT", failure_summary="provider 500",
    )
    # Shot-level retry: FAILED -> READY re-queues just this shot.
    apply_shot_transition(shot, FableShotStatus.READY)
    assert shot.status == "READY"


def test_shot_rejection_requeues_rather_than_failing() -> None:
    shot = _FakeShot(status="REVIEW_REQUIRED")
    apply_shot_transition(shot, FableShotStatus.READY)  # regenerate
    assert shot.status == "READY"


def test_budget_block_sends_a_ready_shot_to_review_and_back() -> None:
    """A shot the project cannot pay for is stopped at READY, before any
    provider call, and that stop is a review -- not a failure. Raising the
    limit re-queues it through the SAME REVIEW_REQUIRED -> READY edge a
    rejected take already uses; no new recovery path exists for money."""
    shot = _FakeShot(status="READY")
    apply_shot_transition(shot, FableShotStatus.REVIEW_REQUIRED)
    assert shot.status == "REVIEW_REQUIRED"
    apply_shot_transition(shot, FableShotStatus.READY)
    assert shot.status == "READY"


def test_a_ready_shot_can_fail_before_it_is_ever_submitted() -> None:
    """Regression: READY -> FAILED was missing, so a provider that refused
    to quote or validate made the runner's own failure handler raise
    InvalidFableTransitionError past the daemon's error isolation."""
    shot = _FakeShot(status="READY")
    apply_shot_transition(
        shot, FableShotStatus.FAILED,
        failure_code="PROVIDER_NOT_CONFIGURED", failure_summary="not registered",
    )
    assert shot.status == "FAILED"


def test_shot_invalid_transition_raises() -> None:
    with pytest.raises(InvalidFableTransitionError):
        check_shot_transition(FableShotStatus.PLANNED, FableShotStatus.GENERATING, {})
