"""Repository-level parity between SQLite and PostgreSQL (Phase 6A-1) --
same schema, same lease-claiming behavior, same backup/restore contract,
across both backends supported by db.schema.create_engine_from_url.

The PostgreSQL cases are skipped unless REEL_HARNESS_TEST_POSTGRES_URL is
set (e.g. every Windows dev run and most CI legs) -- see
docs/OPERATIONS.md for how to point this at a real instance.
test_concurrent_claim_exactly_one_worker_wins is the concrete verification
for the concurrent-claim behavior the Phase 6A architecture audit flagged
as "reasoned about, never verified": SQLite serializes writers at the file
level regardless of what the application code does, so a claim race only
proves something meaningful against a database with real row-level
locking.
"""
from __future__ import annotations

import tempfile
import threading
from pathlib import Path

from reel_harness.core.service import JobService
from reel_harness.db.models import Job
from reel_harness.db.schema import SCHEMA_VERSION, create_engine_from_url, make_session_factory
from reel_harness.ops.db_tools import db_backup, db_restore, db_status, db_verify
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.lease import lease_next_job


def test_schema_reaches_current_version_on_both_backends(db_backend) -> None:
    url, engine = db_backend
    status = db_status(engine, url)
    assert status.current_schema_version == SCHEMA_VERSION
    assert status.pending_migrations == []

    session_factory = make_session_factory(engine)
    result = db_verify(engine, session_factory)
    assert result.ok is True


def test_job_lifecycle_round_trips_on_both_backends(db_backend, tmp_path) -> None:
    url, engine = db_backend
    session_factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(session_factory, storage=storage)
    channel = job_service.create_channel(name="parity-channel", niche="cooking", language="en")
    job, is_replay = job_service.create_job(channel.id, idempotency_key="parity-job", topic="t")
    assert is_replay is False

    with session_factory() as session:
        leased = lease_next_job(session, worker_id="parity-worker")
        assert leased is not None
        assert leased.id == job.id
        assert leased.locked_by == "parity-worker"


def test_backup_restore_round_trips_on_both_backends(db_backend, tmp_path) -> None:
    """Same manifest contract, same checksum verification, regardless of
    which backend produced the backup -- SQLite via sqlite3's online
    backup API, PostgreSQL via a real pg_dump/pg_restore round trip
    (requires pg_dump/pg_restore on PATH alongside
    REEL_HARNESS_TEST_POSTGRES_URL)."""
    url, engine = db_backend
    session_factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(session_factory, storage=storage)
    channel = job_service.create_channel(name="backup-channel", niche="cooking", language="en")
    job_service.create_job(channel.id, idempotency_key="backup-job", topic="t")

    backup_result = db_backup(url, tmp_path / "backups")
    assert backup_result["schema_version"] == SCHEMA_VERSION

    restore_result = db_restore(
        url, backup_result["path"], confirm_restore=True, session_factory=session_factory,
        lease_timeout_seconds=60, pre_restore_backup_dir=tmp_path / "pre_restore", engine=engine,
    )
    assert restore_result["restored"] is True

    # A brand-new engine, since a --clean pg_restore (or the SQLite file
    # swap) can leave the caller's own pooled connections stale.
    verify_engine = create_engine_from_url(url)
    try:
        verify_session_factory = make_session_factory(verify_engine)
        with verify_session_factory() as session:
            jobs = session.query(Job).filter(Job.idempotency_key == "backup-job").all()
            assert len(jobs) == 1
    finally:
        verify_engine.dispose()


def test_concurrent_claim_exactly_one_worker_wins(postgres_engine) -> None:
    """Two real threads, each with its own Session/connection, race
    lease_next_job() for the SAME job against a REAL PostgreSQL server.
    Only one may win: the guarded UPDATE ... WHERE locked_by IS NULL
    relies on PostgreSQL's row-level locking, not on SQLite's whole-file
    writer serialization, to make that true."""
    session_factory = make_session_factory(postgres_engine)
    with tempfile.TemporaryDirectory() as tmp:
        storage = LocalFilesystemStorage(Path(tmp) / "jobs")
        job_service = JobService(session_factory, storage=storage)
        channel = job_service.create_channel(name="race-channel", niche="cooking", language="en")
        job, _ = job_service.create_job(channel.id, idempotency_key="race-job", topic="t")

    winners: list[str] = []
    barrier = threading.Barrier(2)

    def _attempt(worker_id: str) -> None:
        barrier.wait(timeout=10)
        with session_factory() as session:
            leased = lease_next_job(session, worker_id=worker_id)
            if leased is not None:
                winners.append(worker_id)

    threads = [threading.Thread(target=_attempt, args=(f"worker-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert len(winners) == 1
    with session_factory() as session:
        refreshed = session.get(Job, job.id)
        assert refreshed.locked_by == winners[0]
