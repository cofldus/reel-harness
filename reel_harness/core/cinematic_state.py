"""Fable cinematic state machines (Phase F1) -- the third state-machine
pair in this codebase, deliberately separate from Job's and Publication's
(see core.state_machine's closing note: the transition graphs and
required-field sets are intentionally independent, so these are new
enums/tables/functions, not a generic abstraction over all three).

Two machines:
- FableProjectStatus: the whole production's lifecycle, with explicit
  *_REVIEW gates -- the project NEVER advances past a review state
  without a user action, and never starts a cost-incurring GENERATING
  phase from anywhere but an approved SHOT_REVIEW.
- FableShotStatus: one shot's generation lifecycle, the leasable unit
  (mirrors Publication's role relative to Job). Shot-level retry never
  fails the whole project.
"""
from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol


class FableProjectStatus(StrEnum):
    DRAFT = "DRAFT"
    ADAPTING = "ADAPTING"
    STORY_REVIEW = "STORY_REVIEW"
    CASTING = "CASTING"
    CHARACTER_REVIEW = "CHARACTER_REVIEW"
    STORYBOARDING = "STORYBOARDING"
    SHOT_REVIEW = "SHOT_REVIEW"
    GENERATING = "GENERATING"
    TAKE_REVIEW = "TAKE_REVIEW"
    EDITING = "EDITING"
    FINAL_REVIEW = "FINAL_REVIEW"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class FableShotStatus(StrEnum):
    PLANNED = "PLANNED"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    GENERATING = "GENERATING"
    DOWNLOADING = "DOWNLOADING"
    VALIDATING = "VALIDATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


PROJECT_TERMINAL_STATUSES = frozenset({
    FableProjectStatus.COMPLETED, FableProjectStatus.CANCELLED,
})

# Review gates: statuses that only a user action may leave (toward the
# forward direction -- cancellation is always allowed). The service layer
# uses this to refuse any automatic advance.
PROJECT_REVIEW_STATUSES = frozenset({
    FableProjectStatus.STORY_REVIEW, FableProjectStatus.CHARACTER_REVIEW,
    FableProjectStatus.SHOT_REVIEW, FableProjectStatus.TAKE_REVIEW,
    FableProjectStatus.FINAL_REVIEW,
})

