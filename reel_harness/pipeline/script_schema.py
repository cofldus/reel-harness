from __future__ import annotations

import json

from pydantic import BaseModel, Field, ValidationError

from reel_harness.core.errors import SchemaValidationError

MIN_SCENES = 3
MAX_SCENES = 10
MIN_SCENE_DURATION_SEC = 2.0
MAX_SCENE_DURATION_SEC = 12.0


class Scene(BaseModel):
    voiceover: str = Field(min_length=1, max_length=500)
    subtitle: str = Field(min_length=1, max_length=120)
    visual_query: str = Field(min_length=1, max_length=200)
    duration_hint_sec: float = Field(ge=MIN_SCENE_DURATION_SEC, le=MAX_SCENE_DURATION_SEC)


class Script(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    scenes: list[Scene] = Field(min_length=MIN_SCENES, max_length=MAX_SCENES)


def parse_script(raw_text: str) -> Script:
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"script is not valid JSON: {exc}") from exc
    try:
        return Script.model_validate(data)
    except ValidationError as exc:
        raise SchemaValidationError(f"script failed schema validation: {exc}") from exc
