"""Supervisor E2E: `reel-harness serve` run as a REAL subprocess (not
in-process), driving a real job through the render worker and a real
(Fake-provider) publication through the publisher worker, then a real
graceful shutdown (SIGINT/CTRL_BREAK), with no leftover ACTIVE+unlocked
rows and no busy-loop log spam, followed by a "restart" (a fresh
connection to the same DB file) proving state persisted.

The parent pytest process's network is blocked by conftest's
block_real_network fixture (even loopback via socket.create_connection,
which most HTTP client libraries use) -- a raw socket.socket().connect()
probe is used for the API-readiness check instead, since the fixture
explicitly allows loopback at that lower level (see conftest.py). Job/
publication progress is observed by polling the shared SQLite file
directly from the parent process, never by making an HTTP request to the
child."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from reel_harness.core.state_machine import JobStatus, PublicationStatus
from reel_harness.db.schema import create_engine_from_url, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg for an actual render")

_STARTUP_TIMEOUT = 20.0
_PROCESSING_TIMEOUT = 45.0
_SHUTDOWN_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def test_supervisor_subprocess_e2e_full_lifecycle(tmp_path) -> None:
    db_path = tmp_path / "rh.db"
    jobs_dir = tmp_path / "jobs"
    creds_dir = tmp_path.parent / f"{tmp_path.name}-supervisor-creds"
    database_url = f"sqlite:///{db_path}"
    app_api_key = "a-real-non-placeholder-supervisor-test-key"

    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=database_url, jobs_dir=jobs_dir, credential_dir=creds_dir, app_api_key=app_api_key,
    )
    ctx = AppContext(settings)
    channel = ctx.jobs.create_channel(name="c", niche="cooking", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="fried rice")
    ctx.engine.dispose()

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["JOBS_DIR"] = str(jobs_dir)
    env["REEL_HARNESS_CREDENTIAL_DIR"] = str(creds_dir)
    env["APP_API_KEY"] = app_api_key
    deps = check_ffmpeg_available()
    if deps.ffmpeg.path:
        env["REEL_HARNESS_FFMPEG_PATH"] = str(deps.ffmpeg.path)
    if deps.ffprobe.path:
        env["REEL_HARNESS_FFPROBE_PATH"] = str(deps.ffprobe.path)

    port = _free_port()
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    # 1. reel-harness serve, as a real subprocess
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "reel_harness.cli.main", "serve",
            "--host", "127.0.0.1", "--port", str(port), "--render-workers", "1", "--publisher-workers", "1",
        ],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **popen_kwargs,
    )
    stdout_text = ""
    exit_code: int | None = None
    try:
        # 2. API readiness -- a real TCP accept on the configured port.
        assert _wait_for_port("127.0.0.1", port, _STARTUP_TIMEOUT), (
            "serve subprocess never opened its API port"
        )

        session_factory = make_session_factory(create_engine_from_url(database_url))

        # 3/4. job created above; the render worker inside the subprocess
        # picks it up and processes it for real.
        from reel_harness.db.models import Job

        deadline = time.monotonic() + _PROCESSING_TIMEOUT
        reached = False
        while time.monotonic() < deadline:
            with session_factory() as session:
                db_job = session.get(Job, job.id)
                if db_job.status in (JobStatus.REVIEW_REQUIRED.value, JobStatus.COMPLETED.value):
                    reached = True
                    break
            time.sleep(0.3)
        assert reached, "render worker never processed the job within the timeout"

        with session_factory() as session:
            db_job = session.get(Job, job.id)
            if db_job.status != JobStatus.COMPLETED.value:
                from reel_harness.core.state_machine import apply_transition

                apply_transition(db_job, JobStatus.READY)
                apply_transition(db_job, JobStatus.COMPLETED)
                session.commit()

        from reel_harness.storage.local import LocalFilesystemStorage

        storage = LocalFilesystemStorage(jobs_dir)
        from reel_harness.manifest.schema import Manifest

        manifest = Manifest.model_validate_json(storage.read_bytes(job.id, "manifest.json"))
        checksum = manifest.final_video_checksum_sha256
        assert checksum

        # 5. a fake publication is created (direct insert, READY_TO_UPLOAD --
        # Fake-provider jobs are permanently publish-ineligible by design,
        # see CLAUDE.md, so PublicationService.create_publication's
        # eligibility gate is deliberately bypassed here).
        from reel_harness.db.models import Publication
        from reel_harness.providers.registry import publisher_snapshot

        snapshot = publisher_snapshot(settings, "fake", "default")
        with session_factory() as session:
            pub = Publication(
                job_id=job.id, provider="fake", account_reference="default",
                status=PublicationStatus.READY_TO_UPLOAD.value, privacy_status="private",
                idempotency_key="pub-1", final_video_checksum=checksum, publisher_config=snapshot,
            )
            session.add(pub)
            session.commit()
            pub_id = pub.id

        # 6/7. the publisher worker inside the subprocess picks it up and
        # drives it to PUBLISHED for real.
        deadline = time.monotonic() + _PROCESSING_TIMEOUT
        published = False
        while time.monotonic() < deadline:
            with session_factory() as session:
                db_pub = session.get(Publication, pub_id)
                if db_pub.status == PublicationStatus.PUBLISHED.value:
                    published = True
                    break
                if db_pub.status in ("FAILED",):
                    pytest.fail(f"publication reached FAILED: {db_pub.failure_code} {db_pub.failure_summary}")
            time.sleep(0.3)
        assert published, "publisher worker never reached PUBLISHED within the timeout"
    finally:
        # 8/9. graceful shutdown
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            stdout_text, _ = proc.communicate(timeout=_SHUTDOWN_TIMEOUT)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout_text, _ = proc.communicate()
        exit_code = proc.returncode

    assert exit_code == 0, f"serve subprocess did not exit cleanly (code={exit_code}); output:\n{stdout_text}"

    # 10. no forbidden ACTIVE+unlocked rows left behind after shutdown
    from reel_harness.worker.lease import find_orphaned_active_jobs
    from reel_harness.worker.publish_lease import find_orphaned_active_publications

    with session_factory() as session:
        assert find_orphaned_active_jobs(session) == []
        assert find_orphaned_active_publications(session) == []

    # No busy loop / excessive log spam: a healthy short-lived run producing
    # thousands of lines would indicate a tight poll loop, not real work.
    log_lines = [line for line in stdout_text.splitlines() if line.strip()]
    assert len(log_lines) < 500, f"unexpectedly high log volume ({len(log_lines)} lines) -- possible busy loop"

    # 11/12. "restart" -- a fresh engine/session_factory against the same
    # DB file -- and state persisted.
    restarted_session_factory = make_session_factory(create_engine_from_url(database_url))
    with restarted_session_factory() as session:
        from reel_harness.db.models import Job as JobModel
        from reel_harness.db.models import Publication as PublicationModel

        restored_job = session.get(JobModel, job.id)
        restored_pub = session.get(PublicationModel, pub_id)
        assert restored_job is not None
        assert restored_job.status == JobStatus.COMPLETED.value
        assert restored_pub is not None
        assert restored_pub.status == PublicationStatus.PUBLISHED.value
