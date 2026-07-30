from __future__ import annotations

from reel_harness.core.state_machine import JobStatus, PublicationStatus, Stage

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


PUBLICATION_STATUS_LABELS: dict[PublicationStatus, str] = {
    PublicationStatus.CREATED: "생성됨",
    PublicationStatus.ELIGIBILITY_CHECKING: "게시 가능 여부 확인 중",
    PublicationStatus.READY_TO_UPLOAD: "업로드 준비 완료",
    PublicationStatus.UPLOAD_SESSION_CREATED: "업로드 세션 생성됨",
    PublicationStatus.UPLOADING: "업로드 중",
    PublicationStatus.UPLOAD_PAUSED: "업로드 일시 중지됨",
    PublicationStatus.UPLOAD_COMPLETED: "업로드 완료",
    PublicationStatus.PROCESSING: "플랫폼에서 처리 중",
    PublicationStatus.PUBLISHED: "게시 완료",
    PublicationStatus.RETRY_WAIT: "재시도 대기 중",
    PublicationStatus.FAILED: "게시하지 못했습니다",
    PublicationStatus.CANCELLED: "취소됨",
    PublicationStatus.AUTH_REQUIRED: "계정 재인증이 필요합니다",
    PublicationStatus.QUOTA_BLOCKED: "플랫폼 한도에 도달했습니다",
    PublicationStatus.REVIEW_REQUIRED: "검토가 필요합니다",
}

# youtube/tiktok/instagram/fake -> a human-facing name, never the raw
# provider id in a template. "fake" only ever appears in dev/test contexts
# (see web.router's fake-profile visibility gate), never a real publish target.
PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    "youtube": "YouTube",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
    "fake": "Fake (테스트 전용)",
}

# (provider, raw capability privacy value) -> Korean label. Providers reuse
# different vocabularies for the same underlying concept (YouTube's
# "private"/TikTok's "SELF_ONLY" both mean "only I can see this"), so this
# is keyed by (provider, value), never a single flat value->label map.
PRIVACY_VALUE_LABELS: dict[tuple[str, str], str] = {
    ("youtube", "private"): "비공개",
    ("youtube", "unlisted"): "미등록(링크 소지자만)",
    ("youtube", "public"): "공개",
    ("tiktok", "SELF_ONLY"): "나만 보기",
    ("tiktok", "MUTUAL_FOLLOW_FRIENDS"): "맞팔로우 친구만",
    ("tiktok", "FOLLOWER_OF_CREATOR"): "팔로워만",
    ("tiktok", "PUBLIC_TO_EVERYONE"): "전체 공개",
    ("instagram", "PUBLIC"): "공개(비공개 옵션 없음)",
    ("fake", "private"): "비공개",
    ("fake", "unlisted"): "미등록(링크 소지자만)",
    ("fake", "public"): "공개",
}

# Derived from publish_retry.py's real retryable-status set
# (FAILED/AUTH_REQUIRED/QUOTA_BLOCKED/RETRY_WAIT), plus REVIEW_REQUIRED --
# every one of these needs a human to click something before any further
# automatic progress happens. NOT guessed independently of
# core.publish_retry's actual precondition; see
# web.publication_view_models's can_retry, which mirrors the same set.
PUBLICATION_NEEDS_ACTION_STATUSES: frozenset[PublicationStatus] = frozenset({
    PublicationStatus.FAILED, PublicationStatus.AUTH_REQUIRED,
    PublicationStatus.QUOTA_BLOCKED, PublicationStatus.RETRY_WAIT,
    PublicationStatus.REVIEW_REQUIRED,
})


def publication_status_label(value: str) -> str:
    try:
        return PUBLICATION_STATUS_LABELS[PublicationStatus(value)]
    except ValueError:
        return value


def provider_display_name(provider: str) -> str:
    return PROVIDER_DISPLAY_NAMES.get(provider, provider)


def privacy_value_label(provider: str, value: str) -> str:
    return PRIVACY_VALUE_LABELS.get((provider, value), value)
