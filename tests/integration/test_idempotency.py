from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from reel_harness.core.service import JobService
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory


def test_repeated_create_job_call_returns_same_job(job_service, channel) -> None:
    job1, replay1 = job_service.create_job(channel.id, idempotency_key="key-1", topic="topic A")
    job2, replay2 = job_service.create_job(channel.id, idempotency_key="key-1", topic="a different topic entirely")
    assert job1.id == job2.id
    assert replay1 is False
    assert replay2 is True
    # the second call's topic argument must NOT overwrite the original job
    assert job2.topic == "topic A"


def test_different_idempotency_keys_create_different_jobs(job_service, channel) -> None:
    job1, _ = job_service.create_job(channel.id, idempotency_key="key-a")
    job2, _ = job_service.create_job(channel.id, idempotency_key="key-b")
    assert job1.id != job2.id


def test_concurrent_create_job_with_same_key_creates_exactly_one_job(tmp_path, channel) -> None:
    # Each thread gets its own session-factory-backed JobService pointed at the
    # same sqlite file, simulating multiple API request handlers racing on the
    # same idempotency key -- the unique constraint + IntegrityError fallback in
    # JobService.create_job is what has to make this safe.
    db_path = tmp_path / "concurrent.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    service = JobService(session_factory)
    shared_channel = service.create_channel(name="race-channel", niche="x", language="en")

    def _create():
        job, replay = service.create_job(shared_channel.id, idempotency_key="race-key")
        return job.id

    with ThreadPoolExecutor(max_workers=8) as pool:
        job_ids = list(pool.map(lambda _: _create(), range(16)))

    assert len(set(job_ids)) == 1
