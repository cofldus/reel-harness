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
                     "action": "전화벨 소리에 시선을 내린다", "duration_sec": 3.0,
                     "dialogue_line": "받지 않을 거야."},
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
                     "action": "천천히 문 쪽으로 돌아선다", "duration_sec": 4.0,
                     "dialogue_line": "이제 그만하자."},
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


# -- craft rules ---------------------------------------------------------
#
# These four exist because nine real GPT-4o runs across three stories were
# measured, not because they seemed like good ideas. Every one of them is
# a defect that actually occurred: the single line of quoted speech in a
# source dropped entirely, shot counts from half to one-and-a-half times
# what was asked, camera angle collapsed to one value in most runs, and
# 2 of 9 plans where the camera never moved at all. All produced
# documents that were schema-valid and perfectly faithful, which is
# exactly why a separate layer was needed.

def _plan(shots: list[dict], source: str = "그는 창밖을 보았다.") -> tuple:
    """A minimal valid document wrapping the given shots, plus its source."""
    from reel_harness.pipeline.adaptation_schema import AdaptationModel

    document = {
        "logline": "한 인물이 밤의 방에서 결심에 이른다.",
        "synopsis": "그는 창밖을 보았다.",
        "story_bible": {
            "premise": "밤의 방", "theme": "quiet tension", "setting": "실내",
            "time_period": "현대", "visual_style": "soft practical",
            "color_language": {"palette": "cool", "contrast": "low"},
            "narrative_point_of_view": "third person",
            "ending_summary": "그는 돌아선다.",
            "prohibited_elements": ["real people", "explicit content", "minors"],
        },
        "characters": [{
            "name": "지우", "role": "protagonist", "is_adult": True, "age_range": "30s",
            "appearance": "oval face", "wardrobe": "grey coat", "hair": "short black",
            "mannerisms": "slow", "voice_style": "low",
            "fixed_identity": {"face": "oval face", "hair": "short black", "wardrobe": "grey coat"},
        }],
        "locations": [{
            "name": "방", "description": "night room", "lighting": "practical",
            "time_of_day": "night", "weather": "clear",
        }],
        "scenes": [{
            "scene_order": 1, "location_name": "방", "story_purpose": "도입",
            "emotional_beat": "불안", "source_beat": source, "dialogue": [],
            "shots": shots,
        }],
    }
    return AdaptationModel.model_validate(document), source


def _shot(order: int, **overrides) -> dict:
    base = {
        "shot_order": order, "shot_size": "medium", "camera_angle": "eye_level",
        "camera_movement": "locked", "lens_style": "50mm", "subject": "지우",
        "action": f"동작 {order}", "expression": "불안", "blocking": "선 채",
        "lighting": "practical", "duration_sec": 2.0, "dialogue_line": None,
    }
    base.update(overrides)
    return base


def test_a_plan_that_ignores_the_requested_runtime_is_sent_back() -> None:
    """The schema already bounds a plan to 4-15 shots, but that says
    nothing about the runtime that was ASKED for: four shots is a valid
    plan whether you ordered 32 seconds or 120. This ties the count to
    the request."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    four = [_shot(1), _shot(2, camera_angle="low_angle", camera_movement="pan"),
            _shot(3), _shot(4, camera_angle="high_angle")]
    model, source = _plan(four)
    errors = _craft_errors(model, source, target_shot_count=15)
    assert any("4 shots" in e and "needs 15" in e for e in errors)

    # Within one either way is room to end on a beat, not a defect.
    assert not [e for e in _craft_errors(model, source, target_shot_count=5) if "needs" in e]


def test_quoted_speech_in_the_source_must_survive_into_some_shot() -> None:
    """The compose screen tells users to write dialogue in quotes; a
    pipeline that discards it makes that instruction a lie."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    spoken_source = '그가 말했다. "이제 그만하자." 그리고 돌아섰다.'
    shots = [_shot(1), _shot(2, camera_angle="low_angle", camera_movement="pan"),
             _shot(3), _shot(4)]

    model, _ = _plan(shots, source=spoken_source)
    assert any("dialogue_line" in e for e in _craft_errors(model, spoken_source, None))

    kept = [_shot(1, dialogue_line="이제 그만하자."),
            _shot(2, camera_angle="low_angle", camera_movement="pan"), _shot(3), _shot(4)]
    model, _ = _plan(kept, source=spoken_source)
    assert not [e for e in _craft_errors(model, spoken_source, None) if "no shot carries" in e]


def test_a_sequence_shot_from_one_angle_is_sent_back() -> None:
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    model, source = _plan([_shot(1), _shot(2, camera_movement="pan"), _shot(3), _shot(4)])
    assert any("camera_angle" in e for e in _craft_errors(model, source, None))


def test_a_plan_where_the_camera_never_moves_is_sent_back() -> None:
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    model, source = _plan([
        _shot(1), _shot(2, camera_angle="low_angle"),
        _shot(3, camera_angle="high_angle"), _shot(4),
    ])
    assert any("locked" in e for e in _craft_errors(model, source, None))


