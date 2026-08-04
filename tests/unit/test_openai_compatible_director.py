"""Contract tests for the OpenAI-compatible Narrative Director.

These use httpx.MockTransport -- no sockets are opened, so the
network-block fixture stays effective. They are provider-adapter CONTRACT
tests: they prove the adapter speaks the endpoint's protocol correctly
and honors the retry/auth/redaction rules. They do NOT claim a real
live adaptation succeeded; live verification requires real credentials
and is reported separately (see docs/OPERATIONS.md).

All keys below are obviously-fake placeholders.
"""
from __future__ import annotations

import json

import httpx
import pytest

from reel_harness.core.adaptation_service import run_adaptation
from reel_harness.core.errors import ProviderAuthError, SchemaValidationError, TransientProviderError
from reel_harness.pipeline.adaptation_parser import AdaptationValidationError, parse_adaptation
from reel_harness.providers.base import AdaptationRequest
from reel_harness.providers.narrative_prompts import NARRATIVE_PROMPT_VERSION
from reel_harness.providers.openai_compatible_director import OpenAICompatibleNarrativeDirector

FAKE_KEY = "FAKE-DIRECTOR-TEST-KEY-0000000000"

SOURCE = (
    "그날 밤, 지우는 호텔 창밖의 비를 오래 바라보았다. "
    "마침내 그녀는 천천히 문 쪽으로 돌아섰다."
)

# 32 seconds, because _valid_document() below is a four-shot plan and the
# parser's craft layer now checks that a plan actually fits the runtime it
# was asked for. Asking for 60s and returning four shots is exactly the
# defect that check exists to catch.
REQUEST = AdaptationRequest(
    source_text=SOURCE, language="ko", genre="drama", tone="quiet",
    target_duration_sec=32, aspect_ratio="9:16",
)


def _valid_document() -> dict:
    """A minimal document that passes the REAL parser and validators --
    built from the same source text so the fidelity check is genuine."""
    return {
        "logline": "비 오는 밤, 한 인물이 결심에 이른다.",
        "synopsis": "지우는 창밖의 비를 바라보다 문 쪽으로 돌아선다.",
        "story_bible": {
            "premise": "지우는 창밖의 비를 바라보다 돌아선다.",
            "theme": "망설임", "setting": "호텔 방", "time_period": "현대",
            "visual_style": "soft practical lighting",
            "color_language": {"palette": "cool neutrals"},
            "narrative_point_of_view": "third person",
            "ending_summary": "그녀는 문 쪽으로 돌아선다.",
            "prohibited_elements": ["real people", "minors", "explicit content"],
        },
        "characters": [{
            "name": "지우", "role": "protagonist", "is_adult": True, "age_range": "30s",
            "appearance": "oval face", "wardrobe": "grey coat", "hair": "black short hair",
            "fixed_identity": {"hair": "black short hair"},
        }],
        "locations": [{
            "name": "호텔 방", "description": "a hotel room at night",
            "lighting": "soft", "time_of_day": "night", "weather": "rain",
        }],
        "scenes": [{
            "scene_order": 1, "location_name": "호텔 방",
            "story_purpose": "도입", "emotional_beat": "불안",
            "source_beat": "지우는 호텔 창밖의 비를 오래 바라보았다",
            "dialogue": [],
            "shots": [
                {"shot_order": 1, "shot_size": "medium", "camera_angle": "eye_level",
                 "camera_movement": "locked", "subject": "지우",
                 "action": "창밖을 바라본다", "duration_sec": 4.0},
                {"shot_order": 2, "shot_size": "close_up", "camera_angle": "profile",
                 "camera_movement": "locked", "subject": "지우",
                 "action": "시선을 내린다", "duration_sec": 3.0,
                 "dialogue_line": "받지 않을 거야."},
                {"shot_order": 3, "shot_size": "wide", "camera_angle": "low_angle",
                 "camera_movement": "dolly_in", "subject": "지우",
                 "action": "천천히 돌아선다", "duration_sec": 3.0,
                 "dialogue_line": "이제 그만하자."},
                {"shot_order": 4, "shot_size": "medium_close_up", "camera_angle": "eye_level",
                 "camera_movement": "locked", "subject": "지우",
                 "action": "문 앞에 멈춘다", "duration_sec": 3.0},
            ],
        }],
    }


