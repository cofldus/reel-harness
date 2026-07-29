from __future__ import annotations

from reel_harness.config import Settings, load_settings, validate_provider_settings
from reel_harness.core.publish_service import PublicationService
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
        register_secret(self.settings.youtube_client_secret.get_secret_value())
        register_secret(self.settings.tiktok_client_secret.get_secret_value())
        register_secret(self.settings.instagram_app_secret.get_secret_value())
        self._log_startup_fingerprint()
        self.engine = create_engine_from_url(self.settings.database_url)
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.storage = LocalFilesystemStorage(self.settings.jobs_dir)
        from reel_harness.providers.registry import provider_snapshot

        self.jobs = JobService(
            self.session_factory, storage=self.storage,
            provider_snapshot=provider_snapshot(self.settings),
        )
        self.publications = PublicationService(self.session_factory, self.storage)
        self._secret_store = None

    def config_fingerprint(self) -> dict:
        """Safe, non-secret snapshot of how this process is configured --
        see ops.fingerprint.config_fingerprint for what is and is not
        included. Memoized would be wrong here: settings.jobs_dir/etc.
        never change after construction, but callers (e.g. a long-running
        `serve` process) should always see the fingerprint as of right
        now, not a stale copy from process start."""
        from reel_harness.ops.fingerprint import config_fingerprint

        return config_fingerprint(self.settings)

    def _log_startup_fingerprint(self) -> None:
        import json
        import logging

        from reel_harness.ops.fingerprint import config_fingerprint, fingerprint_hash

        fingerprint = config_fingerprint(self.settings)
        logging.getLogger("reel_harness").info(json.dumps({
            "event": "startup_config_fingerprint",
            "config_fingerprint_hash": fingerprint_hash(fingerprint),
            **fingerprint,
        }))

    def _get_secret_store(self):
        """Lazily constructed and memoized: the secret directory is validated
        (rejects a repo-internal path) and created only the first time
        something actually needs it, not on every AppContext startup."""
        if self._secret_store is None:
            from reel_harness.publisher.secret_store import FileSecretStore

            self._secret_store = FileSecretStore(self.settings.credential_dir)
        return self._secret_store

    def credential_backend(self):
        from reel_harness.publisher.credentials import FileCredentialBackend

        return FileCredentialBackend(self._get_secret_store())

    def publish_journal(self):
        """Durable, append-only, fsync'd crash-recovery journal -- rooted
        alongside OAuth credentials/upload-session references (same
        repository-external directory, a distinct subdirectory) so it gets
        the same repo-internal-path rejection for free. See
        publisher.journal.PublishJournal and core.publish_reconciliation."""
        from reel_harness.publisher.journal import PublishJournal

        return PublishJournal(self._get_secret_store().root_dir / "publish_journal")

    def bundle_for_publication(self, publication):
        """The publisher + session store for one leased publication, honoring
        the publisher snapshot the Publication was created with -- mirrors
        providers_for_job's pinning discipline. An unsatisfiable snapshot
        yields a publisher that fails the stage with
        PROVIDER_NOT_CONFIGURED, never a silent switch to a different
        account."""
        from reel_harness.providers.registry import resolve_publisher
        from reel_harness.publisher.session_store import UploadSessionStore
        from reel_harness.worker.publish_runner import PublishBundle

        snapshot = getattr(publication, "publisher_config", None) or {}
        provider_name = snapshot.get("publisher_provider", "fake")
        account_reference = snapshot.get("publisher_account_reference", "default")
        publisher = resolve_publisher(
            provider_name, settings=self.settings,
            credential_backend=self.credential_backend(), account_reference=account_reference,
        )
        return PublishBundle(
            publisher=publisher, session_store=UploadSessionStore(self._get_secret_store()),
            journal=self.publish_journal(),
        )

    def channel_niche_for_job(self, job) -> str | None:
        if job is None:
            return None
        from reel_harness.db.models import Channel

        with self.session_factory() as session:
            channel = session.get(Channel, job.channel_id)
            return channel.niche if channel is not None else None

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
