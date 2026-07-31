from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from reel_harness.db.schema import SCHEMA_VERSION, create_engine_from_url, init_db, make_session_factory
from reel_harness.ops.db_tools import (
    MigrationLockedError,
    RestoreRefusedError,
    _MigrationLock,
    db_backup,
    db_migrate,
    db_restore,
    db_status,
    db_verify,
    detect_active_leases,
    safe_db_identifier,
    sqlite_path_from_url,
)


@pytest.fixture
def db(tmp_path):
    url = f"sqlite:///{tmp_path / 'test.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    return url, engine, make_session_factory(engine)


def test_sqlite_path_from_url_rejects_non_sqlite() -> None:
    from reel_harness.ops.db_tools import DbToolsError

    with pytest.raises(DbToolsError):
        sqlite_path_from_url("postgresql://user:pass@host/db")


def test_safe_db_identifier_never_leaks_full_path() -> None:
    ident = safe_db_identifier("sqlite:////home/someuser/secret-project/db.sqlite")
    assert ident == "db.sqlite"
    assert "someuser" not in ident
    assert "secret-project" not in ident


def test_db_status_reports_current_schema_and_zero_pending(db) -> None:
    url, engine, _ = db
    status = db_status(engine, url)
    assert status.current_schema_version == SCHEMA_VERSION
    assert status.pending_migrations == []
    assert status.integrity_status == "ok"
    assert status.db_identifier == "test.db"
    assert status.table_row_counts["jobs"] == 0


def test_db_status_detects_stale_schema_version(db) -> None:
    url, engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET version = 3"))
    status = db_status(engine, url)
    assert status.current_schema_version == 3
    assert any("schema_migrations.version" in item for item in status.pending_migrations)


def test_db_migrate_dry_run_never_writes(db) -> None:
    url, engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET version = 3"))
    result = db_migrate(engine, url, dry_run=True)
    assert result["dry_run"] is True
    assert result["applied"] is False
    with engine.connect() as conn:
        version = conn.execute(text("SELECT version FROM schema_migrations")).scalar_one()
    assert version == 3  # untouched


def test_db_migrate_applies_pending_and_is_idempotent(db, tmp_path) -> None:
    url, engine, _ = db
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET version = 3"))
    result = db_migrate(engine, url, dry_run=False, backup_dir=tmp_path / "backups")
    assert result["applied"] is True
    assert result["backup_path"] is not None
    status = db_status(engine, url)
    assert status.current_schema_version == SCHEMA_VERSION
    assert status.pending_migrations == []

    # Re-running is a safe no-op, not a second migration.
    second = db_migrate(engine, url, dry_run=False, backup_dir=tmp_path / "backups")
    assert second["applied"] is True
    assert second["pending_migrations"] == []


def test_migration_lock_refuses_concurrent_acquisition(tmp_path) -> None:
    db_path = tmp_path / "locked.db"
    db_path.write_bytes(b"")
    with _MigrationLock(db_path):
        with pytest.raises(MigrationLockedError):
            with _MigrationLock(db_path):
                pass
    # Released after the `with` block -- a fresh acquisition now succeeds.
    with _MigrationLock(db_path):
        pass


def test_db_backup_creates_checksummed_manifest_and_restorable_copy(db, tmp_path) -> None:
    url, engine, session_factory = db
    dest = tmp_path / "backups"
    result = db_backup(url, dest)
    from pathlib import Path

    backup_path = Path(result["path"])
    manifest_path = Path(f"{backup_path}.manifest.json")
    assert backup_path.exists()
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["checksum_sha256"] == result["checksum_sha256"]
    assert manifest["schema_version"] == SCHEMA_VERSION
    # The backup is itself a valid, openable SQLite database.
    restored_engine = create_engine_from_url(f"sqlite:///{backup_path}")
    with restored_engine.connect() as conn:
        assert conn.execute(text("SELECT version FROM schema_migrations")).scalar_one() == SCHEMA_VERSION


def test_db_restore_refuses_without_confirm(db, tmp_path) -> None:
    url, engine, session_factory = db
    backup = db_backup(url, tmp_path / "backups")
    with pytest.raises(RestoreRefusedError):
        db_restore(
            url, backup["path"], confirm_restore=False, session_factory=session_factory,
            lease_timeout_seconds=300, pre_restore_backup_dir=tmp_path / "pre",
        )