def _completion_body(content: str, request_id: str = "req-fake-1") -> dict:
    return {
        "id": request_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 500, "completion_tokens": 900, "total_tokens": 1400},
    }


def _director(handler, **overrides) -> OpenAICompatibleNarrativeDirector:
    defaults = dict(
        base_url="https://llm.example.invalid/v1",
        model="test-model",
        api_key=FAKE_KEY,
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return OpenAICompatibleNarrativeDirector(transport=httpx.MockTransport(handler), **defaults)


def test_successful_adaptation_parses_and_carries_metadata() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["payload"] = json.loads(request.content)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200, json=_completion_body(json.dumps(_valid_document(), ensure_ascii=False)),
        )

    director = _director(handler)
    result = director.adapt_story(REQUEST)

    assert result.provider_id == "openai-compatible"
    assert result.prompt_version == NARRATIVE_PROMPT_VERSION
    assert result.usage == {"prompt_tokens": 500, "completion_tokens": 900, "total_tokens": 1400}
    # JSON mode requested, source story present, credential in the header only.
    assert seen["payload"]["response_format"] == {"type": "json_object"}
    assert SOURCE in seen["payload"]["messages"][1]["content"]
    assert seen["auth"] == f"Bearer {FAKE_KEY}"

    adaptation = parse_adaptation(result.raw_text, source_text=SOURCE)
    assert len(adaptation.scenes) == 1


def test_fenced_output_still_parses_through_the_real_parser() -> None:
    fenced = "```json\n" + json.dumps(_valid_document(), ensure_ascii=False) + "\n```"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body(fenced))

    result = _director(handler).adapt_story(REQUEST)
    assert parse_adaptation(result.raw_text, source_text=SOURCE)


def test_repair_call_carries_source_and_errors() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload["messages"][1]["content"])
        return httpx.Response(
            200, json=_completion_body(json.dumps(_valid_document(), ensure_ascii=False)),
        )

    director = _director(handler)
    director.repair_adaptation(REQUEST, "{\"broken\": true}", ["characters: field required"])

    prompt = seen[0]
    assert SOURCE in prompt  # the story is still in context on a repair
    assert "characters: field required" in prompt
    assert "{\"broken\": true}" in prompt


