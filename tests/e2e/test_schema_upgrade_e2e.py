"""Upgrade E2E: a real DB shaped like an earlier schema version is
migrated forward to the current schema via `db_migrate`, with data
preservation, a fresh fake job+publication driven through after
migration, and a simulated "restart" (a brand-new engine/session_factory
against the same file) all verified for real -- no mocking of SQLAlchemy
or SQLite.

Historical DBs are built by taking the CURRENT (v7) schema via
Base.metadata.create_all() and DROPPING exactly the columns/tables
db.schema._ADDITIVE_COLUMNS records as added after the target version
(SQLite 3.35+ supports ALTER TABLE ... DROP COLUMN) -- this exercises the
real _ensure_column() ADD-COLUMN migration path against a DB that is
structurally a genuine subset of the current one, the same shape
_ensure_column() itself was written to handle. Four starting points are
covered, corresponding to the phases named in the Phase 4A spec:
Phase 1 (v1, pre-lease-fencing), Phase 2B (v3, provider_config just
added), Phase 3A (v5, publications tables just created), and Phase 3D /
current (v7, already up to date -- a real no-op migration)."""
from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from reel_harness.db.models import Base
from reel_harness.db.schema import (
    SCHEMA_VERSION,
    create_engine_from_url,
    init_db,
    make_session_factory,
)
from reel_harness.ops.db_tools import db_backup, db_migrate, db_status, db_verify

# (table, column) pairs added after v1 -- mirrors db.schema._ADDITIVE_COLUMNS
# exactly, used to strip a fresh v7 DB down to each historical shape.
_COLUMNS_ADDED_AFTER_V1 = [
    ("jobs", "lease_token"), ("jobs", "provider_config"),
    ("assets", "attempt_number"), ("assets", "is_current"), ("assets", "provider_asset_id"),
    ("assets", "query_text"), ("assets", "selection_score"), ("assets", "source_page_url"),
    ("assets", "creator_url"), ("assets", "commercial_use_allowed"), ("assets", "modification_allowed"),
    ("assets", "attribution_text"), ("assets", "width"), ("assets", "height"),
    ("assets", "duration_sec"), ("assets", "fps"), ("assets", "request_id"),
    ("publications", "metadata_fingerprint"),
    ("publications", "processing_started_at"), ("publications", "next_poll_at"),
    ("publications", "processing_poll_count"),
]
_COLUMNS_ADDED_AFTER_V3 = _COLUMNS_ADDED_AFTER_V1[2:]  # everything except lease_token/provider_config
_COLUMNS_ADDED_AFTER_V5 = [c for c in _COLUMNS_ADDED_AFTER_V1 if c[0] == "publications"]


def _build_legacy_db(db_path, target_version: int) -> None:
    """Builds a real SQLite file at db_path shaped like `target_version`."""
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)  # full current (v7) shape

    if target_version == 1:
        drop_columns = _COLUMNS_ADDED_AFTER_V1
        drop_tables = ("publications", "publication_audit_events")
    elif target_version == 3:
        drop_columns = _COLUMNS_ADDED_AFTER_V3
        drop_tables = ("publications", "publication_audit_events")
    elif target_version == 5:
        drop_columns = _COLUMNS_ADDED_AFTER_V5
        drop_tables = ()
    else:
        raise ValueError(f"no legacy fixture defined for version {target_version}")

    with engine.begin() as conn:
        for table in drop_tables:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
        for table, column in drop_columns:
            if table in drop_tables:
                continue
            conn.execute(text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL)"))
        conn.execute(text("DELETE FROM schema_migrations"))
        conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": target_version})
    engine.dispose()


def _seed_legacy_data(db_path, target_version: int) -> dict:
    """Inserts one channel + job (+ publication, if the table exists at
    this version) using only the columns real at that version. Returns
    the ids so the test can confirm they survive the migration."""
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        channel_id = session.execute(text(
            "INSERT INTO channels (id, name, niche, language, style_preset, auto_approve, created_at) VALUES "
            "(lower(hex(randomblob(16))), 'legacy channel', 'cooking', 'en', '{}', 0, CURRENT_TIMESTAMP) "
            "RETURNING id"
        )).scalar_one()
        job_id = session.execute(text(
            "INSERT INTO jobs (id, channel_id, idempotency_key, status, attempt_number, retry_count, "
            "cancel_requested, created_at, updated_at) VALUES "
            "(lower(hex(randomblob(16))), :cid, 'legacy-job', 'COMPLETED', 1, 0, 0, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id"
        ), {"cid": channel_id}).scalar_one()
        publication_id = None
        if target_version >= 5:
            checksum = hashlib.sha256(b"legacy final video bytes").hexdigest()
            publication_id = session.execute(text(
                "INSERT INTO publications (id, job_id, provider, account_reference, status, privacy_status, "
                "idempotency_key, final_video_checksum, bytes_uploaded, retry_count, cancel_requested, "
                "created_at, updated_at) "
                "VALUES (lower(hex(randomblob(16))), :jid, 'youtube', 'default', 'PUBLISHED', 'private', "
                "'legacy-pub', :checksum, 0, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id"
            ), {"jid": job_id, "checksum": checksum}).scalar_one()
        session.commit()
    engine.dispose()
    return {"channel_id": channel_id, "job_id": job_id, "publication_id": publication_id}


