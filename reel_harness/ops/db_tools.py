from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import text

from reel_harness.db.schema import _ADDITIVE_COLUMNS, SCHEMA_VERSION

_MANIFEST_SUFFIX = ".manifest.json"


class DbToolsError(Exception):
    pass


class MigrationLockedError(DbToolsError):
    pass


class RestoreRefusedError(DbToolsError):
    pass


def sqlite_path_from_url(database_url: str) -> Path:
    if not database_url.startswith("sqlite"):
        raise DbToolsError(
            f"this command only supports SQLite database URLs, got {database_url!r} -- "
            "see docs/OPERATIONS.md's Phase 4A scope (PostgreSQL is explicitly out of scope)"
        )
    # sqlite:///relative/path.db or sqlite:////absolute/path.db (4 slashes)
    raw = database_url.split("///", 1)[1]
    return Path(raw)


def safe_db_identifier(database_url: str) -> str:
    """Never the full path (could reveal an operator's home directory
    layout in a shared incident bundle) -- just the filename."""
    try:
        return sqlite_path_from_url(database_url).name
    except DbToolsError:
        return "<non-sqlite>"


# ---------------------------------------------------------------------------
# db-status
# ---------------------------------------------------------------------------


@dataclass
class DbStatus:
    current_schema_version: int | None
    latest_schema_version: int
    pending_migrations: list[str]
    db_identifier: str
    table_row_counts: dict[str, int]
    integrity_status: str

    def to_dict(self) -> dict:
        return {
            "current_schema_version": self.current_schema_version,
            "latest_schema_version": self.latest_schema_version,
            "pending_migrations": self.pending_migrations,
            "db_identifier": self.db_identifier,
            "table_row_counts": self.table_row_counts,
            "integrity_status": self.integrity_status,
        }


def _pending_column_migrations(conn) -> list[str]:
    pending = []
    for table, column, _ddl in _ADDITIVE_COLUMNS:
        try:
            existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
        except Exception:  # noqa: BLE001 - table may not exist yet on a brand-new DB
            existing = set()
        if existing and column not in existing:
            pending.append(f"{table}.{column}")
    return pending


def db_status(engine, database_url: str) -> DbStatus:
    with engine.connect() as conn:
        has_migrations_table = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )).first()
        current_version = None
        if has_migrations_table:
            current_version = conn.execute(text("SELECT version FROM schema_migrations")).scalar_one_or_none()

        pending = _pending_column_migrations(conn)
        if current_version != SCHEMA_VERSION:
            pending.append(f"schema_migrations.version {current_version!r} -> {SCHEMA_VERSION}")

        row_counts: dict[str, int] = {}
        for table in ("channels", "jobs", "stage_runs", "assets", "publications", "publication_audit_events"):
            try:
                row_counts[table] = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            except Exception:  # noqa: BLE001 - table doesn't exist yet
                row_counts[table] = 0

        integrity_status = conn.execute(text("PRAGMA integrity_check")).scalar_one()

    return DbStatus(
        current_schema_version=current_version, latest_schema_version=SCHEMA_VERSION,
        pending_migrations=pending, db_identifier=safe_db_identifier(database_url),
        table_row_counts=row_counts, integrity_status=integrity_status,
    )


# ---------------------------------------------------------------------------
# db-migrate
# ---------------------------------------------------------------------------


class _MigrationLock:
    """A plain PID lockfile next to the database -- SQLite has no built-in
    advisory-lock primitive reachable from Python without a third-party
    dependency, and a lockfile is sufficient for this project's
    single-operator, single-machine deployment model. Atomic acquire via
    O_CREAT|O_EXCL (fails if another process already holds it); always
    released in a `finally`, even on an exception mid-migration."""

    def __init__(self, db_path: Path) -> None:
        self._lock_path = db_path.parent / f"{db_path.name}.migrate.lock"
        self._acquired = False

    def __enter__(self) -> _MigrationLock:
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise MigrationLockedError(
                f"migration lock {self._lock_path} already exists -- another db-migrate may be running, "
                "or a previous run crashed without cleaning up (delete the lock file if you've confirmed that)"
            ) from exc
        with os.fdopen(fd, "w") as handle:
            handle.write(str(os.getpid()))
        self._acquired = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._acquired:
            self._lock_path.unlink(missing_ok=True)


