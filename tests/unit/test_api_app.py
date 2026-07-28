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


def test_get_job_assets_requires_api_key_and_exposes_no_local_path(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-assets", topic="t")

        unauthed = client.get(f"/v1/jobs/{job.id}/assets")
        assert unauthed.status_code == 401

        response = client.get(f"/v1/jobs/{job.id}/assets", headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        assert response.json() == []  # no ASSET stage has run yet

        missing = client.get("/v1/jobs/does-not-exist/assets", headers={"Authorization": "Bearer test-key"})
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_create_publication_requires_api_key(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-pub", topic="t")
        response = client.post(
            f"/v1/jobs/{job.id}/publications",
            json={"provider": "youtube", "account_reference": "acct-1"},
        )
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_create_publication_dry_run_reports_ineligible_without_persisting(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-pub-dry", topic="t")
        response = client.post(
            f"/v1/jobs/{job.id}/publications",
            json={"provider": "youtube", "account_reference": "acct-1", "dry_run": True},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 202
        body = response.json()
        assert body["dry_run"] is True
        assert body["eligible"] is False
        assert "JOB_NOT_COMPLETED" in body["eligibility_reasons"]
        assert body["publication_id"] is None
    finally:
        app.dependency_overrides.clear()


def test_create_publication_for_ineligible_job_returns_409_with_reasons(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-pub-409", topic="t")
        response = client.post(
            f"/v1/jobs/{job.id}/publications",
            json={"provider": "youtube", "account_reference": "acct-1"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 409
        assert response.json()["detail"]["eligible"] is False
    finally:
        app.dependency_overrides.clear()


def test_create_publication_public_without_confirm_returns_400(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-pub-public", topic="t")
        response = client.post(
            f"/v1/jobs/{job.id}/publications",
            json={"provider": "youtube", "account_reference": "acct-1", "privacy_status": "public"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 400
    finally:
        app.dependency_overrides.clear()


def test_get_and_cancel_publication_not_found_returns_404(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        get_response = client.get(
            "/v1/publications/does-not-exist", headers={"Authorization": "Bearer test-key"},
        )
        assert get_response.status_code == 404
        cancel_response = client.post(
            "/v1/publications/does-not-exist/cancel", headers={"Authorization": "Bearer test-key"},
        )
        assert cancel_response.status_code == 404
    finally:
        app.dependency_overrides.clear()
