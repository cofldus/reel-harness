from __future__ import annotations

from collections.abc import Callable

from reel_harness.config import Settings
from reel_harness.providers.base import LLMProvider, Publisher, StockMediaProvider, TTSProvider
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider


def _build_openai_compatible_llm(settings: Settings | None) -> LLMProvider:
    if settings is None:
        raise NotImplementedError("the openai-compatible LLM provider requires application settings")
    from reel_harness.providers.openai_compatible_llm import OpenAICompatibleLLMProvider

    return OpenAICompatibleLLMProvider(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        connect_timeout=settings.llm_connect_timeout_seconds,
        read_timeout=settings.llm_read_timeout_seconds,
        max_retries=settings.llm_max_retries,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.llm_max_output_tokens,
    )


# Real vendor names/SDKs must only ever be registered here, never referenced
# from reel_harness.pipeline.*. "openai-compatible" is a protocol shape, not a
# vendor: the concrete vendor is chosen purely via llm_base_url/llm_model.
LLM_PROVIDERS: dict[str, Callable[[Settings | None], LLMProvider]] = {
    "fake": lambda settings: FakeLLMProvider(),
    "openai-compatible": _build_openai_compatible_llm,
}
TTS_PROVIDERS: dict[str, Callable[[], TTSProvider]] = {"fake": FakeTTSProvider}
STOCK_MEDIA_PROVIDERS: dict[str, Callable[[], StockMediaProvider]] = {"fake": FakeStockMediaProvider}
PUBLISHERS: dict[str, Callable[[], Publisher]] = {}


def resolve_llm_provider(name: str, settings: Settings | None = None) -> LLMProvider:
    try:
        return LLM_PROVIDERS[name](settings)
    except KeyError as exc:
        raise NotImplementedError(f"LLM provider '{name}' is not registered yet") from exc


def resolve_tts_provider(name: str) -> TTSProvider:
    try:
        return TTS_PROVIDERS[name]()
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