def db_migrate(
    engine, database_url: str, dry_run: bool = False, backup_dir: Path | None = None,
) -> dict:
    """Wraps the existing idempotent `db.schema.init_db()` with the
    operational guarantees a migration command needs: a default backup
    before any real change, an exclusive lock so two concurrent
    `db-migrate` invocations can't interleave, a dry-run mode that reports
    the plan without touching the database, and safety on repeated runs
    (init_db() is already a no-op once the schema is current)."""
    status_before = db_status(engine, database_url)
    if dry_run:
        return {
            "applied": False, "dry_run": True, "backup_path": None,
            "pending_migrations": status_before.pending_migrations,
            "current_schema_version": status_before.current_schema_version,
            "target_schema_version": SCHEMA_VERSION,
        }

    db_path = sqlite_path_from_url(database_url)
    backup_result = None
    with _MigrationLock(db_path):
        if not status_before.pending_migrations:
            return {
                "applied": True, "dry_run": False, "backup_path": None, "pending_migrations": [],
                "current_schema_version": status_before.current_schema_version,
                "target_schema_version": SCHEMA_VERSION,
                "note": "already at the latest schema -- no-op",
            }
        if backup_dir is not None:
            backup_result = db_backup(database_url, backup_dir)
        from reel_harness.db.schema import init_db

        init_db(engine)
    status_after = db_status(engine, database_url)
    return {
        "applied": True, "dry_run": False,
        "backup_path": backup_result["path"] if backup_result else None,
        "pending_migrations": status_before.pending_migrations,
        "current_schema_version": status_after.current_schema_version,
        "target_schema_version": SCHEMA_VERSION,
    }