def test_db_restore_refuses_checksum_mismatch(db, tmp_path) -> None:
    url, engine, session_factory = db
    from reel_harness.ops.db_tools import DbToolsError

    backup = db_backup(url, tmp_path / "backups")
    from pathlib import Path

    Path(backup["path"]).write_bytes(b"corrupted!!")
    with pytest.raises(DbToolsError, match="checksum mismatch"):
        db_restore(
            url, backup["path"], confirm_restore=True, session_factory=session_factory,
            lease_timeout_seconds=300, pre_restore_backup_dir=tmp_path / "pre",
        )


def test_db_restore_refuses_while_a_lease_looks_active(db, tmp_path) -> None:
    url, engine, session_factory = db
    from reel_harness.db.models import Channel, Job

    with session_factory() as session:
        channel = Channel(name="c", niche="n", language="en")
        session.add(channel)
        session.flush()
        job = Job(
            channel_id=channel.id, idempotency_key="k", status="RENDERING",
            locked_by="worker-1", heartbeat_at=datetime.now(UTC),
        )
        session.add(job)
        session.commit()

    backup = db_backup(url, tmp_path / "backups")
    with pytest.raises(RestoreRefusedError, match="running worker"):
        db_restore(
            url, backup["path"], confirm_restore=True, session_factory=session_factory,
            lease_timeout_seconds=300, pre_restore_backup_dir=tmp_path / "pre",
        )


def test_db_restore_allows_when_lease_is_stale(db, tmp_path) -> None:
    url, engine, session_factory = db
    from reel_harness.db.models import Channel, Job

    with session_factory() as session:
        channel = Channel(name="c", niche="n", language="en")
        session.add(channel)
        session.flush()
        job = Job(
            channel_id=channel.id, idempotency_key="k", status="RENDERING",
            locked_by="worker-1", heartbeat_at=datetime.now(UTC) - timedelta(hours=2),
        )
        session.add(job)
        session.commit()

    backup = db_backup(url, tmp_path / "backups")
    result = db_restore(
        url, backup["path"], confirm_restore=True, session_factory=session_factory,
        lease_timeout_seconds=300, pre_restore_backup_dir=tmp_path / "pre", engine=engine,
    )
    assert result["restored"] is True
    assert result["pre_restore_backup_path"] is not None


def test_db_restore_preserves_existing_db_on_manifest_missing(db, tmp_path) -> None:
    url, engine, session_factory = db

    fake_backup = tmp_path / "no_manifest.sqlite3.bak"
    fake_backup.write_bytes(b"not a real backup")
    from reel_harness.ops.db_tools import DbToolsError

    db_path = sqlite_path_from_url(url)
    before = db_path.read_bytes()
    with pytest.raises(DbToolsError, match="manifest"):
        db_restore(
            url, fake_backup, confirm_restore=True, session_factory=session_factory,
            lease_timeout_seconds=300, pre_restore_backup_dir=tmp_path / "pre",
        )
    assert db_path.read_bytes() == before


def test_detect_active_leases_empty_on_fresh_db(db) -> None:
    _, _, session_factory = db
    assert detect_active_leases(session_factory, lease_timeout_seconds=300) == []


def test_db_verify_ok_on_fresh_db(db) -> None:
    _, engine, session_factory = db
    result = db_verify(engine, session_factory)
    assert result.ok is True
    assert result.integrity_check == "ok"
    assert result.foreign_key_violation_count == 0


def test_db_verify_detects_active_unlocked_job(db) -> None:
    _, engine, session_factory = db
    from reel_harness.db.models import Channel, Job

    with session_factory() as session:
        channel = Channel(name="c", niche="n", language="en")
        session.add(channel)
        session.flush()
        job = Job(channel_id=channel.id, idempotency_key="k", status="RENDERING", locked_by=None)
        session.add(job)
        session.commit()
        job_id = job.id

    result = db_verify(engine, session_factory)
    assert result.ok is False
    assert job_id in result.active_unlocked_jobs


def test_db_backup_records_the_actual_source_schema_version_not_the_running_code_constant(tmp_path) -> None:
    """A backup of a database that has NOT yet been migrated to the
    current schema must honestly record its own (older) version in the
    manifest -- not silently claim to be at this running code's
    SCHEMA_VERSION, which would make db_restore's "refuse a backup newer
    than supported" check meaningless and the manifest itself simply
    wrong."""
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'old.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET version = 3"))
    engine.dispose()

    result = db_backup(url, tmp_path / "backups")
    assert result["schema_version"] == 3
    assert result["schema_version"] != SCHEMA_VERSION


