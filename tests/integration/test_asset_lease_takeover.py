"""ASSET-stage lease takeover: worker A stalls mid-search, its lease expires
and is reclaimed, worker B finishes the ASSET stage (and the rest of the job)
for real; when A finally wakes up and tries to commit, the fenced commit
refuses and A never touches the official assets directory or the Asset table.
Mirrors tests/integration/test_multi_worker.py's TTS takeover scenario.
"""
from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Asset, Job, StageRun
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.base import MediaCandidate
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.lease import find_orphaned_active_jobs, lease_next_job, recover_stale_jobs, release_lease
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available


class _GatedStockMediaProvider:
    """Real FakeStockMediaProvider behavior, but the first search() call
    blocks until released -- simulating a worker stuck in a long provider
    call while its lease expires."""

    provider_id = "fake"

    def __init__(self) -> None:
        self._inner = FakeStockMediaProvider()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked_once = False

    def search(self, query: str, orientation: str, min_duration: float, **kwargs) -> list[MediaCandidate]:
        if not self._blocked_once:
            self._blocked_once = True
            self.entered.set()
            assert self.release.wait(timeout=60), "test forgot to release the gated asset provider"
        return self._inner.search(query, orientation, min_duration, **kwargs)

    def download(self, candidate: MediaCandidate, dest_dir: Path):
        return self._inner.download(candidate, dest_dir)


def test_expired_lease_during_asset_search_is_taken_over_and_late_worker_fenced_out(tmp_path) -> None:
    db_path = tmp_path / "asset-takeover.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    service = JobService(session_factory)
    channel = service.create_channel(name="mw-asset", niche="cooking", language="en")
    job, _ = service.create_job(channel.id, idempotency_key="asset-takeover-1", topic="takeover test")

    storage = LocalFilesystemStorage(tmp_path / "jobs")

    gated = _GatedStockMediaProvider()
    providers_a = ProviderBundle(llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=gated)

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
        assert gated.entered.wait(timeout=30), "worker A never reached ASSET search"

        future = datetime.now(UTC) + timedelta(hours=1)
        with session_factory() as session:
            recovered = recover_stale_jobs(session, lease_timeout_seconds=60, now=future)
            assert recovered == [job.id]

            leased_b = lease_next_job(session, worker_id="worker-b", now=future + timedelta(minutes=1))
            assert leased_b is not None and leased_b.id == job.id
            token_b = leased_b.lease_token
            assert token_b is not None and token_b != token_a

            bundle_b = ProviderBundle(
                llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider(),
            )
            run_job(session, leased_b, channel, bundle_b, storage, lease_token=token_b)
            status_after_b = leased_b.status
            release_lease(session, leased_b, lease_token=token_b)
    finally:
        # Worker A wakes up late and tries to commit its ASSET success: the
        # fenced commit must refuse before any official file is touched.
        gated.release.set()
        thread_a.join(timeout=60)
    assert not thread_a.is_alive()

    with session_factory() as session:
        final_job = session.get(Job, job.id)
        assert final_job.status == status_after_b
        assert final_job.lease_token is None

        runs = session.execute(
            select(StageRun.stage, StageRun.attempt, StageRun.status)
            .where(StageRun.job_id == job.id)
            .order_by(StageRun.started_at),
        ).all()
        asset_runs = sorted((attempt, status) for stage, attempt, status in runs if stage == "ASSET")
        assert asset_runs[0] == (1, "lease_lost")
        assert (2, "success") in asset_runs
        for stage in {s for s, _, _ in runs}:
            attempts = [a for s, a, _ in runs if s == stage]
            assert len(attempts) == len(set(attempts)), f"duplicate attempt numbers in {stage}"
        assert find_orphaned_active_jobs(session) == []

        # Only worker B's Asset rows exist -- A's attempt never reached the DB.
        assets = session.execute(select(Asset).where(Asset.job_id == job.id)).scalars().all()
        assert len(assets) == 3  # SCRIPT_JSON-equivalent fake script scene count
        for asset_row in assets:
            on_disk = Path(asset_row.local_path)
            assert on_disk.is_file()
            assert hashlib.sha256(on_disk.read_bytes()).hexdigest() == asset_row.checksum_sha256

    # No worker-private temp asset trees linger from either worker.
    assert list(storage.job_dir(job.id).glob("assets-inprogress-*")) == [], (
        "worker-private asset temp trees must not linger"
    )

    if FFMPEG_PRESENT:
        assert status_after_b == JobStatus.REVIEW_REQUIRED.value
    else:
        assert status_after_b == JobStatus.FAILED.value
