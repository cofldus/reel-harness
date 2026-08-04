from __future__ import annotations

import json
import time
import uuid
from typing import Any

from reel_harness.core.errors import (
    ProviderAuthError,
    ProviderNotConfiguredError,
    SchemaValidationError,
    TransientProviderError,
)
from reel_harness.providers.base import ChannelContext, ScriptResult, TopicResult

PROMPT_VERSION = "openai-compat-script-v1"

_SCRIPT_SYSTEM_PROMPT = (
    "You write short-form vertical video scripts. Respond with ONLY a JSON object, "
    "no markdown fences, matching exactly this shape: "
    '{"title": string, "scenes": [{"voiceover": string, "subtitle": string, '
    '"visual_query": string, "duration_hint_sec": number}]}. '
    "Rules: 3 to 10 scenes; voiceover max 500 chars; subtitle max 120 chars; "
    "visual_query is a stock-media search phrase; duration_hint_sec between 2.0 and 12.0."
)

_TOPIC_SYSTEM_PROMPT = (
    "You propose one short-form video topic. Respond with ONLY a JSON object: "
    '{"topic": string}. The topic must fit the channel niche and language.'
)


class OpenAICompatibleLLMProvider:
    """Adapter for any OpenAI-compatible /chat/completions endpoint.

    Vendor-neutral on purpose: the concrete vendor is chosen entirely by
    configuration (llm_base_url + llm_model + llm_api_key) -- no vendor name is
    hardcoded here or anywhere in the pipeline. The endpoint shape is isolated
    to this class.

    Safety contract: the API key lives only in the client's Authorization
    header; it is never interpolated into exception messages, logs, or results.
    Request/response bodies are never logged or persisted -- only the provider
    request id and token-usage counts are surfaced as metadata.
    """

    provider_id = "openai-compatible"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str,
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        temperature: float = 0.7,
        max_output_tokens: int = 1200,
        transport: Any = None,
    ) -> None:
        if not base_url:
            raise ValueError("llm_base_url is required for the openai-compatible provider")
        if not model:
            raise ValueError("llm_model is required for the openai-compatible provider")
        if not api_key:
            raise ValueError("llm_api_key is required for the openai-compatible provider")
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover - environment guard
            raise RuntimeError(
                "the openai-compatible LLM provider requires the httpx package "
                "(present in the dev environment; promote it to a runtime "
                "dependency before deploying a real provider)"
            ) from exc

        self.model_id = model
        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._httpx = httpx
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def generate_topic(self, ctx: ChannelContext) -> TopicResult:
        user_prompt = f"Channel niche: {ctx.niche}. Language: {ctx.language}."
        content, request_id, _usage = self._chat(_TOPIC_SYSTEM_PROMPT, user_prompt)
        try:
            topic = json.loads(content)["topic"]
        except (ValueError, KeyError, TypeError) as exc:
            raise SchemaValidationError(f"topic response was not the expected JSON shape: {exc}") from exc
        if not isinstance(topic, str) or not topic.strip():
            raise SchemaValidationError("topic response contained an empty topic")
        return TopicResult(
            topic=topic.strip(), provider_id=self.provider_id, model_id=self.model_id, request_id=request_id,
        )

    def generate_script(self, topic: str, ctx: ChannelContext) -> ScriptResult:
        user_prompt = (
            f"Topic: {topic}. Channel niche: {ctx.niche}. Language: {ctx.language}. "
            f"Style preset: {json.dumps(ctx.style_preset, ensure_ascii=False)}."
        )
        content, request_id, usage = self._chat(_SCRIPT_SYSTEM_PROMPT, user_prompt)
        # Schema validation of the returned JSON happens downstream in
        # pipeline.script_schema.parse_script -- the adapter's contract is to
        # deliver a non-empty raw text plus metadata.
        return ScriptResult(
            raw_text=content,
            provider_id=self.provider_id,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
            request_id=request_id,
            usage=usage,
        )

    # Newer models reject two long-standing parameters outright: they take
    # only the default temperature, and they renamed max_tokens to
    # max_completion_tokens. Sending the old shape fails the whole request
    # with HTTP 400 before a single token is generated, which is how this
    # adapter silently locked the project to older models -- the pipeline
    # reported "unexpected HTTP 400" and nothing said the request itself
    # was the problem.
    #
    # Both are demoted to hints rather than requirements. Temperature is
    # sent only when it differs from the default, so a model that allows
    # just the default is never asked for something else; the token cap is
    # sent under whichever name the endpoint accepts, discovered once per
    # process from the error the endpoint itself returns.
    _DEFAULT_TEMPERATURE = 1.0

    def _build_payload(self, system_prompt: str, user_prompt: str) -> dict:
        payload: dict = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        }
        if self._temperature != self._DEFAULT_TEMPERATURE:
            payload["temperature"] = self._temperature
        payload[self._token_limit_key] = self._max_output_tokens
        return payload

    @property
    def _token_limit_key(self) -> str:
        return getattr(self, "_token_key", "max_tokens")

    def _chat(self, system_prompt: str, user_prompt: str) -> tuple[str, str | None, dict | None]:
        payload = self._build_payload(system_prompt, user_prompt)
        correlation_id = str(uuid.uuid4())
        attempts = self._max_retries + 1
        last_transient: Exception | None = None
        for attempt in range(attempts):
            if attempt > 0:
                time.sleep(self._retry_delay(attempt, last_transient))
            try:
                response = self._client.post(
                    "/chat/completions", json=payload, headers={"X-Request-ID": correlation_id},
                )
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"llm request timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"llm transport error ({type(exc).__name__})")
                continue

            request_id = response.headers.get("x-request-id")
            if response.status_code in (401, 403):
                # Deliberately no response body in the message: auth error
                # bodies can echo the presented credential.
                raise ProviderAuthError(
                    f"llm endpoint rejected the configured credential "
                    f"(HTTP {response.status_code}, request_id={request_id})"
                )
            if response.status_code == 429 or response.status_code >= 500:
                self._last_retry_after = _parse_retry_after(response.headers.get("retry-after"))
                last_transient = TransientProviderError(
                    f"llm endpoint returned HTTP {response.status_code} (request_id={request_id})"
                )
                continue
            if response.status_code == 400:
                detail = _error_message(response)
                # The endpoint tells us exactly which parameter it will
                # not take. Honour it once and retry rather than failing
                # the run: the alternative is every newer model being
                # unusable until someone reads a 400 by hand.
                if "max_completion_tokens" in detail and self._token_limit_key == "max_tokens":
                    self._token_key = "max_completion_tokens"
                    payload = self._build_payload(system_prompt, user_prompt)
                    continue
                if "temperature" in detail and "temperature" in payload:
                    self._temperature = self._DEFAULT_TEMPERATURE
                    payload = self._build_payload(system_prompt, user_prompt)
                    continue
                # A 400 is the request being wrong, which retrying cannot
                # fix -- and it is NOT transient, so it must not be
                # retried into a bill or a stuck job.
                raise ProviderNotConfiguredError(
                    f"llm endpoint rejected the request for model {self.model_id!r}: "
                    f"{detail or 'no detail'} (request_id={request_id})"
                )
            if response.status_code != 200:
                raise TransientProviderError(
                    f"llm endpoint returned unexpected HTTP {response.status_code} (request_id={request_id})"
                )

            try:
                data = response.json()
                message = data["choices"][0]["message"]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise SchemaValidationError(f"llm response envelope was malformed: {exc}") from exc

            if message.get("refusal"):
                raise SchemaValidationError("llm refused the request (no content returned)")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                # A reasoning model spends the SAME budget on thinking and
                # on answering, so a cap sized for the answer alone can be
                # consumed entirely before a single visible token is
                # written. Seen for real: reasoning_tokens == the whole
                # 6000-token cap, finish_reason "length", content empty.
                # Reported as "llm returned an empty response", which
                # names the symptom and hides the cause.
                usage = data.get("usage") or {}
                details = usage.get("completion_tokens_details") or {}
                reasoning = details.get("reasoning_tokens")
                if data["choices"][0].get("finish_reason") == "length":
                    raise SchemaValidationError(
                        "llm hit its token cap before writing any output"
                        + (f" (reasoning used {reasoning} tokens" if reasoning else "")
                        + f" of a {self._max_output_tokens} cap) -- raise the model's"
                        " output-token setting, which on a reasoning model has to cover"
                        " thinking as well as the answer"
                    )
                raise SchemaValidationError("llm returned an empty response")

            body_request_id = data.get("id")
            usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
            return content, request_id or body_request_id, usage

        assert last_transient is not None
        raise TransientProviderError(
            f"llm request failed after {attempts} attempts: {last_transient}"
        )

    _last_retry_after: float | None = None

    def _retry_delay(self, attempt: int, last_error: Exception | None) -> float:
        base = self._retry_backoff_seconds * attempt
        retry_after = self._last_retry_after or 0.0
        self._last_retry_after = None
        return min(max(base, retry_after), 30.0)


def _parse_retry_after(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _error_message(response) -> str:
    """The endpoint's own explanation, or empty.

    Never raises and never returns the body wholesale: a provider error
    body is untrusted input that ends up in logs, so only the message
    field is taken and it is length-capped.
    """
    try:
        payload = response.json()
    except Exception:  # noqa: BLE001 - a malformed body is not a crash
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or "")[:300]
    return ""
