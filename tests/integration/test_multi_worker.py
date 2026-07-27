"""Multi-worker lease expiry and takeover, on a file-based SQLite DB with real
separate sessions and a real second thread for the losing worker.

The core scenario: worker A stalls mid-TTS, its lease times out and is
reclaimed, worker B takes over with a fresh token and finishes the job for
real; when A finally wakes up and tries to commit its stage result, the fenced
commit refuses -- only B's results reach the official status, StageRuns, and
manifest.
"""
from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job, StageRun
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.base import TTSResult
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider
from reel_harness.worker.heartbeat import LeaseHeartbeat
from reel_harness.worker.lease import (
    find_orphaned_active_jobs,
    lease_next_job,
    recover_stale_jobs,
    release_lease,
)
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _fake_bundle() -> ProviderBundle:
    return ProviderBundle(llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider())


def test_healthy_heartbeats_prevent_recovery_and_second_lease(
    job_service, channel, session_factory,
) -> None:
    """Scenario 1: worker A holds the lease and heartbeats; stale recovery must
    not reclaim the job and worker B must not be able to lease it."""
    job, _ = job_service.create_job(channel.id, idempotency_key="hb-protect", topic="t")
    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        token = leased.lease_token

    heartbeat = LeaseHeartbeat(session_factory, job.id, token, interval_seconds=0.05)
    with heartbeat:
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        while heartbeat.beat_count < 2 and datetime.now(UTC) < deadline:
            threading.Event().wait(0.02)
        assert heartbeat.beat_count >= 2

        with session_factory() as session:
            # Even with an aggressive 1-second timeout the freshly-heartbeated
            # job must not be recovered...
            recovered = recover_stale_jobs(session, lease_timeout_seconds=1)
            assert recovered == []
            # ...and worker B cannot lease it while A holds the lock.
            assert lease_next_job(session, worker_id="worker-b") is None

    with session_factory() as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.locked_by == "worker-a"
        assert refreshed.lease_token == token


class _GatedTTSProvider:
    """Real FakeTTSProvider behavior, but the first synthesize() call blocks
    until released -- simulating a worker stuck in a long provider call while
    its lease expires."""

    provider_id = "fake"

    def __init__(self) -> None:
        self._inner = FakeTTSProvider()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked_once = False

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        if not self._blocked_once:
            self._blocked_once = True
            self.entered.set()
            assert self.release.wait(timeout=60), "test forgot to release the gated TTS provider"
        return self._inner.synthesize(text, voice_id, lang, dest_dir)


