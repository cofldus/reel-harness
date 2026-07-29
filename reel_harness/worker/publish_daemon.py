from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from reel_harness.core.state_machine import PublicationStatus
from reel_harness.db.models import Job, Publication
from reel_harness.observability import log_worker_event
from reel_harness.worker.publish_lease import (
    lease_next_via_lanes,
    recover_stale_publications,
    release_publication_lease,
)
from reel_harness.worker.publish_runner import PublishBundle, run_publication

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_INTERRUPTED = 130


def default_publisher_worker_id() -> str:
    return f"publisher-worker-{uuid.uuid4().hex[:12]}"


@dataclass
class PublisherDaemonConfig:
    worker_id: str
    poll_interval_seconds: float
    lease_timeout_seconds: int
    max_publications: int | None = None
    idle_exit_after_seconds: float | None = None
    stop_on_error: bool = False
    # Role split (Phase 3B): if both are True (the default), this daemon
    # handles uploads AND processing polls, alternating fairly between the
    # two lanes each cycle so a backlog in one can never starve the other.
    # Run two separate daemon processes with one flag each to split the
    # roles onto distinct processes instead.
    process_upload: bool = True
    process_status: bool = True


class PublisherDaemon:
    """Continuous polling worker for Publications -- deliberately separate
    from WorkerDaemon (the render pipeline): a render worker never performs
    an upload, and this worker never drives the render pipeline (see
    docs/PUBLISHING.md). Same recover -> lease -> run -> release loop shape
    as WorkerDaemon, with the same error-isolation and graceful-shutdown
    guarantees; see that class's docstring for the full rationale, which
    applies here unchanged.
    """

    def __init__(
        self,
        session_factory,
        storage,
        bundle_for_publication: Callable[[object], PublishBundle],
        channel_niche_for_job: Callable[[object], str | None],
        config: PublisherDaemonConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import threading

        self._session_factory = session_factory
        self._storage = storage
        self._bundle_for_publication = bundle_for_publication
        self._channel_niche_for_job = channel_niche_for_job
        self._config = config
        self._clock = clock
        self._stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.publications_processed = 0
        self.fatal_error: str | None = None
        self._prefer_status_lane = False  # alternated each cycle for fairness

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        log_worker_event(
            event="publisher_worker_shutdown_requested", worker_id=self._config.worker_id, reason=reason,
        )
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> int:
        cfg = self._config
        log_worker_event(
            event="publisher_worker_started", worker_id=cfg.worker_id,
            poll_interval_seconds=cfg.poll_interval_seconds, lease_timeout_seconds=cfg.lease_timeout_seconds,
            max_publications=cfg.max_publications, idle_exit_after_seconds=cfg.idle_exit_after_seconds,
        )
        idle_since: float | None = None
        was_idle = False
        exit_code = EXIT_OK
        try:
            while not self._stop_event.is_set():
                try:
                    outcome = self._poll_once()
                except (SQLAlchemyError, OSError) as exc:
                    self.fatal_error = f"{type(exc).__name__}: {exc}"
                    log_worker_event(
                        event="publisher_worker_fatal_error", worker_id=cfg.worker_id, error=type(exc).__name__,
                    )
                    self.stop_reason = self.stop_reason or "fatal_error"
                    exit_code = EXIT_FATAL
                    break

                if outcome is None:
                    now = self._clock()
                    if idle_since is None:
                        idle_since = now
                    if not was_idle:
                        log_worker_event(event="publisher_worker_idle", worker_id=cfg.worker_id)
                        was_idle = True
                    if (
                        cfg.idle_exit_after_seconds is not None
                        and now - idle_since >= cfg.idle_exit_after_seconds
                    ):
                        self.stop_reason = self.stop_reason or "idle_exit"
                        break
                    self._stop_event.wait(cfg.poll_interval_seconds)
                    continue

                idle_since = None
                was_idle = False
                self.publications_processed += 1
                if outcome == PublicationStatus.FAILED.value and cfg.stop_on_error:
                    self.stop_reason = self.stop_reason or "stop_on_error"
                    exit_code = EXIT_FATAL
                    break
                if cfg.max_publications is not None and self.publications_processed >= cfg.max_publications:
                    self.stop_reason = self.stop_reason or "max_publications"
                    break
        except KeyboardInterrupt:
            self.stop_reason = self.stop_reason or "keyboard_interrupt"
            exit_code = EXIT_INTERRUPTED
        finally:
            if self.stop_reason is None:
                self.stop_reason = "stop_requested"
            log_worker_event(
                event="publisher_worker_stopped", worker_id=cfg.worker_id,
                reason=self.stop_reason, publications_processed=self.publications_processed,
            )
        return exit_code

    def _lease_one(self, session) -> Publication | None:
        """Tries whichever lane(s) this daemon is configured for. When both
        are enabled, alternates which lane is tried first each cycle so a
        deep backlog in one lane can never starve the other."""
        cfg = self._config
        prefer_status = self._prefer_status_lane
        self._prefer_status_lane = not self._prefer_status_lane
        return lease_next_via_lanes(
            session, worker_id=cfg.worker_id, process_upload=cfg.process_upload,
            process_status=cfg.process_status, prefer_status=prefer_status,
        )

    def _poll_once(self) -> str | None:
        """One recover+lease+run cycle. Returns the finished publication's
        status, or None if nothing was leasable."""
        cfg = self._config
        with self._session_factory() as session:
            recovered = recover_stale_publications(session, lease_timeout_seconds=cfg.lease_timeout_seconds)
            if recovered:
                log_worker_event(
                    event="stale_publications_recovered", worker_id=cfg.worker_id, count=len(recovered),
                )
            publication = self._lease_one(session)
            if publication is None:
                return None

            lease_token = publication.lease_token
            assert lease_token is not None  # minted by lease_next_publication
            job = session.get(Job, publication.job_id)
            channel_niche = self._channel_niche_for_job(job)
            log_worker_event(
                event="publication_leased", worker_id=cfg.worker_id, publication_id=publication.id,
                lease=lease_token[:8], status=publication.status,
            )
            bundle = self._bundle_for_publication(publication)
            started = self._clock()
            try:
                run_publication(
                    session, publication, self._storage, bundle,
                    channel_niche=channel_niche, lease_token=lease_token,
                )
            finally:
                release_publication_lease(session, publication, lease_token=lease_token)
                close = getattr(bundle.publisher, "close", None)
                if callable(close):
                    close()

            duration_ms = (self._clock() - started) * 1000
            outcome_event = (
                "publication_failed" if publication.status == PublicationStatus.FAILED.value
                else "publication_worker_cycle_completed"
            )
            log_worker_event(
                event=outcome_event, worker_id=cfg.worker_id, publication_id=publication.id,
                status=publication.status, failure_code=publication.failure_code,
                duration_ms=round(duration_ms, 3),
            )
            return publication.status
