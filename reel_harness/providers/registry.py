from __future__ import annotations

from collections.abc import Callable

from reel_harness.providers.base import LLMProvider, Publisher, StockMediaProvider, TTSProvider
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider

# Real vendor names/SDKs must only ever be registered here, never referenced from
# reel_harness.pipeline.*. As of Phase 0/1 only Fake implementations exist.
LLM_PROVIDERS: dict[str, Callable[[], LLMProvider]] = {"fake": FakeLLMProvider}
TTS_PROVIDERS: dict[str, Callable[[], TTSProvider]] = {"fake": FakeTTSProvider}
STOCK_MEDIA_PROVIDERS: dict[str, Callable[[], StockMediaProvider]] = {"fake": FakeStockMediaProvider}
PUBLISHERS: dict[str, Callable[[], Publisher]] = {}


def resolve_llm_provider(name: str) -> LLMProvider:
    try:
        return LLM_PROVIDERS[name]()
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