# ---------------------------------------------------------------------------
# db-backup / db-restore
# ---------------------------------------------------------------------------


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _actual_schema_version(db_path: Path) -> int | None:
    """Reads the REAL schema_migrations.version from the given SQLite
    file -- never assumes it matches this running code's SCHEMA_VERSION
    constant. A backup of a database that hasn't been migrated yet must
    honestly record its own (older) version, not silently claim to be
    current -- otherwise db_restore's "refuse a backup newer than this
    build supports" check would be meaningless, and the manifest itself
    would just be wrong."""
    conn = sqlite3.connect(str(db_path))
    try:
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        if cursor.fetchone() is None:
            return None
        row = conn.execute("SELECT version FROM schema_migrations").fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def db_backup(database_url: str, dest_dir: Path, timestamp: datetime | None = None) -> dict:
    """Uses SQLite's own online backup API (`sqlite3.Connection.backup`,
    stdlib since Python 3.7) rather than a raw file copy, so a backup is
    safe to take even while another connection holds the database open --
    the API handles the consistent-snapshot semantics itself. Written to a
    uniquely-named temp file in the SAME directory first, then
    os.replace()'d into place, so a crash mid-backup never leaves a
    half-written file at the final name. A checksum + small JSON manifest
    (schema version, app version, source db identifier, created_at) is
    written alongside it -- never the full source path, never a
    credential."""
    from reel_harness._version import __version__

    db_path = sqlite_path_from_url(database_url)
    if not db_path.exists():
        raise DbToolsError(f"database file does not exist: {db_path}")
    dest_dir = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)

    ts = timestamp or datetime.now(UTC)
    stamp = ts.strftime("%Y%m%dT%H%M%S%fZ")
    filename = f"reel_harness-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3.bak"
    final_path = dest_dir / filename
    temp_path = dest_dir / f".{filename}.tmp"

    src_conn = sqlite3.connect(str(db_path))
    try:
        dst_conn = sqlite3.connect(str(temp_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()
    os.replace(temp_path, final_path)

    checksum = _sha256_file(final_path)
    manifest = {
        "app_version": __version__,
        "schema_version": _actual_schema_version(final_path),
        "source_db_identifier": safe_db_identifier(database_url),
        "created_at": ts.isoformat(),
        "checksum_sha256": checksum,
        "file_size_bytes": final_path.stat().st_size,
    }
    (dest_dir / f"{filename}{_MANIFEST_SUFFIX}").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return {"path": str(final_path), **manifest}


@dataclass
class ActiveWorkerLease:
    kind: str  # "job" | "publication"
    id: str


def detect_active_leases(session_factory, lease_timeout_seconds: int) -> list[ActiveWorkerLease]:
    """A running worker is one whose lease is both held (locked_by set) and
    recently heartbeat -- a stale lease (heartbeat older than the timeout)
    is not "running", it's exactly what stale-lease recovery already
    reclaims on the next worker cycle, so it must never block a restore."""
    from datetime import timedelta

    from reel_harness.db.models import Job, Publication

    cutoff = datetime.now(UTC) - timedelta(seconds=lease_timeout_seconds)
    with session_factory() as session:
        jobs = session.query(Job).filter(Job.locked_by.isnot(None), Job.heartbeat_at > cutoff).all()
        pubs = session.query(Publication).filter(
            Publication.locked_by.isnot(None), Publication.heartbeat_at > cutoff,
        ).all()
    return [ActiveWorkerLease("job", j.id) for j in jobs] + [ActiveWorkerLease("publication", p.id) for p in pubs]


def db_restore(
    database_url: str, backup_path: Path, confirm_restore: bool, session_factory,
    lease_timeout_seconds: int, pre_restore_backup_dir: Path, engine=None,
) -> dict:
    """Destructive by nature, so every step before the final atomic
    replace is a pure check that can refuse and leave the existing
    database completely untouched: explicit confirmation required, no
    lease that looks like a live running worker, the backup's checksum
    must match its own manifest (catches a truncated/corrupted copy),
    and its schema version must not be newer than this build supports
    (an older schema is fine -- `db-migrate` after restoring brings it
    forward). The CURRENT database is itself backed up first, so even a
    confirmed, valid restore is never a one-way trip.

    `engine` (the SQLAlchemy engine `session_factory` is bound to) is
    disposed of right before the atomic file swap if given: its
    connection pool otherwise keeps a real open file handle to the
    database being replaced, which os.replace() silently allows on POSIX
    (the old inode just stays around until the handle closes) but Windows
    refuses outright (PermissionError / WinError 5) -- disposing first
    makes the swap actually atomic and successful on both platforms."""
    if not confirm_restore:
        raise RestoreRefusedError("db-restore requires --confirm-restore -- this replaces the live database")

    active = detect_active_leases(session_factory, lease_timeout_seconds)
    if active:
        names = ", ".join(f"{lease.kind}:{lease.id}" for lease in active[:5])
        raise RestoreRefusedError(
            f"refusing to restore while {len(active)} lease(s) look actively held by a running worker "
            f"(e.g. {names}) -- stop all workers first"
        )

    backup_path = Path(backup_path)
    manifest_path = Path(f"{backup_path}{_MANIFEST_SUFFIX}")
    if not backup_path.exists():
        raise DbToolsError(f"backup file does not exist: {backup_path}")
    if not manifest_path.exists():
        raise DbToolsError(f"backup manifest does not exist: {manifest_path} -- cannot verify checksum/schema")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    actual_checksum = _sha256_file(backup_path)
    if actual_checksum != manifest.get("checksum_sha256"):
        raise DbToolsError(
            f"checksum mismatch: backup file does not match its manifest "
            f"(expected {manifest.get('checksum_sha256')}, got {actual_checksum}) -- the backup may be corrupt"
        )
    backup_schema_version = manifest.get("schema_version")
    if isinstance(backup_schema_version, int) and backup_schema_version > SCHEMA_VERSION:
        raise DbToolsError(
            f"backup schema version {backup_schema_version} is newer than this build supports "
            f"({SCHEMA_VERSION}) -- upgrade reel-harness before restoring this backup"
        )

    pre_restore = db_backup(database_url, pre_restore_backup_dir)

    if engine is not None:
        engine.dispose()

    db_path = sqlite_path_from_url(database_url)
    temp_restore_path = db_path.parent / f".{db_path.name}.restoring-{uuid.uuid4().hex}"
    try:
        shutil.copy2(backup_path, temp_restore_path)
        os.replace(temp_restore_path, db_path)
    except OSError:
        Path(temp_restore_path).unlink(missing_ok=True)
        raise

    return {
        "restored": True, "restored_from": str(backup_path),
        "pre_restore_backup_path": pre_restore["path"], "schema_version": backup_schema_version,
    }


# ---------------------------------------------------------------------------
# db-verify
# ---------------------------------------------------------------------------


@dataclass
class DbVerifyResult:
    integrity_check: str
    foreign_key_violation_count: int
    orphan_publication_ids: list[str] = field(default_factory=list)
    active_unlocked_jobs: list[str] = field(default_factory=list)
    active_unlocked_publications: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return (
            self.integrity_check == "ok" and self.foreign_key_violation_count == 0
            and not self.orphan_publication_ids and not self.active_unlocked_jobs
            and not self.active_unlocked_publications
        )

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "integrity_check": self.integrity_check,
            "foreign_key_violation_count": self.foreign_key_violation_count,
            "orphan_publication_ids": self.orphan_publication_ids,
            "active_unlocked_jobs": self.active_unlocked_jobs,
            "active_unlocked_publications": self.active_unlocked_publications,
        }


def db_verify(engine, session_factory) -> DbVerifyResult:
    """DB-internal consistency only (integrity, foreign keys, orphan rows,
    the forbidden ACTIVE+unlocked state) -- reuses the exact same
    orphaned-active-lease detectors the worker daemons themselves use
    (`worker.lease.find_orphaned_active_jobs` /
    `worker.publish_lease.find_orphaned_active_publications`), so this
    reports the identical invariant the workers are built to never
    violate, not a second, possibly-divergent definition of it.
    Cross-checking the DB against what's actually on disk (missing files,
    checksum mismatches, corrupt manifests) is `storage-verify`'s job, not
    this command's -- see docs/OPERATIONS.md."""
    from reel_harness.worker.lease import find_orphaned_active_jobs
    from reel_harness.worker.publish_lease import find_orphaned_active_publications

    with engine.connect() as conn:
        integrity = conn.execute(text("PRAGMA integrity_check")).scalar_one()
        fk_violations = list(conn.execute(text("PRAGMA foreign_key_check")))

    with session_factory() as session:
        orphan_pub_ids = [
            row[0] for row in session.execute(text(
                "SELECT publications.id FROM publications "
                "LEFT JOIN jobs ON publications.job_id = jobs.id WHERE jobs.id IS NULL"
            ))
        ]
        active_unlocked_jobs = find_orphaned_active_jobs(session)
        active_unlocked_pubs = find_orphaned_active_publications(session)

    return DbVerifyResult(
        integrity_check=integrity, foreign_key_violation_count=len(fk_violations),
        orphan_publication_ids=orphan_pub_ids,
        active_unlocked_jobs=active_unlocked_jobs, active_unlocked_publications=active_unlocked_pubs,
    )
