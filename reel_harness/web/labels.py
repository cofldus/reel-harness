from __future__ import annotations

from reel_harness.core.state_machine import JobStatus, Stage

# The one place JobStatus/Stage enum values map to user-facing strings --
# view_models.py builders and templates both import from here; nothing else
# switches on a raw enum value. Keeping this centralized means a future
# second language (or a copy tweak) touches exactly one file.
JOB_STATUS_LABELS: dict[JobStatus, str] = {
    JobStatus.CREATED: "생성됨",
    JobStatus.QUEUED: "대기 중",
    JobStatus.TOPIC_GENERATING: "주제 구성 중",
    JobStatus.SCRIPT_GENERATING: "대본 작성 중",
    JobStatus.POLICY_CHECKING: "콘텐츠 확인 중",
    JobStatus.ASSET_FETCHING: "영상 소스 준비 중",
    JobStatus.TTS_GENERATING: "음성 생성 중",
    JobStatus.RENDERING: "영상 제작 중",
    JobStatus.VALIDATING: "최종 영상 확인 중",
    JobStatus.REVIEW_REQUIRED: "검수가 필요합니다",
    JobStatus.READY: "게시 준비 완료",
    JobStatus.PUBLISHING: "게시 중",
    JobStatus.COMPLETED: "영상이 완성되었습니다",
    JobStatus.RETRY_WAIT: "재시도 대기 중",
    JobStatus.FAILED: "작업을 완료하지 못했습니다",
    JobStatus.CANCELLED: "취소됨",
}

STAGE_LABELS: dict[Stage, str] = {
    Stage.TOPIC: "주제 구성",
    Stage.SCRIPT: "대본 작성",
    Stage.POLICY: "콘텐츠 확인",
    Stage.ASSET: "영상 소스 준비",
    Stage.TTS: "음성 생성",
    Stage.RENDER: "영상 제작",
    Stage.VALIDATE: "최종 영상 확인",
    Stage.PUBLISH: "게시",
}

# Statuses that will never progress further without a human clicking
# something (retry/approve/reject/publish) -- broader than
# state_machine.TERMINAL_STATUSES (COMPLETED/CANCELLED only), which alone
# under-covers "stop polling, nothing more will happen automatically."
NEEDS_ACTION_STATUSES: frozenset[JobStatus] = frozenset({
    JobStatus.FAILED, JobStatus.REVIEW_REQUIRED, JobStatus.READY,
})


def job_status_label(value: str) -> str:
    try:
        return JOB_STATUS_LABELS[JobStatus(value)]
    except ValueError:
        return value


def stage_label(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return STAGE_LABELS[Stage(value)]
    except ValueError:
        return value
