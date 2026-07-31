from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Engine,
    Float,
    Integer,
    String,
    create_engine,
    inspect,
    text,
    true,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import Column, CreateColumn
from sqlalchemy.sql.elements import ColumnElement, TextClause
from sqlalchemy.types import JSON, TypeEngine

# Imported for its side effect only: registering the Fable cinematic tables
# on Base.metadata BEFORE any init_db()/create_all() call. v8 adds only
# brand-new tables (fable_projects/characters/locations/scenes/shots/takes),
# which create_all() handles for both fresh and existing databases -- same
# shape as the v5 publications tables, no _ADDITIVE_COLUMNS entries needed.
import reel_harness.db.cinematic_models  # noqa: F401  (side-effect import)
from reel_harness.db.models import Base, UTCDateTime

SUPPORTED_DATABASE_BACKENDS = frozenset({"sqlite", "postgresql"})

# Migration policy until Alembic is introduced (see docs/ARCHITECTURE.md):
# `create_all` builds the full current schema for new databases, and
# _ADDITIVE_COLUMNS applies forward-only ALTER TABLE ADD COLUMN statements so
# existing dev databases keep working without data loss. Only nullable column
# additions are allowed through this path -- anything destructive or shaped
# differently is the trigger to adopt Alembic for real.
SCHEMA_VERSION = 8

# (table, column, SQLAlchemy type, nullable, server_default). Rendered to
# per-dialect ALTER TABLE ... ADD COLUMN DDL by _ensure_column() via
# sqlalchemy.schema.CreateColumn, which knows how to compile both the type
# and the default literal correctly for whichever dialect it's given --
# e.g. `true()` compiles to `1` on SQLite and `true` on PostgreSQL, which a
# hand-written "BOOLEAN NOT NULL DEFAULT 1" DDL string never could (PostgreSQL
# rejects an integer default for a boolean column outright). One list, two
# dialects, both correct.
_ADDITIVE_COLUMNS: list[tuple[str, str, TypeEngine, bool, TextClause | ColumnElement | None]] = [
    ("jobs", "lease_token", String(), True, None),  # v2: lease fencing token
    ("jobs", "provider_config", JSON(), True, None),  # v3: per-job provider snapshot
    # v4: append-only asset provenance history (see db.models.Asset). Existing
    # rows default to (attempt_number=1, is_current=1) so they read as a
    # single current attempt with no history gap; the metadata columns default
    # to NULL for rows written before this snapshot existed.
    ("assets", "attempt_number", Integer(), False, text("1")),
    ("assets", "is_current", Boolean(), False, true()),
    ("assets", "provider_asset_id", String(), True, None),
    ("assets", "query_text", String(), True, None),
    ("assets", "selection_score", Float(), True, None),
    ("assets", "source_page_url", String(), True, None),
    ("assets", "creator_url", String(), True, None),
    ("assets", "commercial_use_allowed", Boolean(), True, None),
    ("assets", "modification_allowed", Boolean(), True, None),
    ("assets", "attribution_text", String(), True, None),
    ("assets", "width", Integer(), True, None),
    ("assets", "height", Integer(), True, None),
    ("assets", "duration_sec", Float(), True, None),
    ("assets", "fps", Float(), True, None),
    ("assets", "request_id", String(), True, None),
    # v5: no new columns -- `publications` and `publication_audit_events` are
    # brand-new tables (Phase 3A), which create_all() below already handles
    # for both fresh databases and existing ones (it only creates tables that
    # don't exist yet; it never touches existing tables' columns, which is
    # exactly what _ADDITIVE_COLUMNS is for).
    # v6: Phase 3B reconciliation -- a deterministic fingerprint over the
    # metadata actually sent, so a recovered/retried publication can be
    # confirmed to still match the originally intended upload.
    ("publications", "metadata_fingerprint", String(), True, None),
    # v7: Phase 3B processing poller -- see worker.publish_lease and
    # worker.publish_runner._processing_stage.
    ("publications", "processing_started_at", UTCDateTime(), True, None),
    ("publications", "next_poll_at", UTCDateTime(), True, None),
    ("publications", "processing_poll_count", Integer(), False, text("0")),
    # v8: no new columns -- the Fable cinematic tables (db.cinematic_models)
    # are brand-new, which create_all() already handles for both fresh and
    # existing databases; same shape as v5's publications tables.
]


