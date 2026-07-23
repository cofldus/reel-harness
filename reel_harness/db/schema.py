from __future__ import annotations

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from reel_harness.db.models import Base

# Phase 0 uses a single "create everything" schema instead of incremental Alembic
# migrations, since the schema hasn't shipped yet and there is nothing to migrate
# from. Alembic is the documented extension point once a real schema change is
# needed against data that must be preserved (see docs/ARCHITECTURE.md).
SCHEMA_VERSION = 1


def create_engine_from_url(database_url: str) -> Engine:
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    return create_engine(database_url, connect_args=connect_args)


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER NOT NULL)"))
        count = conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one()
        if count == 0:
            conn.execute(text("INSERT INTO schema_migrations (version) VALUES (:v)"), {"v": SCHEMA_VERSION})


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)
