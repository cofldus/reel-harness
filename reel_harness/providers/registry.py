from __future__ import annotations

from collections.abc import Callable
from urllib.parse import urlsplit

from reel_harness.config import Settings, normalize_provider_name
from reel_harness.core.errors import ProviderNotConfiguredError
from reel_harness.pipeline.asset_query import QUERY_VERSION as ASSET_QUERY_VERSION
from reel_harness.pipeline.asset_selection import SELECTION_VERSION as ASSET_SELECTION_VERSION
from reel_harness.providers.base import (
    ChannelContext,
    CinematicGenerationHandle,
    CinematicGenerationRequest,
    CinematicVideoProvider,
    CreatorInfo,
    LLMProvider,
    ProcessingStatusResult,
    PublicationMetadata,
    Publisher,
    PublisherCapabilities,
    StockMediaProvider,
    TTSProvider,
    UploadChunkResult,
    UploadSessionHandle,
)
from reel_harness.providers.demo_llm import PROMPT_VERSION as DEMO_PROMPT_VERSION
from reel_harness.providers.demo_llm import DemoLLMProvider
from reel_harness.providers.demo_stock_media import DemoStockMediaProvider
from reel_harness.providers.demo_tts import DemoTTSProvider
from reel_harness.providers.fake_llm import PROMPT_VERSION as FAKE_PROMPT_VERSION
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider
from reel_harness.publisher.credentials import CredentialBackend


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


class _UnconfiguredCinematicVideoProvider:
    """Cinematic counterpart of _UnconfiguredLLMProvider: any generation
    attempt fails with PROVIDER_NOT_CONFIGURED. `capabilities` is a
    maximally-restrictive placeholder (everything False/empty) so
    capability-gate checks that run before any method call stay
    structurally valid without ever enabling a feature."""

    provider_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        from reel_harness.providers.base import CinematicCapabilities

        self._reason = reason
        self.capabilities = CinematicCapabilities(
            text_to_video=False, image_to_video=False, first_frame=False, last_frame=False,
            character_reference=False, multiple_references=False, video_reference=False,
            native_audio=False, lip_sync=False, supports_seed=False,
            supports_negative_prompt=False, supported_durations_sec=frozenset(),
            supported_aspect_ratios=frozenset(), supported_resolutions=frozenset(),
            max_concurrent_jobs=None,
        )

    def validate_request(self, request: CinematicGenerationRequest):
        raise ProviderNotConfiguredError(self._reason)

    def estimate_cost(self, request: CinematicGenerationRequest):
        raise ProviderNotConfiguredError(self._reason)

    def create_generation(self, request: CinematicGenerationRequest):
        raise ProviderNotConfiguredError(self._reason)

    def get_generation_status(self, handle: CinematicGenerationHandle):
        raise ProviderNotConfiguredError(self._reason)

    def cancel_generation(self, handle: CinematicGenerationHandle):
        raise ProviderNotConfiguredError(self._reason)

    def download_result(self, handle: CinematicGenerationHandle, dest_dir):
        raise ProviderNotConfiguredError(self._reason)


class _UnconfiguredReferenceImageProvider:
    """Reference-image counterpart of _UnconfiguredLLMProvider: any
    generation attempt fails with PROVIDER_NOT_CONFIGURED. Capabilities
    are a maximally-restrictive placeholder so a capability check that
    runs before any call stays structurally valid without enabling
    anything."""

    provider_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        from reel_harness.providers.base import ImageCapabilities

        self._reason = reason
        self.capabilities = ImageCapabilities(
            text_to_image=False, character_reference=False, max_character_references=0,
            supported_resolutions=frozenset(), supported_aspect_ratios=frozenset(),
            watermarked=False,
        )

    def validate_request(self, request):
        raise ProviderNotConfiguredError(self._reason)

    def estimate_cost(self, request):
        raise ProviderNotConfiguredError(self._reason)

    def generate_reference(self, request, dest_dir):
        raise ProviderNotConfiguredError(self._reason)


