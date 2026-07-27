"""Worker daemon lifecycle: polling, idle exit, max-jobs, failure isolation,
graceful shutdown, and no-leak guarantees. All tests use short intervals --
no long real sleeps.
"""
from __future__ import annotations

import threading

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.daemon import EXIT_FATAL, EXIT_OK, DaemonConfig, WorkerDaemon
from reel_harness.worker.lease import find_orphaned_active_jobs
from reel_harness.worker.runner import ProviderBundle

FFMPEG_PRESENT = check_ffmpeg_available().all_available

_TERMINAL_OK = {JobStatus.REVIEW_REQUIRED.value, JobStatus.FAILED.value}


def _config(**overrides) -> DaemonConfig:
    defaults = dict(
        worker_id="daemon-test",
        poll_interval_seconds=0.02,
        lease_timeout_seconds=300,
        heartbeat_interval_seconds=0.05,
        max_jobs=None,
        idle_exit_after_seconds=None,
        stop_on_error=False,
    )
    defaults.update(overrides)
    return DaemonConfig(**defaults)


def _daemon(session_factory, storage, providers, **cfg) -> WorkerDaemon:
    return WorkerDaemon(session_factory, storage, lambda job: providers, _config(**cfg))


def test_idle_daemon_polls_then_exits_after_idle_deadline(
    session_factory, storage, fake_providers,
) -> None:
    daemon = _daemon(session_factory, storage, fake_providers, idle_exit_after_seconds=0.1)
    exit_code = daemon.run()
    assert exit_code == EXIT_OK
    assert daemon.stop_reason == "idle_exit"
    assert daemon.jobs_processed == 0


def test_daemon_processes_one_job_then_idle_exits(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="d1", topic="t")
    daemon = _daemon(session_factory, storage, fake_providers, idle_exit_after_seconds=0.1)
    exit_code = daemon.run()
    assert exit_code == EXIT_OK
    assert daemon.jobs_processed == 1
    refreshed = job_service.get_job(job.id)
    assert refreshed.status in _TERMINAL_OK
    assert refreshed.locked_by is None, "lease must be released after the job"
    with session_factory() as session:
        assert find_orphaned_active_jobs(session) == []