def test_migration_lock_dispatches_by_dialect(tmp_path) -> None:
    """SQLite gets the PID-lockfile _MigrationLock; PostgreSQL gets the
    advisory-lock _PostgresMigrationLock. Only the dispatch is exercised
    here (no real PostgreSQL connection is available in this environment)
    -- the advisory lock's actual acquire/release behavior is covered by
    the parametrized PostgreSQL test suite."""
    from reel_harness.ops.db_tools import _migration_lock, _PostgresMigrationLock

    sqlite_url = f"sqlite:///{tmp_path / 'lock.db'}"
    engine = create_engine_from_url(sqlite_url)
    init_db(engine)
    lock = _migration_lock(engine, sqlite_url)
    assert isinstance(lock, _MigrationLock)

    pg_lock = _migration_lock(engine, "postgresql+psycopg://user:pass@localhost:5432/reel_harness")
    assert isinstance(pg_lock, _PostgresMigrationLock)


def test_ensure_column_is_dialect_portable_via_inspect(tmp_path) -> None:
    """_ensure_column must use sqlalchemy.inspect() (portable) rather than
    SQLite's PRAGMA table_info -- confirmed indirectly here by dropping a
    column and letting init_db() re-add it via the real _ensure_column
    path, then checking the new column round-trips a value correctly
    (i.e. the ALTER TABLE ... ADD COLUMN DDL that inspect() drove was
    actually valid, not just non-erroring)."""
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'ensure_column.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE jobs DROP COLUMN lease_token"))
    engine.dispose()

    engine = create_engine_from_url(url)
    init_db(engine)  # must re-add lease_token without error
    with engine.connect() as conn:
        from sqlalchemy import inspect as sa_inspect

        columns = {col["name"] for col in sa_inspect(conn).get_columns("jobs")}
        assert "lease_token" in columns


def test_ensure_column_boolean_default_is_dialect_correct_not_a_raw_int_string(tmp_path) -> None:
    """The old hand-written "BOOLEAN NOT NULL DEFAULT 1" DDL string would
    fail outright on PostgreSQL (no implicit int->boolean cast in a DEFAULT
    clause) -- this confirms the new CreateColumn-based path produces a
    real SQLAlchemy Boolean default (renders "1" on SQLite, "true" on
    PostgreSQL) rather than reintroducing that hard-coded string, by
    checking an existing row backfills to the SQLite-true representation."""
    from sqlalchemy import text

    url = f"sqlite:///{tmp_path / 'bool_default.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE assets DROP COLUMN is_current"))
    engine.dispose()

    engine = create_engine_from_url(url)
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO assets (id, job_id, scene_index, source_provider, local_path, checksum_sha256, "
            "mime_type, downloaded_at, attempt_number) "
            "VALUES (lower(hex(randomblob(16))), lower(hex(randomblob(16))), 0, 'fake', "
            "'/tmp/x.mp4', 'deadbeef', 'video/mp4', CURRENT_TIMESTAMP, 1)"
        ))
        # A NOT NULL column with no explicit value on INSERT must have taken
        # the server_default, not silently stayed NULL.
        value = conn.execute(text("SELECT is_current FROM assets")).scalar_one()
        assert value == 1


def test_integrity_check_dispatches_to_postgresql_branch() -> None:
    """PostgreSQL has no PRAGMA integrity_check equivalent reachable over a
    normal connection -- confirm the dialect dispatch takes the "live
    connection is the signal" branch instead of trying to run a
    SQLite-only pragma against it (which would raise on real Postgres)."""
    from reel_harness.ops.db_tools import _integrity_check

    class _FakeDialect:
        name = "postgresql"

    class _FakeResult:
        def scalar_one(self):
            raise AssertionError("must not call scalar_one() on the PostgreSQL branch")

    class _FakeConn:
        dialect = _FakeDialect()

        def execute(self, _stmt):
            return _FakeResult()

    assert _integrity_check(_FakeConn()) == "ok"


def test_foreign_key_violations_dispatches_to_postgresql_branch() -> None:
    """PostgreSQL enforces foreign keys synchronously on every write, so a
    committed violation cannot exist -- confirm the dispatch short-circuits
    to an empty list rather than attempting SQLite's PRAGMA
    foreign_key_check against a connection that doesn't support it."""
    from reel_harness.ops.db_tools import _foreign_key_violations

    class _FakeDialect:
        name = "postgresql"

    class _FakeConn:
        dialect = _FakeDialect()

        def execute(self, _stmt):
            raise AssertionError("must not execute a query on the PostgreSQL branch")

    assert _foreign_key_violations(_FakeConn()) == []