class _UnconfiguredNarrativeDirector:
    """Narrative counterpart of _UnconfiguredLLMProvider: any adaptation
    attempt fails with PROVIDER_NOT_CONFIGURED."""

    provider_id = "unconfigured"

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def adapt_story(self, request):
        raise ProviderNotConfiguredError(self._reason)

    def repair_adaptation(self, request, previous_raw, errors):
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
    if name == "demo":
        return {
            "llm_provider": "demo",
            "llm_model": DemoLLMProvider.model_id,
            "prompt_version": DEMO_PROMPT_VERSION,
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
    if name == "demo":
        return {
            "tts_provider": "demo",
            "tts_model": "demo-tts-local-v1",
            "tts_voice": "demo-auto-selected",
            "tts_adapter_version": "demo-tts-local-v1",
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
    if name == "demo":
        return {
            "asset_provider": "demo",
            "asset_adapter_version": "demo-stock-media-v1",
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
    if name == "demo":
        return DemoStockMediaProvider()
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
    if name == "demo":
        return DemoTTSProvider()
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
    if name == "demo":
        return DemoLLMProvider()
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
    "demo": lambda settings: DemoLLMProvider(),
    "openai-compatible": _build_openai_compatible_llm,
}
TTS_PROVIDERS: dict[str, Callable[[Settings | None], TTSProvider]] = {
    "fake": lambda settings: FakeTTSProvider(),
    "demo": lambda settings: DemoTTSProvider(),
    "openai-compatible": _build_openai_compatible_tts,
}
STOCK_MEDIA_PROVIDERS: dict[str, Callable[[Settings | None], StockMediaProvider]] = {
    "fake": lambda settings: FakeStockMediaProvider(),
    "demo": lambda settings: DemoStockMediaProvider(),
    "pexels": _build_pexels_stock_media,
}


def _build_fake_cinematic_video(settings: Settings | None) -> CinematicVideoProvider:
    from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider

    return FakeCinematicVideoProvider()


# Fable cinematic generation (Phase F1): fake only for now -- the demo tier
# lands in F3 and the first real adapter in F5, both registered HERE and
# nowhere else, per the same vendor-name discipline as every other family.
CINEMATIC_VIDEO_PROVIDERS: dict[str, Callable[[Settings | None], CinematicVideoProvider]] = {
    "fake": _build_fake_cinematic_video,
}


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


def resolve_cinematic_video_provider(name: str, settings: Settings | None = None) -> CinematicVideoProvider:
    try:
        return CINEMATIC_VIDEO_PROVIDERS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"Cinematic video provider '{name}' is not registered yet") from exc


def _build_fake_reference_image(settings: Settings | None):
    from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider

    return FakeReferenceImageProvider()


# Reference-image generation (Fable F3). The demo tier and the real
# Google adapter are registered by later commits; every vendor name stays
# confined to this module.
REFERENCE_IMAGE_PROVIDERS: dict[str, Callable[[Settings | None], object]] = {
    "fake": _build_fake_reference_image,
}


def resolve_reference_image_provider(name: str, settings: Settings | None = None):
    try:
        return REFERENCE_IMAGE_PROVIDERS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"Reference image provider '{name}' is not registered yet") from exc


def resolve_reference_image_for_snapshot(snapshot: dict | None, settings: Settings | None):
    """Snapshot-pinned resolution -- identical fail-loud ladder as every
    other provider family; never a silent fallback."""
    if not snapshot or "reference_image_provider" not in snapshot:
        return resolve_reference_image_provider(
            normalize_provider_name(settings.reference_image_provider) if settings else "fake",
            settings,
        )
    name = normalize_provider_name(snapshot.get("reference_image_provider"))
    if name not in REFERENCE_IMAGE_PROVIDERS:
        return _UnconfiguredReferenceImageProvider(
            f"project is pinned to reference image provider {name!r}, which is not registered"
        )
    return REFERENCE_IMAGE_PROVIDERS[name](settings)


def _build_fake_narrative_director(settings: Settings | None):
    from reel_harness.providers.fake_narrative_director import FakeNarrativeDirector

    return FakeNarrativeDirector()


def _build_openai_compatible_director(settings: Settings | None):
    if settings is None:
        raise NotImplementedError("the openai-compatible narrative director requires settings")
    from reel_harness.providers.openai_compatible_director import (
        OpenAICompatibleNarrativeDirector,
    )

    return OpenAICompatibleNarrativeDirector(
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key.get_secret_value(),
        connect_timeout=settings.llm_connect_timeout_seconds,
        read_timeout=settings.narrative_read_timeout_seconds,
        max_retries=settings.llm_max_retries,
        retry_backoff_seconds=settings.llm_retry_backoff_seconds,
        temperature=settings.llm_temperature,
        max_output_tokens=settings.narrative_max_output_tokens,
    )