def test_daemon_processes_multiple_jobs_sequentially(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    ids = [job_service.create_job(channel.id, idempotency_key=f"d{i}", topic="t")[0].id for i in range(3)]
    daemon = _daemon(session_factory, storage, fake_providers, idle_exit_after_seconds=0.1)
    assert daemon.run() == EXIT_OK
    assert daemon.jobs_processed == 3
    for job_id in ids:
        assert job_service.get_job(job_id).status in _TERMINAL_OK


def test_max_jobs_is_honored_exactly(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    for i in range(3):
        job_service.create_job(channel.id, idempotency_key=f"m{i}", topic="t")
    daemon = _daemon(session_factory, storage, fake_providers, max_jobs=2)
    assert daemon.run() == EXIT_OK
    assert daemon.stop_reason == "max_jobs"
    assert daemon.jobs_processed == 2
    remaining = [j for j in job_service.list_jobs(status=JobStatus.QUEUED.value)]
    assert len(remaining) == 1, "exactly one job must remain queued"


class _ExplodingLLM:
    provider_id = "fake"
    model_id = "exploding"

    def generate_topic(self, ctx):
        raise RuntimeError("boom topic")

    def generate_script(self, topic, ctx):
        raise RuntimeError("boom script")


def test_one_failing_job_does_not_stop_the_daemon(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    bad, _ = job_service.create_job(channel.id, idempotency_key="bad", topic="t")
    good, _ = job_service.create_job(channel.id, idempotency_key="good", topic="t")

    from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
    from reel_harness.providers.fake_tts import FakeTTSProvider

    def providers_for_job(job) -> ProviderBundle:
        if job.id == bad.id:
            return ProviderBundle(
                llm=_ExplodingLLM(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider(),
            )
        return fake_providers

    daemon = WorkerDaemon(
        session_factory, storage, providers_for_job, _config(idle_exit_after_seconds=0.1),
    )
    assert daemon.run() == EXIT_OK
    assert daemon.jobs_processed == 2
    assert job_service.get_job(bad.id).status == JobStatus.FAILED.value
    assert job_service.get_job(bad.id).failure_code == "UNEXPECTED_PIPELINE_ERROR"
    assert job_service.get_job(good.id).status in _TERMINAL_OK
    with session_factory() as session:
        assert find_orphaned_active_jobs(session) == []


def test_stop_on_error_exits_after_the_first_failed_job(
    job_service, channel, session_factory, storage,
) -> None:
    from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
    from reel_harness.providers.fake_tts import FakeTTSProvider

    job_service.create_job(channel.id, idempotency_key="bad1", topic="t")
    job_service.create_job(channel.id, idempotency_key="never-run", topic="t")
    exploding = ProviderBundle(
        llm=_ExplodingLLM(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider(),
    )
    daemon = _daemon(session_factory, storage, exploding, stop_on_error=True)
    assert daemon.run() == EXIT_FATAL
    assert daemon.stop_reason == "stop_on_error"
    assert daemon.jobs_processed == 1
    queued = job_service.list_jobs(status=JobStatus.QUEUED.value)
    assert len(queued) == 1


def test_request_stop_interrupts_the_idle_wait_promptly(
    session_factory, storage, fake_providers,
) -> None:
    """Graceful shutdown: a stop request during the poll wait exits without
    waiting out the interval (no busy loop, no long block)."""
    daemon = _daemon(
        session_factory, storage, fake_providers,
        poll_interval_seconds=30.0, idle_exit_after_seconds=None,
    )
    runner = threading.Thread(target=daemon.run)
    runner.start()
    try:
        deadline = threading.Event()
        deadline.wait(0.1)  # let it enter the idle wait
        daemon.request_stop("test_stop")
        runner.join(timeout=5.0)
        assert not runner.is_alive(), "daemon must exit promptly on stop request"
    finally:
        daemon.request_stop("cleanup")
        runner.join(timeout=5.0)
    assert daemon.stop_reason in {"test_stop", "stop_requested"}


def test_no_threads_leak_after_daemon_run(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job_service.create_job(channel.id, idempotency_key="leak", topic="t")
    before = {t.name for t in threading.enumerate()}
    daemon = _daemon(session_factory, storage, fake_providers, idle_exit_after_seconds=0.1)
    daemon.run()
    after = {t.name for t in threading.enumerate()}
    lingering = {name for name in after - before if name.startswith("lease-heartbeat-")}
    assert lingering == set(), "heartbeat threads must be joined by daemon exit"


def test_stale_job_is_recovered_and_then_processed(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    from datetime import UTC, datetime, timedelta

    from reel_harness.core.state_machine import apply_transition

    job, _ = job_service.create_job(channel.id, idempotency_key="stale-d", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        db_job.current_stage = "SCRIPT"
        db_job.locked_by = "dead-worker"
        db_job.lease_token = "dead-token"
        db_job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=9999)
        session.commit()

    daemon = _daemon(
        session_factory, storage, fake_providers,
        lease_timeout_seconds=60, idle_exit_after_seconds=0.3, poll_interval_seconds=0.05,
    )
    assert daemon.run() == EXIT_OK
    # Recovery routed the job to RETRY_WAIT with a short backoff; whether this
    # daemon instance then picked it up depends on the backoff (~10s), so the
    # hard assertions are: recovered, unlocked or reprocessed, never orphaned.
    refreshed = job_service.get_job(job.id)
    assert refreshed.status in {JobStatus.RETRY_WAIT.value, *_TERMINAL_OK}
    with session_factory() as session:
        assert find_orphaned_active_jobs(session) == []


def test_two_daemons_never_process_the_same_job(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    from sqlalchemy import select

    from reel_harness.db.models import StageRun

    ids = [job_service.create_job(channel.id, idempotency_key=f"race{i}", topic="t")[0].id for i in range(4)]
    daemon_a = WorkerDaemon(
        session_factory, storage, lambda job: fake_providers,
        _config(worker_id="daemon-a", idle_exit_after_seconds=0.3, poll_interval_seconds=0.02),
    )
    daemon_b = WorkerDaemon(
        session_factory, storage, lambda job: fake_providers,
        _config(worker_id="daemon-b", idle_exit_after_seconds=0.3, poll_interval_seconds=0.02),
    )
    thread_a = threading.Thread(target=daemon_a.run)
    thread_b = threading.Thread(target=daemon_b.run)
    thread_a.start()
    thread_b.start()
    thread_a.join(timeout=120)
    thread_b.join(timeout=120)
    assert not thread_a.is_alive() and not thread_b.is_alive()

    assert daemon_a.jobs_processed + daemon_b.jobs_processed == 4
    with session_factory() as session:
        for job_id in ids:
            db_job = session.get(Job, job_id)
            assert db_job.status in _TERMINAL_OK
            # No stage ran twice: every (stage, attempt) pair is unique and no
            # attempt exceeds 1 on the happy path.
            runs = session.execute(
                select(StageRun.stage, StageRun.attempt).where(StageRun.job_id == job_id),
            ).all()
            assert len(runs) == len(set(runs))
            assert all(attempt == 1 for _, attempt in runs)
        assert find_orphaned_active_jobs(session) == []
