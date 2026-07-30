from __future__ import annotations

import re
from dataclasses import dataclass, field

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

TOPIC_MAX_LENGTH = 200
DURATION_MIN_SECONDS = 10
DURATION_MAX_SECONDS = 180
ALLOWED_LANGUAGES = ("ko", "en")
ALLOWED_STYLES = ("general", "cooking", "fitness", "tech", "lifestyle")
ALLOWED_PROVIDER_PROFILES = ("demo", "real", "fake")


@dataclass
class NewJobFormInput:
    topic: str
    language: str
    duration_seconds: int
    style: str
    provider_profile: str
    burn_subtitles: bool


@dataclass
class NewJobFormResult:
    value: NewJobFormInput | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


def validate_new_job_form(
    topic: str, language: str, duration_seconds: int, style: str, provider_profile: str, burn_subtitles: bool,
) -> NewJobFormResult:
    """Server-side validation -- the browser form is never trusted alone.
    Every failure is reported per-field so the New Job page can re-render
    the same form with the submitted values preserved and inline errors,
    never a raw exception body."""
    errors: dict[str, str] = {}

    cleaned_topic = topic.strip()
    if not cleaned_topic:
        errors["topic"] = "주제를 입력해주세요."
    elif len(cleaned_topic) > TOPIC_MAX_LENGTH:
        errors["topic"] = f"주제는 {TOPIC_MAX_LENGTH}자 이하로 입력해주세요."
    elif _CONTROL_CHARS_RE.search(cleaned_topic):
        errors["topic"] = "주제에 사용할 수 없는 문자가 포함되어 있습니다."

    if language not in ALLOWED_LANGUAGES:
        errors["language"] = f"지원하지 않는 언어입니다 (지원: {', '.join(ALLOWED_LANGUAGES)})."

    if not (DURATION_MIN_SECONDS <= duration_seconds <= DURATION_MAX_SECONDS):
        errors["duration_seconds"] = (
            f"목표 길이는 {DURATION_MIN_SECONDS}~{DURATION_MAX_SECONDS}초 사이여야 합니다."
        )

    if style not in ALLOWED_STYLES:
        errors["style"] = "지원하지 않는 스타일입니다."

    if provider_profile not in ALLOWED_PROVIDER_PROFILES:
        errors["provider_profile"] = "지원하지 않는 provider profile입니다."

    if errors:
        return NewJobFormResult(value=None, errors=errors)
    return NewJobFormResult(value=NewJobFormInput(
        topic=cleaned_topic, language=language, duration_seconds=duration_seconds, style=style,
        provider_profile=provider_profile, burn_subtitles=burn_subtitles,
    ))