# Narrative Director (Fable F2). "openai-compatible" is a protocol shape,
# not a vendor -- the concrete vendor is chosen purely by the configured
# LLM base URL and model, exactly as for script generation.
NARRATIVE_DIRECTORS: dict[str, Callable[[Settings | None], object]] = {
    "fake": _build_fake_narrative_director,
    "openai-compatible": _build_openai_compatible_director,
}


def resolve_narrative_director(name: str, settings: Settings | None = None):
    try:
        return NARRATIVE_DIRECTORS[normalize_provider_name(name)](settings)
    except KeyError as exc:
        raise NotImplementedError(f"Narrative director '{name}' is not registered yet") from exc


def cinematic_provider_snapshot(settings: Settings | None) -> dict:
    """Per-project provider snapshot block (Fable) -- same pinning
    discipline as llm/tts/asset_provider_snapshot: provider ids, model,
    safe base-URL host, and the prompt version that shaped the
    adaptation. Never a credential."""
    from reel_harness.providers.narrative_prompts import NARRATIVE_PROMPT_VERSION

    cinematic = normalize_provider_name(settings.cinematic_provider) if settings else "fake"
    narrative = normalize_provider_name(settings.narrative_provider) if settings else "fake"
    reference = normalize_provider_name(settings.reference_image_provider) if settings else "fake"
    snapshot = {
        "cinematic_provider": cinematic,
        "narrative_provider": narrative,
        "narrative_prompt_version": NARRATIVE_PROMPT_VERSION,
        "reference_image_provider": reference,
    }
    if narrative == "openai-compatible" and settings is not None:
        snapshot["narrative_model"] = settings.llm_model
        snapshot["narrative_base_url_host"] = urlsplit(settings.llm_base_url).netloc
    return snapshot


def resolve_narrative_director_for_snapshot(snapshot: dict | None, settings: Settings | None):
    """Resolves the director a project's adaptation must run with,
    honoring its creation-time snapshot -- identical fail-loud ladder as
    the other families, no silent fallback to a different provider."""
    if not snapshot or "narrative_provider" not in snapshot:
        return resolve_narrative_director(
            normalize_provider_name(settings.narrative_provider) if settings else "fake", settings,
        )
    name = normalize_provider_name(snapshot.get("narrative_provider"))
    if name == "fake":
        return _build_fake_narrative_director(settings)
    if name not in NARRATIVE_DIRECTORS:
        return _UnconfiguredNarrativeDirector(
            f"project is pinned to narrative provider {name!r}, which is not registered"
        )
    if settings is None or not settings.llm_base_url or not settings.llm_api_key.get_secret_value():
        return _UnconfiguredNarrativeDirector(
            "project is pinned to the openai-compatible narrative director but "
            "REEL_HARNESS_LLM_BASE_URL / REEL_HARNESS_LLM_API_KEY are not configured"
        )
    pinned_host = snapshot.get("narrative_base_url_host")
    current_host = urlsplit(settings.llm_base_url).netloc
    if pinned_host and current_host != pinned_host:
        return _UnconfiguredNarrativeDirector(
            f"configured llm endpoint host {current_host!r} does not match the project's "
            f"pinned host {pinned_host!r}"
        )
    return NARRATIVE_DIRECTORS[name](settings)


def resolve_cinematic_video_for_snapshot(
    snapshot: dict | None, settings: Settings | None,
) -> CinematicVideoProvider:
    """Resolves the cinematic provider a leased shot must generate with,
    honoring the project's creation-time snapshot -- identical fail-loud
    ladder as the other families, no silent fallback."""
    if not snapshot or "cinematic_provider" not in snapshot:
        return resolve_cinematic_video_provider(
            normalize_provider_name(settings.cinematic_provider) if settings else "fake", settings,
        )
    name = normalize_provider_name(snapshot.get("cinematic_provider"))
    if name == "fake":
        return _build_fake_cinematic_video(settings)
    return _UnconfiguredCinematicVideoProvider(
        f"project is pinned to cinematic provider {name!r}, which is not registered"
    )


