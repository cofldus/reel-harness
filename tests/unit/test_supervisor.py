from __future__ import annotations

import socket
import threading
import time

import pytest

from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.ops.supervisor import Supervisor, SupervisorConfig


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rh.db'}", jobs_dir=tmp_path / "jobs",
        credential_dir=tmp_path.parent / "creds", app_api_key="a-real-non-placeholder-key-value",
    )
    context = AppContext(settings)
    yield context
    context.engine.dispose()


def _run_and_stop(supervisor: Supervisor, delay: float = 0.3) -> int:
    result: dict[str, int] = {}

    def _runner() -> None:
        result["exit_code"] = supervisor.run()

    thread = threading.Thread(target=_runner)
    thread.start()
    time.sleep(delay)
    supervisor.request_stop("test")
    thread.join(timeout=10.0)
    assert not thread.is_alive(), "supervisor.run() did not return after request_stop()"
    return result["exit_code"]


def test_supervisor_starts_and_stops_all_components_cleanly(ctx) -> None:
    config = SupervisorConfig(
        run_api=True, run_render_worker=True, run_publisher_worker=True,
        host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    exit_code = _run_and_stop(supervisor)
    assert exit_code == 0
    for handle in supervisor._threads:
        assert not handle.thread.is_alive()
    assert supervisor._api_thread is not None
    assert not supervisor._api_thread.is_alive()


def test_supervisor_api_actually_binds_and_shares_the_supervisor_appcontext(ctx) -> None:
    """The test suite blocks real sockets outright (even loopback -- see
    conftest.block_real_network), so this can't do a live HTTP round trip;
    it instead proves the two things that would make one work: uvicorn
    actually reports `started`, and api.app's module-level context was
    rebound to THIS Supervisor's AppContext (never a second, separately
    constructed one) -- exactly what a real request would be served by."""
    from reel_harness.api import app as api_app_module

    port = _free_port()
    config = SupervisorConfig(
        run_api=True, run_render_worker=False, run_publisher_worker=False,
        host="127.0.0.1", port=port, shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (supervisor._api_server and supervisor._api_server.started):
            time.sleep(0.05)
        assert supervisor._api_server is not None
        assert supervisor._api_server.started is True
        assert api_app_module._ctx is ctx
    finally:
        supervisor.request_stop("test")
        thread.join(timeout=10.0)


def test_supervisor_can_disable_individual_components(ctx) -> None:
    config = SupervisorConfig(
        run_api=False, run_render_worker=True, run_publisher_worker=False,
        host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    exit_code = _run_and_stop(supervisor)
    assert exit_code == 0
    assert supervisor._api_thread is None
    assert len(supervisor._threads) == 1
    assert supervisor._threads[0].name.startswith("render:")


def test_supervisor_multiple_render_workers(ctx) -> None:
    config = SupervisorConfig(
        run_api=False, run_render_worker=True, run_publisher_worker=False,
        render_workers=3, host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    exit_code = _run_and_stop(supervisor)
    assert exit_code == 0
    assert len(supervisor._threads) == 3
    names = {h.name for h in supervisor._threads}
    assert len(names) == 3  # distinct worker ids, no collision


def test_supervisor_component_status_reports_running_then_stopped(ctx) -> None:
    config = SupervisorConfig(
        run_api=False, run_render_worker=True, run_publisher_worker=False,
        host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    thread = threading.Thread(target=supervisor.run)
    thread.start()
    time.sleep(0.2)
    status = supervisor.component_status()
    assert status["workers"][0]["status"] == "running"
    supervisor.request_stop("test")
    thread.join(timeout=10.0)
    status = supervisor.component_status()
    assert status["workers"][0]["status"] == "stopped"


def test_supervisor_api_startup_failure_is_fatal_and_never_starts_workers(ctx, monkeypatch) -> None:
    """Simulates a real API startup failure (e.g. a genuinely occupied port)
    deterministically via monkeypatch rather than racing a real OS-level
    bind conflict -- a real bind conflict is not reliably observable this
    way on every platform/socket-option combination, and a flaky false
    negative here would silently hide a real regression instead of hanging
    the way it did while this test was being written (see git history)."""
    import uvicorn

    def _boom(self, sockets=None) -> None:
        raise OSError("simulated: address already in use")

    monkeypatch.setattr(uvicorn.Server, "run", _boom)

    config = SupervisorConfig(
        run_api=True, run_render_worker=True, run_publisher_worker=False,
        host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    exit_code = supervisor.run()
    assert exit_code == 1
    assert "api" in supervisor.fatal_errors
    assert supervisor._threads == []  # render worker never started


def test_supervisor_render_worker_fatal_does_not_kill_publisher_worker(ctx, monkeypatch) -> None:
    from reel_harness.worker.daemon import WorkerDaemon

    def _boom(self) -> int:
        self.fatal_error = "SimulatedFatalError: injected for test"
        return 1

    monkeypatch.setattr(WorkerDaemon, "run", _boom)

    config = SupervisorConfig(
        run_api=False, run_render_worker=True, run_publisher_worker=True,
        host="127.0.0.1", port=_free_port(), shutdown_timeout_seconds=10.0,
    )
    supervisor = Supervisor(ctx, config)
    exit_code = _run_and_stop(supervisor, delay=0.3)
    assert any(key.startswith("render:") for key in supervisor.fatal_errors)
    publisher_handles = [h for h in supervisor._threads if h.name.startswith("publisher:")]
    assert len(publisher_handles) == 1
    assert exit_code == 1  # supervisor.run() surfaces the fatal error in its own exit code
