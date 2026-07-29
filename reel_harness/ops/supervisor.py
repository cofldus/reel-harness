from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from reel_harness.worker.daemon import DaemonConfig, WorkerDaemon, default_worker_id
from reel_harness.worker.publish_daemon import (
    PublisherDaemon,
    PublisherDaemonConfig,
    default_publisher_worker_id,
)

_logger = logging.getLogger("reel_harness")

# SQLite is a single-writer database; running an unbounded number of worker
# threads against it in one process just means most of them spend their
# time waiting on the write lock instead of doing useful work. This is a
# soft guidance cap (WARN via config_fingerprint-style startup logging),
# never enforced -- an operator who has measured their own workload and
# wants more is not blocked.
_SQLITE_WORKER_COUNT_WARN_THRESHOLD = 4


def _log_event(event: str, **fields: object) -> None:
    payload: dict[str, object] = {"event": event}
    payload.update(fields)
    _logger.info(json.dumps(payload))


@dataclass
class SupervisorConfig:
    run_api: bool = True
    run_render_worker: bool = True
    run_publisher_worker: bool = True
    host: str = "127.0.0.1"
    port: int = 8000
    render_workers: int = 1
    publisher_workers: int = 1
    shutdown_timeout_seconds: float = 30.0
    render_daemon_config: DaemonConfig | None = None
    publisher_daemon_config: PublisherDaemonConfig | None = None


if TYPE_CHECKING:
    import uvicorn


@dataclass
class _ThreadHandle:
    name: str
    thread: threading.Thread
    stop: Callable[[], None]
    fatal_error_getter: Callable[[], str | None]


class SupervisorFatalError(Exception):
    """Raised by Supervisor.run() when a component that the supervisor
    treats as fatal to the whole process (the API server itself, or every
    configured render/publisher worker dying) fails to start or exits with
    an unrecoverable error -- never for an ordinary single-job/single-
    publication failure, which the daemons already isolate themselves."""


