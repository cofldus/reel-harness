from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from reel_harness.config import Settings, normalize_provider_name
from reel_harness.core.errors import ProviderNotConfiguredError
from reel_harness.pipeline.asset_query import QUERY_VERSION as ASSET_QUERY_VERSION
from reel_harness.pipeline.asset_selection import SELECTION_VERSION as ASSET_SELECTION_VERSION
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


class _UnconfiguredTTSProvider:
    """TTS counterpart of _UnconfiguredLLMProvider: any synthesis attempt fails
    the stage with PROVIDER_NOT_CONFIGURED."""

    provider_id = "unconfigured"
    model_id = "unconfigured"
    voice_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir):
        raise ProviderNotConfiguredError(self._reason)


class _UnconfiguredStockMediaProvider:
    """Stock-media counterpart of _UnconfiguredLLMProvider: any search/download
    attempt fails the stage with PROVIDER_NOT_CONFIGURED."""

    provider_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def search(self, query: str, orientation: str, min_duration: float, **kwargs):
        raise ProviderNotConfiguredError(self._reason)

    def download(self, candidate, dest_dir):
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


def tts_provider_snapshot(settings: Settings | None) -> dict:
    """TTS configuration captured onto a job at creation: provider id, model,
    safe base-URL host, voice, format, speed, adapter version, and the
    canonical output policy -- NEVER the API key, headers, or URLs with
    credentials."""
    from reel_harness.media.tts_audio import CANONICAL_CHANNELS, CANONICAL_CODEC, CANONICAL_SAMPLE_RATE

    output_policy = {
        "sample_rate": CANONICAL_SAMPLE_RATE,
        "channels": CANONICAL_CHANNELS,
        "codec": CANONICAL_CODEC,
    }
    name = normalize_provider_name(settings.tts_provider) if settings else "fake"
    if name == "fake":
        return {
            "tts_provider": "fake",
            "tts_model": "fake-tts-v1",
            "tts_voice": "fake-voice-1",
            "tts_adapter_version": "fake-tts-v1",
            "tts_output_policy": output_policy,
        }
    from reel_harness.providers.openai_compatible_tts import ADAPTER_VERSION

    assert settings is not None
    return {
        "tts_provider": name,
        "tts_model": settings.tts_model,
        "tts_base_url_host": urlsplit(settings.tts_base_url).netloc,
        "tts_voice": settings.tts_voice,
        "tts_format": settings.tts_format,
        "tts_speed": settings.tts_speed,
        "tts_adapter_version": ADAPTER_VERSION,
        "tts_output_policy": output_policy,
    }


def asset_provider_snapshot(settings: Settings | None) -> dict:
    """Stock-media configuration captured onto a job at creation: provider id,
    safe base-URL host, adapter version, search/selection policy (orientation,
    per-page, min width/height, duration bounds, safe-search), and schema
    versions -- NEVER the API key, headers, or signed download URLs."""
    search_policy = {
        "orientation": settings.asset_orientation if settings else "portrait",
        "per_page": settings.asset_per_page if settings else 15,
        "min_width": settings.asset_min_width if settings else 480,
        "min_height": settings.asset_min_height if settings else 480,
        "min_duration_sec": settings.asset_min_duration_seconds if settings else 1.0,
        "max_duration_sec": settings.asset_max_duration_seconds if settings else 60.0,
        "safe_search": settings.asset_safe_search if settings else True,
    }
    name = normalize_provider_name(settings.asset_provider) if settings else "fake"
    if name == "fake":
        return {
            "asset_provider": "fake",
            "asset_adapter_version": "fake-stock-media-v1",
            "asset_search_policy": search_policy,
            "asset_query_version": ASSET_QUERY_VERSION,
            "asset_selection_version": ASSET_SELECTION_VERSION,
        }
    from reel_harness.providers.pexels_stock_media import ADAPTER_VERSION

    assert settings is not None
    return {
        "asset_provider": name,
        "asset_base_url_host": urlsplit(settings.asset_base_url).netloc,
        "asset_adapter_version": ADAPTER_VERSION,
        "asset_search_policy": search_policy,
        "asset_query_version": ASSET_QUERY_VERSION,
        "asset_selection_version": ASSET_SELECTION_VERSION,
    }


def provider_snapshot(settings: Settings | None) -> dict:
    """Combined per-job provider snapshot (LLM + TTS + asset blocks)."""
    return {
        **llm_provider_snapshot(settings),
        **tts_provider_snapshot(settings),
        **asset_provider_snapshot(settings),
    }