ALLOWED_PROJECT_TRANSITIONS: dict[FableProjectStatus, set[FableProjectStatus]] = {
    FableProjectStatus.DRAFT: {FableProjectStatus.ADAPTING, FableProjectStatus.CANCELLED},
    FableProjectStatus.ADAPTING: {
        FableProjectStatus.STORY_REVIEW, FableProjectStatus.FAILED, FableProjectStatus.CANCELLED,
    },
    # Rejection during story review re-runs adaptation (same project id).
    FableProjectStatus.STORY_REVIEW: {
        FableProjectStatus.CASTING, FableProjectStatus.ADAPTING, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.CASTING: {
        FableProjectStatus.CHARACTER_REVIEW, FableProjectStatus.FAILED, FableProjectStatus.CANCELLED,
    },
    # Rejecting a character re-runs casting; approval moves to storyboarding.
    FableProjectStatus.CHARACTER_REVIEW: {
        FableProjectStatus.STORYBOARDING, FableProjectStatus.CASTING, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.STORYBOARDING: {
        FableProjectStatus.SHOT_REVIEW, FableProjectStatus.FAILED, FableProjectStatus.CANCELLED,
    },
    # SHOT_REVIEW -> GENERATING is THE cost gate: the only entry into paid
    # generation, and it requires an explicit user approval action.
    FableProjectStatus.SHOT_REVIEW: {
        FableProjectStatus.GENERATING, FableProjectStatus.STORYBOARDING, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.GENERATING: {
        FableProjectStatus.TAKE_REVIEW, FableProjectStatus.FAILED, FableProjectStatus.CANCELLED,
    },
    # Re-generation of rejected shots goes back to GENERATING (shot-level
    # bookkeeping decides WHICH shots re-run -- the project status only
    # says generation work is active again).
    FableProjectStatus.TAKE_REVIEW: {
        FableProjectStatus.EDITING, FableProjectStatus.GENERATING, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.EDITING: {
        FableProjectStatus.FINAL_REVIEW, FableProjectStatus.FAILED, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.FINAL_REVIEW: {
        FableProjectStatus.COMPLETED, FableProjectStatus.EDITING,
        FableProjectStatus.TAKE_REVIEW, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.FAILED: {
        # Operator-triggered manual retry only, back into the phase that
        # failed (service layer picks the concrete target).
        FableProjectStatus.ADAPTING, FableProjectStatus.CASTING,
        FableProjectStatus.STORYBOARDING, FableProjectStatus.GENERATING,
        FableProjectStatus.EDITING, FableProjectStatus.CANCELLED,
    },
    FableProjectStatus.COMPLETED: set(),
    FableProjectStatus.CANCELLED: set(),
}

REQUIRED_FIELDS_FOR_PROJECT_STATUS: dict[FableProjectStatus, tuple[str, ...]] = {
    FableProjectStatus.FAILED: ("failure_code", "failure_summary"),
}


SHOT_TERMINAL_STATUSES = frozenset({FableShotStatus.SELECTED})

ALLOWED_SHOT_TRANSITIONS: dict[FableShotStatus, set[FableShotStatus]] = {
    FableShotStatus.PLANNED: {FableShotStatus.READY, FableShotStatus.REJECTED},
    # READY -> REVIEW_REQUIRED is the budget/paid-gate block (F3): the
    # worker refuses to submit a shot it cannot pay for, and that is a
    # human decision (raise the limit, or stop), never a shot failure.
    # No provider call has been made, so the shot carries no take.
    #
    # READY -> FAILED covers everything that can go wrong BEFORE
    # submission -- an unconfigured provider refusing to quote or
    # validate, a request the adapter rejects outright. Its absence was a
    # real defect: worker.fable_runner's failure handler would itself
    # raise InvalidFableTransitionError out of run_shot and past the
    # daemon's error isolation, turning a routine "provider not
    # configured" into a dead worker lane.
    FableShotStatus.READY: {
        FableShotStatus.SUBMITTED, FableShotStatus.REJECTED,
        FableShotStatus.REVIEW_REQUIRED, FableShotStatus.FAILED,
    },
    FableShotStatus.SUBMITTED: {FableShotStatus.GENERATING, FableShotStatus.FAILED},
    FableShotStatus.GENERATING: {
        FableShotStatus.DOWNLOADING, FableShotStatus.REVIEW_REQUIRED, FableShotStatus.FAILED,
    },
    FableShotStatus.DOWNLOADING: {FableShotStatus.VALIDATING, FableShotStatus.FAILED},
    FableShotStatus.VALIDATING: {FableShotStatus.REVIEW_REQUIRED, FableShotStatus.FAILED},
    # Take selection resolves the review; rejection re-queues generation.
    FableShotStatus.REVIEW_REQUIRED: {FableShotStatus.SELECTED, FableShotStatus.READY},
    FableShotStatus.SELECTED: set(),
    FableShotStatus.REJECTED: {FableShotStatus.READY},  # un-reject: re-plan the shot
    FableShotStatus.FAILED: {FableShotStatus.READY},  # shot-level manual retry
}

REQUIRED_FIELDS_FOR_SHOT_STATUS: dict[FableShotStatus, tuple[str, ...]] = {
    FableShotStatus.FAILED: ("failure_code", "failure_summary"),
}


class InvalidFableTransitionError(Exception):
    pass


class MissingFableTransitionFieldsError(Exception):
    pass


class FableProjectLike(Protocol):
    status: str
    failure_code: str | None
    failure_summary: str | None


class FableShotLike(Protocol):
    status: str
    failure_code: str | None
    failure_summary: str | None
    next_retry_at: datetime | None


def check_project_transition(
    current_status: FableProjectStatus, new_status: FableProjectStatus, fields: dict,
) -> None:
    allowed = ALLOWED_PROJECT_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidFableTransitionError(
            f"fable project {current_status} -> {new_status} is not an allowed transition"
        )
    required = REQUIRED_FIELDS_FOR_PROJECT_STATUS.get(new_status, ())
    missing = [name for name in required if fields.get(name) is None]
    if missing:
        raise MissingFableTransitionFieldsError(
            f"fable project transition to {new_status} is missing required fields: {missing}"
        )


def apply_project_transition(
    project: FableProjectLike, new_status: FableProjectStatus, **fields: object,
) -> None:
    """Validate and apply in place -- kept as a separate function from
    apply_transition/apply_publication_transition per the deliberate
    non-genericity documented in core.state_machine."""
    current = FableProjectStatus(project.status)
    check_project_transition(current, new_status, fields)
    project.status = new_status.value
    for key, value in fields.items():
        setattr(project, key, value)


def check_shot_transition(
    current_status: FableShotStatus, new_status: FableShotStatus, fields: dict,
) -> None:
    allowed = ALLOWED_SHOT_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidFableTransitionError(
            f"fable shot {current_status} -> {new_status} is not an allowed transition"
        )
    required = REQUIRED_FIELDS_FOR_SHOT_STATUS.get(new_status, ())
    missing = [name for name in required if fields.get(name) is None]
    if missing:
        raise MissingFableTransitionFieldsError(
            f"fable shot transition to {new_status} is missing required fields: {missing}"
        )


def apply_shot_transition(shot: FableShotLike, new_status: FableShotStatus, **fields: object) -> None:
    current = FableShotStatus(shot.status)
    check_shot_transition(current, new_status, fields)
    shot.status = new_status.value
    for key, value in fields.items():
        setattr(shot, key, value)


# --- Shot grammar (film vocabulary) ---------------------------------------
# Stored as plain strings on FableShot, validated against these enums at
# the service/adaptation boundary -- same convention as status strings.

# The resolution every shot generation is requested at. One constant
# because two things must agree exactly: what worker.fable_runner asks
# the provider to generate, and what core.cost_service prices before
# approving. A per-shot estimate priced at a resolution the worker then
# does not request would be a lie about money, so neither side is allowed
# its own literal.
DEFAULT_SHOT_RESOLUTION = "360p"


class ShotSize(StrEnum):
    EXTREME_WIDE = "extreme_wide"
    WIDE = "wide"
    MEDIUM = "medium"
    MEDIUM_CLOSE_UP = "medium_close_up"
    CLOSE_UP = "close_up"
    EXTREME_CLOSE_UP = "extreme_close_up"
    INSERT = "insert"
    OVER_THE_SHOULDER = "over_the_shoulder"


class CameraAngle(StrEnum):
    EYE_LEVEL = "eye_level"
    HIGH_ANGLE = "high_angle"
    LOW_ANGLE = "low_angle"
    PROFILE = "profile"
    THREE_QUARTER = "three_quarter"
    OVERHEAD = "overhead"


class CameraMovement(StrEnum):
    """One movement per shot -- a shot spec never combines two movements
    (the plan-level rule from the Fable spec; enforced by this being a
    single-valued field, not a list)."""

    LOCKED = "locked"
    PAN = "pan"
    TILT = "tilt"
    DOLLY_IN = "dolly_in"
    DOLLY_OUT = "dolly_out"
    TRACKING = "tracking"
    HANDHELD_SUBTLE = "handheld_subtle"
    ORBIT = "orbit"
