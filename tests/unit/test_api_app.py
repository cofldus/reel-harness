from __future__ import annotations

from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api-test.db'}",
        jobs_dir=tmp_path / "jobs",
        app_api_key="test-key",
    )
    return AppContext(settings=settings)


def test_healthz_reports_dependency_status(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "ffmpeg_available" in body
    finally:
        app.dependency_overrides.clear()


def test_create_job_requires_api_key(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        response = client.post("/v1/jobs", json={"channel_id": channel.id, "idempotency_key": "k1"})
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_create_job_with_valid_api_key_returns_job_id(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        response = client.post(
            "/v1/jobs",
            json={"channel_id": channel.id, "idempotency_key": "k1"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["status"] == "QUEUED"

        get_response = client.get(f"/v1/jobs/{body['job_id']}", headers={"Authorization": "Bearer test-key"})
        assert get_response.status_code == 200
    finally:
        app.dependency_overrides.clear()
