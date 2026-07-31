"""Adaptation schema + parser (pipeline.adaptation_schema /
adaptation_parser): schema-level hard rules (adult-only, grammar enums,
one-action-per-shot, structural limits), lenient-extraction bounds, the
seven semantic rules, and the fidelity heuristic -- every rule gets both
a passing and a failing case."""
from __future__ import annotations

import json

import pytest

from reel_harness.pipeline.adaptation_parser import (
    AdaptationValidationError,
    parse_adaptation,
)

SOURCE = (
    "그날 밤, 지우는 호텔 창밖의 비를 오래 바라보았다. "
    "전화벨이 울렸지만 받지 않았다. "
    "마침내 그녀는 천천히 문 쪽으로 돌아섰다."
)


def _valid_adaptation() -> dict:
    return {
        "logline": "비 오는 밤, 한 여자가 전화를 외면하고 문을 향해 돌아선다.",
        "synopsis": (
            "호텔 방에 머무는 지우는 창밖 비를 바라보며 걸려오는 전화를 외면하다, "
            "결심한 듯 문을 향해 돌아선다."
        ),
        "story_bible": {
            "premise": "지우는 호텔 창밖의 비를 바라보다 문 쪽으로 돌아선다.",
            "theme": "망설임과 결심",
            "setting": "비 오는 밤의 호텔 방",
            "time_period": "현대",
            "visual_style": "soft practical lighting, muted palette",
            "color_language": {"palette": "cool neutrals"},
            "narrative_point_of_view": "third person",
            "ending_summary": "그녀는 천천히 문 쪽으로 돌아선다.",
            "prohibited_elements": ["real people", "minors", "explicit content"],
        },
        "characters": [
            {
                "name": "지우", "role": "protagonist", "is_adult": True, "age_range": "30s",
                "appearance": "oval face, calm eyes", "wardrobe": "grey coat",
                "hair": "black short hair", "fixed_identity": {"hair": "black short hair"},
            },
        ],
        "locations": [
            {"name": "호텔 방", "description": "a hotel room at night, rain outside",
             "lighting": "soft practicals", "time_of_day": "night", "weather": "rain"},
        ],
        "scenes": [
            {
                "scene_order": 1, "location_name": "호텔 방",
                "story_purpose": "도입", "emotional_beat": "정적인 불안",
                "source_beat": "지우는 호텔 창밖의 비를 오래 바라보았다",
                "dialogue": [],
                "shots": [
                    {"shot_order": 1, "shot_size": "medium", "camera_angle": "eye_level",
                     "camera_movement": "locked", "subject": "지우",
                     "action": "창밖을 바라본다", "duration_sec": 4.0},
                    {"shot_order": 2, "shot_size": "close_up", "camera_angle": "profile",
                     "camera_movement": "locked", "subject": "지우",
                     "action": "전화벨 소리에 시선을 내린다", "duration_sec": 3.0},
                ],
            },
            {
                "scene_order": 2, "location_name": "호텔 방",
                "story_purpose": "전환", "emotional_beat": "결심",
                "source_beat": "그녀는 천천히 문 쪽으로 돌아섰다",
                "dialogue": [],
                "shots": [
                    {"shot_order": 1, "shot_size": "medium_close_up", "camera_angle": "eye_level",
                     "camera_movement": "dolly_in", "subject": "지우",
                     "action": "천천히 문 쪽으로 돌아선다", "duration_sec": 4.0},
                    {"shot_order": 2, "shot_size": "wide", "camera_angle": "low_angle",
                     "camera_movement": "locked", "subject": "지우",
                     "action": "문 앞에 멈춰 선다", "duration_sec": 3.0},
                ],
            },
        ],
    }


def _parse(data: dict, **kwargs):
    return parse_adaptation(json.dumps(data, ensure_ascii=False), source_text=SOURCE, **kwargs)


def test_valid_adaptation_parses() -> None:
    adaptation = _parse(_valid_adaptation())
    assert adaptation.logline
    assert len(adaptation.characters) == 1
    assert sum(len(s.shots) for s in adaptation.scenes) == 4


def test_fenced_json_is_extracted_once() -> None:
    raw = "```json\n" + json.dumps(_valid_adaptation(), ensure_ascii=False) + "\n```"
    adaptation = parse_adaptation(raw, source_text=SOURCE)
    assert adaptation.logline


