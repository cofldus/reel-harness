"""Metrics for the fable-adapt-eval command.

Everything here is pure over an already-validated model: no network, no
provider, no cost. The point of the command is to make prompt regressions
visible, which only works if the measurement itself is trustworthy.
"""
from __future__ import annotations

from reel_harness.ops.adapt_eval import (
    SAMPLE_STORIES,
    format_plan,
    format_report,
    has_quoted_speech,
    measure,
)
from reel_harness.pipeline.adaptation_schema import AdaptationModel

SOURCE = '그가 말했다. "이제 그만하자." 그리고 그는 천천히 돌아섰다.'


def _shot(order: int, **over) -> dict:
    base = {
        "shot_order": order, "shot_size": "medium", "camera_angle": "eye_level",
        "camera_movement": "locked", "lens_style": "50mm", "subject": "지우",
        "action": f"동작 {order}", "expression": "불안", "blocking": "선 채",
        "lighting": "practical", "duration_sec": 2.0, "dialogue_line": None,
    }
    base.update(over)
    return base


def _model(shots: list[dict], beat: str = SOURCE) -> AdaptationModel:
    return AdaptationModel.model_validate({
        "logline": "한 인물이 밤의 방에서 결심에 이른다.",
        "synopsis": "그는 창밖을 보았다.",
        "story_bible": {
            "premise": "밤의 방", "theme": "quiet", "setting": "실내",
            "time_period": "현대", "visual_style": "soft", "narrative_point_of_view": "third person",
            "color_language": {"palette": "cool", "contrast": "low"},
            "ending_summary": "그는 돌아선다.",
            "prohibited_elements": ["real people", "explicit content", "minors"],
        },
        "characters": [{
            "name": "지우", "role": "protagonist", "is_adult": True, "age_range": "30s",
            "appearance": "oval face", "wardrobe": "grey coat", "hair": "short",
            "mannerisms": "slow", "voice_style": "low",
            "fixed_identity": {"face": "oval", "hair": "short", "wardrobe": "grey coat"},
        }],
        "locations": [{
            "name": "방", "description": "night room", "lighting": "practical",
            "time_of_day": "night", "weather": "clear",
        }],
        "scenes": [{
            "scene_order": 1, "location_name": "방", "story_purpose": "도입",
            "emotional_beat": "불안", "source_beat": beat, "dialogue": [], "shots": shots,
        }],
    })


def test_a_static_single_angle_plan_is_reported_as_both() -> None:
    """The two collapse modes are independent: a plan can vary its angle
    and still never move, so they are measured separately."""
    metrics = measure(_model([_shot(1), _shot(2), _shot(3), _shot(4)]), SOURCE)
    assert metrics.single_angle
    assert metrics.camera_never_moves
    assert metrics.move_top_share == 1.0


def test_a_varied_plan_trips_neither_flag() -> None:
    metrics = measure(_model([
        _shot(1), _shot(2, camera_angle="low_angle", camera_movement="pan"),
        _shot(3, camera_angle="high_angle", camera_movement="dolly_in"), _shot(4),
    ]), SOURCE)
    assert not metrics.single_angle
    assert not metrics.camera_never_moves
    assert metrics.size_kinds == 1  # sizes are measured independently of angles


def test_runtime_fit_allows_one_either_way_and_no_more() -> None:
    """A plan needs room to end on a beat; half the ordered runtime is a
    different film."""
    shots = [_shot(n) for n in range(1, 5)]
    assert measure(_model(shots), SOURCE, target_shots=4).fits_runtime
    assert measure(_model(shots), SOURCE, target_shots=5).fits_runtime
    assert not measure(_model(shots), SOURCE, target_shots=8).fits_runtime
    # No target asked for means nothing to fail against.
    assert measure(_model(shots), SOURCE).fits_runtime


def test_duplicate_actions_and_dialogue_share_are_counted() -> None:
    metrics = measure(_model([
        _shot(1, action="같은 동작", dialogue_line="이제 그만하자."),
        _shot(2, action="같은 동작"), _shot(3), _shot(4),
    ]), SOURCE)
    assert metrics.duplicate_actions == 1
    assert metrics.dialogue_share == 0.25


def test_fidelity_counts_only_beats_actually_drawn_from_the_source() -> None:
    real = measure(_model([_shot(n) for n in range(1, 5)]), SOURCE)
    assert real.beats_quoted == 1.0
    invented = measure(_model([_shot(n) for n in range(1, 5)], beat="완전히 다른 문장입니다."), SOURCE)
    assert invented.beats_quoted == 0.0


def test_quoted_speech_detection_matches_the_parser() -> None:
    """The eval and the validator must agree on what counts as speech, or
    the report contradicts the repair loop."""
    assert has_quoted_speech(SOURCE)
    assert has_quoted_speech("“이제 그만하자.” 그녀가 말했다.")
    assert not has_quoted_speech("그는 아무 말도 하지 않았다.")


def test_the_report_names_each_collapse_it_finds() -> None:
    from reel_harness.ops.adapt_eval import RunResult

    flat = measure(_model([_shot(n) for n in range(1, 5)]), SOURCE, target_shots=8)
    text = format_report([RunResult("작품", 1, flat, None)])
    assert "ONE-ANGLE" in text and "NO-MOVEMENT" in text and "RUNTIME" in text
    assert "single angle:   1" in text


def test_a_failed_run_is_reported_rather_than_swallowed() -> None:
    from reel_harness.ops.adapt_eval import RunResult

    text = format_report([RunResult("작품", 1, None, None, "TimeoutError: upstream")])
    assert "FAILED" in text and "TimeoutError" in text


def test_the_plan_printout_shows_grammar_and_dialogue() -> None:
    """Numbers say whether a plan is varied; only the plan says whether it
    is any good."""
    text = format_plan(_model([
        _shot(1, dialogue_line="이제 그만하자."),
        _shot(2, camera_movement="pan"), _shot(3), _shot(4),
    ]))
    assert "이제 그만하자." in text
    assert "medium · eye_level · pan" in text


def test_the_bundled_samples_differ_from_each_other() -> None:
    """A prompt that only works on quiet interior monologue is not a
    prompt that works, so the samples must not all be the same shape."""
    assert len(SAMPLE_STORIES) >= 3
    speaking = [t for t, s in SAMPLE_STORIES.items() if has_quoted_speech(s)]
    assert speaking and len(speaking) < len(SAMPLE_STORIES)
