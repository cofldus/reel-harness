from __future__ import annotations

import os
import socket

import pytest

from reel_harness.core.service import JobService
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.runner import ProviderBundle

_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}

# Opt-in PostgreSQL test target (Phase 6A-1) -- unset by default, so every
# existing Windows dev run and most CI legs skip PostgreSQL-backed tests
# cleanly, matching the FFMPEG_PRESENT/DEMO_TTS_STATUS skipif convention
# used throughout this suite. See docs/OPERATIONS.md for how to point this
# at a real instance (a local `docker run postgres` or the CI service
# container).
REEL_HARNESS_TEST_POSTGRES_URL = os.environ.get("REEL_HARNESS_TEST_POSTGRES_URL")
POSTGRES_TEST_AVAILABLE = bool(REEL_HARNESS_TEST_POSTGRES_URL)


@pytest.fixture(autouse=True)
def isolate_dotenv(monkeypatch):
    """Tests must never read the developer's real `.env`.

    `Settings` is configured with `env_file=".env"`, so a bare
    `Settings()` in a test silently picks up whatever credentials and
    provider selections the developer happens to have configured locally
    -- making tests pass or fail depending on the machine they run on.
    This was latent until a real `.env` existed, at which point two
    fingerprint/readiness tests started failing because they saw a real
    LLM host where they asserted `None`.

    Neutralizing the env_file here fixes every present and future test at
    once, instead of requiring each call site to remember `_env_file=None`.
    Environment VARIABLES are deliberately left alone: tests that set them
    via monkeypatch do so in the test body (after this fixture), and some
    legitimately-exported ones (e.g. REEL_HARNESS_FFMPEG_PATH) must keep
    working.
    """
    from reel_harness.config import Settings

    monkeypatch.setitem(Settings.model_config, "env_file", None)


@pytest.fixture(autouse=True)
def block_real_network(monkeypatch):
    """Phase 0/1 only ever talks to Fake providers and a local SQLite file, so any
    attempt to open a real (non-loopback) network socket during a test is a bug,
    not a feature. Loopback is allowed because Windows emulates socket.socketpair()
    (used internally by asyncio's ProactorEventLoop, which FastAPI's TestClient
    needs) via a real 127.0.0.1 TCP connection -- blocking that would break test
    infrastructure that never leaves the machine.
    """
    original_connect = socket.socket.connect

    def _guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host not in _LOOPBACK_HOSTS:
            raise RuntimeError(f"real network access is blocked in tests -- use a Fake provider (host={host!r})")
        return original_connect(self, address, *args, **kwargs)

    def _blocked_create_connection(address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        raise RuntimeError(f"real network access is blocked in tests -- use a Fake provider (host={host!r})")

    monkeypatch.setattr(socket.socket, "connect", _guarded_connect)
    monkeypatch.setattr(socket, "create_connection", _blocked_create_connection)


@pytest.fixture
def engine(tmp_path):
    eng = create_engine_from_url(f"sqlite:///{tmp_path / 'test.db'}")
    init_db(eng)
    return eng


@pytest.fixture
def session_factory(engine):
    return make_session_factory(engine)


@pytest.fixture
def postgres_engine():
    """A clean, real PostgreSQL engine for one test -- skipped unless
    REEL_HARNESS_TEST_POSTGRES_URL is set. There is no per-test tmp_path
    equivalent for a real server the way there is for a SQLite file, so
    every table this app owns is dropped and recreated via init_db()
    before the test runs, ensuring no leftover rows from a previous run
    against the same shared test database."""
    if not POSTGRES_TEST_AVAILABLE:
        pytest.skip("REEL_HARNESS_TEST_POSTGRES_URL not set -- skipping PostgreSQL-backed tests")
    from reel_harness.db.models import Base

    eng = create_engine_from_url(REEL_HARNESS_TEST_POSTGRES_URL)
    Base.metadata.drop_all(eng)
    init_db(eng)
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def db_backend(request, tmp_path):
    """Parametrized (database_url, engine) pair for the repository-parity
    suite (tests/integration/test_postgres_backend_parity.py) -- every test
    using this fixture runs once against SQLite and once against
    PostgreSQL. The PostgreSQL case is skipped by default; see
    `postgres_engine` above."""
    if request.param == "postgresql":
        if not POSTGRES_TEST_AVAILABLE:
            pytest.skip("REEL_HARNESS_TEST_POSTGRES_URL not set -- skipping PostgreSQL-backed tests")
        from reel_harness.db.models import Base

        url = REEL_HARNESS_TEST_POSTGRES_URL
        eng = create_engine_from_url(url)
        Base.metadata.drop_all(eng)
    else:
        url = f"sqlite:///{tmp_path / 'test.db'}"
        eng = create_engine_from_url(url)
    init_db(eng)
    try:
        yield url, eng
    finally:
        eng.dispose()


@pytest.fixture
def storage(tmp_path):
    return LocalFilesystemStorage(tmp_path / "jobs")


@pytest.fixture
def job_service(session_factory, storage):
    return JobService(session_factory, storage=storage)


@pytest.fixture
def channel(job_service):
    return job_service.create_channel(name="test-channel", niche="cooking", language="en")


@pytest.fixture
def fake_providers():
    return ProviderBundle(llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider())


def walk_casting(fable, project_id: str) -> None:
    """Drive a Fable project through F3's casting phase: generate every
    character's reference sheet, then approve each one.

    A shared helper rather than copy-paste because casting is now a real
    stop between STORY_REVIEW and CHARACTER_REVIEW (F3 commit 3), and
    every test that walks the gates has to pass through it. Tests that are
    ABOUT casting call the service methods directly instead -- this is
    only for the ones whose subject is further downstream."""
    fable.generate_references(project_id)
    for character in fable.project_characters(project_id):
        fable.approve_reference(character.id)
