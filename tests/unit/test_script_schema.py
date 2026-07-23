from __future__ import annotations

import json

import pytest

from reel_harness.core.errors import SchemaValidationError
from reel_harness.pipeline.script_schema import parse_script


def _valid_script(scene_count: int = 3) -> str:
    scenes = [
        {
            "voiceover": f"voiceover {i}",
            "subtitle": f"subtitle {i}",
            "visual_query": f"query {i}",
            "duration_hint_sec": 4.0,
        }
        for i in range(scene_count)
    ]
    return json.dumps({"title": "A title", "scenes": scenes})


def test_valid_script_parses() -> None:
    script = parse_script(_valid_script())
    assert len(script.scenes) == 3


def test_malformed_json_raises_schema_error() -> None:
    with pytest.raises(SchemaValidationError):
        parse_script("{not valid json")


def test_too_few_scenes_raises_schema_error() -> None:
    with pytest.raises(SchemaValidationError):
        parse_script(_valid_script(scene_count=1))


def test_scene_duration_out_of_bounds_raises_schema_error() -> None:
    payload = json.loads(_valid_script())
    payload["scenes"][0]["duration_hint_sec"] = 999
    with pytest.raises(SchemaValidationError):
        parse_script(json.dumps(payload))


def test_missing_required_field_raises_schema_error() -> None:
    payload = json.loads(_valid_script())
    del payload["scenes"][0]["subtitle"]
    with pytest.raises(SchemaValidationError):
        parse_script(json.dumps(payload))