def test_expired_lease_is_taken_over_and_the_late_worker_is_fenced_out(tmp_path) -> None:
    db_path = tmp_path / "takeover.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    service = JobService(session_factory)
    channel = service.create_channel(name="mw", niche="cooking", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="takeover-1", topic="takeover test")

    from reel_harness.storage.local import LocalFilesystemStorage

    storage = LocalFilesystemStorage(tmp_path / "jobs")

    gated_tts = _GatedTTSProvider()
    providers_a = ProviderBundle(
        llm=FakeLLMProvider(), tts=gated_tts, stock_media=FakeStockMediaProvider(),
    )

    with session_factory() as session:
        leased = lease_next_job(session, worker_id="worker-a")
        token_a = leased.lease_token
        assert token_a is not None

    def worker_a() -> None:
        with session_factory() as session:
            job_a = session.get(Job, job.id)
            run_job(session, job_a, channel, providers_a, storage, lease_token=token_a)

    thread_a = threading.Thread(target=worker_a, name="worker-a")
    thread_a.start()
    try:
        assert gated_tts.entered.wait(timeout=30), "worker A never reached TTS"

        # Worker A is stalled inside TTS with no heartbeat. Its lease expires;
        # recovery reclaims the job and rotates the token.
        future = datetime.now(UTC) + timedelta(hours=1)
        with session_factory() as session:
            recovered = recover_stale_jobs(session, lease_timeout_seconds=60, now=future)
            assert recovered == [job.id]

            # Recovery scheduled the retry with a short backoff after `future`;
            # lease as worker B once that backoff has elapsed.
            leased_b = lease_next_job(session, worker_id="worker-b", now=future + timedelta(minutes=1))
            assert leased_b is not None and leased_b.id == job.id
            token_b = leased_b.lease_token
            assert token_b is not None and token_b != token_a

            run_job(session, leased_b, channel, _fake_bundle(), storage, lease_token=token_b)
            status_after_b = leased_b.status
            release_lease(session, leased_b, lease_token=token_b)

        if FFMPEG_PRESENT:
            assert status_after_b == JobStatus.REVIEW_REQUIRED.value
            official_manifest = json.loads(storage.read_bytes(job.id, "manifest.json"))
            checksum_b = official_manifest["final_video_checksum_sha256"]
            assert checksum_b is not None
        else:
            assert status_after_b == JobStatus.FAILED.value
            checksum_b = None
    finally:
        # Worker A wakes up late and tries to commit its TTS success: the
        # fenced commit must refuse.
        gated_tts.release.set()
        thread_a.join(timeout=60)
    assert not thread_a.is_alive()

    with session_factory() as session:
        final_job = session.get(Job, job.id)
        # A's late write changed nothing: the job still shows B's outcome.
        assert final_job.status == status_after_b
        assert final_job.lease_token is None  # B released; A's stale token could not resurrect anything

        runs = session.execute(
            select(StageRun.stage, StageRun.attempt, StageRun.status)
            .where(StageRun.job_id == job.id)
            .order_by(StageRun.started_at),
        ).all()
        tts_runs = sorted((attempt, status) for stage, attempt, status in runs if stage == "TTS")
        # A's abandoned attempt 1 is closed as lease_lost; B's attempt 2 is the
        # only success. No duplicate attempt numbers on any stage.
        assert tts_runs[0] == (1, "lease_lost")
        assert (2, "success") in tts_runs or not FFMPEG_PRESENT
        for stage in {s for s, _, _ in runs}:
            attempts = [a for s, a, _ in runs if s == stage]
            assert len(attempts) == len(set(attempts)), f"duplicate attempt numbers in {stage}"

        assert find_orphaned_active_jobs(session) == []

    if FFMPEG_PRESENT:
        # The official final.mp4 still matches B's manifest checksum -- A never
        # touched the official output.
        import hashlib

        final_path = storage.job_dir(job.id) / "final" / "final.mp4"
        assert hashlib.sha256(final_path.read_bytes()).hexdigest() == checksum_b
        leftovers = list((storage.job_dir(job.id) / "final").glob("final-inprogress-*.mp4"))
        assert leftovers == [], "worker-private temp renders must not linger"


def test_release_with_rotated_token_cannot_release_new_owner(
    job_service, channel, session_factory,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="rel-guard", topic="t")
    now = datetime.now(UTC)
    with session_factory() as session:
        leased_a = lease_next_job(session, worker_id="worker-a", now=now)
        token_a = leased_a.lease_token
        # A crashes; recovery reclaims; B leases with a new token.
        from reel_harness.core.state_machine import apply_transition

        apply_transition(leased_a, JobStatus.SCRIPT_GENERATING)
        leased_a.current_stage = "SCRIPT"
        leased_a.heartbeat_at = now - timedelta(seconds=999)
        session.commit()
        recover_stale_jobs(session, lease_timeout_seconds=60, now=now)

        leased_b = lease_next_job(session, worker_id="worker-b", now=now + timedelta(hours=1))
        assert leased_b is not None
        token_b = leased_b.lease_token

        # A's stale release attempt must not strip B's lease. (The job is in
        # RETRY_WAIT here -- a non-ACTIVE status -- so only the token guard
        # protects it.)
        release_lease(session, leased_b, lease_token=token_a)
        session.expire(leased_b)
        assert leased_b.locked_by == "worker-b"
        assert leased_b.lease_token == token_b
