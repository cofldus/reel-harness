from __future__ import annotations

from pathlib import Path

from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProviderConfigurationError(ValueError):
    """A real provider is selected but its required configuration is missing or
    invalid. Raised at startup so misconfiguration fails loudly and early --
    never at some later point mid-pipeline."""


def normalize_provider_name(name: str | None) -> str:
    """Accept both spellings ('openai_compatible' and 'openai-compatible');
    canonical form uses hyphens."""
    return (name or "fake").strip().lower().replace("_", "-")


def _llm_alias(canonical: str, *legacy: str) -> AliasChoices:
    return AliasChoices(canonical, *legacy)


# Audio formats the pipeline can safely accept from a real TTS provider (every
# result is normalized to canonical PCM WAV before rendering regardless).
TTS_SUPPORTED_FORMATS = frozenset({"wav", "mp3"})
TTS_SPEED_RANGE = (0.25, 4.0)

ASSET_SUPPORTED_ORIENTATIONS = frozenset({"portrait", "landscape", "square"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", populate_by_name=True,
    )

    database_url: str = "sqlite:///./reel_harness.db"
    # PostgreSQL-only (create_engine_from_url ignores these for SQLite, which
    # has no connection pool or server-side statement-timeout concept of its
    # own) -- see db.schema.create_engine_from_url.
    db_pool_size: int = Field(5, validation_alias=_llm_alias("REEL_HARNESS_DB_POOL_SIZE", "DB_POOL_SIZE"))
    db_pool_max_overflow: int = Field(
        10, validation_alias=_llm_alias("REEL_HARNESS_DB_POOL_MAX_OVERFLOW", "DB_POOL_MAX_OVERFLOW"),
    )
    db_statement_timeout_seconds: float | None = Field(
        None,
        validation_alias=_llm_alias("REEL_HARNESS_DB_STATEMENT_TIMEOUT_SECONDS", "DB_STATEMENT_TIMEOUT_SECONDS"),
    )
    jobs_dir: Path = Path("./jobs")
    # Fable cinematic projects get their own storage root (a second
    # LocalFilesystemStorage instance on AppContext) -- project artifacts
    # live under fable_projects/{project_id}/..., never mixed into
    # jobs/{job_id}/... The same UUID-only/path-traversal rules apply.
    fable_projects_dir: Path = Field(
        Path("./fable_projects"),
        validation_alias=_llm_alias("REEL_HARNESS_FABLE_PROJECTS_DIR", "FABLE_PROJECTS_DIR"),
    )
    app_api_key: str = "changeme-local-dev-key"
    # `serve --host` defaults from this (a CLI flag still overrides for a
    # one-off run) so ops.preflight's public-bind check and the running
    # server always agree on the same source of truth. The web UI has no
    # login -- only CSRF protection for browser-originated requests -- so
    # binding beyond loopback needs a real authenticating reverse proxy in
    # front of it; see ops.preflight._check_public_bind_security.
    api_host: str = Field("127.0.0.1", validation_alias=_llm_alias("REEL_HARNESS_API_HOST", "API_HOST"))
    log_level: str = "INFO"

    # Worker lease policy. The heartbeat interval must stay well below the
    # timeout (<= 1/3) so a healthy worker in a long ffmpeg/provider stage is
    # never reclaimed as stale.
    lease_timeout_seconds: int = 300
    lease_heartbeat_seconds: int = 60

    # Continuous worker daemon (reel-harness worker-run). CLI flags override
    # these per invocation.
    worker_poll_interval_seconds: float = 5.0
    worker_idle_exit_after_seconds: float | None = None  # None = run until stopped
    worker_max_jobs: int | None = None  # None = unlimited
    worker_stop_on_error: bool = False

    # LLM provider selection and its adapter configuration. "fake" needs none
    # of the rest; "openai-compatible" (also accepted: "openai_compatible")
    # talks to any /chat/completions-style endpoint chosen via
    # llm_base_url/llm_model. Canonical env vars are REEL_HARNESS_LLM_*; the
    # bare LLM_* names remain accepted for backward compatibility. The API key
    # is a SecretStr (never shown in repr), read from the environment/.env
    # only, registered as a redaction secret at bootstrap, and never written
    # to the DB, manifests, or logs.
    llm_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_LLM_PROVIDER", "LLM_PROVIDER"))
    llm_base_url: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_LLM_BASE_URL", "LLM_BASE_URL"))
    llm_model: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_LLM_MODEL", "LLM_MODEL"))
    llm_api_key: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_LLM_API_KEY", "LLM_API_KEY"))
    llm_connect_timeout_seconds: float = Field(
        10.0, validation_alias=_llm_alias(
            "REEL_HARNESS_LLM_CONNECT_TIMEOUT", "LLM_CONNECT_TIMEOUT_SECONDS"))
    llm_read_timeout_seconds: float = Field(
        60.0, validation_alias=_llm_alias("REEL_HARNESS_LLM_READ_TIMEOUT", "LLM_READ_TIMEOUT_SECONDS"))
    llm_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_LLM_MAX_RETRIES", "LLM_MAX_RETRIES"))
    llm_retry_backoff_seconds: float = Field(
        2.0, validation_alias=_llm_alias("REEL_HARNESS_LLM_RETRY_BACKOFF", "LLM_RETRY_BACKOFF_SECONDS"))
    llm_temperature: float = Field(
        0.7, validation_alias=_llm_alias("REEL_HARNESS_LLM_TEMPERATURE", "LLM_TEMPERATURE"))
    llm_max_output_tokens: int = Field(
        1200, validation_alias=_llm_alias("REEL_HARNESS_LLM_MAX_OUTPUT_TOKENS", "LLM_MAX_OUTPUT_TOKENS"))

    # TTS provider selection and adapter configuration. Same conventions as the
    # LLM block: "fake" needs nothing, "openai-compatible" talks to any
    # /audio/speech-style endpoint, the API key is a SecretStr registered for
    # redaction and never persisted. The audio format is restricted to
    # TTS_SUPPORTED_FORMATS -- never a free-form string.
    tts_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_TTS_PROVIDER", "TTS_PROVIDER"))
    tts_base_url: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_TTS_BASE_URL", "TTS_BASE_URL"))
    tts_model: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_TTS_MODEL", "TTS_MODEL"))
    tts_api_key: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_TTS_API_KEY", "TTS_API_KEY"))
    tts_voice: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_TTS_VOICE", "TTS_VOICE"))
    tts_format: str = Field(
        "wav", validation_alias=_llm_alias("REEL_HARNESS_TTS_FORMAT", "TTS_FORMAT"))
    tts_speed: float = Field(
        1.0, validation_alias=_llm_alias("REEL_HARNESS_TTS_SPEED", "TTS_SPEED"))
    tts_connect_timeout_seconds: float = Field(
        10.0, validation_alias=_llm_alias("REEL_HARNESS_TTS_CONNECT_TIMEOUT", "TTS_CONNECT_TIMEOUT_SECONDS"))
    tts_read_timeout_seconds: float = Field(
        60.0, validation_alias=_llm_alias("REEL_HARNESS_TTS_READ_TIMEOUT", "TTS_READ_TIMEOUT_SECONDS"))
    tts_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_TTS_MAX_RETRIES", "TTS_MAX_RETRIES"))
    tts_retry_backoff_seconds: float = Field(
        2.0, validation_alias=_llm_alias("REEL_HARNESS_TTS_RETRY_BACKOFF", "TTS_RETRY_BACKOFF_SECONDS"))

    # Cinematic video generation provider (Fable, Phase F1). Same
    # conventions as the other provider blocks: "fake" needs nothing and is
    # the default; the real adapter (F5) will add its base-url/key fields
    # here when it lands. Selection only for now.
    cinematic_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_CINEMATIC_PROVIDER", "CINEMATIC_PROVIDER"))
    cinematic_model: str = Field(
        "veo-3.1-fast-generate-001", validation_alias=_llm_alias(
            "REEL_HARNESS_CINEMATIC_MODEL", "CINEMATIC_MODEL"))
    # Published list price per generated SECOND. Configurable rather than
    # hardcoded for the same reason as the image price -- a vendor's
    # tariff is not this project's to promise. Unset it and estimates
    # report unknown, which makes a budgeted project refuse to run.
    cinematic_price_per_second_usd: float | None = Field(
        0.15, validation_alias=_llm_alias(
            "REEL_HARNESS_CINEMATIC_PRICE_PER_SECOND_USD", "CINEMATIC_PRICE_PER_SECOND_USD"))
    # Veo generates native audio. Left on by default because a silent
    # clip is a worse default than one with sound the editor can mute.
    cinematic_generate_audio: bool = Field(
        True, validation_alias=_llm_alias(
            "REEL_HARNESS_CINEMATIC_GENERATE_AUDIO", "CINEMATIC_GENERATE_AUDIO"))
    # Film assembly (media.film_editor). "cut" is the default because a
    # hard cut is a lossless stream COPY -- every other option blends
    # pixels and therefore costs a full re-encode.
    fable_transition: str = Field(
        "cut", validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_TRANSITION", "FABLE_TRANSITION"))
    fable_transition_seconds: float = Field(
        0.5, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_TRANSITION_SECONDS", "FABLE_TRANSITION_SECONDS"))
    fable_fade_in_seconds: float = Field(
        0.0, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_FADE_IN_SECONDS", "FABLE_FADE_IN_SECONDS"))
    fable_fade_out_seconds: float = Field(
        0.0, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_FADE_OUT_SECONDS", "FABLE_FADE_OUT_SECONDS"))
    # Muting is an explicit editorial choice. Veo generates native audio,
    # and silently dropping it would be a surprising default.
    fable_mute_audio: bool = Field(
        False, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_MUTE_AUDIO", "FABLE_MUTE_AUDIO"))
    # A transition-bearing assembly re-encodes every frame; the default
    # subprocess timeout is sized for a stream copy, not for that.
    # How long one shot generation may take before the worker stops
    # waiting. A real video model takes one to three minutes; the old
    # hardcoded budget was twelve SECONDS, so every real generation was
    # written off as transient and billed anyway.
    fable_generation_timeout_seconds: float = Field(
        600.0, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_GENERATION_TIMEOUT_SECONDS",
            "FABLE_GENERATION_TIMEOUT_SECONDS"))
    fable_generation_poll_interval_seconds: float = Field(
        5.0, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_GENERATION_POLL_INTERVAL_SECONDS",
            "FABLE_GENERATION_POLL_INTERVAL_SECONDS"))
    fable_render_timeout_seconds: float = Field(
        1800.0, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_RENDER_TIMEOUT_SECONDS", "FABLE_RENDER_TIMEOUT_SECONDS"))
    # Narrative Director (story -> shot plan). "openai-compatible" reuses
    # the REEL_HARNESS_LLM_* endpoint/credentials above -- adaptation is a
    # chat-completions call against the same kind of endpoint, so a second
    # set of connection settings would be duplication, not isolation.
    narrative_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_NARRATIVE_PROVIDER", "NARRATIVE_PROVIDER"))
    # An adaptation is a much larger generation than a short-form script
    # (bible + characters + locations + scenes + shots), so it gets its
    # own output budget and read timeout rather than reusing the script
    # call's much smaller ones.
    narrative_max_output_tokens: int = Field(
        6000, validation_alias=_llm_alias(
            "REEL_HARNESS_NARRATIVE_MAX_OUTPUT_TOKENS", "NARRATIVE_MAX_OUTPUT_TOKENS"))
    narrative_read_timeout_seconds: float = Field(
        120.0, validation_alias=_llm_alias(
            "REEL_HARNESS_NARRATIVE_READ_TIMEOUT", "NARRATIVE_READ_TIMEOUT_SECONDS"))
    # Reference-image generation (virtual actor / location stills).
    reference_image_provider: str = Field(
        "fake", validation_alias=_llm_alias(
            "REEL_HARNESS_REFERENCE_IMAGE_PROVIDER", "REFERENCE_IMAGE_PROVIDER"))
    # gemini-2.5-flash-image rather than the newer 3.1: 3.1 appears in
    # the model list but a live probe of four candidates on a real
    # project returned 404 for it, so the previous default was a name
    # that could not actually be called. A default that fails on a fresh
    # account is worse than an older model that works; override the env
    # var once 3.1 is generally reachable.
    reference_image_model: str = Field(
        "gemini-2.5-flash-image", validation_alias=_llm_alias(
            "REEL_HARNESS_REFERENCE_IMAGE_MODEL", "REFERENCE_IMAGE_MODEL"))
    # Published list price per generated image. Configurable rather than
    # hardcoded because a vendor's price list is not this project's to
    # promise -- set it to 0 to force `known=False` estimates (and so
    # refuse to run under a budget) when the real tariff is unknown.
    reference_image_price_usd: float | None = Field(
        0.067, validation_alias=_llm_alias(
            "REEL_HARNESS_REFERENCE_IMAGE_PRICE_USD", "REFERENCE_IMAGE_PRICE_USD"))
    # Google credentials, shared by the reference-image adapter and (F5)
    # the Veo video adapter -- ONE SDK and ONE credential for both is why
    # this vendor was chosen. Two auth paths: an API key against the
    # Gemini Developer API, or Vertex AI with a project/location using
    # ambient application-default credentials.
    google_api_key: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_GOOGLE_API_KEY", "GOOGLE_API_KEY"))
    google_project: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_GOOGLE_PROJECT", "GOOGLE_PROJECT"))
    # us-central1 only for Veo's GA endpoint (docs/STATUS.md's research);
    # the default matches so F5 needs no second decision.
    google_location: str = Field(
        "us-central1", validation_alias=_llm_alias(
            "REEL_HARNESS_GOOGLE_LOCATION", "GOOGLE_LOCATION"))
    google_use_vertex: bool = Field(
        False, validation_alias=_llm_alias("REEL_HARNESS_GOOGLE_USE_VERTEX", "GOOGLE_USE_VERTEX"))
    # How many candidate takes to generate per shot (1, 2 or 4). Each take
    # is a separate paid generation, so the default is 1 -- more candidates
    # is an explicit choice to spend N times as much for something to
    # choose between. A project may override this for itself.
    fable_takes_per_shot: int = Field(
        1, validation_alias=_llm_alias(
            "REEL_HARNESS_FABLE_TAKES_PER_SHOT", "FABLE_TAKES_PER_SHOT"))

    # Stock-media (asset) provider selection and adapter configuration. Same
    # conventions as the LLM/TTS blocks: "fake" needs nothing, "pexels" talks
    # to the real Pexels Video API, the API key is a SecretStr registered for
    # redaction and never persisted. See docs/OPERATIONS.md for why Pexels was
    # chosen and its license terms.
    asset_provider: str = Field(
        "fake", validation_alias=_llm_alias("REEL_HARNESS_ASSET_PROVIDER", "ASSET_PROVIDER"))
    asset_base_url: str = Field(
        "https://api.pexels.com/videos",
        validation_alias=_llm_alias("REEL_HARNESS_ASSET_BASE_URL", "ASSET_BASE_URL"))
    asset_api_key: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_ASSET_API_KEY", "ASSET_API_KEY"))
    asset_connect_timeout_seconds: float = Field(
        10.0, validation_alias=_llm_alias("REEL_HARNESS_ASSET_CONNECT_TIMEOUT", "ASSET_CONNECT_TIMEOUT_SECONDS"))
    asset_read_timeout_seconds: float = Field(
        60.0, validation_alias=_llm_alias("REEL_HARNESS_ASSET_READ_TIMEOUT", "ASSET_READ_TIMEOUT_SECONDS"))
    asset_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_ASSET_MAX_RETRIES", "ASSET_MAX_RETRIES"))
    asset_retry_backoff_seconds: float = Field(
        2.0, validation_alias=_llm_alias("REEL_HARNESS_ASSET_RETRY_BACKOFF", "ASSET_RETRY_BACKOFF_SECONDS"))
    asset_per_page: int = Field(
        15, validation_alias=_llm_alias("REEL_HARNESS_ASSET_PER_PAGE", "ASSET_PER_PAGE"))
    asset_orientation: str = Field(
        "portrait", validation_alias=_llm_alias("REEL_HARNESS_ASSET_ORIENTATION", "ASSET_ORIENTATION"))
    asset_min_width: int = Field(
        480, validation_alias=_llm_alias("REEL_HARNESS_ASSET_MIN_WIDTH", "ASSET_MIN_WIDTH"))
    asset_min_height: int = Field(
        480, validation_alias=_llm_alias("REEL_HARNESS_ASSET_MIN_HEIGHT", "ASSET_MIN_HEIGHT"))
    asset_min_duration_seconds: float = Field(
        1.0, validation_alias=_llm_alias("REEL_HARNESS_ASSET_MIN_DURATION", "ASSET_MIN_DURATION_SECONDS"))
    asset_max_duration_seconds: float = Field(
        60.0, validation_alias=_llm_alias("REEL_HARNESS_ASSET_MAX_DURATION", "ASSET_MAX_DURATION_SECONDS"))
    asset_safe_search: bool = Field(
        True, validation_alias=_llm_alias("REEL_HARNESS_ASSET_SAFE_SEARCH", "ASSET_SAFE_SEARCH"))

    # Generic render-stage feature, independent of which provider produced a
    # scene's asset/audio (see providers/registry.py's own rule against
    # vendor-name branching outside the registry): burns each scene's
    # Script.subtitle text into the rendered video via ffmpeg's drawtext
    # filter (media/ffmpeg_render.py). Off by default so every existing
    # fake/real pipeline's rendered output is unchanged unless explicitly
    # opted in -- primarily useful for Demo Mode, where it turns a silent
    # colored placeholder into something with readable on-screen captions.
    render_burn_subtitles: bool = Field(
        False, validation_alias=_llm_alias("REEL_HARNESS_RENDER_BURN_SUBTITLES", "RENDER_BURN_SUBTITLES"))

    # Publisher (Phase 3A) global safety switch: `public` privacy uploads are
    # refused (core.publish_service.PublicationService.create_publication)
    # unless this AND the caller's own explicit --confirm-public-upload are
    # both true. `private` is always available with no extra confirmation.
    allow_public_upload: bool = Field(
        False, validation_alias=_llm_alias("REEL_HARNESS_ALLOW_PUBLIC_UPLOAD", "ALLOW_PUBLIC_UPLOAD"))

    # Fable (Phase F3) global spend switch, deliberately shaped exactly like
    # allow_public_upload above: a cost-incurring cinematic/reference-image
    # provider runs only when this is true AND the project itself carries an
    # explicit budget limit (core.cost_service.assert_paid_generation_allowed).
    # Two independent decisions -- one operator-wide, one per project -- so
    # neither can be inferred from the other. The offline fake/demo tiers cost
    # nothing and are never gated by this.
    allow_paid_generation: bool = Field(
        False, validation_alias=_llm_alias(
            "REEL_HARNESS_ALLOW_PAID_GENERATION", "ALLOW_PAID_GENERATION"))

    # YouTube OAuth (installed-app/loopback flow -- see docs/PUBLISHING.md
    # and publisher.oauth_youtube). The client id/secret come from a Google
    # Cloud Console OAuth client of type "Desktop app"; unlike the LLM/TTS/
    # asset provider keys, these authorize the *application*, not a
    # specific account -- per-account access/refresh tokens live in the
    # credential backend below, never in Settings or the jobs DB.
    youtube_client_id: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_ID"))
    youtube_client_secret: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "YOUTUBE_CLIENT_SECRET"))
    # Where OAuth credentials and upload-session references are stored --
    # must never resolve inside the repository (enforced by
    # publisher.secret_store.resolve_secret_dir). Defaults to
    # ~/.reel-harness/credentials.
    credential_dir: Path | None = Field(
        None, validation_alias=_llm_alias("REEL_HARNESS_CREDENTIAL_DIR", "CREDENTIAL_DIR"))
    youtube_upload_chunk_size: int = Field(
        8 * 262144,  # 2 MiB; must stay a multiple of 262144 (256 KiB) per the resumable upload protocol
        validation_alias=_llm_alias("REEL_HARNESS_YOUTUBE_CHUNK_SIZE", "YOUTUBE_UPLOAD_CHUNK_SIZE"),
    )
    youtube_category_id: str = Field(
        "22",  # "People & Blogs" -- a reasonable default; always overridable per channel/job
        validation_alias=_llm_alias("REEL_HARNESS_YOUTUBE_CATEGORY_ID", "YOUTUBE_CATEGORY_ID"),
    )
    youtube_made_for_kids: bool = Field(
        False, validation_alias=_llm_alias("REEL_HARNESS_YOUTUBE_MADE_FOR_KIDS", "YOUTUBE_MADE_FOR_KIDS"))

    # TikTok OAuth + Content Posting API (Phase 3C -- see docs/PUBLISHING.md
    # for the official docs this was built from, checked 2026-07-29). Unlike
    # YouTube's fixed Google endpoints, TikTok's auth/token/base URLs are
    # exposed as settings (not hardcoded) because they're what a
    # contract-test fake server, or a future regional API variant, needs to
    # override. `tiktok_redirect_uri` has NO default -- it must be a URL the
    # operator has actually registered with their TikTok app; there is no
    # safe generic default the way `credential_dir` has one.
    tiktok_client_key: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_CLIENT_KEY", "TIKTOK_CLIENT_KEY"))
    tiktok_client_secret: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_CLIENT_SECRET", "TIKTOK_CLIENT_SECRET"))
    tiktok_redirect_uri: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_REDIRECT_URI", "TIKTOK_REDIRECT_URI"))
    tiktok_base_url: str = Field(
        "https://open.tiktokapis.com",
        validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_BASE_URL", "TIKTOK_BASE_URL"))
    tiktok_auth_url: str = Field(
        "https://www.tiktok.com/v2/auth/authorize/",
        validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_AUTH_URL", "TIKTOK_AUTH_URL"))
    tiktok_token_url: str = Field(
        "https://open.tiktokapis.com/v2/oauth/token/",
        validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_TOKEN_URL", "TIKTOK_TOKEN_URL"))
    # The official Content Posting API docs do not specify a min/max chunk
    # size for FILE_UPLOAD (see docs/PUBLISHING.md's "not specified" section)
    # -- 10 MiB is a conservative, fully-operator-overridable default, not a
    # documented requirement.
    tiktok_upload_chunk_size: int = Field(
        10 * 1024 * 1024,
        validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_UPLOAD_CHUNK_SIZE", "TIKTOK_UPLOAD_CHUNK_SIZE"))
    tiktok_connect_timeout_seconds: float = Field(
        10.0, validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_CONNECT_TIMEOUT", "TIKTOK_CONNECT_TIMEOUT_SECONDS"))
    tiktok_read_timeout_seconds: float = Field(
        30.0, validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_READ_TIMEOUT", "TIKTOK_READ_TIMEOUT_SECONDS"))
    tiktok_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_MAX_RETRIES", "TIKTOK_MAX_RETRIES"))
    tiktok_retry_backoff_seconds: float = Field(
        2.0, validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_RETRY_BACKOFF", "TIKTOK_RETRY_BACKOFF_SECONDS"))
    # SELF_ONLY is TikTok's most restrictive privacy_level -- always the
    # provider default per the capability model's "most restrictive by
    # default" rule (see providers.base.PublisherCapabilities), independent
    # of the unaudited-app restriction that forces private visibility
    # regardless of what's requested anyway.
    tiktok_default_privacy: str = Field(
        "SELF_ONLY", validation_alias=_llm_alias("REEL_HARNESS_TIKTOK_DEFAULT_PRIVACY", "TIKTOK_DEFAULT_PRIVACY"))

    # Instagram Reels OAuth + Content Publishing (Phase 3D -- see
    # docs/PUBLISHING.md, official docs checked 2026-07-29). Uses Instagram
    # Login for Business only (no Facebook Page/Business Manager
    # dependency) -- `instagram_app_id`/`_secret` come from a Meta App
    # Dashboard app, `instagram_redirect_uri` has NO default (must be a URI
    # the operator actually registered), same reasoning as TikTok's.
    instagram_app_id: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_APP_ID", "INSTAGRAM_APP_ID"))
    instagram_app_secret: SecretStr = Field(
        SecretStr(""), validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_APP_SECRET", "INSTAGRAM_APP_SECRET"))
    instagram_redirect_uri: str = Field(
        "", validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", "INSTAGRAM_REDIRECT_URI"))
    instagram_auth_url: str = Field(
        "https://www.instagram.com/oauth/authorize",
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_AUTH_URL", "INSTAGRAM_AUTH_URL"))
    instagram_token_url: str = Field(
        "https://api.instagram.com/oauth/access_token",
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_TOKEN_URL", "INSTAGRAM_TOKEN_URL"))
    # Separate from the short-lived token exchange above -- Instagram Login
    # for Business's long-lived-token step is its own endpoint (see
    # docs/PUBLISHING.md), unlike YouTube/TikTok's single-endpoint refresh.
    instagram_graph_url: str = Field(
        "https://graph.instagram.com",
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_GRAPH_BASE_URL", "INSTAGRAM_GRAPH_BASE_URL"))
    # Graph API versions are periodically deprecated/sunset by Meta (see
    # docs/PUBLISHING.md) -- kept operator-overridable rather than
    # hardcoded so a sunset version doesn't require a code change.
    instagram_graph_api_version: str = Field(
        "v25.0",
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_GRAPH_API_VERSION", "INSTAGRAM_GRAPH_API_VERSION"))
    instagram_connect_timeout_seconds: float = Field(
        10.0,
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_CONNECT_TIMEOUT", "INSTAGRAM_CONNECT_TIMEOUT_SECONDS"))
    instagram_read_timeout_seconds: float = Field(
        60.0,
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_READ_TIMEOUT", "INSTAGRAM_READ_TIMEOUT_SECONDS"))
    instagram_max_retries: int = Field(
        3, validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_MAX_RETRIES", "INSTAGRAM_MAX_RETRIES"))
    instagram_retry_backoff_seconds: float = Field(
        2.0,
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_RETRY_BACKOFF", "INSTAGRAM_RETRY_BACKOFF_SECONDS"))
    # "resumable" (default, implemented): direct binary upload to
    # rupload.facebook.com, no public hosting needed -- see
    # docs/PUBLISHING.md's "Two upload paths" section for why this is the
    # default instead of Meta's video_url-hosted alternative.
    # "external_url" is accepted as a config value (an operator may already
    # have a hosted-URL setup) but is NOT implemented this phase --
    # selecting it fails validation explicitly, the same
    # documented-but-unimplemented precedent as TikTok's PULL_FROM_URL.
    instagram_media_url_mode: str = Field(
        "resumable",
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_MEDIA_URL_MODE", "INSTAGRAM_MEDIA_URL_MODE"))
    # Only meaningful for the (unimplemented) external_url mode -- kept as a
    # validated setting now so enabling that mode later is a pure adapter
    # change, not a config-schema change.
    instagram_media_url_ttl_seconds: float = Field(
        3600.0,
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_MEDIA_URL_TTL", "INSTAGRAM_MEDIA_URL_TTL_SECONDS"))
    # Reels publishing is always public (Instagram has no private/unlisted
    # concept -- see docs/PUBLISHING.md) -- share_to_feed only controls
    # Feed+Reels-tab vs Reels-tab-only visibility, not privacy.
    instagram_share_to_feed_default: bool = Field(
        False,
        validation_alias=_llm_alias("REEL_HARNESS_INSTAGRAM_SHARE_TO_FEED", "INSTAGRAM_SHARE_TO_FEED_DEFAULT"))

    # Processing poller (Phase 3B, worker.publish_runner._processing_stage /
    # worker.publish_lease.lease_next_processing_publication). Pinned onto
    # each publication's publisher_config at session-creation time (like
    # youtube_chunk_size already is) rather than read live from Settings, so
    # a publication's polling behavior never silently changes mid-flight if
    # the operator edits config while it's in progress.
    publisher_processing_poll_interval_seconds: float = Field(
        30.0, validation_alias=_llm_alias(
            "REEL_HARNESS_PUBLISHER_PROCESSING_POLL_INTERVAL", "PUBLISHER_PROCESSING_POLL_INTERVAL_SECONDS",
        ),
    )
    publisher_processing_max_duration_seconds: float = Field(
        3600.0, validation_alias=_llm_alias(
            "REEL_HARNESS_PUBLISHER_PROCESSING_MAX_DURATION", "PUBLISHER_PROCESSING_MAX_DURATION_SECONDS",
        ),
    )


def _validate_llm_settings(settings: Settings) -> None:
    name = normalize_provider_name(settings.llm_provider)
    if name in ("fake", "demo"):
        return
    if name != "openai-compatible":
        raise ProviderConfigurationError(
            f"unknown llm provider {settings.llm_provider!r} (supported: fake, demo, openai_compatible)"
        )
    missing = [
        var for var, value in (
            ("REEL_HARNESS_LLM_BASE_URL", settings.llm_base_url),
            ("REEL_HARNESS_LLM_MODEL", settings.llm_model),
            ("REEL_HARNESS_LLM_API_KEY", settings.llm_api_key.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "llm provider 'openai-compatible' is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def _validate_tts_settings(settings: Settings) -> None:
    if settings.tts_format not in TTS_SUPPORTED_FORMATS:
        raise ProviderConfigurationError(
            f"unsupported tts format {settings.tts_format!r} "
            f"(supported: {', '.join(sorted(TTS_SUPPORTED_FORMATS))})"
        )
    low, high = TTS_SPEED_RANGE
    if not (low <= settings.tts_speed <= high):
        raise ProviderConfigurationError(f"tts speed {settings.tts_speed} outside [{low}, {high}]")
    if settings.tts_connect_timeout_seconds <= 0 or settings.tts_read_timeout_seconds <= 0:
        raise ProviderConfigurationError("tts timeouts must be positive")
    if settings.tts_max_retries < 0:
        raise ProviderConfigurationError("tts retry count must not be negative")

    name = normalize_provider_name(settings.tts_provider)
    if name in ("fake", "demo"):
        return
    if name != "openai-compatible":
        raise ProviderConfigurationError(
            f"unknown tts provider {settings.tts_provider!r} (supported: fake, demo, openai_compatible)"
        )
    missing = [
        var for var, value in (
            ("REEL_HARNESS_TTS_BASE_URL", settings.tts_base_url),
            ("REEL_HARNESS_TTS_MODEL", settings.tts_model),
            ("REEL_HARNESS_TTS_VOICE", settings.tts_voice),
            ("REEL_HARNESS_TTS_API_KEY", settings.tts_api_key.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "tts provider 'openai-compatible' is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def _validate_cinematic_settings(settings: Settings) -> None:
    from reel_harness.providers.registry import CINEMATIC_VIDEO_PROVIDERS

    name = normalize_provider_name(settings.cinematic_provider)
    if name not in CINEMATIC_VIDEO_PROVIDERS:
        raise ProviderConfigurationError(
            f"unknown cinematic provider {settings.cinematic_provider!r} "
            f"(supported: {', '.join(sorted(CINEMATIC_VIDEO_PROVIDERS))})"
        )
    if name != "google":
        return
    # Selecting the real adapter without a way to authenticate -- or
    # pinned to a region the model is not served from -- fails HERE, at
    # startup, with the exact variable names. Never at first use, halfway
    # through a paid generation run.
    from reel_harness.providers.google_cinematic_video import SUPPORTED_LOCATION

    if settings.google_use_vertex:
        if not settings.google_project:
            raise ProviderConfigurationError(
                f"cinematic provider {name!r} is selected but credentials are not "
                "configured: missing REEL_HARNESS_GOOGLE_PROJECT"
            )
        if settings.google_location != SUPPORTED_LOCATION:
            raise ProviderConfigurationError(
                f"{settings.cinematic_model} is only served from "
                f"{SUPPORTED_LOCATION!r}, but REEL_HARNESS_GOOGLE_LOCATION is "
                f"{settings.google_location!r}"
            )
    elif not settings.google_api_key.get_secret_value():
        raise ProviderConfigurationError(
            f"cinematic provider {name!r} is selected but credentials are not "
            "configured: missing REEL_HARNESS_GOOGLE_API_KEY"
        )


def _validate_narrative_settings(settings: Settings) -> None:
    from reel_harness.providers.registry import NARRATIVE_DIRECTORS

    name = normalize_provider_name(settings.narrative_provider)
    if name not in NARRATIVE_DIRECTORS:
        raise ProviderConfigurationError(
            f"unknown narrative provider {settings.narrative_provider!r} "
            f"(supported: {', '.join(sorted(NARRATIVE_DIRECTORS))})"
        )
    if name == "fake":
        return
    # Adaptation reuses the LLM endpoint block -- report the exact vars.
    missing = [
        var for var, value in (
            ("REEL_HARNESS_LLM_BASE_URL", settings.llm_base_url),
            ("REEL_HARNESS_LLM_MODEL", settings.llm_model),
            ("REEL_HARNESS_LLM_API_KEY", settings.llm_api_key.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            f"narrative provider {name!r} is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def _validate_edit_plan(settings: Settings) -> None:
    """A malformed edit plan fails at STARTUP rather than after every shot
    has been generated and paid for -- the final render is the very last
    step, and discovering an unsupported transition there would waste the
    whole run."""
    from reel_harness.media.film_editor import SUPPORTED_TRANSITIONS, EditPlan, validate_plan

    if settings.fable_transition not in SUPPORTED_TRANSITIONS:
        raise ProviderConfigurationError(
            f"unsupported fable transition {settings.fable_transition!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_TRANSITIONS))})"
        )
    plan = edit_plan_from_settings(settings)
    try:
        # Validated against a nominal 8s clip pair -- the real durations
        # are re-checked at render time against the actual footage.
        validate_plan(plan, [8.0, 8.0])
    except ValueError as exc:
        raise ProviderConfigurationError(f"invalid fable edit plan: {exc}") from exc
    assert isinstance(plan, EditPlan)


def edit_plan_from_settings(settings: Settings):
    """The one place settings become an EditPlan, so the startup check and
    the renderer can never disagree about what was configured."""
    from reel_harness.media.film_editor import EditPlan

    return EditPlan(
        transition=settings.fable_transition,
        transition_sec=settings.fable_transition_seconds,
        fade_in_sec=settings.fable_fade_in_seconds,
        fade_out_sec=settings.fable_fade_out_seconds,
        mute_audio=settings.fable_mute_audio,
    )


def _validate_takes_per_shot(settings: Settings) -> None:
    from reel_harness.core.cinematic_state import SUPPORTED_TAKES_PER_SHOT

    if settings.fable_takes_per_shot not in SUPPORTED_TAKES_PER_SHOT:
        raise ProviderConfigurationError(
            f"unsupported fable_takes_per_shot {settings.fable_takes_per_shot!r} "
            f"(supported: {', '.join(str(n) for n in sorted(SUPPORTED_TAKES_PER_SHOT))})"
        )


def _validate_reference_image_settings(settings: Settings) -> None:
    from reel_harness.providers.registry import REFERENCE_IMAGE_PROVIDERS

    name = normalize_provider_name(settings.reference_image_provider)
    if name not in REFERENCE_IMAGE_PROVIDERS:
        raise ProviderConfigurationError(
            f"unknown reference image provider {settings.reference_image_provider!r} "
            f"(supported: {', '.join(sorted(REFERENCE_IMAGE_PROVIDERS))})"
        )
    if name != "google":
        return
    # Selecting the real adapter without a way to authenticate fails HERE,
    # at startup, with the exact missing variable names -- never at first
    # use, halfway through a paid casting run.
    if settings.google_use_vertex:
        missing = [
            var for var, value in (
                ("REEL_HARNESS_GOOGLE_PROJECT", settings.google_project),
                ("REEL_HARNESS_GOOGLE_LOCATION", settings.google_location),
            ) if not value
        ]
    else:
        missing = [
            "REEL_HARNESS_GOOGLE_API_KEY",
        ] if not settings.google_api_key.get_secret_value() else []
    if missing:
        raise ProviderConfigurationError(
            f"reference image provider {name!r} is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def _validate_asset_settings(settings: Settings) -> None:
    if settings.asset_orientation not in ASSET_SUPPORTED_ORIENTATIONS:
        raise ProviderConfigurationError(
            f"unsupported asset orientation {settings.asset_orientation!r} "
            f"(supported: {', '.join(sorted(ASSET_SUPPORTED_ORIENTATIONS))})"
        )
    if settings.asset_connect_timeout_seconds <= 0 or settings.asset_read_timeout_seconds <= 0:
        raise ProviderConfigurationError("asset timeouts must be positive")
    if settings.asset_max_retries < 0:
        raise ProviderConfigurationError("asset retry count must not be negative")
    if settings.asset_per_page < 1:
        raise ProviderConfigurationError("asset per-page count must be at least 1")
    if settings.asset_min_width < 1 or settings.asset_min_height < 1:
        raise ProviderConfigurationError("asset minimum width/height must be positive")
    if settings.asset_min_duration_seconds <= 0:
        raise ProviderConfigurationError("asset minimum duration must be positive")
    if settings.asset_max_duration_seconds < settings.asset_min_duration_seconds:
        raise ProviderConfigurationError(
            "asset maximum duration must be >= asset minimum duration"
        )

    name = normalize_provider_name(settings.asset_provider)
    if name in ("fake", "demo"):
        return
    if name != "pexels":
        raise ProviderConfigurationError(
            f"unknown asset provider {settings.asset_provider!r} (supported: fake, demo, pexels)"
        )
    missing = [
        var for var, value in (
            ("REEL_HARNESS_ASSET_BASE_URL", settings.asset_base_url),
            ("REEL_HARNESS_ASSET_API_KEY", settings.asset_api_key.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "asset provider 'pexels' is selected but credentials are not "
            "configured: missing " + ", ".join(missing)
        )


def _validate_youtube_settings(settings: Settings) -> None:
    if settings.youtube_upload_chunk_size <= 0 or settings.youtube_upload_chunk_size % 262144 != 0:
        raise ProviderConfigurationError(
            "youtube upload chunk size must be a positive multiple of 262144 bytes (256 KiB) -- "
            "see docs/PUBLISHING.md's resumable upload protocol notes"
        )
    if settings.publisher_processing_poll_interval_seconds <= 0:
        raise ProviderConfigurationError("publisher processing poll interval must be positive")
    if settings.publisher_processing_max_duration_seconds <= 0:
        raise ProviderConfigurationError("publisher processing max duration must be positive")


def _validate_tiktok_settings(settings: Settings) -> None:
    if settings.tiktok_upload_chunk_size <= 0:
        raise ProviderConfigurationError("tiktok upload chunk size must be positive")
    if settings.tiktok_connect_timeout_seconds <= 0 or settings.tiktok_read_timeout_seconds <= 0:
        raise ProviderConfigurationError("tiktok timeouts must be positive")
    if settings.tiktok_max_retries < 0:
        raise ProviderConfigurationError("tiktok retry count must not be negative")
    _tiktok_redirect_prefixes = ("https://", "http://127.0.0.1", "http://localhost")
    if settings.tiktok_redirect_uri and not settings.tiktok_redirect_uri.startswith(_tiktok_redirect_prefixes):
        raise ProviderConfigurationError(
            "tiktok redirect_uri must be an https:// URL registered with the TikTok app, or a "
            "loopback http://127.0.0.1/http://localhost address (TikTok's docs require HTTPS "
            "generally; loopback is offered as a convenience -- see docs/PUBLISHING.md)"
        )


def validate_tiktok_credentials_configured(settings: Settings) -> None:
    """Called only when TikTok publishing is actually attempted
    (publisher-auth tiktok, publish-job --provider tiktok, provider-smoke
    publisher tiktok) -- mirrors validate_youtube_credentials_configured."""
    missing = [
        var for var, value in (
            ("REEL_HARNESS_TIKTOK_CLIENT_KEY", settings.tiktok_client_key),
            ("REEL_HARNESS_TIKTOK_CLIENT_SECRET", settings.tiktok_client_secret.get_secret_value()),
            ("REEL_HARNESS_TIKTOK_REDIRECT_URI", settings.tiktok_redirect_uri),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "tiktok publishing requires an OAuth client: missing " + ", ".join(missing)
        )


def _validate_instagram_settings(settings: Settings) -> None:
    if settings.instagram_connect_timeout_seconds <= 0 or settings.instagram_read_timeout_seconds <= 0:
        raise ProviderConfigurationError("instagram timeouts must be positive")
    if settings.instagram_max_retries < 0:
        raise ProviderConfigurationError("instagram retry count must not be negative")
    if settings.instagram_media_url_ttl_seconds <= 0:
        raise ProviderConfigurationError("instagram media url TTL must be positive")
    if settings.instagram_media_url_mode not in ("resumable", "external_url"):
        raise ProviderConfigurationError(
            f"unknown instagram media_url_mode {settings.instagram_media_url_mode!r} "
            "(supported: resumable, external_url)"
        )
    _instagram_redirect_prefixes = ("https://", "http://127.0.0.1", "http://localhost")
    if settings.instagram_redirect_uri and not settings.instagram_redirect_uri.startswith(
        _instagram_redirect_prefixes
    ):
        raise ProviderConfigurationError(
            "instagram redirect_uri must be an https:// URL registered with the Meta app, or a "
            "loopback http://127.0.0.1/http://localhost address -- see docs/PUBLISHING.md"
        )


def validate_instagram_credentials_configured(settings: Settings) -> None:
    """Called only when Instagram publishing is actually attempted
    (publisher-auth instagram, publish-job --provider instagram,
    provider-smoke publisher instagram) -- mirrors
    validate_tiktok_credentials_configured. Also refuses explicitly if
    media_url_mode=external_url is selected -- that path is not
    implemented this phase (see docs/PUBLISHING.md), so it must fail
    loudly before any network call, not silently fall back to resumable."""
    missing = [
        var for var, value in (
            ("REEL_HARNESS_INSTAGRAM_APP_ID", settings.instagram_app_id),
            ("REEL_HARNESS_INSTAGRAM_APP_SECRET", settings.instagram_app_secret.get_secret_value()),
            ("REEL_HARNESS_INSTAGRAM_REDIRECT_URI", settings.instagram_redirect_uri),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "instagram publishing requires an OAuth client: missing " + ", ".join(missing)
        )
    if settings.instagram_media_url_mode == "external_url":
        raise ProviderConfigurationError(
            "REEL_HARNESS_INSTAGRAM_MEDIA_URL_MODE=external_url is not implemented this phase -- "
            "only 'resumable' (direct upload to rupload.facebook.com) is supported; see docs/PUBLISHING.md"
        )


def validate_youtube_credentials_configured(settings: Settings) -> None:
    """Called only when YouTube publishing is actually attempted (publisher-
    auth youtube, publish-job --provider youtube, provider-smoke publisher
    youtube) -- unlike the LLM/TTS/asset providers there is no separate
    'youtube_provider=fake' toggle, since publishing is always opt-in per
    command rather than a pipeline-wide default."""
    missing = [
        var for var, value in (
            ("REEL_HARNESS_YOUTUBE_CLIENT_ID", settings.youtube_client_id),
            ("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", settings.youtube_client_secret.get_secret_value()),
        ) if not value
    ]
    if missing:
        raise ProviderConfigurationError(
            "youtube publishing requires an OAuth client: missing " + ", ".join(missing)
        )


_SUPPORTED_DATABASE_BACKENDS = frozenset({"sqlite", "postgresql"})


def _validate_database_url(settings: Settings) -> None:
    from sqlalchemy.engine import make_url
    from sqlalchemy.exc import ArgumentError

    try:
        backend = make_url(settings.database_url).get_backend_name()
    except ArgumentError as exc:
        raise ProviderConfigurationError(
            f"DATABASE_URL is not a valid database URL: {settings.database_url!r} ({exc})"
        ) from exc
    if backend not in _SUPPORTED_DATABASE_BACKENDS:
        raise ProviderConfigurationError(
            f"unsupported database backend {backend!r} (from DATABASE_URL) -- reel-harness supports: "
            f"{', '.join(sorted(_SUPPORTED_DATABASE_BACKENDS))}"
        )


def validate_provider_settings(settings: Settings) -> None:
    """Startup gate: selecting a real provider with incomplete or invalid
    configuration fails immediately with a clear message (no network is
    touched). The fake providers never require anything."""
    _validate_database_url(settings)
    _validate_llm_settings(settings)
    _validate_tts_settings(settings)
    _validate_cinematic_settings(settings)
    _validate_narrative_settings(settings)
    _validate_reference_image_settings(settings)
    _validate_takes_per_shot(settings)
    _validate_edit_plan(settings)
    _validate_asset_settings(settings)
    _validate_youtube_settings(settings)
    _validate_tiktok_settings(settings)
    _validate_instagram_settings(settings)


def load_settings() -> Settings:
    # mypy's pydantic integration treats aliased fields as required constructor
    # arguments even though every one has a default and populate_by_name is on.
    return Settings()  # type: ignore[call-arg]
