from __future__ import annotations

from reel_harness.config import Settings, load_settings
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
        configure_logging(self.settings.log_level)
        register_secret(self.settings.app_api_key)
        register_secret(self.settings.llm_api_key)
        self.engine = create_engine_from_url(self.settings.database_url)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.storage = LocalFilesystemStorage(self.settings.jobs_dir)
        self.jobs = JobService(self.session_factory, storage=self.storage)

    def providers_for_job(self, job) -> ProviderBundle:
        """Providers to use for one leased job. (Snapshot-aware resolution is
        introduced with the provider-configuration work; the default bundle is
        the fallback.)"""
        return self.default_providers()

    def default_providers(self) -> ProviderBundle:
        from reel_harness.providers.registry import (
            resolve_llm_provider,
            resolve_stock_media_provider,
            resolve_tts_provider,
        )

        return ProviderBundle(
            llm=resolve_llm_provider(self.settings.llm_provider, self.settings),
            tts=resolve_tts_provider("fake"),
            stock_media=resolve_stock_media_provider("fake"),
        )
