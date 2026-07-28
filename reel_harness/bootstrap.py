from __future__ import annotations

from reel_harness.config import Settings, load_settings, validate_provider_settings
from reel_harness.core.service import JobService
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.observability import configure_logging, register_secret
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.runner import ProviderBundle


class AppContext:
    """Wires config -> engine/session factory -> storage -> default (fake)
    providers -> JobService. One place both the CLI and the API import from so
    they can't drift into different wiring."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        # Fail loudly at startup if a real provider is selected without its
        # required configuration (raises ProviderConfigurationError).
        validate_provider_settings(self.settings)
        configure_logging(self.settings.log_level)
        register_secret(self.settings.app_api_key)
        register_secret(self.settings.llm_api_key.get_secret_value())
        register_secret(self.settings.tts_api_key.get_secret_value())
        register_secret(self.settings.asset_api_key.get_secret_value())
        self.engine = create_engine_from_url(self.settings.database_url)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.storage = LocalFilesystemStorage(self.settings.jobs_dir)
        from reel_harness.providers.registry import provider_snapshot

        self.jobs = JobService(
            self.session_factory, storage=self.storage,
            provider_snapshot=provider_snapshot(self.settings),
        )

    def providers_for_job(self, job) -> ProviderBundle:
        """Providers for one leased job, honoring the provider snapshot the job
        was created with: retries/resumes never silently switch provider or
        model because the environment changed. An unsatisfiable snapshot yields
        a provider that fails the stage with PROVIDER_NOT_CONFIGURED."""
        from reel_harness.providers.registry import (
            resolve_llm_for_snapshot,
            resolve_stock_media_for_snapshot,
            resolve_tts_for_snapshot,
        )

        snapshot = getattr(job, "provider_config", None)
        return ProviderBundle(
            llm=resolve_llm_for_snapshot(snapshot, self.settings),
            tts=resolve_tts_for_snapshot(snapshot, self.settings),
            stock_media=resolve_stock_media_for_snapshot(snapshot, self.settings),
        )

    def default_providers(self) -> ProviderBundle:
        from reel_harness.providers.registry import (
            resolve_llm_provider,
            resolve_stock_media_provider,
            resolve_tts_provider,
        )

        return ProviderBundle(
            llm=resolve_llm_provider(self.settings.llm_provider, self.settings),
            tts=resolve_tts_provider(self.settings.tts_provider, self.settings),
            stock_media=resolve_stock_media_provider(self.settings.asset_provider, self.settings),
        )
