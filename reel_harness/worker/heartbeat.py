from __future__ import annotations

import logging
import threading

from reel_harness.worker.lease import heartbeat_lease

_logger = logging.getLogger("reel_harness")


class LeaseHeartbeat:
    """Background heartbeat for one leased job.

    Runs on its own thread with its own short-lived sessions (never the worker's
    main session -- SQLAlchemy sessions are not thread-safe), refreshing
    heartbeat_at every `interval_seconds` while a long stage (ffmpeg render,
    provider network call) is executing. The interval must be well below the
    lease timeout -- Settings defaults keep it at <= 1/3.

    Failure policy: a token mismatch means the lease was reclaimed -- the
    `lease_lost` event is set and the thread exits (the worker's fenced commits
    will refuse to write anyway). DB errors are never silently swallowed: they
    are counted, kept in `last_error`, and logged through the redacting logger;
    the thread keeps trying, because a transiently locked SQLite file must not
    kill a healthy render while the fenced commit path still protects
    correctness.
    """

    def __init__(self, session_factory, job_id: str, lease_token: str, interval_seconds: float) -> None:
        self._session_factory = session_factory
        self._job_id = job_id
        self._lease_token = lease_token
        self._interval = interval_seconds
        self._stop = threading.Event()
        self.lease_lost = threading.Event()
        self.beat_count = 0
        self.error_count = 0
        self.last_error: Exception | None = None
        self._thread = threading.Thread(
            target=self._run, name=f"lease-heartbeat-{job_id[:8]}", daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self, join_timeout: float = 5.0) -> None:
        """Idempotent; always called on job completion, failure, and cancel so
        no heartbeat thread outlives its job."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join(timeout=join_timeout)

    @property
    def is_running(self) -> bool:
        return self._thread.is_alive()

    def __enter__(self) -> LeaseHeartbeat:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    def _beat(self) -> bool:
        """One heartbeat write; returns whether this worker still owns the
        lease. The single subclass hook: worker.fable_daemon overrides it
        to target fable_shots instead of jobs while reusing the thread
        lifecycle unchanged."""
        with self._session_factory() as session:
            return heartbeat_lease(session, self._job_id, self._lease_token)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                still_owner = self._beat()
                if not still_owner:
                    self.lease_lost.set()
                    _logger.warning(
                        '{"event": "heartbeat_lease_lost", "job_id": "%s"}', self._job_id,
                    )
                    return
                self.beat_count += 1
            except Exception as exc:  # noqa: BLE001 - counted and surfaced, never silent
                self.error_count += 1
                self.last_error = exc
                _logger.warning(
                    '{"event": "heartbeat_error", "job_id": "%s", "error": "%s"}',
                    self._job_id, type(exc).__name__,
                )
