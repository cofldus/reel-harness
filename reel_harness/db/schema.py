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
SCHEMA_VERSION = 2

# (table, column, sqlite type) added after the table first shipped.
_ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("jobs", "lease_token", "VARCHAR"),  # v2: lease fencing token
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
