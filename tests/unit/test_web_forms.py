from __future__ import annotations

from reel_harness.web.forms import validate_new_job_form


def _valid_kwargs(**overrides) -> dict:
    base = dict(
        topic="김치찌개 맛있게 끓이는 법", language="ko", duration_seconds=30, style="cooking",
        provider_profile="demo", burn_subtitles=True,
    )
    base.update(overrides)
    return base


def test_valid_form_passes() -> None:
    result = validate_new_job_form(**_valid_kwargs())
    assert result.ok
    assert result.value.topic == "김치찌개 맛있게 끓이는 법"
    assert result.errors == {}


def test_empty_topic_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(topic="   "))
    assert not result.ok
    assert "topic" in result.errors


def test_topic_too_long_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(topic="a" * 201))
    assert not result.ok
    assert "topic" in result.errors


def test_topic_with_control_characters_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(topic="hello\x00world"))
    assert not result.ok
    assert "topic" in result.errors


def test_unsupported_language_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(language="fr"))
    assert not result.ok
    assert "language" in result.errors


def test_duration_out_of_range_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(duration_seconds=5))
    assert not result.ok
    assert "duration_seconds" in result.errors

    result_high = validate_new_job_form(**_valid_kwargs(duration_seconds=999))
    assert not result_high.ok
    assert "duration_seconds" in result_high.errors


def test_unsupported_style_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(style="not-a-style"))
    assert not result.ok
    assert "style" in result.errors


def test_unsupported_provider_profile_rejected() -> None:
    result = validate_new_job_form(**_valid_kwargs(provider_profile="not-a-profile"))
    assert not result.ok
    assert "provider_profile" in result.errors


def test_topic_is_stripped() -> None:
    result = validate_new_job_form(**_valid_kwargs(topic="  hello  "))
    assert result.ok
    assert result.value.topic == "hello"