def test_non_json_collects_a_clear_error() -> None:
    with pytest.raises(AdaptationValidationError) as excinfo:
        parse_adaptation("죄송하지만 그 요청은...", source_text=SOURCE)
    assert "JSON" in excinfo.value.errors[0]


def test_minor_character_rejected_at_schema_layer() -> None:
    data = _valid_adaptation()
    data["characters"][0]["age_range"] = "teens"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("age_range" in e for e in excinfo.value.errors)

    data = _valid_adaptation()
    data["characters"][0]["is_adult"] = False
    with pytest.raises(AdaptationValidationError):
        _parse(data)


def test_dropped_required_prohibitions_rejected() -> None:
    data = _valid_adaptation()
    data["story_bible"]["prohibited_elements"] = ["real people"]
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("prohibited_elements" in e for e in excinfo.value.errors)


def test_unknown_camera_grammar_rejected() -> None:
    data = _valid_adaptation()
    data["scenes"][0]["shots"][0]["camera_movement"] = "pan and dolly"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("camera_movement" in e for e in excinfo.value.errors)


def test_chained_action_rejected() -> None:
    data = _valid_adaptation()
    data["scenes"][0]["shots"][0]["action"] = "창밖을 바라보고 한 다음 전화를 받는다"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("ONE filmable action" in e for e in excinfo.value.errors)


def test_total_shot_count_limits() -> None:
    data = _valid_adaptation()
    # Drop to 3 shots total (below MIN_TOTAL_SHOTS=4).
    data["scenes"][1]["shots"] = data["scenes"][1]["shots"][:1]
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("total shot count" in e for e in excinfo.value.errors)


def test_undeclared_subject_and_location_rejected() -> None:
    data = _valid_adaptation()
    data["scenes"][0]["shots"][0]["subject"] = "민수"
    data["scenes"][1]["location_name"] = "카페"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    joined = "\n".join(excinfo.value.errors)
    assert "'민수'" in joined and "'카페'" in joined


def test_dialogue_scene_requires_shot_reverse_shot() -> None:
    data = _valid_adaptation()
    data["characters"].append({
        "name": "민수", "role": "supporting", "is_adult": True, "age_range": "40s",
        "appearance": "tall, tired eyes", "wardrobe": "dark suit", "hair": "grey short hair",
    })
    scene = data["scenes"][0]
    scene["dialogue"] = [
        {"speaker": "지우", "line": "왜 왔어요?"},
        {"speaker": "민수", "line": "할 말이 있어서."},
    ]
    # Both shots stay on 지우 -- consecutive same-subject in a 2-speaker scene.
    scene["shots"][0]["dialogue_line"] = "왜 왔어요?"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("shot/reverse-shot" in e for e in excinfo.value.errors)

    # Alternating subjects passes.
    scene["shots"][1]["subject"] = "민수"
    scene["shots"][1]["dialogue_line"] = "할 말이 있어서."
    adaptation = _parse(data)
    assert adaptation.scenes[0].shots[1].subject == "민수"


def test_dialogue_line_must_belong_to_subject() -> None:
    data = _valid_adaptation()
    data["scenes"][0]["dialogue"] = [{"speaker": "지우", "line": "왜 왔어요?"}]
    data["scenes"][0]["shots"][0]["dialogue_line"] = "여기 없는 대사"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("dialogue_line" in e for e in excinfo.value.errors)


def test_fabricated_source_beat_rejected_and_real_quote_passes() -> None:
    data = _valid_adaptation()
    data["scenes"][0]["source_beat"] = "우주선이 화성에 착륙했다"
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("source_beat" in e for e in excinfo.value.errors)
    # The unmodified fixture's beats are genuine quotes -- passes.
    assert _parse(_valid_adaptation())


def test_scene_and_shot_order_must_be_contiguous() -> None:
    data = _valid_adaptation()
    data["scenes"][1]["scene_order"] = 5
    with pytest.raises(AdaptationValidationError) as excinfo:
        _parse(data)
    assert any("scene_order" in e for e in excinfo.value.errors)


def test_error_class_is_stage_retryable_schema_invalid() -> None:
    from reel_harness.core.errors import SchemaValidationError

    try:
        parse_adaptation("not json", source_text=SOURCE)
    except SchemaValidationError as exc:
        assert exc.code == "SCHEMA_INVALID"
        assert exc.retryable is True
    else:  # pragma: no cover
        pytest.fail("expected SchemaValidationError")