# A neutral, maximally-restrictive placeholder -- _UnconfiguredPublisher is
# never actually usable (every method raises), so these values are never
# acted on; they exist only so the instance stays structurally valid for
# any code that reads `.capabilities` before calling a method (e.g. a
# capability-gate check that runs ahead of the actual publish attempt).
_UNCONFIGURED_CAPABILITIES = PublisherCapabilities(
    supports_direct_publish=False, supports_upload_only=False, supports_scheduled_publish=False,
    supports_public_privacy=False, supports_unlisted_privacy=False, supports_comments_control=False,
    supports_remix_control=False, supports_processing_poll=False, supports_remote_delete=False,
    requires_creator_info=False, requires_user_confirmation=False,
    privacy_values=frozenset(), default_privacy="", public_privacy_values=frozenset(),
)


class _UnconfiguredPublisher:
    """Publisher counterpart of _UnconfiguredLLMProvider: any use fails with
    an explicit PROVIDER_NOT_CONFIGURED -- never a silent fallback to a
    different account or provider."""

    provider_id = "unconfigured"
    capabilities = _UNCONFIGURED_CAPABILITIES

    def __init__(self, reason: str) -> None:
        self._reason = reason

    def validate_configuration(self) -> None:
        raise ProviderNotConfiguredError(self._reason)

    def get_creator_info(self) -> CreatorInfo | None:
        raise ProviderNotConfiguredError(self._reason)

    def create_upload_session(
        self, metadata: PublicationMetadata, total_bytes: int, mime_type: str, correlation_id: str,
    ) -> UploadSessionHandle:
        raise ProviderNotConfiguredError(self._reason)

    def upload_chunk(
        self, session: UploadSessionHandle, chunk: bytes, start_byte: int, total_bytes: int,
    ) -> UploadChunkResult:
        raise ProviderNotConfiguredError(self._reason)

    def query_upload_offset(self, session: UploadSessionHandle, total_bytes: int) -> int | None:
        raise ProviderNotConfiguredError(self._reason)

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        raise ProviderNotConfiguredError(self._reason)


def publisher_snapshot(settings: Settings | None, provider: str, account_reference: str) -> dict:
    """Publisher configuration captured onto a Publication at creation:
    provider id, adapter version, safe account reference, default category/
    madeForKids policy, chunk size -- NEVER an OAuth token or client secret.
    Mirrors llm_provider_snapshot/tts_provider_snapshot/
    asset_provider_snapshot's role for jobs."""
    name = normalize_provider_name(provider)
    poll_interval = settings.publisher_processing_poll_interval_seconds if settings else 30.0
    max_duration = settings.publisher_processing_max_duration_seconds if settings else 3600.0
    if name == "fake":
        return {
            "publisher_provider": "fake",
            "publisher_adapter_version": "fake-publisher-v1",
            "publisher_account_reference": account_reference,
            "processing_poll_interval_seconds": poll_interval,
            "processing_max_duration_seconds": max_duration,
        }
    if name == "tiktok":
        from reel_harness.providers.tiktok_publisher import ADAPTER_VERSION as TIKTOK_ADAPTER_VERSION

        assert settings is not None
        return {
            "publisher_provider": "tiktok",
            "publisher_adapter_version": TIKTOK_ADAPTER_VERSION,
            "publisher_account_reference": account_reference,
            "tiktok_chunk_size": settings.tiktok_upload_chunk_size,
            "processing_poll_interval_seconds": poll_interval,
            "processing_max_duration_seconds": max_duration,
        }
    if name == "instagram":
        from reel_harness.providers.instagram_publisher import ADAPTER_VERSION as INSTAGRAM_ADAPTER_VERSION

        assert settings is not None
        return {
            "publisher_provider": "instagram",
            "publisher_adapter_version": INSTAGRAM_ADAPTER_VERSION,
            "publisher_account_reference": account_reference,
            "instagram_graph_url": settings.instagram_graph_url,
            "instagram_api_version": settings.instagram_graph_api_version,
            "processing_poll_interval_seconds": poll_interval,
            "processing_max_duration_seconds": max_duration,
        }
    if name != "youtube":
        raise NotImplementedError(f"Publisher '{provider}' is not registered yet")
    from reel_harness.providers.youtube_publisher import ADAPTER_VERSION

    assert settings is not None
    return {
        "publisher_provider": "youtube",
        "publisher_adapter_version": ADAPTER_VERSION,
        "publisher_account_reference": account_reference,
        "youtube_category_id": settings.youtube_category_id,
        "youtube_made_for_kids": settings.youtube_made_for_kids,
        "youtube_chunk_size": settings.youtube_upload_chunk_size,
        "processing_poll_interval_seconds": poll_interval,
        "processing_max_duration_seconds": max_duration,
    }


