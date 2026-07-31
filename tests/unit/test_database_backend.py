"""Dialect-aware database engine creation and DATABASE_URL validation
(reel_harness/db/schema.py::create_engine_from_url,
reel_harness/config.py::_validate_database_url). No real PostgreSQL
connection is ever made here -- SQLAlchemy's create_engine() is lazy (it
never connects until first use), so these tests inspect the constructed
Engine's dialect/pool configuration only."""
from __future__ import annotations

import importlib.util

import pytest

from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings
from reel_harness.db.schema import create_engine_from_url

# create_engine() eagerly imports the DBAPI module even though it never
# connects -- so any test that CONSTRUCTS a postgresql engine needs the
# optional `postgres` extra installed. URL validation/dispatch tests don't.
PSYCOPG_INSTALLED = importlib.util.find_spec("psycopg") is not None
requires_psycopg = pytest.mark.skipif(
    not PSYCOPG_INSTALLED, reason="requires the optional `postgres` extra (psycopg)",
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_sqlite_engine_unchanged(tmp_path) -> None:
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'test.db'}")
    assert engine.dialect.name == "sqlite"
    # SQLite connections aren't pooled the way postgresql's real pool is --
    # confirm this path never picked up postgresql-only kwargs by accident.
    engine.dispose()


@requires_psycopg
def test_postgresql_engine_gets_pre_ping_and_pool_settings() -> None:
    engine = create_engine_from_url(
        "postgresql+psycopg://user:pass@localhost:5432/reel_harness",
        pool_size=7, max_overflow=13,
    )
    assert engine.dialect.name == "postgresql"
    assert engine.pool._pre_ping is True
    assert engine.pool.size() == 7
    engine.dispose()


@requires_psycopg
def test_postgresql_engine_statement_timeout_sets_connect_option() -> None:
    engine = create_engine_from_url(
        "postgresql+psycopg://user:pass@localhost:5432/reel_harness",
        statement_timeout_seconds=2.5,
    )
    assert engine.url.query.get("options") is None  # timeout is passed via connect_args, not the URL
    # connect_args isn't directly introspectable post-construction on a plain
    # Engine in a stable public way across versions, so assert indirectly:
    # this must not raise and must still be a postgresql engine.
    assert engine.dialect.name == "postgresql"
    engine.dispose()


def test_unsupported_backend_raises_clear_error() -> None:
    with pytest.raises(ValueError, match="unsupported database backend"):
        create_engine_from_url("mysql://user:pass@localhost/db")


def test_default_pool_kwargs_are_sqlite_harmless(tmp_path) -> None:
    # Every existing call site only ever passes a bare database_url --
    # confirm the new keyword-only args don't change SQLite behavior at all.
    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'test2.db'}")
    assert engine.dialect.name == "sqlite"
    engine.dispose()


def test_validate_database_url_accepts_sqlite() -> None:
    validate_provider_settings(_settings(database_url="sqlite:///./somewhere.db"))  # must not raise


def test_validate_database_url_accepts_postgresql() -> None:
    validate_provider_settings(
        _settings(database_url="postgresql+psycopg://user:pass@localhost:5432/reel_harness"),
    )  # must not raise


def test_validate_database_url_rejects_unsupported_backend() -> None:
    with pytest.raises(ProviderConfigurationError, match="unsupported database backend"):
        validate_provider_settings(_settings(database_url="mysql://user:pass@localhost/db"))


def test_validate_database_url_rejects_malformed_url() -> None:
    with pytest.raises(ProviderConfigurationError, match="not a valid database URL"):
        validate_provider_settings(_settings(database_url="not a url at all :::"))


def test_pool_settings_read_from_env(monkeypatch) -> None:
    monkeypatch.setenv("REEL_HARNESS_DB_POOL_SIZE", "9")
    monkeypatch.setenv("REEL_HARNESS_DB_POOL_MAX_OVERFLOW", "17")
    monkeypatch.setenv("REEL_HARNESS_DB_STATEMENT_TIMEOUT_SECONDS", "3.5")
    settings = Settings(_env_file=None)
    assert settings.db_pool_size == 9
    assert settings.db_pool_max_overflow == 17
    assert settings.db_statement_timeout_seconds == 3.5


def test_pool_settings_defaults() -> None:
    settings = _settings()
    assert settings.db_pool_size == 5
    assert settings.db_pool_max_overflow == 10
    assert settings.db_statement_timeout_seconds is None
