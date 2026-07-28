"""Schema v3 -> v4 additive migration (asset provenance history columns) and
append-only Asset persistence across a reject/retry. No network."""
from __future__ import annotations

from sqlalchemy import text

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Asset, Job
from reel_harness.db.schema import SCHEMA_VERSION, create_engine_from_url, init_db
from reel_harness.worker.runner import run_job


def test_schema_version_is_at_least_4() -> None:
    # >=4 rather than ==4: later phases add their own additive schema bumps
    # (see db/schema.py) without invalidating this v3->v4 migration's own
    # concern, which is that the v4 asset-provenance columns exist.
    assert SCHEMA_VERSION >= 4


def test_v3_shaped_database_upgrades_without_losing_existing_asset_rows(tmp_path) -> None:
    """Builds a database shaped like a pre-v4 install (assets table without any
    of the provenance columns, one already-persisted row) and proves init_db()
    upgrades it in place: the new columns exist, the legacy row is still
    readable, and it defaults to (attempt_number=1, is_current=True) so it
    reads as a single current attempt with no history gap."""
    db_path = tmp_path / "legacy.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")

    # Build the pre-v4 schema shape directly (bypassing the current models),
    # then insert one row exactly as v3 code would have.
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE channels (
                id VARCHAR PRIMARY KEY, name VARCHAR, niche VARCHAR, language VARCHAR,
                style_preset JSON, auto_approve BOOLEAN, created_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE jobs (
                id VARCHAR PRIMARY KEY, channel_id VARCHAR, idempotency_key VARCHAR, topic VARCHAR,
                script JSON, status VARCHAR, current_stage VARCHAR, attempt_number INTEGER,
                retry_count INTEGER, retry_target_stage VARCHAR, next_retry_at DATETIME,
                failure_code VARCHAR, failure_summary VARCHAR, reason_code VARCHAR,
                cancel_requested BOOLEAN, parent_job_id VARCHAR, locked_by VARCHAR,
                heartbeat_at DATETIME, created_at DATETIME, updated_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE stage_runs (
                id VARCHAR PRIMARY KEY, job_id VARCHAR, stage VARCHAR, attempt INTEGER,
                status VARCHAR, error_detail VARCHAR, started_at DATETIME, finished_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE approval_decisions (
                id VARCHAR PRIMARY KEY, job_id VARCHAR, decision VARCHAR, reason VARCHAR,
                regenerate_from_stage VARCHAR, decided_at DATETIME
            )
        """))
        conn.execute(text("""
            CREATE TABLE assets (
                id VARCHAR PRIMARY KEY, job_id VARCHAR, scene_index INTEGER, source_provider VARCHAR,
                source_url VARCHAR, author VARCHAR, license_type VARCHAR, local_path VARCHAR,
                checksum_sha256 VARCHAR, mime_type VARCHAR, downloaded_at DATETIME
            )
        """))
        conn.execute(text("""
            INSERT INTO channels VALUES ('c1', 'ch', 'n', 'en', '{}', 0, '2026-01-01 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO jobs (id, channel_id, idempotency_key, status, attempt_number, retry_count,
                cancel_requested, created_at, updated_at)
            VALUES ('j1', 'c1', 'k1', 'CREATED', 1, 0, 0, '2026-01-01 00:00:00', '2026-01-01 00:00:00')
        """))
        conn.execute(text("""
            INSERT INTO assets (id, job_id, scene_index, source_provider, source_url, author,
                license_type, local_path, checksum_sha256, mime_type, downloaded_at)
            VALUES ('a1', 'j1', 0, 'fake', 'fake://asset/0', 'Legacy Author', 'FAKE_TEST_LICENSE',
                '/tmp/legacy.png', 'deadbeef', 'image/png', '2026-01-01 00:00:00')
        """))

    # This is the upgrade under test: init_db() on the pre-existing v3-shaped file.
    init_db(engine)

    with engine.begin() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(assets)"))}
        for expected in (
            "attempt_number", "is_current", "provider_asset_id", "query_text", "selection_score",
            "source_page_url", "creator_url", "commercial_use_allowed", "modification_allowed",
            "attribution_text", "width", "height", "duration_sec", "fps", "request_id",
        ):
            assert expected in columns, f"missing column after upgrade: {expected}"
        version = conn.execute(text("SELECT version FROM schema_migrations")).scalar_one()
        assert version == SCHEMA_VERSION

    from reel_harness.db.schema import make_session_factory

    session_factory = make_session_factory(engine)
    with session_factory() as session:
        legacy = session.get(Asset, "a1")
        assert legacy is not None
        assert legacy.attempt_number == 1
        assert legacy.is_current is True
        assert legacy.provider_asset_id is None
        assert legacy.checksum_sha256 == "deadbeef"  # pre-existing data untouched


def test_reject_and_retry_of_asset_stage_preserves_prior_attempt_as_history(
    job_service, channel, session_factory, storage, fake_providers,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="prov-1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value

    with session_factory() as session:
        first_attempt = session.execute(
            Asset.__table__.select().where(Asset.job_id == job.id),
        ).fetchall()
    assert len(first_attempt) == 3
    assert all(row.attempt_number == 1 and row.is_current for row in first_attempt)

    job_service.reject(job.id, reason="wrong vibe", regenerate_from_stage="ASSET")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, fake_providers, storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value

    with session_factory() as session:
        all_rows = session.execute(
            Asset.__table__.select().where(Asset.job_id == job.id).order_by(Asset.attempt_number),
        ).fetchall()
    assert len(all_rows) == 6, "both attempts must still exist -- history is append-only"
    attempt_1 = [r for r in all_rows if r.attempt_number == 1]
    attempt_2 = [r for r in all_rows if r.attempt_number == 2]
    assert len(attempt_1) == 3 and all(not r.is_current for r in attempt_1)
    assert len(attempt_2) == 3 and all(r.is_current for r in attempt_2)

    # Rendering/resume only ever sees the current attempt.
    from reel_harness.worker.runner import _restore_assets

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        restored = _restore_assets(session, db_job)
        assert len(restored) == 3
