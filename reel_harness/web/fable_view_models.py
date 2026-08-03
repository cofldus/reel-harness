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


# The seven gates a project walks, in order, with the working states that
# lead into each. Drives the progress stepper -- a user in the middle of a
# seven-step approval flow could not previously tell where they were.
PIPELINE_STEPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("각색", (FableProjectStatus.DRAFT.value, FableProjectStatus.ADAPTING.value)),
    ("스토리 검수", (FableProjectStatus.STORY_REVIEW.value,)),
    ("캐스팅", (FableProjectStatus.CASTING.value,)),
    ("배우 검수", (FableProjectStatus.CHARACTER_REVIEW.value,)),
    ("스토리보드", (FableProjectStatus.STORYBOARDING.value, FableProjectStatus.SHOT_REVIEW.value)),
    ("영상 생성", (FableProjectStatus.GENERATING.value, FableProjectStatus.TAKE_REVIEW.value)),
    ("편집·완성", (
        FableProjectStatus.EDITING.value, FableProjectStatus.FINAL_REVIEW.value,
        FableProjectStatus.COMPLETED.value,
    )),
)


@dataclass
class PipelineStepView:
    label: str
    # "done" | "current" | "todo" | "blocked"
    state: str


def build_pipeline_steps(status: str) -> list[PipelineStepView]:
    """A step is done once the project has moved past its states, current
    while inside them, todo otherwise. A failed or cancelled project marks
    the step it stopped in as blocked rather than pretending progress."""
    stopped = status in (FableProjectStatus.FAILED.value, FableProjectStatus.CANCELLED.value)
    current_index = next(
        (index for index, (_label, states) in enumerate(PIPELINE_STEPS) if status in states),
        None,
    )
    steps: list[PipelineStepView] = []
    for index, (label, _states) in enumerate(PIPELINE_STEPS):
        if stopped:
            state = "blocked" if index == 0 else "todo"
        elif current_index is None:
            state = "todo"
        elif index < current_index:
            state = "done"
        elif index == current_index:
            state = "done" if status == FableProjectStatus.COMPLETED.value else "current"
        else:
            state = "todo"
        steps.append(PipelineStepView(label=label, state=state))
    return steps


@dataclass
class ReferenceViewImage:
    view: str
    label: str
    present: bool
    # Set only when the view exists -- a src pointing at a 404 renders as
    # a broken-image icon, which reads as "something is wrong" rather
    # than "this view has not been generated yet".
    url: str | None = None


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
    status_label: str
    # Playable only once the clip is actually on disk. Choosing between
    # candidate takes is the most visual decision in the product, and it
    # was previously made from a table of "#1 / #2" with no video at all.
    video_url: str | None
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
    # The film grammar the adaptation actually chose. A storyboard that
    # hides it is a list of sentences, not a shot plan.
    shot_size: str | None = None
    camera_angle: str | None = None
    camera_movement: str | None = None
    subject: str | None = None
    dialogue_line: str | None = None
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
class StoryBibleView:
    """What the adaptation actually produced, shown at the story gate.

    Approving a story you cannot read is not a review -- and until this
    existed the page asked exactly that: a "스토리 승인" button with the
    adaptation nowhere on the page.
    """

    logline: str | None
    synopsis: str | None
    premise: str | None
    theme: str | None
    setting: str | None
    visual_style: str | None
    ending_summary: str | None
    prohibited_elements: list[str]


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
    pipeline: list[PipelineStepView]
    story: StoryBibleView | None
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


def build_character_view(character, project_id: str | None = None) -> FableCharacterView:
    images = character.reference_images or {}
    view_images = [
        ReferenceViewImage(
            view=view.value,
            label=view.value.replace("_", " "),
            present=view.value in images,
            url=(
                f"/fable/{project_id}/characters/{character.id}/reference/{view.value}"
                if project_id and view.value in images else None
            ),
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


TAKE_STATUS_LABELS: dict[str, str] = {
    "SUBMITTED": "제출됨",
    "GENERATING": "생성 중",
    "DOWNLOADED": "확인 가능",
    "MODERATED": "정책 검토",
    "FAILED": "실패",
}


def take_status_label(status: str) -> str:
    return TAKE_STATUS_LABELS.get(status, status)


def build_story_view(story_bible) -> StoryBibleView | None:
    """None before adaptation has run -- the template then says so
    instead of rendering an empty shell."""
    if not story_bible:
        return None
    return StoryBibleView(
        logline=story_bible.get("logline"),
        synopsis=story_bible.get("synopsis"),
        premise=story_bible.get("premise"),
        theme=story_bible.get("theme"),
        setting=story_bible.get("setting"),
        visual_style=story_bible.get("visual_style"),
        ending_summary=story_bible.get("ending_summary"),
        prohibited_elements=list(story_bible.get("prohibited_elements") or []),
    )


def build_shot_view(shot, takes, project_id: str | None = None) -> FableShotView:
    return FableShotView(
        shot_id=shot.id,
        shot_order=shot.shot_order,
        status=shot.status,
        status_label=shot_status_label(shot.status),
        action=shot.action,
        duration_sec=shot.duration_sec,
        failure_code=shot.failure_code,
        shot_size=shot.shot_size,
        camera_angle=shot.camera_angle,
        camera_movement=shot.camera_movement,
        subject=shot.subject,
        dialogue_line=shot.dialogue_line,
        takes=[
            FableTakeView(
                take_id=take.id,
                attempt_number=take.attempt_number,
                status=take.status,
                status_label=take_status_label(take.status),
                video_url=(
                    f"/fable/{project_id}/takes/{take.id}/video"
                    if project_id and take.media_path else None
                ),
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
    character_views = [build_character_view(c, project.id) for c in characters]
    pipeline = build_pipeline_steps(status)
    story = build_story_view(project.story_bible)

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
        pipeline=pipeline,
        story=story,
        shots=[build_shot_view(shot, takes, project.id) for shot, takes in shots_with_takes],
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
