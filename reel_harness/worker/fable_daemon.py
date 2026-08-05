"""Continuous polling daemon for Fable shot generation -- same
recover -> lease -> run -> release loop shape as worker.daemon
(WorkerDaemon) and worker.publish_daemon (PublisherDaemon); see
WorkerDaemon's docstring for the full shutdown/error-isolation
rationale, which applies here unchanged. After each shot finishes, the
daemon checks whether its project's generation phase is complete and
advances GENERATING -> TAKE_REVIEW."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.exc import SQLAlchemyError

from reel_harness.core.cinematic_state import FableShotStatus
from reel_harness.db.cinematic_models import FableScene, StoryProject
from reel_harness.observability import log_worker_event
from reel_harness.providers.base import CinematicVideoProvider
from reel_harness.worker.daemon import EXIT_FATAL, EXIT_INTERRUPTED, EXIT_OK
from reel_harness.worker.fable_lease import (
    lease_next_shot,
    maybe_advance_project_after_generation,
    recover_stale_shots,
    release_shot_lease,
)
from reel_harness.worker.fable_runner import (
    DEFAULT_GENERATION_TIMEOUT_SEC,
    DEFAULT_POLL_INTERVAL_SEC,
    run_shot,
    takes_per_shot_for,
)
from reel_harness.worker.heartbeat import LeaseHeartbeat


@dataclass
class FableDaemonConfig:
    worker_id: str
    poll_interval_seconds: float
    lease_timeout_seconds: int
    heartbeat_interval_seconds: float
    max_shots: int | None = None
    idle_exit_after_seconds: float | None = None
    stop_on_error: bool = False
    # Settings.allow_paid_generation, carried down to run_shot's cost
    # gate. Defaults to False so a daemon constructed without an opinion
    # can only ever run the free tiers -- the safe default for a switch
    # about money.
    allow_paid_generation: bool = False
    # Settings.fable_dialogue_source == "video". False keeps speech out
    # of the generated clip so synthesised dialogue can be mixed over it
    # without two voices saying the same line.
    spoken_by_video: bool = True
    # Settings.fable_takes_per_shot. A project may override it for itself;
    # this is the operator-wide default the override falls back to.
    takes_per_shot: int = 1
    # Settings.fable_generation_timeout_seconds / _poll_interval_seconds.
    # Defaults match the runner's, so a daemon built without an opinion
    # still waits long enough for a real generation.
    # Named generation_* to keep it distinct from poll_interval_seconds
    # above, which is how often this daemon looks for WORK -- a different
    # question from how often it asks about one running generation.
    generation_timeout_seconds: float = DEFAULT_GENERATION_TIMEOUT_SEC
    generation_poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SEC


class FableDaemon:
    def __init__(
        self,
        session_factory,
        storage,
        provider_for_shot: Callable[[object], CinematicVideoProvider],
        config: FableDaemonConfig,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        import threading

        self._session_factory = session_factory
        self._storage = storage
        self._provider_for_shot = provider_for_shot
        self._config = config
        self._clock = clock
        self._stop_event = threading.Event()
        self.stop_reason: str | None = None
        self.shots_processed = 0
        self.fatal_error: str | None = None

    def request_stop(self, reason: str) -> None:
        if self.stop_reason is None:
            self.stop_reason = reason
        log_worker_event(
            event="fable_worker_shutdown_requested", worker_id=self._config.worker_id, reason=reason,
        )
        self._stop_event.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def run(self) -> int:
        cfg = self._config
        log_worker_event(
            event="fable_worker_started", worker_id=cfg.worker_id,
            poll_interval_seconds=cfg.poll_interval_seconds,
            lease_timeout_seconds=cfg.lease_timeout_seconds, max_shots=cfg.max_shots,
        )
        idle_since: float | None = None
        was_idle = False
        exit_code = EXIT_OK
        try:
            while not self._stop_event.is_set():
                try:
                    shot_status = self._poll_once()
                except (SQLAlchemyError, OSError) as exc:
                    self.fatal_error = f"{type(exc).__name__}: {exc}"
                    log_worker_event(
                        event="fable_worker_fatal_error", worker_id=cfg.worker_id,
                        error=type(exc).__name__,
                    )
                    self.stop_reason = self.stop_reason or "fatal_error"
                    exit_code = EXIT_FATAL
                    break

                if shot_status is None:
                    now = self._clock()
                    if idle_since is None:
                        idle_since = now
                    if not was_idle:
                        log_worker_event(event="fable_worker_idle", worker_id=cfg.worker_id)
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
                self.shots_processed += 1
                if shot_status == FableShotStatus.FAILED.value and cfg.stop_on_error:
                    self.stop_reason = self.stop_reason or "stop_on_error"
                    exit_code = EXIT_FATAL
                    break
                if cfg.max_shots is not None and self.shots_processed >= cfg.max_shots:
                    self.stop_reason = self.stop_reason or "max_shots"
                    break
        except KeyboardInterrupt:
            self.stop_reason = self.stop_reason or "keyboard_interrupt"
            exit_code = EXIT_INTERRUPTED
        finally:
            if self.stop_reason is None:
                self.stop_reason = "stop_requested"
            log_worker_event(
                event="fable_worker_stopped", worker_id=cfg.worker_id,
                reason=self.stop_reason, shots_processed=self.shots_processed,
            )
        return exit_code

    def _poll_once(self) -> str | None:
        cfg = self._config
        with self._session_factory() as session:
            recovered = recover_stale_shots(session, lease_timeout_seconds=cfg.lease_timeout_seconds)
            if recovered:
                log_worker_event(
                    event="stale_fable_shots_recovered", worker_id=cfg.worker_id, count=len(recovered),
                )
            shot = lease_next_shot(session, worker_id=cfg.worker_id)
            if shot is None:
                return None

            lease_token = shot.lease_token
            assert lease_token is not None  # minted by lease_next_shot
            log_worker_event(
                event="fable_shot_leased", worker_id=cfg.worker_id, job_id=shot.id,
                lease=lease_token[:8], status=shot.status,
            )
            provider = self._provider_for_shot(shot)
            heartbeat = _ShotHeartbeat(self._session_factory, shot.id, lease_token,
                                       cfg.heartbeat_interval_seconds)
            heartbeat.start()
            started = self._clock()
            scene = session.get(FableScene, shot.scene_id)
            project_id = scene.project_id if scene is not None else None
            project = session.get(StoryProject, project_id) if project_id else None
            try:
                run_shot(
                    session, shot, provider, self._storage, lease_token=lease_token,
                    allow_paid_generation=cfg.allow_paid_generation,
                    takes_per_shot=takes_per_shot_for(project, cfg.takes_per_shot),
                    spoken_by_video=cfg.spoken_by_video,
                    generation_timeout_sec=cfg.generation_timeout_seconds,
                    poll_interval_sec=cfg.generation_poll_interval_seconds,
                )
            finally:
                heartbeat.stop()
                release_shot_lease(session, shot, lease_token=lease_token)

            if project_id is not None:
                maybe_advance_project_after_generation(session, project_id)

            duration_ms = (self._clock() - started) * 1000
            outcome = (
                "fable_shot_failed" if shot.status == FableShotStatus.FAILED.value
                else "fable_shot_finished"
            )
            log_worker_event(
                event=outcome, worker_id=cfg.worker_id, job_id=shot.id,
                status=shot.status, failure_code=shot.failure_code,
                duration_ms=round(duration_ms, 3),
            )
            return shot.status


class _ShotHeartbeat(LeaseHeartbeat):
    """LeaseHeartbeat beats via worker.lease.heartbeat_lease, which targets
    the jobs table -- override the single beat operation to target
    fable_shots instead. Kept as a subclass so the thread lifecycle
    (start/stop/join, interval pacing, beat counting) stays one
    implementation."""

    def _beat(self) -> bool:
        from datetime import UTC, datetime

        from sqlalchemy import update

        from reel_harness.db.cinematic_models import FableShot

        with self._session_factory() as session:
            result = session.execute(
                update(FableShot)
                .where(FableShot.id == self._job_id, FableShot.lease_token == self._lease_token)
                .values(heartbeat_at=datetime.now(UTC)),
            )
            session.commit()
            return result.rowcount == 1
