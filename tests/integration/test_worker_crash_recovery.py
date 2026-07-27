"""Regression tests for worker crash recovery (the audit BLOCKER-2 / HIGH
class of failures): stale-lease recovery must work on datetimes that actually
round-tripped through the SQLite file (not same-session Python objects), and an
unexpected exception must never leave a job ACTIVE + unlocked.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Job, StageRun
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.base import TTSResult
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.worker.lease import (
    find_orphaned_active_jobs,
    lease_next_job,
    recover_stale_jobs,
    release_lease,
)
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def test_stale_recovery_works_after_a_real_db_roundtrip(tmp_path) -> None:
    """The full BLOCKER-2 scenario: write leases with one engine, dispose it,
    reopen the same SQLite file with a new engine/session (as a restarted
    worker process would), and run recovery on rows freshly loaded from disk."""
    db_path = tmp_path / "stale.db"
    engine1 = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine1)
    factory1 = make_session_factory(engine1)
    service = JobService(factory1)
    channel = service.create_channel(name="c", niche="n", language="en")
    stale_job, _ = service.create_job(channel.id, idempotency_key="stale", topic="t")
    fresh_job, _ = service.create_job(channel.id, idempotency_key="fresh", topic="t")

    now = datetime.now(UTC)
    with factory1() as session:
        dead = session.get(Job, stale_job.id)
        apply_transition(dead, JobStatus.SCRIPT_GENERATING)
        dead.current_stage = "SCRIPT"
        dead.locked_by = "dead-worker"
        dead.heartbeat_at = now - timedelta(seconds=999)
        alive = session.get(Job, fresh_job.id)
        apply_transition(alive, JobStatus.SCRIPT_GENERATING)
        alive.current_stage = "SCRIPT"
        alive.locked_by = "alive-worker"
        alive.heartbeat_at = now
        session.commit()
    engine1.dispose()  # discard every prior session and in-memory object

    engine2 = create_engine_from_url(f"sqlite:///{db_path}")
    factory2 = make_session_factory(engine2)
    with factory2() as session:
        loaded = session.get(Job, stale_job.id)
        assert loaded.heartbeat_at is not None
        assert loaded.heartbeat_at.tzinfo is not None, "datetimes read from the DB must be aware UTC"

        recovered = recover_stale_jobs(session, lease_timeout_seconds=60)  # must not raise TypeError
        assert recovered == [stale_job.id]

        reloaded = session.get(Job, stale_job.id)
        assert reloaded.status == JobStatus.RETRY_WAIT.value
        assert reloaded.retry_target_stage == "SCRIPT"
        assert reloaded.failure_code == "WORKER_CRASHED"
        assert reloaded.locked_by is None
        assert reloaded.next_retry_at is not None

        untouched = session.get(Job, fresh_job.id)
        assert untouched.status == JobStatus.SCRIPT_GENERATING.value
        assert untouched.locked_by == "alive-worker"

        # The recovered job is leaseable again once its backoff elapses.
        leased = lease_next_job(session, worker_id="worker-2", now=now + timedelta(hours=1))
        assert leased is not None
        assert leased.id == stale_job.id
        assert find_orphaned_active_jobs(session) == []


class _ExplodingTTSProvider:
    """A provider whose failure mode is a plain uncategorized exception --
    exactly the kind the worker's unexpected-exception boundary must absorb."""

    provider_id = "fake"

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        raise RuntimeError("boom: provider blew up in an uncategorized way")


def test_unexpected_exception_fails_the_job_instead_of_stranding_it(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    exploding = ProviderBundle(
        llm=FakeLLMProvider(), tts=_ExplodingTTSProvider(), stock_media=FakeStockMediaProvider(),
    )
    job, _ = job_service.create_job(channel.id, idempotency_key="boom-1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, exploding, storage)  # must not raise
        assert db_job.status == JobStatus.FAILED.value
        assert db_job.failure_code == "UNEXPECTED_PIPELINE_ERROR"
        assert "RuntimeError" in db_job.failure_summary
        assert "Traceback" not in db_job.failure_summary

        tts_run = session.execute(
            select(StageRun).where(StageRun.job_id == job.id, StageRun.stage == "TTS"),
        ).scalar_one()
        assert tts_run.status == "failed"
        assert "RuntimeError" in tts_run.error_detail
        assert find_orphaned_active_jobs(session) == []

    # One broken job must not take the worker down: the next pass leases and
    # processes a healthy job normally.
    healthy, _ = job_service.create_job(channel.id, idempotency_key="boom-2", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        assert leased is not None
        assert leased.id == healthy.id
        run_job(session, leased, channel, fake_providers, storage)
        if FFMPEG_PRESENT:
            assert leased.status == JobStatus.REVIEW_REQUIRED.value
        else:
            assert leased.status == JobStatus.FAILED.value
            assert leased.failure_code == "BLOCKED_DEPENDENCY"
        release_lease(session, leased)
        assert find_orphaned_active_jobs(session) == []


def test_release_lease_refuses_to_unlock_a_job_still_in_an_active_status(
    job_service, channel, session_factory,
) -> None:
    """Invariant: an ACTIVE job always has a lease owner. If a crash path ever
    reaches release_lease without recording a final state, the lease is kept so
    recover_stale_jobs can reclaim the job later."""
    job, _ = job_service.create_job(channel.id, idempotency_key="lease-guard", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        db_job.current_stage = "SCRIPT"
        db_job.locked_by = "worker-a"
        db_job.heartbeat_at = datetime.now(UTC)
        session.commit()

        release_lease(session, db_job)
        assert db_job.locked_by == "worker-a", "ACTIVE job must keep its lease"
        assert find_orphaned_active_jobs(session) == []

        apply_transition(
            db_job, JobStatus.RETRY_WAIT,
            retry_target_stage="SCRIPT", next_retry_at=datetime.now(UTC),
            failure_code="UPSTREAM_TRANSIENT", failure_summary="x",
        )
        release_lease(session, db_job)
        assert db_job.locked_by is None, "non-ACTIVE job releases normally"
