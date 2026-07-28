"""Contract tests for the OpenAI-compatible LLM adapter.

These use httpx.MockTransport (no sockets are opened, so the network-block
fixture stays effective) and are classified as provider-adapter contract
tests -- they do NOT claim a real end-to-end provider call succeeded.
All keys below are obviously-fake placeholders.
"""
from __future__ import annotations

import json

import httpx
import pytest

from reel_harness.core.errors import ProviderAuthError, SchemaValidationError, TransientProviderError
from reel_harness.pipeline.script_schema import parse_script
from reel_harness.providers.base import ChannelContext
from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider

FAKE_KEY = "FAKE-ADAPTER-TEST-KEY-000000000000"

CTX = ChannelContext(channel_id="ch1", niche="cooking", language="en", style_preset={})

VALID_SCRIPT = {
    "title": "3 knife skills",
    "scenes": [
        {
            "voiceover": f"Scene {i} voiceover.",
            "subtitle": f"Scene {i}",
            "visual_query": f"kitchen scene {i}",
            "duration_hint_sec": 4.0,
        }
        for i in range(3)
    ],
}


def _completion_body(content: str, request_id: str = "req-fake-1") -> dict:
    return {
        "id": request_id,
        "choices": [{"message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300},
    }


def _provider(handler, **overrides) -> OpenAICompatibleLLMProvider:
    defaults = dict(
        base_url="https://llm.example.invalid/v1",
        model="test-model",
        api_key=FAKE_KEY,
        max_retries=2,
        retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return OpenAICompatibleLLMProvider(transport=httpx.MockTransport(handler), **defaults)


def test_successful_script_response_parses_and_carries_metadata() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["correlation"] = request.headers.get("x-request-id")
        seen["payload"] = json.loads(request.content)
        return httpx.Response(
            200, json=_completion_body(json.dumps(VALID_SCRIPT)), headers={"x-request-id": "hdr-req-9"},
        )

    result = _provider(handler).generate_script("3 knife skills", CTX)
    assert result.provider_id == "openai-compatible"
    assert result.model_id == "test-model"
    assert result.prompt_version == "openai-compat-script-v1"
    assert result.request_id == "hdr-req-9"
    assert result.usage == {"prompt_tokens": 100, "completion_tokens": 200, "total_tokens": 300}

    script = parse_script(result.raw_text)  # downstream schema validation accepts it
    assert len(script.scenes) == 3

    assert seen["auth"] == f"Bearer {FAKE_KEY}"  # auth sent as a header only
    assert seen["correlation"], "a correlation id must be attached to every request"
    payload = seen["payload"]
    assert payload["model"] == "test-model"
    assert [m["role"] for m in payload["messages"]] == ["system", "user"]


def test_generate_topic_parses_structured_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body(json.dumps({"topic": "5 minute fried rice"})))

    topic = _provider(handler).generate_topic(CTX)
    assert topic.topic == "5 minute fried rice"


def test_auth_error_is_non_retryable_and_leaks_no_key() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"error": "bad key"})

    with pytest.raises(ProviderAuthError) as excinfo:
        _provider(handler).generate_script("t", CTX)
    assert calls["n"] == 1, "401 must not be retried"
    assert FAKE_KEY not in str(excinfo.value)


def test_429_with_retry_after_is_retried_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_completion_body(json.dumps(VALID_SCRIPT)))

    result = _provider(handler).generate_script("t", CTX)
    assert calls["n"] == 2
    assert parse_script(result.raw_text).title == VALID_SCRIPT["title"]


def test_server_errors_are_retried_up_to_the_limit() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(502)
        return httpx.Response(200, json=_completion_body(json.dumps(VALID_SCRIPT)))

    _provider(handler).generate_script("t", CTX)
    assert calls["n"] == 3


def test_persistent_server_errors_exhaust_retries_as_transient() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with pytest.raises(TransientProviderError):
        _provider(handler).generate_script("t", CTX)
    assert calls["n"] == 3  # max_retries=2 -> 3 attempts total


def test_timeout_maps_to_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    with pytest.raises(TransientProviderError):
        _provider(handler).generate_script("t", CTX)


def test_empty_response_is_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body("   "))

    with pytest.raises(SchemaValidationError):
        _provider(handler).generate_script("t", CTX)


def test_refusal_is_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = {"id": "r", "choices": [{"message": {"role": "assistant", "refusal": "cannot comply"}}]}
        return httpx.Response(200, json=body)

    with pytest.raises(SchemaValidationError):
        _provider(handler).generate_script("t", CTX)


def test_malformed_content_fails_downstream_schema_validation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_completion_body("{not valid json"))

    result = _provider(handler).generate_script("t", CTX)
    with pytest.raises(SchemaValidationError):
        parse_script(result.raw_text)


def test_malformed_envelope_is_a_schema_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(SchemaValidationError):
        _provider(handler).generate_script("t", CTX)


def test_configuration_is_validated_up_front() -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleLLMProvider(base_url="", model="m", api_key=FAKE_KEY)
    with pytest.raises(ValueError):
        OpenAICompatibleLLMProvider(base_url="https://x.invalid", model="", api_key=FAKE_KEY)
    with pytest.raises(ValueError):
        OpenAICompatibleLLMProvider(base_url="https://x.invalid", model="m", api_key="")


def test_registry_resolves_the_adapter_from_settings() -> None:
    from reel_harness.config import Settings
    from reel_harness.providers.registry import resolve_llm_provider

    settings = Settings(
        llm_provider="openai-compatible",
        llm_base_url="https://llm.example.invalid/v1",
        llm_model="test-model",
        llm_api_key=FAKE_KEY,
        _env_file=None,
    )
    provider = resolve_llm_provider(settings.llm_provider, settings)
    assert provider.provider_id == "openai-compatible"
    provider.close()

    fake = resolve_llm_provider("fake")
    assert fake.provider_id == "fake"