def test_a_plan_that_satisfies_every_craft_rule_passes_clean() -> None:
    """The rules must be satisfiable together, not merely individually --
    a set of checks no real plan can pass at once would just exhaust the
    repair budget on every adaptation."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    spoken_source = '그가 말했다. "이제 그만하자." 그리고 돌아섰다.'
    model, _ = _plan([
        _shot(1, dialogue_line="이제 그만하자."),
        _shot(2, camera_angle="low_angle", camera_movement="pan",
              dialogue_line="가야 해."),
        _shot(3, camera_angle="high_angle", camera_movement="dolly_in"),
        _shot(4),
    ], source=spoken_source)
    assert _craft_errors(model, spoken_source, target_shot_count=4) == []


def test_one_location_split_into_a_scene_per_beat_is_sent_back() -> None:
    """A real run put four shots inside the same bus into four separate
    scenes. Cutting between scenes tells the audience that time or place
    jumped; here nothing had."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors
    from reel_harness.pipeline.adaptation_schema import AdaptationModel

    model, source = _plan([_shot(1), _shot(2, camera_angle="low_angle", camera_movement="pan"),
                           _shot(3, camera_angle="high_angle"), _shot(4)])
    # One scene holding all four shots is exactly right, and passes.
    assert not [e for e in _craft_errors(model, source, None) if "one shot each" in e]

    document = model.model_dump()
    base = document["scenes"][0]
    document["scenes"] = [
        {**base, "scene_order": n + 1, "shots": [{**base["shots"][n], "shot_order": 1}]}
        for n in range(4)
    ]
    split = AdaptationModel.model_validate(document)
    assert any("one shot each" in e for e in _craft_errors(split, source, None))


def test_a_single_scene_piece_is_never_asked_to_subdivide() -> None:
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    model, source = _plan([_shot(1), _shot(2, camera_angle="low_angle", camera_movement="pan"),
                           _shot(3, camera_angle="high_angle"), _shot(4)])
    assert not [e for e in _craft_errors(model, source, None) if "scene" in e]


def test_a_film_where_almost_nobody_speaks_is_sent_back() -> None:
    """Prose narrates what a screenplay lets people say. A real run gave
    one spoken line across four shots because the adaptation only ever
    forwarded the source's quoted speech -- writing lines is an
    adaptation's job, and the prompt constrains it to lines rather than
    events."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    quiet = [_shot(1, dialogue_line="한 마디."), _shot(2), _shot(3), _shot(4)]
    model, source = _plan(quiet)
    assert any("carry a dialogue_line" in e for e in _craft_errors(model, source, None))

    talkative = [_shot(1, dialogue_line="한 마디."), _shot(2, dialogue_line="두 마디."),
                 _shot(3), _shot(4)]
    model, source = _plan(talkative)
    assert not [e for e in _craft_errors(model, source, None) if "carry a dialogue_line" in e]


def test_silence_is_allowed_where_it_is_the_point() -> None:
    """Half, not all: a departure or a held look earns its silence, and a
    rule demanding every shot speak would make worse films."""
    from reel_harness.pipeline.adaptation_parser import _craft_errors

    model, source = _plan([
        _shot(1, dialogue_line="가져가세요."), _shot(2, dialogue_line="고맙네."),
        _shot(3), _shot(4),
    ])
    assert not [e for e in _craft_errors(model, source, None) if "carry a dialogue_line" in e]


def test_a_story_may_introduce_more_than_two_characters() -> None:
    """Two was a short-form default and it silently cut people out of
    real stories. A test film's mother -- who the plot turns on -- was
    dropped, so the hospital and crash shots held a person with no
    reference sheet: the video model invented her, and made her male."""
    from reel_harness.pipeline.adaptation_schema import MAX_CHARACTERS

    assert MAX_CHARACTERS >= 4


def test_the_character_cap_reaches_the_director_as_a_constraint() -> None:
    """The schema is the gate, but the prompt is what stops the model
    writing a cast it will then have to cut."""
    from reel_harness.providers.base import AdaptationRequest
    from reel_harness.providers.narrative_prompts import build_user_prompt

    prompt = build_user_prompt(AdaptationRequest(
        source_text="짧은 이야기입니다.", language="ko", genre=None, tone=None,
        target_duration_sec=64, aspect_ratio="9:16", max_characters=4,
    ))
    assert "Maximum characters: 4" in prompt


def test_the_director_is_told_a_location_must_state_its_layout() -> None:
    """A description that is only mood leaves every shot to invent the
    room again. The one that produced a clerk entering his own shop said
    "작은 계산대와 유리문이 있는 편의점" -- true, and useless for staging."""
    from reel_harness.providers.base import AdaptationRequest
    from reel_harness.providers.narrative_prompts import build_user_prompt

    prompt = build_user_prompt(AdaptationRequest(
        source_text="짧은 이야기입니다.", language="ko", genre=None, tone=None,
        target_duration_sec=32, aspect_ratio="9:16",
    ))
    assert "GEOGRAPHY" in prompt
    assert "which direction is outside" in prompt
    assert "never against the frame alone" in prompt


def test_every_adaptation_rule_reaches_the_adaptation_prompt() -> None:
    """Three rules were written into the REFINEMENT prompt by mistake and
    the adaptation never saw any of them, so an apparent improvement in
    logline and field quality was the model change alone. Rules about
    adapting belong where adapting is asked for."""
    from reel_harness.providers.base import AdaptationRequest
    from reel_harness.providers.narrative_prompts import (
        REFINEMENT_SYSTEM_PROMPT,
        build_user_prompt,
    )

    prompt = build_user_prompt(AdaptationRequest(
        source_text="짧은 이야기입니다.", language="ko", genre=None, tone=None,
        target_duration_sec=32, aspect_ratio="9:16",
    ))
    for rule in ("NUMBERING", "GEOGRAPHY", "FIELD DISCIPLINE"):
        assert rule in prompt, f"{rule} never reaches the director"
        assert rule not in REFINEMENT_SYSTEM_PROMPT, f"{rule} is still in the wrong prompt"