def default_platform_options(provider: str) -> dict:
    """The safe, most-restrictive default `platform_options` for a
    provider that has TikTok-style platform-specific fields; `{}` for a
    provider that doesn't (YouTube). Used by worker.publish_runner to
    build PublicationMetadata.platform_options without hardcoding a
    vendor name there -- see providers.base.PublicationMetadata's
    docstring and docs/PUBLISHING.md."""
    name = normalize_provider_name(provider)
    if name == "tiktok":
        from reel_harness.providers.tiktok_publisher import TikTokPostOptions

        return TikTokPostOptions().as_platform_options()
    if name == "instagram":
        from reel_harness.providers.instagram_publisher import InstagramReelsOptions

        return InstagramReelsOptions().as_platform_options()
    return {}


def validate_platform_options(provider: str, creator_info, privacy_status: str, platform_options: dict) -> None:
    """Dispatches to the provider-specific platform-options validator
    (TikTok's/Instagram's own providers.*_publisher.validate_publish_options)
    -- a no-op for a provider without one (YouTube, whose capabilities has
    requires_creator_info=False so callers never even reach this with a
    real creator_info). Keeps worker.publish_runner free of a hardcoded
    vendor name; see docs/PUBLISHING.md's capability model."""
    name = normalize_provider_name(provider)
    if name == "tiktok":
        from reel_harness.providers.tiktok_publisher import platform_options_from_dict, validate_publish_options

        validate_publish_options(creator_info, privacy_status, platform_options_from_dict(platform_options))
    if name == "instagram":
        from reel_harness.providers.instagram_publisher import (
            platform_options_from_dict as instagram_options_from_dict,
        )
        from reel_harness.providers.instagram_publisher import validate_publish_options as validate_instagram

        validate_instagram(creator_info, instagram_options_from_dict(platform_options))


def provider_capabilities(provider: str) -> PublisherCapabilities:
    """Static, credential-free capability lookup (see
    providers.base.PublisherCapabilities and docs/PUBLISHING.md's Phase 3C
    capability model) -- usable by CLI/service validation before any live
    adapter instance can be constructed (e.g. before OAuth credentials
    exist, or during --dry-run, which never touches credentials at all).
    Domain code checks capabilities instead of branching on the provider
    name directly."""
    name = normalize_provider_name(provider)
    if name == "fake":
        return FakePublisher.capabilities
    if name == "youtube":
        from reel_harness.providers.youtube_publisher import CAPABILITIES as YOUTUBE_CAPABILITIES

        return YOUTUBE_CAPABILITIES
    if name == "tiktok":
        from reel_harness.providers.tiktok_publisher import CAPABILITIES as TIKTOK_CAPABILITIES

        return TIKTOK_CAPABILITIES
    if name == "instagram":
        from reel_harness.providers.instagram_publisher import CAPABILITIES as INSTAGRAM_CAPABILITIES

        return INSTAGRAM_CAPABILITIES
    raise NotImplementedError(f"Publisher '{provider}' is not registered yet")


_PUBLISHER_PROVIDER_IDS = ("youtube", "tiktok", "instagram")


def configured_publishers(settings: Settings, credential_backend) -> dict[str, list[str]]:
    """Provider id -> connected account aliases, for the three real
    publisher providers (never "fake" -- it has no OAuth client and no
    credentials to connect). A provider is a KEY in the returned dict only
    if its OAuth client is actually configured (REEL_HARNESS_*_CLIENT_ID/
    _SECRET present) -- so `provider not in result` means "not configured"
    and `result[provider] == []` means "configured, zero accounts
    connected yet", two different UI states callers must not collapse into
    one. No I/O beyond local file reads -- never a real network call."""
    from reel_harness.config import (
        ProviderConfigurationError,
        validate_instagram_credentials_configured,
        validate_tiktok_credentials_configured,
        validate_youtube_credentials_configured,
    )

    validators = {
        "youtube": validate_youtube_credentials_configured,
        "tiktok": validate_tiktok_credentials_configured,
        "instagram": validate_instagram_credentials_configured,
    }
    result: dict[str, list[str]] = {}
    for provider in _PUBLISHER_PROVIDER_IDS:
        try:
            validators[provider](settings)
        except ProviderConfigurationError:
            continue
        result[provider] = credential_backend.list_accounts(provider)
    return result