def resolve_stock_media_for_snapshot(snapshot: dict | None, settings: Settings | None) -> StockMediaProvider:
    """Resolves the stock-media provider a leased job must run with, honoring
    the job's creation-time snapshot. Legacy jobs whose snapshot predates the
    asset block (or have no snapshot) use the current settings. Every
    unsatisfiable case fails explicitly -- there is no silent fallback to a
    different provider."""
    if not snapshot or "asset_provider" not in snapshot:
        return resolve_stock_media_provider(
            normalize_provider_name(settings.asset_provider) if settings else "fake", settings,
        )
    name = normalize_provider_name(snapshot.get("asset_provider"))
    if name == "fake":
        return FakeStockMediaProvider()
    if name != "pexels":
        return _UnconfiguredStockMediaProvider(
            f"job is pinned to asset provider {name!r}, which is not registered"
        )
    if settings is None or not settings.asset_base_url or not settings.asset_api_key.get_secret_value():
        return _UnconfiguredStockMediaProvider(
            "job is pinned to the pexels asset provider but "
            "REEL_HARNESS_ASSET_BASE_URL / REEL_HARNESS_ASSET_API_KEY are not configured"
        )
    pinned_host = snapshot.get("asset_base_url_host")
    current_host = urlsplit(settings.asset_base_url).netloc
    if pinned_host and current_host != pinned_host:
        return _UnconfiguredStockMediaProvider(
            f"configured asset endpoint host {current_host!r} does not match the "
            f"job's pinned host {pinned_host!r}"
        )
    return _build_pexels_stock_media(settings)


def resolve_tts_for_snapshot(snapshot: dict | None, settings: Settings | None) -> TTSProvider:
    """Resolves the TTS provider a leased job must run with, honoring the
    job's creation-time snapshot. Legacy jobs whose snapshot predates the TTS
    block (or have no snapshot) use the current settings. Every unsatisfiable
    case fails explicitly -- there is no silent fallback."""
    if not snapshot or "tts_provider" not in snapshot:
        return resolve_tts_provider(
            normalize_provider_name(settings.tts_provider) if settings else "fake", settings,
        )
    name = normalize_provider_name(snapshot.get("tts_provider"))
    if name == "fake":
        return FakeTTSProvider()
    if name != "openai-compatible":
        return _UnconfiguredTTSProvider(
            f"job is pinned to tts provider {name!r}, which is not registered"
        )
    if settings is None or not settings.tts_base_url or not settings.tts_api_key.get_secret_value():
        return _UnconfiguredTTSProvider(
            "job is pinned to the openai-compatible tts provider but "
            "REEL_HARNESS_TTS_BASE_URL / REEL_HARNESS_TTS_API_KEY are not configured"
        )
    pinned_host = snapshot.get("tts_base_url_host")
    current_host = urlsplit(settings.tts_base_url).netloc
    if pinned_host and current_host != pinned_host:
        return _UnconfiguredTTSProvider(
            f"configured tts endpoint host {current_host!r} does not match the "
            f"job's pinned host {pinned_host!r}"
        )
    return _build_openai_compatible_tts(
        settings,
        model_override=snapshot.get("tts_model"),
        voice_override=snapshot.get("tts_voice"),
        format_override=snapshot.get("tts_format"),
        speed_override=snapshot.get("tts_speed"),
    )


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


def _build_pexels_stock_media(settings: Settings | None) -> StockMediaProvider:
    if settings is None:
        raise NotImplementedError("the pexels stock media provider requires application settings")
    from reel_harness.providers.pexels_stock_media import PexelsStockMediaProvider

    return PexelsStockMediaProvider(
        api_key=settings.asset_api_key.get_secret_value(),
        base_url=settings.asset_base_url,
        connect_timeout=settings.asset_connect_timeout_seconds,
        read_timeout=settings.asset_read_timeout_seconds,
        max_retries=settings.asset_max_retries,
        retry_backoff_seconds=settings.asset_retry_backoff_seconds,
    )


# Real vendor names/SDKs must only ever be registered here, never referenced
# from reel_harness.pipeline.*. "openai-compatible" is a protocol shape, not a
# vendor: the concrete vendor is chosen purely via the configured base URL and
# model/voice. "pexels" IS a concrete vendor (stock-video search has no
# equivalent protocol-shaped standard) -- see docs/OPERATIONS.md for why it
# was chosen.
LLM_PROVIDERS: dict[str, Callable[[Settings | None], LLMProvider]] = {
    "fake": lambda settings: FakeLLMProvider(),
    "openai-compatible": _build_openai_compatible_llm,
}
TTS_PROVIDERS: dict[str, Callable[[Settings | None], TTSProvider]] = {
    "fake": lambda settings: FakeTTSProvider(),
    "openai-compatible": _build_openai_compatible_tts,
}
STOCK_MEDIA_PROVIDERS: dict[str, Callable[[Settings | None], StockMediaProvider]] = {
    "fake": lambda settings: FakeStockMediaProvider(),
    "pexels": _build_pexels_stock_media,
}
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


def resolve_stock_media_provider(name: str, settings: Settings | None = None) -> StockMediaProvider:
    try:
        return STOCK_MEDIA_PROVIDERS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"Stock media provider '{name}' is not registered yet") from exc


def resolve_publisher(name: str) -> Publisher:
    try:
        return PUBLISHERS[name]()
    except KeyError as exc:
        raise NotImplementedError(f"Publisher '{name}' is not registered yet") from exc
