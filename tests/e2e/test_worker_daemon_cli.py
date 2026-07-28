"""Real-subprocess E2E for the worker daemon CLI: `python -m
reel_harness.cli.main worker-run` is started as an actual OS process against a
file DB in tmp_path and must process queued work, then exit on its own with a
clean exit code.

Windows note: graceful shutdown via console signals cannot be exercised from a
non-console pytest parent (CTRL events need a shared console, and
Popen.terminate() is TerminateProcess -- a hard kill, not a signal). The
graceful-stop path is covered in-process by test_worker_daemon.py via
request_stop(); a hard kill is exactly the crash case stale-lease recovery
covers.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available

FFMPEG_PRESENT = check_ffmpeg_available().all_available

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_daemon(tmp_path: Path, *extra_args: str, timeout: float = 180.0) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'daemon.db').as_posix()}"
    env["JOBS_DIR"] = str(tmp_path / "jobs")
    return subprocess.run(
        [sys.executable, "-B", "-m", "reel_harness.cli.main", "worker-run", *extra_args],
        cwd=_REPO_ROOT, env=env, capture_output=True, text=True, timeout=timeout,
    )


def _service(tmp_path: Path) -> JobService:
    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'daemon.db').as_posix()}")
    init_db(engine)
    return JobService(make_session_factory(engine))


def test_daemon_subprocess_idle_exits_cleanly_with_no_jobs(tmp_path) -> None:
    _service(tmp_path)  # initialize the schema so the daemon has a DB to poll
    result = _run_daemon(tmp_path, "--idle-exit-after", "0.5", "--poll-interval", "0.1")
    assert result.returncode == 0, result.stderr
    assert '"event": "worker_started"' in result.stderr or '"event": "worker_started"' in result.stdout
    combined = result.stdout + result.stderr
    assert '"worker_idle"' in combined
    assert '"worker_stopped"' in combined
    assert '"reason": "idle_exit"' in combined


def test_daemon_subprocess_processes_two_queued_jobs_and_exits(tmp_path) -> None:
    service = _service(tmp_path)
    channel = service.create_channel(name="cli-e2e", niche="cooking", language="en")
    job1, _ = service.create_job(channel.id, idempotency_key="cli-1", topic="daemon cli job one")
    job2, _ = service.create_job(channel.id, idempotency_key="cli-2", topic="daemon cli job two")

    result = _run_daemon(
        tmp_path, "--max-jobs", "2", "--idle-exit-after", "30", "--poll-interval", "0.1",
    )
    assert result.returncode == 0, result.stderr

    expected = JobStatus.REVIEW_REQUIRED.value if FFMPEG_PRESENT else JobStatus.FAILED.value
    for job_id in (job1.id, job2.id):
        refreshed = service.get_job(job_id)
        assert refreshed.status == expected
        assert refreshed.locked_by is None
        assert refreshed.lease_token is None

    combined = result.stdout + result.stderr
    assert combined.count('"job_leased"') == 2
    assert '"reason": "max_jobs"' in combined