def test_repair_loop_recovers_through_the_real_service() -> None:
    """First response is schema-invalid, the repair is valid -- the whole
    loop runs against the adapter, not a stub."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=_completion_body('{"logline": "only this"}'))
        return httpx.Response(
            200, json=_completion_body(json.dumps(_valid_document(), ensure_ascii=False)),
        )

    outcome = run_adaptation(_director(handler), REQUEST)
    assert outcome.attempts == 2
    assert calls["n"] == 2
    assert outcome.repair_errors


def test_repair_exhaustion_raises_after_bounded_calls() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json=_completion_body('{"logline": "never complete"}'))

    with pytest.raises(AdaptationValidationError):
        run_adaptation(_director(handler), REQUEST)
    assert calls["n"] == 3  # initial + 2 repairs, never unbounded


def test_refusal_is_a_schema_error_not_a_retry_storm() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, json={
            "id": "req-refusal",
            "choices": [{"message": {"role": "assistant", "refusal": "I can't help with that"}}],
        })

    with pytest.raises(SchemaValidationError):
        _director(handler).adapt_story(REQUEST)
    assert calls["n"] == 1  # a refusal is never retried at the transport layer


def test_rate_limit_is_retried_and_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"}, json={"error": "slow down"})
        return httpx.Response(
            200, json=_completion_body(json.dumps(_valid_document(), ensure_ascii=False)),
        )

    result = _director(handler).adapt_story(REQUEST)
    assert calls["n"] == 2
    assert parse_adaptation(result.raw_text, source_text=SOURCE)


def test_timeout_is_retried_then_surfaces_as_transient() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ReadTimeout("simulated read timeout", request=request)

    with pytest.raises(TransientProviderError):
        _director(handler).adapt_story(REQUEST)
    assert calls["n"] == 3  # initial + max_retries


def test_auth_failure_is_never_retried_and_never_echoes_the_key() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        # A hostile/naive endpoint echoing the credential back in the body.
        return httpx.Response(401, json={"error": f"bad key {FAKE_KEY}"})

    with pytest.raises(ProviderAuthError) as excinfo:
        _director(handler).adapt_story(REQUEST)
    assert calls["n"] == 1
    assert FAKE_KEY not in str(excinfo.value)


def test_registry_resolves_the_tier_and_snapshot_pins_it() -> None:
    from reel_harness.config import Settings
    from reel_harness.providers.registry import (
        cinematic_provider_snapshot,
        resolve_narrative_director,
        resolve_narrative_director_for_snapshot,
    )

    settings = Settings(
        _env_file=None, narrative_provider="openai-compatible",
        llm_base_url="https://llm.example.invalid/v1", llm_model="test-model",
        llm_api_key="FAKE-REGISTRY-TEST-KEY",
    )
    director = resolve_narrative_director("openai-compatible", settings)
    assert director.provider_id == "openai-compatible"

    snapshot = cinematic_provider_snapshot(settings)
    assert snapshot["narrative_provider"] == "openai-compatible"
    assert snapshot["narrative_model"] == "test-model"
    assert snapshot["narrative_base_url_host"] == "llm.example.invalid"
    # The safe host is pinned; the credential never appears anywhere.
    assert "FAKE-REGISTRY-TEST-KEY" not in json.dumps(snapshot)

    resolved = resolve_narrative_director_for_snapshot(snapshot, settings)
    assert resolved.provider_id == "openai-compatible"


def test_pinned_host_mismatch_yields_an_unconfigured_director() -> None:
    from reel_harness.config import Settings
    from reel_harness.core.errors import ProviderNotConfiguredError
    from reel_harness.providers.registry import resolve_narrative_director_for_snapshot

    settings = Settings(
        _env_file=None, narrative_provider="openai-compatible",
        llm_base_url="https://different.example.invalid/v1", llm_model="test-model",
        llm_api_key="FAKE-REGISTRY-TEST-KEY",
    )
    snapshot = {
        "narrative_provider": "openai-compatible",
        "narrative_base_url_host": "llm.example.invalid",
    }
    director = resolve_narrative_director_for_snapshot(snapshot, settings)
    with pytest.raises(ProviderNotConfiguredError, match="does not match"):
        director.adapt_story(REQUEST)


def test_missing_credentials_fail_startup_validation() -> None:
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    with pytest.raises(ProviderConfigurationError, match="credentials are not configured"):
        validate_provider_settings(
            Settings(_env_file=None, narrative_provider="openai-compatible"),
        )
    with pytest.raises(ProviderConfigurationError, match="unknown narrative provider"):
        validate_provider_settings(Settings(_env_file=None, narrative_provider="veo"))


def test_adapter_requires_endpoint_configuration() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleNarrativeDirector(base_url="", model="m", api_key=FAKE_KEY)
    with pytest.raises(ValueError):
        OpenAICompatibleNarrativeDirector(
            base_url="https://x.invalid", model="", api_key=FAKE_KEY,
        )
    with pytest.raises(ValueError):
        OpenAICompatibleNarrativeDirector(base_url="https://x.invalid", model="m", api_key="")
