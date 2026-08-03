"""Server-side validation for the Fable web forms (Phase F4).

Same discipline as web.forms: the browser form is never trusted alone,
every failure is reported per-field so the page re-renders with the
submitted values intact, and the allow-lists are sourced from the domain
(`SUPPORTED_TAKES_PER_SHOT`) rather than retyped here -- a list retyped in
the UI is a list that drifts from the one the service enforces.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from reel_harness.core.cinematic_state import SUPPORTED_TAKES_PER_SHOT

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")

TITLE_MAX_LENGTH = 200
# A story short enough to be a stray keystroke cannot be adapted into
# anything; the adaptation schema needs at least a few beats to quote.
SOURCE_TEXT_MIN_LENGTH = 20
# Generous, but bounded: the whole source text goes into an LLM prompt,
# and an unbounded paste is a bill nobody approved.
SOURCE_TEXT_MAX_LENGTH = 40_000
ALLOWED_LANGUAGES = ("ko", "en")
ALLOWED_ASPECT_RATIOS = ("9:16", "16:9")

# Genre and tone are prompt HINTS, not domain constraints -- the
# adaptation schema never validates them. They are offered as a curated
# list rather than a free-text box because a typo'd or contradictory hint
# ("comdey", "loud quiet") silently degrades every shot prompt with no
# error anywhere, and because a short list of terms a film model actually
# recognizes beats whatever a user invents on the spot. "" means "let the
# adaptation decide", which is a real choice, not a missing value.
GENRE_CHOICES = (
    ("", "지정 안 함"),
    ("drama", "드라마"),
    ("thriller", "스릴러"),
    ("romance", "로맨스"),
    ("mystery", "미스터리"),
    ("horror", "호러"),
    ("comedy", "코미디"),
    ("fantasy", "판타지"),
    ("science fiction", "SF"),
    ("documentary", "다큐멘터리"),
)
TONE_CHOICES = (
    ("", "지정 안 함"),
    ("quiet tension", "고요한 긴장"),
    ("melancholy", "쓸쓸함"),
    ("warm", "따뜻함"),
    ("tense", "긴박함"),
    ("dreamlike", "몽환적"),
    ("bleak", "황량함"),
    ("hopeful", "희망적"),
    ("playful", "경쾌함"),
)
ALLOWED_GENRES = frozenset(value for value, _label in GENRE_CHOICES)
ALLOWED_TONES = frozenset(value for value, _label in TONE_CHOICES)

# Reference-driven Veo runs are fixed at 8 seconds per shot, and the
# adaptation schema caps a plan at 15 shots -- so these are the durations
# a plan can actually hit, not arbitrary round numbers. The label states
# the shot count because that, not the seconds, is what drives cost.
DURATION_CHOICES = (
    (32, "약 32초 (4샷)"),
    (48, "약 48초 (6샷)"),
    (64, "약 64초 (8샷)"),
    (96, "약 96초 (12샷)"),
    (120, "약 120초 (15샷, 최대)"),
)
ALLOWED_DURATIONS = frozenset(value for value, _label in DURATION_CHOICES)
DEFAULT_DURATION_SEC = 32
# ISO-4217-shaped: three letters. Not validated against a real currency
# table -- the fake tier bills in "FAKE", and inventing a whitelist would
# reject a legitimate provider's currency for no benefit.
_CURRENCY_RE = re.compile(r"^[A-Za-z]{3,8}$")


@dataclass
class NewFableFormInput:
    title: str
    source_text: str
    language: str
    aspect_ratio: str
    takes_per_shot: int | None
    # None rather than "" when unspecified: the service treats a genre of
    # None as "no hint" and would otherwise put an empty string into the
    # prompt, which reads as a deleted word rather than an absent one.
    genre: str | None
    tone: str | None
    target_duration_sec: int


@dataclass
class BudgetFormInput:
    limit_amount: float
    currency: str


@dataclass
class NewFableFormResult:
    value: NewFableFormInput | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


@dataclass
class BudgetFormResult:
    value: BudgetFormInput | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


def validate_new_fable_form(
    title: str, source_text: str, language: str, aspect_ratio: str, takes_per_shot: int,
    genre: str = "", tone: str = "", target_duration_sec: int = DEFAULT_DURATION_SEC,
) -> NewFableFormResult:
    errors: dict[str, str] = {}

    cleaned_title = title.strip()
    if not cleaned_title:
        errors["title"] = "제목을 입력해주세요."
    elif len(cleaned_title) > TITLE_MAX_LENGTH:
        errors["title"] = f"제목은 {TITLE_MAX_LENGTH}자 이하로 입력해주세요."
    elif _CONTROL_CHARS_RE.search(cleaned_title):
        errors["title"] = "제목에 사용할 수 없는 문자가 포함되어 있습니다."

    cleaned_source = source_text.strip()
    if len(cleaned_source) < SOURCE_TEXT_MIN_LENGTH:
        errors["source_text"] = f"원작 텍스트는 최소 {SOURCE_TEXT_MIN_LENGTH}자 이상이어야 합니다."
    elif len(cleaned_source) > SOURCE_TEXT_MAX_LENGTH:
        errors["source_text"] = f"원작 텍스트는 {SOURCE_TEXT_MAX_LENGTH}자 이하여야 합니다."

    if language not in ALLOWED_LANGUAGES:
        errors["language"] = f"지원하지 않는 언어입니다 (지원: {', '.join(ALLOWED_LANGUAGES)})."

    if aspect_ratio not in ALLOWED_ASPECT_RATIOS:
        errors["aspect_ratio"] = f"지원하지 않는 화면비입니다 (지원: {', '.join(ALLOWED_ASPECT_RATIOS)})."

    # 0 means "not specified" -- the form's empty value -- and leaves the
    # project on the operator-wide default rather than pinning it to 1.
    takes: int | None = None
    if takes_per_shot:
        if takes_per_shot not in SUPPORTED_TAKES_PER_SHOT:
            allowed = ", ".join(str(n) for n in sorted(SUPPORTED_TAKES_PER_SHOT))
            errors["takes_per_shot"] = f"샷당 테이크 수는 {allowed} 중 하나여야 합니다."
        else:
            takes = takes_per_shot

    if genre not in ALLOWED_GENRES:
        errors["genre"] = "지원하지 않는 장르입니다."
    if tone not in ALLOWED_TONES:
        errors["tone"] = "지원하지 않는 분위기입니다."
    if target_duration_sec not in ALLOWED_DURATIONS:
        allowed = ", ".join(str(n) for n in sorted(ALLOWED_DURATIONS))
        errors["target_duration_sec"] = f"목표 길이는 {allowed}초 중 하나여야 합니다."

    if errors:
        return NewFableFormResult(value=None, errors=errors)
    return NewFableFormResult(value=NewFableFormInput(
        title=cleaned_title, source_text=cleaned_source, language=language,
        aspect_ratio=aspect_ratio, takes_per_shot=takes,
        genre=genre or None, tone=tone or None,
        target_duration_sec=target_duration_sec,
    ))


def validate_budget_form(limit_amount: str, currency: str) -> BudgetFormResult:
    """A budget limit is the per-project half of the paid-generation gate,
    so a malformed one is refused rather than coerced -- a typo that
    silently became a bigger number would be the worst possible failure
    mode for a spending ceiling."""
    errors: dict[str, str] = {}

    amount: float | None = None
    raw = limit_amount.strip()
    if not raw:
        errors["limit_amount"] = "예산 한도를 입력해주세요."
    else:
        try:
            amount = float(raw)
        except ValueError:
            errors["limit_amount"] = "예산 한도는 숫자여야 합니다."
        else:
            if amount <= 0:
                errors["limit_amount"] = "예산 한도는 0보다 커야 합니다."

    cleaned_currency = currency.strip().upper()
    if not cleaned_currency:
        errors["currency"] = "통화를 입력해주세요."
    elif not _CURRENCY_RE.match(cleaned_currency):
        errors["currency"] = "통화 코드 형식이 올바르지 않습니다."

    if errors or amount is None:
        return BudgetFormResult(value=None, errors=errors)
    return BudgetFormResult(value=BudgetFormInput(limit_amount=amount, currency=cleaned_currency))
