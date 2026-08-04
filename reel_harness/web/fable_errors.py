"""Turning a refusal into something a person can act on.

Every mutating Fable route answers Post/Redirect/Get, so a refusal has to
survive a redirect as text. It used to survive as the service's own
internal sentence -- accurate, but written for whoever reads the log, not
for whoever has to decide what to do next. "provider 'google' costs money
and project 8f23bf9a-... has no budget limit set (fable-budget --limit)"
tells a developer everything and a user nothing: it names a CLI flag on a
page that has a button for the same thing.

So refusals now travel as a CODE plus the original detail. The code picks
a title, an explanation and -- the part that matters -- what to do about
it. The original detail is kept and shown under a disclosure, because
throwing away the precise message would trade one failure of honesty for
another.

Codes come from the domain (`core.errors`), never invented here, so a new
failure mode shows up as an unstyled-but-correct fallback rather than
silently rendering as something it is not.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorPresentation:
    """What to render. `action_label`/`action_anchor` are present only when
    there is a real control on this page that resolves the problem --
    never a link to somewhere the user cannot fix it."""

    title: str
    explanation: str
    severity: str = "error"
    action_label: str | None = None
    action_anchor: str | None = None
    detail: str | None = None


# Keyed by the failure codes core.errors actually raises. Deliberately not
# a catch-all pattern match: an unknown code falls through to a fallback
# that shows the raw detail, which is worse-looking but never wrong.
_PRESENTATIONS: dict[str, ErrorPresentation] = {
    "PAID_GENERATION_NOT_ALLOWED": ErrorPresentation(
        title="예산 한도를 먼저 정해야 합니다",
        explanation=(
            "유료 제공자는 프로젝트마다 상한을 정해야 실행됩니다. "
            "한 번의 실수로 예상보다 큰 금액이 나가는 것을 막기 위한 장치입니다."
        ),
        severity="info",
        action_label="예산 정하기",
        action_anchor="budget",
    ),
    "BUDGET_EXCEEDED": ErrorPresentation(
        title="남은 예산으로는 부족합니다",
        explanation=(
            "이번 작업의 예상 비용이 남은 한도를 넘습니다. "
            "한도를 올리거나, 샷 수 또는 샷당 테이크 수를 줄이세요."
        ),
        action_label="한도 조정",
        action_anchor="budget",
    ),
    "BUDGET_CURRENCY_MISMATCH": ErrorPresentation(
        title="통화가 맞지 않습니다",
        explanation=(
            "제공자가 청구하는 통화와 예산 통화가 다릅니다. "
            "환율을 임의로 적용하지 않으므로 예산 통화를 맞춰주세요."
        ),
        action_label="예산 통화 변경",
        action_anchor="budget",
    ),
    "GENERATION_PLAN_CONFLICT": ErrorPresentation(
        title="이 제공자가 만들 수 없는 샷 계획입니다",
        explanation=(
            "샷의 길이나 해상도가 선택된 제공자의 지원 범위를 벗어납니다. "
            "스토리를 다시 각색하거나 제공자를 바꿔야 합니다."
        ),
    ),
    "GENERATION_TIMEOUT": ErrorPresentation(
        title="생성이 시간 안에 끝나지 않았습니다",
        explanation=(
            "제공자 쪽에서는 아직 작업이 돌고 있을 수 있고, 이미 과금됐을 가능성이 높습니다. "
            "같은 샷을 다시 생성하면 두 번 결제됩니다. 아래 기술 정보의 작업 ID로 "
            "제공자 콘솔에서 상태를 먼저 확인하세요."
        ),
        severity="warn",
    ),
    "CONTENT_POLICY_REVIEW": ErrorPresentation(
        title="제공자가 생성을 거부했습니다",
        explanation=(
            "안전 필터에 걸렸습니다. 같은 요청을 다시 보내도 같은 결과가 나오므로, "
            "캐릭터 설정이나 샷 묘사를 수정해야 합니다."
        ),
        severity="warn",
    ),
    "PROVIDER_NOT_CONFIGURED": ErrorPresentation(
        title="제공자 설정이 올바르지 않습니다",
        explanation=(
            "선택된 제공자를 쓸 수 없습니다. 자격증명이나 지역 설정을 확인하세요. "
            "재시도로는 해결되지 않습니다."
        ),
    ),
    "UPSTREAM_AUTH": ErrorPresentation(
        title="제공자 인증에 실패했습니다",
        explanation="키나 권한 문제입니다. 재시도해도 같은 결과이므로 설정을 고쳐야 합니다.",
    ),
    "UPSTREAM_TRANSIENT": ErrorPresentation(
        title="제공자 쪽에서 일시적인 문제가 있었습니다",
        explanation="잠시 후 다시 시도하면 성공할 수 있습니다.",
        severity="warn",
    ),
    "BLOCKED_DEPENDENCY": ErrorPresentation(
        title="ffmpeg을 찾을 수 없습니다",
        explanation="영상 처리에 필요합니다. 설치한 뒤 다시 시도하세요.",
    ),
}

_FALLBACK = ErrorPresentation(
    title="작업을 완료하지 못했습니다",
    explanation="아래 내용을 확인하세요.",
)

# A refusal that carries no code at all -- a plain InvalidActionError from
# a service precondition. Those messages are already written for a person
# ("승인되지 않은 레퍼런스 시트가 2개 있습니다"), so they become the body
# rather than being hidden behind a generic title.
_UNCODED = ErrorPresentation(
    title="아직 할 수 없는 작업입니다",
    explanation="",
    severity="info",
)


# The codes this module can present. Exposed so the router can recover a
# code from a service message that wrapped one, without keeping a second
# list that drifts from this one.
KNOWN_FAILURE_CODES: tuple[str, ...] = tuple(_PRESENTATIONS)


def present_error(raw: str | None) -> ErrorPresentation | None:
    """Parses a redirect's error text into something renderable.

    The wire format is "CODE: detail" when a code is known and plain text
    otherwise -- the same shape the CLI prints, so the two surfaces cannot
    drift into describing the same failure differently.
    """
    if not raw:
        return None
    code, _, detail = raw.partition(": ")
    detail = detail.strip()
    if code in _PRESENTATIONS:
        base = _PRESENTATIONS[code]
        return ErrorPresentation(
            title=base.title, explanation=base.explanation, severity=base.severity,
            action_label=base.action_label, action_anchor=base.action_anchor,
            detail=detail or None,
        )
    # No recognized code: the whole string is the message.
    text = raw.strip()
    if not text:
        return None
    return ErrorPresentation(
        title=_UNCODED.title, explanation=text, severity=_UNCODED.severity,
    )


def format_for_redirect(code: str | None, message: str) -> str:
    """The wire format above, built in ONE place so the routes cannot
    disagree about it."""
    return f"{code}: {message}" if code else message
