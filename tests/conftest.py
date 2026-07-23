from __future__ import annotations

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