def _resolve_fresh_youtube_access_token(
    settings: Settings, credential_backend, account_reference: str, oauth_transport: object = None,
) -> str:
    """Called by the adapter fresh before every request (see
    YouTubePublisher's access_token_provider) -- refreshes proactively
    (2-minute safety margin) rather than waiting for a 401, since a
    resumable upload session can span many requests over a long file.
    `oauth_transport` is exposed only for contract tests (httpx.MockTransport);
    production callers never pass it, so the real Google token endpoint is
    used."""
    from datetime import UTC, datetime, timedelta

    from reel_harness.core.errors import ProviderAuthError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_youtube import YouTubeOAuthClient

    cred = credential_backend.get_credential("youtube", account_reference)
    if cred is None:
        raise ProviderNotConfiguredError(f"no youtube credential saved for account {account_reference!r}")
    if cred.invalid:
        raise ProviderAuthError(
            f"youtube credential for {account_reference!r} is marked invalid after a failed refresh "
            "-- re-run publisher-auth youtube"
        )
    if cred.expires_at is None or cred.expires_at > datetime.now(UTC) + timedelta(minutes=2):
        return cred.access_token
    if not cred.refresh_token:
        raise ProviderAuthError(f"youtube credential for {account_reference!r} expired and has no refresh token")

    client = YouTubeOAuthClient(
        settings.youtube_client_id, settings.youtube_client_secret.get_secret_value(),
        transport=oauth_transport,
    )
    try:
        try:
            tokens = client.refresh(cred.refresh_token)
        except ProviderAuthError as exc:
            # The refresh token itself is dead (revoked/expired) -- record
            # this on the credential (never delete it) so publisher-doctor
            # and publisher-account-show can surface it without a network
            # call, then let the caller's own AUTH_REQUIRED handling apply.
            cred.last_refresh_error = (redact(str(exc)) or "")[:300]
            cred.invalid = True
            credential_backend.save_credential(cred)
            raise
    finally:
        client.close()
    refreshed = OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token or cred.refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in), scope=tokens.scope,
        provider="youtube", account_reference=account_reference,
        channel_id=cred.channel_id, channel_title=cred.channel_title,
        created_at=cred.created_at, last_refreshed_at=datetime.now(UTC),
        last_refresh_error=None, invalid=False,
    )
    credential_backend.save_credential(refreshed)
    return refreshed.access_token


def _resolve_fresh_tiktok_access_token(
    settings: Settings, credential_backend, account_reference: str, oauth_transport: object = None,
) -> str:
    """Mirrors _resolve_fresh_youtube_access_token, with one real behavioral
    difference documented in docs/PUBLISHING.md: a TikTok refresh call MAY
    return a *different* refresh_token than the one that was sent, which
    must always replace the stored one (`tokens.refresh_token or
    cred.refresh_token` below already handles both cases -- a rotated
    token wins, a response that omits one falls back to the existing
    one). `refresh_expires_at` is tracked too, since TikTok's refresh
    tokens themselves expire (365 days) unlike Google's."""
    from datetime import UTC, datetime, timedelta

    from reel_harness.core.errors import ProviderAuthError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_tiktok import TikTokOAuthClient

    cred = credential_backend.get_credential("tiktok", account_reference)
    if cred is None:
        raise ProviderNotConfiguredError(f"no tiktok credential saved for account {account_reference!r}")
    if cred.invalid:
        raise ProviderAuthError(
            f"tiktok credential for {account_reference!r} is marked invalid after a failed refresh "
            "-- re-run publisher-auth tiktok"
        )
    if cred.expires_at is None or cred.expires_at > datetime.now(UTC) + timedelta(minutes=2):
        return cred.access_token
    if not cred.refresh_token:
        raise ProviderAuthError(f"tiktok credential for {account_reference!r} expired and has no refresh token")

    client = TikTokOAuthClient(
        settings.tiktok_client_key, settings.tiktok_client_secret.get_secret_value(), settings.tiktok_token_url,
        transport=oauth_transport,
    )
    try:
        try:
            tokens = client.refresh(cred.refresh_token)
        except ProviderAuthError as exc:
            cred.last_refresh_error = (redact(str(exc)) or "")[:300]
            cred.invalid = True
            credential_backend.save_credential(cred)
            raise
    finally:
        client.close()
    refreshed = OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token or cred.refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        refresh_expires_at=(
            datetime.now(UTC) + timedelta(seconds=tokens.refresh_expires_in)
            if tokens.refresh_expires_in is not None else cred.refresh_expires_at
        ),
        scope=tokens.scope, provider="tiktok", account_reference=account_reference,
        channel_id=tokens.open_id or cred.channel_id, channel_title=cred.channel_title,
        created_at=cred.created_at, last_refreshed_at=datetime.now(UTC),
        last_refresh_error=None, invalid=False,
    )
    credential_backend.save_credential(refreshed)
    return refreshed.access_token