@pytest.mark.parametrize("legacy_version", [1, 3, 5])
def test_upgrade_preserves_data_and_reaches_current_schema(legacy_version, tmp_path) -> None:
    db_path = tmp_path / f"legacy_v{legacy_version}.db"
    _build_legacy_db(db_path, legacy_version)
    ids = _seed_legacy_data(db_path, legacy_version)

    database_url = f"sqlite:///{db_path}"
    engine = create_engine_from_url(database_url)

    # 1. backup (of the pre-migration legacy DB)
    backup_result = db_backup(database_url, tmp_path / "backups")
    assert backup_result["schema_version"] == legacy_version

    # 2. migrate
    status_before = db_status(engine, database_url)
    assert status_before.current_schema_version == legacy_version
    assert status_before.pending_migrations  # something really is pending
    migrate_result = db_migrate(engine, database_url, dry_run=False, backup_dir=tmp_path / "backups")
    assert migrate_result["applied"] is True

    # 3. verify
    status_after = db_status(engine, database_url)
    assert status_after.current_schema_version == SCHEMA_VERSION
    assert status_after.pending_migrations == []
    session_factory = make_session_factory(engine)
    verify_result = db_verify(engine, session_factory)
    assert verify_result.ok is True

    # 4 & 5. existing jobs/publications still queryable, with real data intact
    with session_factory() as session:
        from reel_harness.db.models import Job, Publication

        job = session.get(Job, ids["job_id"])
        assert job is not None
        assert job.status == "COMPLETED"
        assert job.idempotency_key == "legacy-job"
        if ids["publication_id"] is not None:
            pub = session.get(Publication, ids["publication_id"])
            assert pub is not None
            assert pub.status == "PUBLISHED"
            assert pub.idempotency_key == "legacy-pub"

    # 6. a brand-new fake job runs through the current pipeline for real
    from reel_harness.core.service import JobService
    from reel_harness.storage.local import LocalFilesystemStorage
    from reel_harness.worker.runner import ProviderBundle, run_job

    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(session_factory, storage=storage)
    with session_factory() as session:
        from reel_harness.db.models import Channel

        channel = session.get(Channel, ids["channel_id"])
        assert channel is not None
        new_job, _ = job_service.create_job(ids["channel_id"], idempotency_key="post-migration-job", topic="t")

    from reel_harness.providers.fake_llm import FakeLLMProvider
    from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
    from reel_harness.providers.fake_tts import FakeTTSProvider

    providers = ProviderBundle(llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider())
    with session_factory() as session:
        from reel_harness.db.models import Job

        db_job = session.get(Job, new_job.id)
        db_channel = session.get(Channel, ids["channel_id"])
        run_job(session, db_job, db_channel, providers, storage)
        assert db_job.status in ("REVIEW_REQUIRED", "COMPLETED")

    # 7. a new publication row against the freshly-rendered job -- inserted
    # directly (not through PublicationService.create_publication) because
    # eligibility is deliberately, permanently unreachable for a
    # Fake-provider job (FAKE_TEST_LICENSE never passes
    # ASSET_LICENSE_NOT_PUBLISHABLE, by design -- see CLAUDE.md); that
    # business rule is covered elsewhere. What this step actually needs
    # to prove is that the MIGRATED publications table accepts a real
    # new write with the full current column set, which a direct ORM
    # insert demonstrates just as validly.
    from reel_harness.db.models import Publication

    with session_factory() as session:
        new_pub = Publication(
            job_id=new_job.id, provider="fake", account_reference="default", status="CREATED",
            privacy_status="private", idempotency_key="post-migration-pub",
            final_video_checksum=hashlib.sha256(b"post-migration video").hexdigest(),
        )
        session.add(new_pub)
        session.commit()
        new_pub_id = new_pub.id

    # 8. "restart" -- a brand-new engine/session_factory against the same file
    engine.dispose()
    restarted_engine = create_engine_from_url(database_url)
    restarted_session_factory = make_session_factory(restarted_engine)

    # 9. data persisted across the "restart"
    with restarted_session_factory() as session:
        from reel_harness.db.models import Job as JobModel2
        from reel_harness.db.models import Publication as PublicationModel2

        assert session.get(JobModel2, ids["job_id"]) is not None
        assert session.get(JobModel2, new_job.id) is not None
        assert session.get(PublicationModel2, new_pub_id) is not None
    restarted_engine.dispose()


def test_upgrade_dry_run_leaves_legacy_db_completely_untouched(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    _build_legacy_db(db_path, 3)
    ids = _seed_legacy_data(db_path, 3)
    database_url = f"sqlite:///{db_path}"
    engine = create_engine_from_url(database_url)

    before_bytes = db_path.read_bytes()
    result = db_migrate(engine, database_url, dry_run=True)
    assert result["applied"] is False
    assert db_path.read_bytes() == before_bytes  # byte-for-byte untouched

    status = db_status(engine, database_url)
    assert status.current_schema_version == 3
    assert ids["job_id"]  # sanity: fixture actually seeded something
    engine.dispose()


def test_upgrade_at_current_schema_is_a_real_no_op(tmp_path) -> None:
    """The fourth starting point from the spec -- a DB already at the
    latest (Phase 3D / v7) schema -- migrates as a genuine no-op, not
    just "nothing to assert"."""
    db_path = tmp_path / "current.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    database_url = f"sqlite:///{db_path}"

    before_bytes = db_path.read_bytes()
    result = db_migrate(engine, database_url, dry_run=False, backup_dir=tmp_path / "backups")
    assert result["applied"] is True
    assert result["pending_migrations"] == []
    assert result["backup_path"] is None  # no-op path never takes a needless backup
    assert db_path.read_bytes() == before_bytes
    engine.dispose()
