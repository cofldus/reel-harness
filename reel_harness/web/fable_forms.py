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


@dataclass(frozen=True)
class WritingGuideItem:
    """One thing that measurably changes what the pipeline can produce.

    `key` is matched by the browser's live checklist (see app.js); the
    rest is copy. Server-side rather than hardcoded in the template so the
    same list can be asserted in tests and reused if a CLI or API ever
    wants to explain the same thing.
    """

    key: str
    label: str
    why: str
    bad: str
    good: str


# Adaptation quality is bounded by the source text, and the things that
# matter are not guessable from the outside: a character's clothes are
# what the reference image is built from, summarised speech yields no
# dialogue line, and an inner state no camera can see yields no shot. A
# first-time writer has no way to know any of that, so the form says it --
# each item paired with the pipeline consequence, and with an example,
# because a contrast pair teaches faster than a rule.
WRITING_GUIDE: tuple[WritingGuideItem, ...] = (
    WritingGuideItem(
        key="character",
        label="인물을 눈에 보이게",
        why="배우 레퍼런스 이미지가 이 묘사로 만들어집니다. 나이·머리·옷차림처럼 "
            "카메라에 실제로 찍히는 것을 쓰세요.",
        bad="지우는 지쳐 있었다.",
        good="서른쯤의 지우는 젖은 트렌치코트 차림에 머리를 하나로 묶고 있었다.",
    ),
    WritingGuideItem(
        key="place",
        label="장소와 빛",
        why="영상의 톤과 조명이 여기서 정해집니다. 시간대와 광원을 한 줄만 적어도 "
            "모든 샷의 분위기가 달라집니다.",
        bad="호텔에서 그녀는 기다렸다.",
        good="새벽 세 시 호텔 방, 네온 간판이 창으로 붉게 들어온다.",
    ),
    WritingGuideItem(
        key="dialogue",
        label="대사는 따옴표 안에 그대로",
        why="따옴표 안의 말이 그대로 샷의 대사가 됩니다. 요약해서 쓰면 그 대사는 "
            "영상에서 사라집니다.",
        bad="그녀는 가겠다고 말했다.",
        good="“나 갈게.” 그녀가 문고리를 잡은 채 말했다.",
    ),
    WritingGuideItem(
        key="action",
        label="동작은 찍을 수 있는 것으로",
        why="카메라가 담을 수 없는 마음속 상태는 샷이 되지 못합니다. 몸으로 드러나는 "
            "행동으로 바꿔 쓰세요.",
        bad="그는 지난 일을 오래 후회했다.",
        good="그는 사진을 반으로 접어 주머니에 밀어 넣었다.",
    ),
    WritingGuideItem(
        key="turn",
        label="무언가 바뀌는 순간",
        why="시작과 끝이 같으면 샷이 나열만 되고 이야기가 되지 않습니다. 결정·발견·"
            "포기 같은 전환점이 하나는 있어야 합니다.",
        bad="그녀는 계속 비를 바라보았다.",
        good="한참 뒤, 그녀는 창을 닫고 가방을 집어 들었다.",
    ),
)
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


# Offered ceilings, with what each one actually buys at current list
# prices. Presets exist so setting a budget is one click rather than a
# number pulled out of the air -- but a preset is still an explicit
# choice: nothing is applied until the operator picks one. The paid gate
# requires a DECISION, and quietly defaulting one would hollow it out.
BUDGET_PRESETS: tuple[tuple[float, str], ...] = (
    (2.0, "$2 — 레퍼런스 시트만 (배우 2~4명)"),
    (5.0, "$5 — 단편 1편 (8초 x 4샷)"),
    (10.0, "$10 — 여유 있게 (재생성 포함)"),
    (25.0, "$25 — 여러 편 작업"),
)

# Pre-selected in the form. The most restrictive preset that still
# completes a film, so the default errs toward stopping early rather than
# overspending -- the two directions are not symmetric.
DEFAULT_BUDGET_PRESET = 5.0

# Above this, the form asks for a second confirmation. Not a cap: an
# operator who means it can spend more, but a mistyped extra zero should
# not sail through on one click.
BUDGET_CONFIRM_THRESHOLD = 50.0


def validate_budget_form(
    limit_amount: str, currency: str, confirm_large: bool = False,
) -> BudgetFormResult:
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

    # A large ceiling is allowed, but only when explicitly confirmed --
    # the difference between $5 and $50 is a keystroke, and only one of
    # them is recoverable.
    if amount is not None and amount > BUDGET_CONFIRM_THRESHOLD and not confirm_large:
        errors["limit_amount"] = (
            f"${amount:,.0f}는 큰 금액입니다. 확인란을 체크하면 설정됩니다."
        )

    if errors or amount is None:
        return BudgetFormResult(value=None, errors=errors)
    return BudgetFormResult(value=BudgetFormInput(limit_amount=amount, currency=cleaned_currency))
