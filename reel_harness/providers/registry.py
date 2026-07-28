from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from reel_harness.config import Settings, normalize_provider_name
from reel_harness.core.errors import ProviderNotConfiguredError
from reel_harness.providers.base import ChannelContext, LLMProvider, Publisher, StockMediaProvider, TTSProvider
from reel_harness.providers.fake_llm import PROMPT_VERSION as FAKE_PROMPT_VERSION
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider


class _UnconfiguredLLMProvider:
    """Stands in when a job's pinned provider cannot be satisfied. Any use
    fails the stage with an explicit PROVIDER_NOT_CONFIGURED -- never a silent
    fallback to a different provider."""

    provider_id = "unconfigured"
    model_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def generate_topic(self, ctx: ChannelContext):
        raise ProviderNotConfiguredError(self._reason)

    def generate_script(self, topic: str, ctx: ChannelContext):
        raise ProviderNotConfiguredError(self._reason)


def _build_openai_compatible_llm(
    settings: Settings | None,
    *,
    model_override: str | None = None,
    temperature_override: float | None = None,
    max_output_tokens_override: int | None = None,
) -> LLMProvider:
    if settings is None:
        raise NotImplementedError("the openai-compatible LLM provider requires application settings")
    from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider

    return OpenAICompatibleLLMProvider(
        base_url=settings.llm_base_url,
        model=model_override or settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        connect_timeout=settings.llm_connect_timeout_seconds,
        read_timeout=settings.llm_read_timeout_seconds,
        max_retries=settings.llm_max_retries,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        temperature=(
            temperature_override if temperature_override is not None else settings.llm_temperature
        ),
        max_output_tokens=(
            max_output_tokens_override if max_output_tokens_override is not None
            else settings.llm_max_output_tokens
        ),
    )


def llm_provider_snapshot(settings: Settings | None) -> dict:
    """The provider configuration captured onto a job at creation. Contains the
    provider id, model, a safe base-URL identifier (host only), prompt version,
    and sampling params -- NEVER the API key or the full URL."""
    name = normalize_provider_name(settings.llm_provider) if settings else "fake"
    if name == "fake":
        return {
            "llm_provider": "fake",
            "llm_model": FakeLLMProvider.model_id,
            "prompt_version": FAKE_PROMPT_VERSION,
        }
    from reel_harness.providers.openai_compatible_llm import PROMPT_VERSION

    assert settings is not None
    return {
        "llm_provider": name,
        "llm_model": settings.llm_model,
        "llm_base_url_host": urlsplit(settings.llm_base_url).netloc,
        "prompt_version": PROMPT_VERSION,
        "temperature": settings.llm_temperature,
        "max_output_tokens": settings.llm_max_output_tokens,
    }


def resolve_llm_for_snapshot(snapshot: dict | None, settings: Settings | None) -> LLMProvider:
    """Resolves the LLM provider a leased job must run with, honoring the
    job's creation-time snapshot. Legacy jobs without a snapshot use the
    current settings. Every unsatisfiable case returns a provider whose use
    fails explicitly -- there is no silent fallback."""
    if not snapshot:
        return resolve_llm_provider(
            normalize_provider_name(settings.llm_provider) if settings else "fake", settings,
        )
    name = normalize_provider_name(snapshot.get("llm_provider"))
    if name == "fake":
        return FakeLLMProvider()
    if name != "openai-compatible":
        return _UnconfiguredLLMProvider(
            f"job is pinned to llm provider {name!r}, which is not registered"
        )
    if settings is None or not settings.llm_base_url or not settings.llm_api_key.get_secret_value():
        return _UnconfiguredLLMProvider(
            "job is pinned to the openai-compatible llm provider but "
            "REEL_HARNESS_LLM_BASE_URL / REEL_HARNESS_LLM_API_KEY are not configured"
        )
    pinned_host = snapshot.get("llm_base_url_host")
    current_host = urlsplit(settings.llm_base_url).netloc
    if pinned_host and current_host != pinned_host:
        return _UnconfiguredLLMProvider(
            f"configured llm endpoint host {current_host!r} does not match the "
            f"job's pinned host {pinned_host!r}"
        )
    return _build_openai_compatible_llm(
        settings,
        model_override=snapshot.get("llm_model"),
        temperature_override=snapshot.get("temperature"),
        max_output_tokens_override=snapshot.get("max_output_tokens"),
    )


def _build_openai_compatible_tts(
    settings: Settings | None,
    *,
    model_override: str | None = None,
    voice_override: str | None = None,
    format_override: str | None = None,
    speed_override: float | None = None,
) -> TTSProvider:
    if settings is None:
        raise NotImplementedError("the openai-compatible TTS provider requires application settings")
    from reel_harness.providers.openai_compatible_tts import OpenAICompatibleTTSProvider

    return OpenAICompatibleTTSProvider(
        base_url=settings.tts_base_url,
        model=model_override or settings.tts_model,
        api_key=settings.tts_api_key.get_secret_value(),
        voice=voice_override or settings.tts_voice,
        audio_format=format_override or settings.tts_format,
        speed=speed_override if speed_override is not None else settings.tts_speed,
        connect_timeout=settings.tts_connect_timeout_seconds,
        read_timeout=settings.tts_read_timeout_seconds,
        max_retries=settings.tts_max_retries,
        retry_backoff_seconds=settings.tts_retry_backoff_seconds,
    )


# Real vendor names/SDKs must only ever be registered here, never referenced
# from reel_harness.pipeline.*. "openai-compatible" is a protocol shape, not a
# vendor: the concrete vendor is chosen purely via the configured base URL and
# model/voice.
LLM_PROVIDERS: dict[str, Callable[[Settings | None], LLMProvider]] = {
    "fake": lambda settings: FakeLLMProvider(),
    "openai-compatible": _build_openai_compatible_llm,
}
TTS_PROVIDERS: dict[str, Callable[[Settings | None], TTSProvider]] = {
    "fake": lambda settings: FakeTTSProvider(),
    "openai-compatible": _build_openai_compatible_tts,
}
STOCK_MEDIA_PROVIDERS: dict[str, Callable[[], StockMediaProvider]] = {"fake": FakeStockMediaProvider}
PUBLISHERS: dict[str, Callable[[], Publisher]] = {}


def resolve_llm_provider(name: str, settings: Settings | None = None) -> LLMProvider:
    try:
        return LLM_PROVIDERS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"LLM provider '{name}' is not registered yet") from exc


def resolve_tts_provider(name: str, settings: Settings | None = None) -> TTSProvider:
    try:
        return TTS_PROVIDERS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"TTS provider '{name}' is not registered yet") from exc


def resolve_stock_media_provider(name: str) -> StockMediaProvider:
    try:
        return STOCK_MEDIA_PROVIDERS[name]()
    except KeyError as exc:
        raise NotImplementedError(f"Stock media provider '{name}' is not registered yet") from exc


def resolve_publisher(name: str) -> Publisher:
    try:
        return PUBLISHERS[name]()
    except KeyError as exc:
        raise NotImplementedError(f"Publisher '{name}' is not registered yet") from exc
