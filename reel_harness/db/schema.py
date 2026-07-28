from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from reel_harness.db.models import Base

# Migration policy until Alembic is introduced (see docs/ARCHITECTURE.md):
# `create_all` builds the full current schema for new databases, and
# _ADDITIVE_COLUMNS applies forward-only ALTER TABLE ADD COLUMN statements so
# existing dev databases keep working without data loss. Only nullable column
# additions are allowed through this path -- anything destructive or shaped
# differently is the trigger to adopt Alembic for real.
SCHEMA_VERSION = 7

# (table, column, sqlite type) added after the table first shipped.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("jobs", "lease_token", "VARCHAR"),  # v2: lease fencing token
    ("jobs", "provider_config", "JSON"),  # v3: per-job provider snapshot
    # v4: append-only asset provenance history (see db.models.Asset). Existing
    # rows default to (attempt_number=1, is_current=1) so they read as a
    # single current attempt with no history gap; the metadata columns default
    # to NULL for rows written before this snapshot existed.
    ("assets", "attempt_number", "INTEGER NOT NULL DEFAULT 1"),
    ("assets", "is_current", "BOOLEAN NOT NULL DEFAULT 1"),
    ("assets", "provider_asset_id", "VARCHAR"),
    ("assets", "query_text", "VARCHAR"),
    ("assets", "selection_score", "FLOAT"),
    ("assets", "source_page_url", "VARCHAR"),
    ("assets", "creator_url", "VARCHAR"),
    ("assets", "commercial_use_allowed", "BOOLEAN"),
    ("assets", "modification_allowed", "BOOLEAN"),
    ("assets", "attribution_text", "VARCHAR"),
    ("assets", "width", "INTEGER"),
    ("assets", "height", "INTEGER"),
    ("assets", "duration_sec", "FLOAT"),
    ("assets", "fps", "FLOAT"),
    ("assets", "request_id", "VARCHAR"),
    # v5: no new columns -- `publications` and `publication_audit_events` are
    # brand-new tables (Phase 3A), which create_all() below already handles
    # for both fresh databases and existing ones (it only creates tables that
    # don't exist yet; it never touches existing tables' columns, which is
    # exactly what _ADDITIVE_COLUMNS is for).
    # v6: Phase 3B reconciliation -- a deterministic fingerprint over the
    # metadata actually sent, so a recovered/retried publication can be
    # confirmed to still match the originally intended upload.
    ("publications", "metadata_fingerprint", "VARCHAR"),
    # v7: Phase 3B processing poller -- see worker.publish_lease and
    # worker.publish_runner._processing_stage.
    ("publications", "processing_started_at", "DATETIME"),
    ("publications", "next_poll_at", "DATETIME"),
    ("publications", "processing_poll_count", "INTEGER NOT NULL DEFAULT 0"),
]


def create_engine_from_url(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def _ensure_column(conn, table: str, column: str, ddl_type: str) -> None:
    existing = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
    if column not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}"))


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for table, column, ddl_type in _ADDITIVE_COLUMNS:
            _ensure_column(conn, table, column, ddl_type)
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL)"))
        count = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
        if count == 0:
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": SCHEMA_VERSION})
        else:
            conn.execute(text("UPDATE schema_migrations SET version = :v"), {"v": SCHEMA_VERSION})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