def _resolve_fresh_instagram_access_token(
    settings: Settings, credential_backend, account_reference: str, oauth_transport: object = None,
) -> str:
    """Mirrors _resolve_fresh_tiktok_access_token's shape, but Instagram's
    token model is genuinely different from every other provider here:
    there is no separate refresh_token grant. A long-lived access token
    refreshes *itself* (presenting its own current value to the refresh
    endpoint) and gets back a new long-lived token with a renewed ~60-day
    expiry -- see publisher.oauth_instagram.InstagramOAuthClient's
    docstring. `OAuthCredential.refresh_token` stays None for every
    Instagram credential; `cred.access_token` is both the live credential
    and the thing presented to refresh itself."""
    from datetime import UTC, datetime, timedelta

    from reel_harness.core.errors import ProviderAuthError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_instagram import InstagramOAuthClient

    cred = credential_backend.get_credential("instagram", account_reference)
    if cred is None:
        raise ProviderNotConfiguredError(f"no instagram credential saved for account {account_reference!r}")
    if cred.invalid:
        raise ProviderAuthError(
            f"instagram credential for {account_reference!r} is marked invalid after a failed refresh "
            "-- re-run publisher-auth instagram"
        )
    if cred.expires_at is None or cred.expires_at > datetime.now(UTC) + timedelta(minutes=2):
        return cred.access_token

    client = InstagramOAuthClient(
        settings.instagram_app_id, settings.instagram_app_secret.get_secret_value(),
        settings.instagram_token_url, settings.instagram_graph_url, transport=oauth_transport,
    )
    try:
        try:
            tokens = client.refresh_long_lived_token(cred.access_token)
        except ProviderAuthError as exc:
            # The token is too near/past expiry (or younger than the
            # documented 24-hour minimum) to refresh -- record this on
            # the credential (never delete it) so publisher-doctor can
            # surface it without a network call, then let the caller's
            # own AUTH_REQUIRED handling apply.
            cred.last_refresh_error = (redact(str(exc)) or "")[:300]
            cred.invalid = True
            credential_backend.save_credential(cred)
            raise
    finally:
        client.close()
    refreshed = OAuthCredential(
        access_token=tokens.access_token, refresh_token=None,
        expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        scope=cred.scope, provider="instagram", account_reference=account_reference,
        channel_id=cred.channel_id, channel_title=cred.channel_title,
        created_at=cred.created_at, last_refreshed_at=datetime.now(UTC),
        last_refresh_error=None, invalid=False,
    )
    credential_backend.save_credential(refreshed)
    return refreshed.access_token