def create_engine_from_url(
    database_url: str, *, pool_size: int = 5, max_overflow: int = 10,
    statement_timeout_seconds: float | None = None,
) -> Engine:
    """Dialect-aware engine construction. SQLite keeps its existing
    single-file settings unchanged (`check_same_thread=False`, SQLAlchemy's
    default `SingletonThreadPool`-adjacent pooling for a file DB -- never a
    real connection pool, since there is only ever one underlying file
    handle model to reason about). PostgreSQL gets a real bounded
    connection pool with a pre-ping health check (`pool_pre_ping=True` --
    verifies a pooled connection is still alive before handing it out,
    rather than surfacing a stale-connection error mid-request) and,
    optionally, a server-side statement timeout via a `-c
    statement_timeout=<ms>` libpq connection option. SQLite has no
    equivalent to a statement timeout, so `statement_timeout_seconds` is
    simply never applied there rather than faked.

    `pool_size`/`max_overflow`/`statement_timeout_seconds` are keyword-only
    with SQLite-harmless defaults so every existing call site that only
    ever passed a bare `database_url` keeps working unchanged; real values
    come from `Settings.db_pool_size`/`db_pool_max_overflow`/
    `db_statement_timeout_seconds` (see bootstrap.AppContext).

    A bare `postgresql://...` URL (no `+driver` suffix -- the shape most
    managed-Postgres providers hand out) is normalized to
    `postgresql+psycopg://...` so the operator never needs to know which
    driver this project bundles (`psycopg`, the modern v3 driver -- see the
    `postgres` optional-dependency group in pyproject.toml). A URL that
    already names an explicit driver (e.g. `postgresql+psycopg2://`) is
    left alone -- never silently overridden."""
    url = make_url(database_url)
    backend = url.get_backend_name()
    if backend == "sqlite":
        return create_engine(database_url, connect_args={"check_same_thread": False})
    if backend == "postgresql":
        if url.drivername == "postgresql":  # bare scheme, no explicit +driver
            url = url.set(drivername="postgresql+psycopg")
        connect_args: dict = {}
        if statement_timeout_seconds is not None:
            timeout_ms = int(statement_timeout_seconds * 1000)
            connect_args["options"] = f"-c statement_timeout={timeout_ms}"
        return create_engine(
            url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow,
            connect_args=connect_args,
        )
    raise ValueError(
        f"unsupported database backend {backend!r} (from {database_url!r}) -- "
        f"reel-harness supports: {', '.join(sorted(SUPPORTED_DATABASE_BACKENDS))}"
    )


def _ensure_column(
    conn, table: str, column: str, column_type: TypeEngine, nullable: bool,
    server_default: TextClause | ColumnElement | None,
) -> None:
    """Portable column-presence check via `sqlalchemy.inspect()` (works
    identically against SQLite and PostgreSQL) instead of SQLite's
    `PRAGMA table_info`. The ADD COLUMN fragment itself is rendered by
    `CreateColumn`, the same DDL compiler SQLAlchemy uses for `CREATE
    TABLE`, so the type and default are always dialect-correct."""
    existing = {col["name"] for col in inspect(conn).get_columns(table)}
    if column in existing:
        return
    new_column = Column(column, column_type, nullable=nullable, server_default=server_default)
    ddl_fragment = str(CreateColumn(new_column).compile(dialect=conn.dialect))
    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment}"))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, column_type, nullable, server_default in _ADDITIVE_COLUMNS:
            _ensure_column(conn, table, column, column_type, nullable, server_default)
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL)"))
        count = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
        if count == 0:
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": SCHEMA_VERSION})
        else:
            conn.execute(text("UPDATE schema_migrations SET version = :v"), {"v": SCHEMA_VERSION})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
