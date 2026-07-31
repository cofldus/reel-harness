"""View models for the Fable web UI (Phase F4).

Same discipline Phase 5A established and 5B proved the value of: every
`can_*` mirrors the REAL service precondition, not a guess derived from
the transition table. A button the UI offers must be a button the service
will actually accept, and a button it hides must be one the service would
actually refuse -- 5B found a real backend bug precisely because a
`can_cancel` was written against the true precondition rather than the
convenient one.

These are plain dataclasses over already-detached ORM objects. No session
is held, no service is called from a template.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reel_harness.core.cinematic_state import (
    PROJECT_TERMINAL_STATUSES,
    FableProjectStatus,
    FableShotStatus,
)
from reel_harness.pipeline.reference_prompt import REFERENCE_VIEWS
from reel_harness.web.formatting import format_elapsed_since

# Korean labels for every project status, matching web.labels' convention
# for jobs. A status with no entry falls back to its raw value rather than
# rendering blank -- an unknown status should look wrong, not invisible.
PROJECT_STATUS_LABELS: dict[str, str] = {
    "DRAFT": "초안",
    "ADAPTING": "각색 중",
    "STORY_REVIEW": "스토리 검수",
    "CASTING": "캐스팅",
    "CHARACTER_REVIEW": "캐릭터 검수",
    "STORYBOARDING": "스토리보드 작성 중",
    "SHOT_REVIEW": "샷 검수",
    "GENERATING": "생성 중",
    "TAKE_REVIEW": "테이크 검수",
    "EDITING": "편집 중",
    "FINAL_REVIEW": "최종 검수",
    "COMPLETED": "완료",
    "FAILED": "실패",
    "CANCELLED": "취소됨",
}

SHOT_STATUS_LABELS: dict[str, str] = {
    "PLANNED": "계획됨",
    "READY": "대기 중",
    "SUBMITTED": "제출됨",
    "GENERATING": "생성 중",
    "DOWNLOADING": "내려받는 중",
    "VALIDATING": "검증 중",
    "REVIEW_REQUIRED": "검수 필요",
    "SELECTED": "선택됨",
    "REJECTED": "반려됨",
    "FAILED": "실패",
}

# Statuses where the project is waiting on a person. Drives the "needs
# action" marker and stops the detail page's progress poll -- the same
# distinction the job UI draws between "working" and "waiting for you".
PROJECT_NEEDS_ACTION_STATUSES = frozenset({
    FableProjectStatus.STORY_REVIEW.value,
    FableProjectStatus.CASTING.value,
    FableProjectStatus.CHARACTER_REVIEW.value,
    FableProjectStatus.SHOT_REVIEW.value,
    FableProjectStatus.TAKE_REVIEW.value,
    FableProjectStatus.EDITING.value,
    FableProjectStatus.FINAL_REVIEW.value,
    FableProjectStatus.FAILED.value,
})

_TERMINAL_VALUES = frozenset(s.value for s in PROJECT_TERMINAL_STATUSES)


def project_status_label(status: str) -> str:
    return PROJECT_STATUS_LABELS.get(status, status)


def shot_status_label(status: str) -> str:
    return SHOT_STATUS_LABELS.get(status, status)


@dataclass
class FableProjectSummaryView:
    project_id: str
    title: str
    status: str
    status_label: str
    elapsed: str
    needs_action: bool
    is_terminal: bool
    detail_url: str


@dataclass
class ReferenceViewImage:
    view: str
    label: str
    present: bool


@dataclass
class FableCharacterView:
    character_id: str
    name: str
    age_range: str | None
    adult_confirmed: bool
    reference_approved: bool
    images: list[ReferenceViewImage]
    sheet_complete: bool
    failure_code: str | None
    failure_summary: str | None
    cost_amount: float | None
    cost_currency: str | None
    # Mirrors FableService.approve_reference's real precondition: a sheet
    # missing any view is refused, because approving three of four would
    # let a shot request a view that does not exist.
    can_approve: bool
    can_reject: bool


@dataclass
class FableTakeView:
    take_id: str
    attempt_number: int
    status: str
    selected: bool
    cost_amount: float | None
    cost_currency: str | None
    # Mirrors FableService.select_take: only a DOWNLOADED take is
    # selectable, whatever the shot's own status happens to be.
    can_select: bool


@dataclass
class FableShotView:
    shot_id: str
    shot_order: int
    status: str
    status_label: str
    action: str | None
    duration_sec: float | None
    failure_code: str | None
    takes: list[FableTakeView] = field(default_factory=list)


@dataclass
class FableBudgetView:
    limit_amount: float | None
    currency: str | None
    spent_amount: float
    remaining_amount: float | None
    unpriced_take_count: int
    # True when spend is a LOWER bound -- some completed generation
    # reported no price. Shown rather than hidden.
    spend_is_incomplete: bool


@dataclass
class FableProjectDetailView:
    project_id: str
    title: str
    status: str
    status_label: str
    aspect_ratio: str
    takes_per_shot: int | None
    failure_code: str | None
    failure_summary: str | None
    needs_action: bool
    is_terminal: bool
    characters: list[FableCharacterView]
    shots: list[FableShotView]
    budget: FableBudgetView
    # Every action, each mirroring the service's own precondition.
    can_adapt: bool
    can_approve_story: bool
    can_generate_references: bool
    can_approve_characters: bool
    can_approve_shots: bool
    can_render: bool
    can_approve_final: bool
    can_cancel: bool
    # Why approve_characters is unavailable even at its own gate. The
    # button being absent is not self-explanatory, and "generate the
    # sheets first" is exactly the next step.
    characters_blocked_reason: str | None


def build_fable_summary_view(project) -> FableProjectSummaryView:
    return FableProjectSummaryView(
        project_id=project.id,
        title=project.title,
        status=project.status,
        status_label=project_status_label(project.status),
        elapsed=format_elapsed_since(project.created_at),
        needs_action=project.status in PROJECT_NEEDS_ACTION_STATUSES,
        is_terminal=project.status in _TERMINAL_VALUES,
        detail_url=f"/fable/{project.id}",
    )


def build_character_view(character) -> FableCharacterView:
    images = character.reference_images or {}
    view_images = [
        ReferenceViewImage(
            view=view.value,
            label=view.value.replace("_", " "),
            present=view.value in images,
        )
        for view in REFERENCE_VIEWS
    ]
    complete = all(image.present for image in view_images)
    return FableCharacterView(
        character_id=character.id,
        name=character.name,
        age_range=character.age_range,
        adult_confirmed=character.adult_confirmed,
        reference_approved=character.reference_approved,
        images=view_images,
        sheet_complete=complete,
        failure_code=character.reference_failure_code,
        failure_summary=character.reference_failure_summary,
        cost_amount=character.reference_cost_amount,
        cost_currency=character.reference_cost_currency,
        can_approve=complete and not character.reference_approved,
        # Rejecting is only meaningful once something exists to reject.
        can_reject=bool(images),
    )


def build_shot_view(shot, takes) -> FableShotView:
    return FableShotView(
        shot_id=shot.id,
        shot_order=shot.shot_order,
        status=shot.status,
        status_label=shot_status_label(shot.status),
        action=shot.action,
        duration_sec=shot.duration_sec,
        failure_code=shot.failure_code,
        takes=[
            FableTakeView(
                take_id=take.id,
                attempt_number=take.attempt_number,
                status=take.status,
                selected=take.selected,
                cost_amount=take.cost_amount,
                cost_currency=take.cost_currency,
                can_select=take.status == "DOWNLOADED" and not take.selected,
            )
            for take in takes
        ],
    )


def build_budget_view(budget) -> FableBudgetView:
    return FableBudgetView(
        limit_amount=budget.limit_amount,
        currency=budget.currency,
        spent_amount=budget.spent_amount,
        remaining_amount=budget.remaining_amount,
        unpriced_take_count=budget.unpriced_take_count,
        spend_is_incomplete=budget.unpriced_take_count > 0,
    )


def build_fable_detail_view(project, characters, shots_with_takes, budget) -> FableProjectDetailView:
    """`shots_with_takes` is a list of (shot, takes) pairs, loaded by the
    caller so this stays a pure function over detached objects."""
    status = project.status
    character_views = [build_character_view(c) for c in characters]

    unapproved = [c for c in character_views if not c.reference_approved]
    if status != FableProjectStatus.CHARACTER_REVIEW.value:
        blocked_reason = None
    elif not character_views:
        blocked_reason = "캐릭터가 아직 없습니다. 각색을 먼저 실행하세요."
    elif unapproved:
        refused = [c for c in unapproved if c.failure_code]
        blocked_reason = (
            f"레퍼런스 생성이 거부된 캐릭터가 있습니다: {refused[0].name}"
            if refused
            else f"승인되지 않은 레퍼런스 시트가 {len(unapproved)}개 있습니다."
        )
    else:
        blocked_reason = None

    return FableProjectDetailView(
        project_id=project.id,
        title=project.title,
        status=status,
        status_label=project_status_label(status),
        aspect_ratio=project.aspect_ratio,
        takes_per_shot=project.takes_per_shot,
        failure_code=project.failure_code,
        failure_summary=project.failure_summary,
        needs_action=status in PROJECT_NEEDS_ACTION_STATUSES,
        is_terminal=status in _TERMINAL_VALUES,
        characters=character_views,
        shots=[build_shot_view(shot, takes) for shot, takes in shots_with_takes],
        budget=build_budget_view(budget),
        # ADAPTING is included deliberately: adapt_project resumes a run
        # that crashed mid-flight, so offering the button there is what
        # makes a stuck project recoverable by clicking.
        can_adapt=status in (
            FableProjectStatus.DRAFT.value, FableProjectStatus.ADAPTING.value,
        ),
        can_approve_story=status == FableProjectStatus.STORY_REVIEW.value,
        can_generate_references=status == FableProjectStatus.CASTING.value,
        can_approve_characters=(
            status == FableProjectStatus.CHARACTER_REVIEW.value and blocked_reason is None
        ),
        can_approve_shots=status == FableProjectStatus.SHOT_REVIEW.value,
        can_render=status == FableProjectStatus.EDITING.value,
        can_approve_final=status == FableProjectStatus.FINAL_REVIEW.value,
        # cancel_project goes straight to CANCELLED, which every
        # non-terminal status allows -- see ALLOWED_PROJECT_TRANSITIONS.
        can_cancel=status not in _TERMINAL_VALUES,
        characters_blocked_reason=blocked_reason,
    )


def selectable_shot_statuses() -> frozenset[str]:
    """Exposed for tests: the shot statuses at which a take may be
    selected, per FableShotStatus's own transition table."""
    return frozenset({FableShotStatus.REVIEW_REQUIRED.value})