def resolve_publisher(
    name: str, settings: Settings | None = None,
    credential_backend: CredentialBackend | None = None, account_reference: str = "default",
) -> Publisher:
    """Unlike resolve_llm_provider/resolve_tts_provider/
    resolve_stock_media_provider, publisher resolution is inherently per-
    ACCOUNT, not just per-provider-name -- a publication always targets one
    specific OAuth-connected channel."""
    normalized = normalize_provider_name(name)
    if normalized == "fake":
        return FakePublisher()
    if normalized == "tiktok":
        return _resolve_tiktok_publisher(settings, credential_backend, account_reference)
    if normalized == "instagram":
        return _resolve_instagram_publisher(settings, credential_backend, account_reference)
    if normalized != "youtube":
        return _UnconfiguredPublisher(f"publisher {name!r} is not registered")
    if settings is None or not settings.youtube_client_id or not settings.youtube_client_secret.get_secret_value():
        return _UnconfiguredPublisher(
            "youtube publishing requires REEL_HARNESS_YOUTUBE_CLIENT_ID / "
            "REEL_HARNESS_YOUTUBE_CLIENT_SECRET"
        )
    if credential_backend is None or not credential_backend.has_credential("youtube", account_reference):
        return _UnconfiguredPublisher(
            f"no youtube credential saved for account {account_reference!r} -- run publisher-auth youtube"
        )
    from reel_harness.providers.youtube_publisher import YouTubePublisher

    return YouTubePublisher(
        access_token_provider=lambda: _resolve_fresh_youtube_access_token(
            settings, credential_backend, account_reference,
        ),
        chunk_size=settings.youtube_upload_chunk_size,
        connect_timeout=10.0, read_timeout=60.0, max_retries=3, retry_backoff_seconds=2.0,
    )


def _resolve_tiktok_publisher(
    settings: Settings | None, credential_backend: CredentialBackend | None, account_reference: str,
) -> Publisher:
    if (
        settings is None or not settings.tiktok_client_key or not settings.tiktok_client_secret.get_secret_value()
        or not settings.tiktok_redirect_uri
    ):
        return _UnconfiguredPublisher(
            "tiktok publishing requires REEL_HARNESS_TIKTOK_CLIENT_KEY / REEL_HARNESS_TIKTOK_CLIENT_SECRET / "
            "REEL_HARNESS_TIKTOK_REDIRECT_URI"
        )
    if credential_backend is None or not credential_backend.has_credential("tiktok", account_reference):
        return _UnconfiguredPublisher(
            f"no tiktok credential saved for account {account_reference!r} -- run publisher-auth tiktok"
        )
    from reel_harness.providers.tiktok_publisher import TikTokPublisher

    return TikTokPublisher(
        access_token_provider=lambda: _resolve_fresh_tiktok_access_token(
            settings, credential_backend, account_reference,
        ),
        base_url=settings.tiktok_base_url, chunk_size=settings.tiktok_upload_chunk_size,
        connect_timeout=settings.tiktok_connect_timeout_seconds, read_timeout=settings.tiktok_read_timeout_seconds,
        max_retries=settings.tiktok_max_retries, retry_backoff_seconds=settings.tiktok_retry_backoff_seconds,
    )


def _resolve_instagram_publisher(
    settings: Settings | None, credential_backend: CredentialBackend | None, account_reference: str,
) -> Publisher:
    if (
        settings is None or not settings.instagram_app_id
        or not settings.instagram_app_secret.get_secret_value() or not settings.instagram_redirect_uri
    ):
        return _UnconfiguredPublisher(
            "instagram publishing requires REEL_HARNESS_INSTAGRAM_APP_ID / "
            "REEL_HARNESS_INSTAGRAM_APP_SECRET / REEL_HARNESS_INSTAGRAM_REDIRECT_URI"
        )
    if credential_backend is None:
        return _UnconfiguredPublisher(
            f"no instagram credential saved for account {account_reference!r} -- run publisher-auth instagram"
        )
    cred = credential_backend.get_credential("instagram", account_reference)
    if cred is None or not cred.channel_id:
        return _UnconfiguredPublisher(
            f"no instagram credential saved for account {account_reference!r} -- run publisher-auth instagram"
        )
    from reel_harness.providers.instagram_publisher import InstagramPublisher

    return InstagramPublisher(
        access_token_provider=lambda: _resolve_fresh_instagram_access_token(
            settings, credential_backend, account_reference,
        ),
        graph_url=settings.instagram_graph_url, api_version=settings.instagram_graph_api_version,
        account_id=cred.channel_id,
        connect_timeout=settings.instagram_connect_timeout_seconds,
        read_timeout=settings.instagram_read_timeout_seconds,
        max_retries=settings.instagram_max_retries, retry_backoff_seconds=settings.instagram_retry_backoff_seconds,
    )
