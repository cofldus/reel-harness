from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Channel
from reel_harness.observability import log_worker_event
from reel_harness.worker.heartbeat import LeaseHeartbeat
from reel_harness.worker.lease import lease_next_job, recover_stale_jobs, release_lease
from reel_harness.worker.runner import ProviderBundle, run_job

# Daemon exit codes.
EXIT_OK = 0  # graceful: max-jobs reached, idle-exit, or shutdown request honored
EXIT_FATAL = 1  # infrastructure failure (DB/storage unusable) or --stop-on-error
EXIT_INTERRUPTED = 130  # hard interrupt mid-job (KeyboardInterrupt)


def default_worker_id() -> str:
    return f"worker-{uuid.uuid4().hex[:12]}"


@dataclass
class DaemonConfig:
    worker_id: str
    poll_interval_seconds: float
    lease_timeout_seconds: int
    heartbeat_interval_seconds: float
    max_jobs: int | None = None
    idle_exit_after_seconds: float | None = None
    stop_on_error: bool = False


class WorkerDaemon:
    """Continuous polling worker: recover stale leases, lease one ready job,
    run it (fenced, with a live heartbeat), release, repeat; sleep
    `poll_interval_seconds` when nothing is leasable (the sleep is a stop-event
    wait, so shutdown requests interrupt it immediately -- no busy loop).

    Error isolation: job-level failures are absorbed by run_job itself and
    only affect that job; the daemon moves on (unless `stop_on_error`).
    Infrastructure failures in the polling loop itself (DB gone, schema
    incompatible, storage root unusable) are fatal: logged and EXIT_FATAL.

    Shutdown: `request_stop()` (wired to Ctrl+C/SIGINT/SIGTERM/SIGBREAK by the
    CLI, and callable from tests) stops new leasing; the in-flight job runs to
    its next safe boundary because run_job commits stage-by-stage and a
    genuinely hard kill is already covered by stale-lease recovery. The
    heartbeat thread is always stopped and joined; the lease is always
    released (token-guarded) before exit.
    """

    def __init__(
        self,
        session_factory,
        storage,
        providers_for_job: Callable[[object], ProviderBundle],
        config: DaemonConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import threading

        self._session_factory = session_factory
        self._storage = storage
        self._providers_for_job = providers_for_job
        self._config = config
        self._clock = clock
        self._stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.jobs_processed = 0
        self.fatal_error: str | None = None

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        log_worker_event(
            event="worker_shutdown_requested", worker_id=self._config.worker_id, reason=reason,
        )
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> int:
        cfg = self._config
        log_worker_event(
            event="worker_started", worker_id=cfg.worker_id,
            poll_interval_seconds=cfg.poll_interval_seconds,
            lease_timeout_seconds=cfg.lease_timeout_seconds,
            max_jobs=cfg.max_jobs, idle_exit_after_seconds=cfg.idle_exit_after_seconds,
        )
        idle_since: float | None = None
        was_idle = False
        exit_code = EXIT_OK
        try:
            while not self._stop_event.is_set():
                try:
                    job_status = self._poll_once(was_idle)
                except (SQLAlchemyError, OSError) as exc:
                    # Infrastructure failure: the loop itself cannot make
                    # progress (job-level errors never surface here).
                    self.fatal_error = f"{type(exc).__name__}: {exc}"
                    log_worker_event(
                        event="worker_fatal_error", worker_id=cfg.worker_id,
                        error=type(exc).__name__,
                    )
                    self.stop_reason = self.stop_reason or "fatal_error"
                    exit_code = EXIT_FATAL
                    break

                if job_status is None:
                    now = self._clock()
                    if idle_since is None:
                        idle_since = now
                    if not was_idle:
                        log_worker_event(event="worker_idle", worker_id=cfg.worker_id)
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
                self.jobs_processed += 1
                if job_status == JobStatus.FAILED.value and cfg.stop_on_error:
                    self.stop_reason = self.stop_reason or "stop_on_error"
                    exit_code = EXIT_FATAL
                    break
                if cfg.max_jobs is not None and self.jobs_processed >= cfg.max_jobs:
                    self.stop_reason = self.stop_reason or "max_jobs"
                    break
        except KeyboardInterrupt:
            self.stop_reason = self.stop_reason or "keyboard_interrupt"
            exit_code = EXIT_INTERRUPTED
        finally:
            if self.stop_reason is None:
                self.stop_reason = "stop_requested"
            log_worker_event(
                event="worker_stopped", worker_id=cfg.worker_id,
                reason=self.stop_reason, jobs_processed=self.jobs_processed,
            )
        return exit_code

    def _poll_once(self, was_idle: bool) -> str | None:
        """One recover+lease+run cycle. Returns the finished job's status, or
        None if nothing was leasable."""
        cfg = self._config
        with self._session_factory() as session:
            recovered = recover_stale_jobs(session, lease_timeout_seconds=cfg.lease_timeout_seconds)
            if recovered:
                log_worker_event(
                    event="stale_jobs_recovered", worker_id=cfg.worker_id, count=len(recovered),
                )
            job = lease_next_job(session, worker_id=cfg.worker_id)
            if job is None:
                return None

            lease_token = job.lease_token
            assert lease_token is not None  # minted by lease_next_job
            channel = session.get(Channel, job.channel_id)
            log_worker_event(
                event="job_leased", worker_id=cfg.worker_id, job_id=job.id,
                lease=lease_token[:8], status=job.status,
            )
            providers = self._providers_for_job(job)
            heartbeat = LeaseHeartbeat(
                self._session_factory, job.id, lease_token, cfg.heartbeat_interval_seconds,
            )
            heartbeat.start()
            started = self._clock()
            try:
                run_job(session, job, channel, providers, self._storage, lease_token=lease_token)
            finally:
                heartbeat.stop()
                release_lease(session, job, lease_token=lease_token)
                close = getattr(providers.llm, "close", None)
                if callable(close):
                    close()

            duration_ms = (self._clock() - started) * 1000
            outcome_event = "job_failed" if job.status == JobStatus.FAILED.value else "job_completed"
            log_worker_event(
                event=outcome_event, worker_id=cfg.worker_id, job_id=job.id,
                status=job.status, current_stage=job.current_stage,
                failure_code=job.failure_code, duration_ms=round(duration_ms, 3),
            )
            return job.status