class Supervisor:
    """Runs the FastAPI server, the render-worker daemon(s), and the
    publisher-worker daemon(s) as threads inside ONE process
    (`reel-harness serve`) -- threads, not subprocesses, because almost
    all the work here is I/O-bound (DB queries, HTTP calls to real
    providers, waiting on an ffmpeg SUBPROCESS that already runs outside
    the GIL) and every worker already assumes concurrent access to the
    same SQLite DB via the existing lease-fencing mechanism (see
    worker.lease/worker.publish_lease, exercised directly by
    tests/integration/test_multi_worker.py) -- there is no GIL-bound CPU
    work here for separate processes to usefully parallelize, and threads
    avoid the real complexity multiprocess coordination would add
    (separate AppContexts, IPC for shutdown, no memory sharing) for zero
    benefit in this single-machine tool.

    Failure policy (see docs/OPERATIONS.md for the full table): the API
    server dying is treated as fatal to the whole supervisor (new requests
    can never be served again without it) -- render/publisher workers are
    signaled to stop and the process exits non-zero. A render-worker
    thread dying leaves the publisher worker running (existing
    publications can still be processed) and vice-versa; either one dying
    marks readiness "degraded" (surfaced via ops.metrics/status, not this
    class) rather than tearing everything down for an isolated failure
    the daemons themselves already contain per-job/per-publication.
    """

    def __init__(self, app_context, config: SupervisorConfig) -> None:
        self._ctx = app_context
        self._config = config
        self._threads: list[_ThreadHandle] = []
        self._api_server: uvicorn.Server | None = None
        self._api_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.fatal_errors: dict[str, str] = {}

    def request_stop(self, reason: str = "requested") -> None:
        _log_event("supervisor_shutdown_requested", reason=reason)
        self._stop_event.set()
        for handle in self._threads:
            handle.stop()
        if self._api_server is not None:
            self._api_server.should_exit = True

    @property
    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def _start_render_workers(self) -> None:
        cfg = self._config
        base = cfg.render_daemon_config
        for i in range(cfg.render_workers):
            worker_id = f"{base.worker_id if base else default_worker_id()}-{i}" if cfg.render_workers > 1 else (
                base.worker_id if base else default_worker_id()
            )
            daemon_config = DaemonConfig(
                worker_id=worker_id,
                poll_interval_seconds=base.poll_interval_seconds if base else 5.0,
                lease_timeout_seconds=base.lease_timeout_seconds if base else 300,
                heartbeat_interval_seconds=base.heartbeat_interval_seconds if base else 60.0,
                max_jobs=base.max_jobs if base else None,
                idle_exit_after_seconds=base.idle_exit_after_seconds if base else None,
                stop_on_error=base.stop_on_error if base else False,
            )
            daemon = WorkerDaemon(
                self._ctx.session_factory, self._ctx.storage, self._ctx.providers_for_job, daemon_config,
            )
            thread = threading.Thread(
                target=self._run_render_daemon, args=(daemon, worker_id), name=f"render:{worker_id}",
            )

            def _stop(d: WorkerDaemon = daemon) -> None:
                d.request_stop("supervisor_shutdown")

            def _fatal_error(d: WorkerDaemon = daemon) -> str | None:
                return d.fatal_error

            self._threads.append(_ThreadHandle(
                name=f"render:{worker_id}", thread=thread, stop=_stop, fatal_error_getter=_fatal_error,
            ))
            thread.start()

    def _run_render_daemon(self, daemon: WorkerDaemon, worker_id: str) -> None:
        exit_code = daemon.run()
        if daemon.fatal_error:
            self.fatal_errors[f"render:{worker_id}"] = daemon.fatal_error
            _log_event("supervisor_component_fatal", component="render_worker", error=daemon.fatal_error)
        elif exit_code != 0:
            _log_event("supervisor_component_stopped", component="render_worker", exit_code=exit_code)

    def _start_publisher_workers(self) -> None:
        cfg = self._config
        base = cfg.publisher_daemon_config
        for i in range(cfg.publisher_workers):
            worker_id = (
                f"{base.worker_id if base else default_publisher_worker_id()}-{i}"
                if cfg.publisher_workers > 1
                else (base.worker_id if base else default_publisher_worker_id())
            )
            daemon_config = PublisherDaemonConfig(
                worker_id=worker_id,
                poll_interval_seconds=base.poll_interval_seconds if base else 5.0,
                lease_timeout_seconds=base.lease_timeout_seconds if base else 300,
                max_publications=base.max_publications if base else None,
                idle_exit_after_seconds=base.idle_exit_after_seconds if base else None,
                stop_on_error=base.stop_on_error if base else False,
                process_upload=base.process_upload if base else True,
                process_status=base.process_status if base else True,
            )
            daemon = PublisherDaemon(
                self._ctx.session_factory, self._ctx.storage, self._ctx.bundle_for_publication,
                self._ctx.channel_niche_for_job, daemon_config,
            )
            thread = threading.Thread(
                target=self._run_publisher_daemon, args=(daemon, worker_id), name=f"publisher:{worker_id}",
            )

            def _stop(d: PublisherDaemon = daemon) -> None:
                d.request_stop("supervisor_shutdown")

            def _fatal_error(d: PublisherDaemon = daemon) -> str | None:
                return d.fatal_error

            self._threads.append(_ThreadHandle(
                name=f"publisher:{worker_id}", thread=thread, stop=_stop, fatal_error_getter=_fatal_error,
            ))
            thread.start()

    def _run_publisher_daemon(self, daemon: PublisherDaemon, worker_id: str) -> None:
        exit_code = daemon.run()
        if daemon.fatal_error:
            self.fatal_errors[f"publisher:{worker_id}"] = daemon.fatal_error
            _log_event("supervisor_component_fatal", component="publisher_worker", error=daemon.fatal_error)
        elif exit_code != 0:
            _log_event("supervisor_component_stopped", component="publisher_worker", exit_code=exit_code)

    def _start_api(self) -> None:
        import uvicorn

        from reel_harness.api import app as api_app_module

        api_app_module._ctx = self._ctx  # share this Supervisor's AppContext, never a second one
        api_app_module._supervisor = self  # lets /status report live component/fatal-error state
        uv_config = uvicorn.Config(
            api_app_module.app, host=self._config.host, port=self._config.port,
            log_level="warning", access_log=False,
        )
        server = uvicorn.Server(uv_config)
        self._api_server = server

        def _run() -> None:
            try:
                server.run()
            except Exception as exc:  # noqa: BLE001 - API death is fatal to the supervisor, see class docstring
                self.fatal_errors["api"] = f"{type(exc).__name__}: {exc}"
                _log_event("supervisor_component_fatal", component="api", error=str(exc))
                self.request_stop("api_fatal_error")

        self._api_thread = threading.Thread(target=_run, name="api")
        self._api_thread.start()
        # Give uvicorn a moment to either bind successfully or fail fast
        # (e.g. port already in use) before reporting startup as complete.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if server.started or "api" in self.fatal_errors:
                break
            time.sleep(0.05)

    def run(self) -> int:
        cfg = self._config
        component_counts = {
            "api": cfg.run_api, "render_workers": cfg.render_workers if cfg.run_render_worker else 0,
            "publisher_workers": cfg.publisher_workers if cfg.run_publisher_worker else 0,
        }
        total_workers = component_counts["render_workers"] + component_counts["publisher_workers"]
        if total_workers > _SQLITE_WORKER_COUNT_WARN_THRESHOLD:
            _log_event(
                "supervisor_worker_count_warning", total_workers=total_workers,
                threshold=_SQLITE_WORKER_COUNT_WARN_THRESHOLD,
                detail="SQLite is single-writer; a high worker count mostly waits on the write lock",
            )
        _log_event("supervisor_started", **component_counts)

        if cfg.run_api:
            self._start_api()
            if "api" in self.fatal_errors:
                _log_event("supervisor_stopped", reason="api_failed_to_start")
                return 1
        if cfg.run_render_worker:
            self._start_render_workers()
        if cfg.run_publisher_worker:
            self._start_publisher_workers()

        try:
            while not self._stop_event.is_set():
                if "api" in self.fatal_errors:
                    self.request_stop("api_fatal_error")
                    break
                self._stop_event.wait(1.0)
        except KeyboardInterrupt:
            self.request_stop("keyboard_interrupt")

        self._shutdown()
        exit_code = 1 if self.fatal_errors else 0
        _log_event("supervisor_stopped", exit_code=exit_code, fatal_errors=list(self.fatal_errors))
        return exit_code

    def _shutdown(self) -> None:
        deadline = time.monotonic() + self._config.shutdown_timeout_seconds
        for handle in self._threads:
            handle.stop()
        for handle in self._threads:
            remaining = max(0.0, deadline - time.monotonic())
            handle.thread.join(timeout=remaining)
            if handle.thread.is_alive():
                _log_event("supervisor_shutdown_timeout", component=handle.name)
        if self._api_server is not None:
            self._api_server.should_exit = True
        if self._api_thread is not None:
            remaining = max(0.0, deadline - time.monotonic())
            self._api_thread.join(timeout=remaining)
            if self._api_thread.is_alive():
                _log_event("supervisor_shutdown_timeout", component="api")

    def component_status(self) -> dict:
        """Safe, non-secret summary for /status -- alive/dead per thread,
        never a stack trace or provider response."""
        return {
            "api": (
                "running" if self._api_thread is not None and self._api_thread.is_alive()
                else ("fatal" if "api" in self.fatal_errors else "not_running")
            ),
            "workers": [
                {
                    "name": handle.name,
                    "status": "running" if handle.thread.is_alive() else (
                        "fatal" if handle.fatal_error_getter() else "stopped"
                    ),
                }
                for handle in self._threads
            ],
        }
