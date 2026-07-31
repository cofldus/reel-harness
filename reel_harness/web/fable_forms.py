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

    if errors:
        return NewFableFormResult(value=None, errors=errors)
    return NewFableFormResult(value=NewFableFormInput(
        title=cleaned_title, source_text=cleaned_source, language=language,
        aspect_ratio=aspect_ratio, takes_per_shot=takes,
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
