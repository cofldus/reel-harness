from __future__ import annotations

from reel_harness.config import (
    Settings,
    edit_plan_from_settings,
    load_settings,
    validate_provider_settings,
)
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
        self.engine = create_engine_from_url(
            self.settings.database_url, pool_size=self.settings.db_pool_size,
            max_overflow=self.settings.db_pool_max_overflow,
            statement_timeout_seconds=self.settings.db_statement_timeout_seconds,
        )
        init_db(self.engine)
        self.session_factory = make_session_factory(self.engine)
        self.storage = LocalFilesystemStorage(self.settings.jobs_dir)
        # Fable cinematic projects: a SECOND storage instance with its own
        # root -- same backend class, same UUID/path-traversal enforcement,
        # different directory tree (fable_projects/{project_id}/...).
        self.fable_storage = LocalFilesystemStorage(self.settings.fable_projects_dir)
        from reel_harness.providers.registry import provider_snapshot

        self.jobs = JobService(
            self.session_factory, storage=self.storage,
            provider_snapshot=provider_snapshot(self.settings),
        )
        self.publications = PublicationService(self.session_factory, self.storage)
        from reel_harness.core.fable_service import FableService
        from reel_harness.providers.registry import cinematic_provider_snapshot

        self.fable = FableService(
            self.session_factory, storage=self.fable_storage,
            provider_snapshot=cinematic_provider_snapshot(self.settings),
            narrative_director_resolver=self.narrative_director_for_project,
            cinematic_provider_resolver=self.cinematic_provider_for_project,
            allow_paid_generation=self.settings.allow_paid_generation,
            reference_provider_resolver=self.reference_provider_for_project,
            takes_per_shot=self.settings.fable_takes_per_shot,
            max_characters=self.settings.fable_max_characters,
            dialogue_source=self.settings.fable_dialogue_source,
            tts_resolver=self.speech_provider_for_dialogue,
            edit_plan=edit_plan_from_settings(self.settings),
            render_timeout_seconds=self.settings.fable_render_timeout_seconds,
        )
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

    def oauth_flow_store(self):
        """Transient, single-use state bridging a browser OAuth connect
        request and its callback -- see publisher.oauth_flow_store for why
        this is a separate namespace from credentials, never a DB table."""
        from reel_harness.publisher.oauth_flow_store import OAuthFlowStore

        return OAuthFlowStore(self._get_secret_store())

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

    def speech_provider_for_dialogue(self):
        """The TTS provider that speaks a film's dialogue.

        Resolved lazily, per render, from current settings rather than
        pinned at construction: a project's spoken lines are synthesised
        at assembly time, long after creation, and there is no reason to
        hold a client open for the whole life of the process.
        """
        from reel_harness.providers.registry import resolve_tts_provider

        return resolve_tts_provider(self.settings.tts_provider, self.settings)

    def narrative_director_for_project(self, project):
        """The Narrative Director for one project, honoring its
        creation-time snapshot -- an unsatisfiable snapshot yields a
        director that fails with PROVIDER_NOT_CONFIGURED, never a silent
        switch to a different model or endpoint."""
        from reel_harness.providers.registry import resolve_narrative_director_for_snapshot

        snapshot = getattr(project, "provider_config", None) if project is not None else None
        return resolve_narrative_director_for_snapshot(snapshot, self.settings)

    def cinematic_provider_for_shot(self, shot):
        """The cinematic provider for one leased Fable shot, honoring the
        project's creation-time snapshot -- mirrors providers_for_job's
        pinning discipline. An unsatisfiable snapshot yields a provider
        that fails the shot with PROVIDER_NOT_CONFIGURED, never a silent
        switch."""
        from reel_harness.db.cinematic_models import FableScene, StoryProject
        from reel_harness.providers.registry import resolve_cinematic_video_for_snapshot

        snapshot = None
        with self.session_factory() as session:
            scene = session.get(FableScene, shot.scene_id)
            if scene is not None:
                project = session.get(StoryProject, scene.project_id)
                if project is not None:
                    snapshot = project.provider_config
        return resolve_cinematic_video_for_snapshot(snapshot, self.settings)

    def cinematic_provider_for_project(self, project):
        """Same snapshot-pinned resolution as cinematic_provider_for_shot,
        entered from the project rather than a shot -- used for pricing
        (cost estimate, approval gate), where there is no leased shot yet.
        Both go through resolve_cinematic_video_for_snapshot, so a project
        is never priced with a provider its shots would not run on."""
        from reel_harness.providers.registry import resolve_cinematic_video_for_snapshot

        snapshot = getattr(project, "provider_config", None) if project is not None else None
        return resolve_cinematic_video_for_snapshot(snapshot, self.settings)

    def reference_provider_for_project(self, project):
        """The reference-image provider a project's casting must use,
        honoring its creation-time snapshot -- same fail-loud ladder as
        every other family, no silent fallback."""
        from reel_harness.providers.registry import resolve_reference_image_for_snapshot

        snapshot = getattr(project, "provider_config", None) if project is not None else None
        return resolve_reference_image_for_snapshot(snapshot, self.settings)

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
            render_burn_subtitles=self.settings.render_burn_subtitles,
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
            render_burn_subtitles=self.settings.render_burn_subtitles,
        )
