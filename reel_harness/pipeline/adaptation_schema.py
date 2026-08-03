"""Strict schema for a Narrative Director adaptation (Fable F2) --
pydantic models mirroring pipeline.script_schema's conventions, plus the
film-specific hard rules the Fable spec demands at the SCHEMA level
(cheapest, earliest rejection point):

- adult-only characters: `is_adult: Literal[True]` plus an age_range
  whitelist -- an adaptation describing a minor-looking character fails
  parsing outright, before anything is persisted.
- shot grammar validated against core.cinematic_state's enums; exactly
  one camera movement per shot by construction (single-valued field).
- one filmable action per shot: bounded length and a conjunction-pattern
  check (deterministic heuristic -- see _MULTI_ACTION_PATTERNS).
- structural limits: 1-2 characters, 1-3 locations, 1-6 scenes, 4-15
  shots total, per-shot duration 2-8s.

Cross-field/semantic rules that need the WHOLE document (subject
references, shot/reverse-shot alternation, fidelity to the source text)
live in pipeline.adaptation_parser, not here."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from reel_harness.core.cinematic_state import CameraAngle, CameraMovement, ShotSize

MIN_CHARACTERS = 1
MAX_CHARACTERS = 2
MIN_LOCATIONS = 1
MAX_LOCATIONS = 3
MIN_SCENES = 1
MAX_SCENES = 6
MIN_TOTAL_SHOTS = 4
MAX_TOTAL_SHOTS = 15
MIN_SHOT_DURATION_SEC = 2.0
MAX_SHOT_DURATION_SEC = 8.0

# Adult age brackets only -- anything outside this set (including "teen",
# "child", numeric ranges under 20) is rejected at the schema layer.
ALLOWED_AGE_RANGES = frozenset({"20s", "30s", "40s", "50s", "60s"})

# The three prohibitions every story bible must carry verbatim -- written
# by the prompt, verified here so a model that drops them fails parsing.
REQUIRED_PROHIBITED_ELEMENTS = ("real people", "minors", "explicit content")

# Deterministic multi-action heuristic: a shot whose action chains steps
# with these connectors is not "one filmable action". Deliberately a
# small, documented list -- never claimed to catch every phrasing.
_MULTI_ACTION_PATTERNS = ("그리고 나서", "한 후에", "한 다음", "; ", " then ", " and then ")


class StoryBibleModel(BaseModel):
    premise: str = Field(min_length=1, max_length=300)
    theme: str = Field(min_length=1, max_length=200)
    setting: str = Field(min_length=1, max_length=300)
    time_period: str = Field(min_length=1, max_length=100)
    visual_style: str = Field(min_length=1, max_length=300)
    color_language: dict = Field(default_factory=dict)
    narrative_point_of_view: str = Field(min_length=1, max_length=100)
    ending_summary: str = Field(min_length=1, max_length=300)
    prohibited_elements: list[str]

    @field_validator("prohibited_elements")
    @classmethod
    def _must_include_required_prohibitions(cls, value: list[str]) -> list[str]:
        missing = [item for item in REQUIRED_PROHIBITED_ELEMENTS if item not in value]
        if missing:
            raise ValueError(f"prohibited_elements must include {missing}")
        return value


class CharacterModel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    role: str = Field(min_length=1, max_length=80)
    is_adult: Literal[True]
    age_range: str
    appearance: str = Field(min_length=1, max_length=400)
    wardrobe: str = Field(min_length=1, max_length=200)
    hair: str = Field(min_length=1, max_length=120)
    mannerisms: str = Field(default="", max_length=200)
    voice_style: str = Field(default="", max_length=120)
    # Identity elements held constant across every shot -- consumed
    # deterministically by pipeline.shot_prompt (F2 commit 4).
    fixed_identity: dict = Field(default_factory=dict)

    @field_validator("age_range")
    @classmethod
    def _adult_age_range_only(cls, value: str) -> str:
        if value not in ALLOWED_AGE_RANGES:
            raise ValueError(
                f"age_range {value!r} is not an allowed adult bracket "
                f"({', '.join(sorted(ALLOWED_AGE_RANGES))})"
            )
        return value


class LocationModel(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=400)
    lighting: str = Field(default="", max_length=120)
    time_of_day: str = Field(default="", max_length=60)
    weather: str = Field(default="", max_length=60)


class DialogueLineModel(BaseModel):
    speaker: str = Field(min_length=1, max_length=80)
    line: str = Field(min_length=1, max_length=200)


class ShotModel(BaseModel):
    shot_order: int = Field(ge=1)
    shot_size: str
    camera_angle: str
    camera_movement: str
    lens_style: str = Field(default="", max_length=60)
    subject: str = Field(min_length=1, max_length=80)
    action: str = Field(min_length=1, max_length=160)
    expression: str = Field(default="", max_length=120)
    blocking: str = Field(default="", max_length=160)
    lighting: str = Field(default="", max_length=120)
    duration_sec: float = Field(ge=MIN_SHOT_DURATION_SEC, le=MAX_SHOT_DURATION_SEC)
    dialogue_line: str | None = Field(default=None, max_length=200)

    @field_validator("shot_size")
    @classmethod
    def _valid_shot_size(cls, value: str) -> str:
        if value not in {s.value for s in ShotSize}:
            raise ValueError(f"unknown shot_size {value!r}")
        return value

    @field_validator("camera_angle")
    @classmethod
    def _valid_camera_angle(cls, value: str) -> str:
        if value not in {a.value for a in CameraAngle}:
            raise ValueError(f"unknown camera_angle {value!r}")
        return value

    @field_validator("camera_movement")
    @classmethod
    def _valid_camera_movement(cls, value: str) -> str:
        if value not in {m.value for m in CameraMovement}:
            raise ValueError(f"unknown camera_movement {value!r}")
        return value

    @field_validator("action")
    @classmethod
    def _single_filmable_action(cls, value: str) -> str:
        for pattern in _MULTI_ACTION_PATTERNS:
            if pattern in value:
                raise ValueError(
                    f"action must be ONE filmable action -- found chained-action pattern {pattern!r}"
                )
        return value


class SceneModel(BaseModel):
    scene_order: int = Field(ge=1)
    location_name: str = Field(min_length=1, max_length=80)
    story_purpose: str = Field(min_length=1, max_length=200)
    emotional_beat: str = Field(min_length=1, max_length=120)
    # Verbatim-ish anchor into the SOURCE text this scene dramatizes --
    # checked against the real source by the fidelity validator.
    source_beat: str = Field(min_length=1, max_length=160)
    dialogue: list[DialogueLineModel] = Field(default_factory=list)
    shots: list[ShotModel] = Field(min_length=1, max_length=5)


class AdaptationModel(BaseModel):
    logline: str = Field(min_length=1, max_length=200)
    synopsis: str = Field(min_length=1, max_length=1000)
    story_bible: StoryBibleModel
    characters: list[CharacterModel] = Field(min_length=MIN_CHARACTERS, max_length=MAX_CHARACTERS)
    locations: list[LocationModel] = Field(min_length=MIN_LOCATIONS, max_length=MAX_LOCATIONS)
    scenes: list[SceneModel] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)

    @field_validator("scenes")
    @classmethod
    def _total_shot_count_in_range(cls, value: list[SceneModel]) -> list[SceneModel]:
        total = sum(len(scene.shots) for scene in value)
        if not (MIN_TOTAL_SHOTS <= total <= MAX_TOTAL_SHOTS):
            raise ValueError(
                f"total shot count {total} outside [{MIN_TOTAL_SHOTS}, {MAX_TOTAL_SHOTS}]"
            )
        return value
