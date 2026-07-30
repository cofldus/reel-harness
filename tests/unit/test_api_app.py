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


def test_list_jobs_requires_api_key_and_paginates(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        for i in range(3):
            ctx.jobs.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")

        unauthed = client.get("/v1/jobs")
        assert unauthed.status_code == 401

        response = client.get("/v1/jobs?limit=2", headers={"Authorization": "Bearer test-key"})
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert body["limit"] == 2
        assert len(body["jobs"]) == 2

        bad_limit = client.get("/v1/jobs?limit=0", headers={"Authorization": "Bearer test-key"})
        assert bad_limit.status_code == 422
    finally:
        app.dependency_overrides.clear()


def test_reject_job_requires_review_required_status(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-reject", topic="t")

        unauthed = client.post(
            f"/v1/jobs/{job.id}/reject", json={"reason": "r", "regenerate_from_stage": "SCRIPT"},
        )
        assert unauthed.status_code == 401

        response = client.post(
            f"/v1/jobs/{job.id}/reject", json={"reason": "r", "regenerate_from_stage": "SCRIPT"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 409  # job is QUEUED, not REVIEW_REQUIRED

        missing = client.post(
            "/v1/jobs/does-not-exist/reject", json={"reason": "r", "regenerate_from_stage": "SCRIPT"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert missing.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_retry_job_requires_failed_status(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-retry", topic="t")

        unauthed = client.post(f"/v1/jobs/{job.id}/retry", json={"stage": "SCRIPT"})
        assert unauthed.status_code == 401

        response = client.post(
            f"/v1/jobs/{job.id}/retry", json={"stage": "SCRIPT"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 409  # job is QUEUED, not FAILED

        missing = client.post(
            "/v1/jobs/does-not-exist/retry", json={"stage": "SCRIPT"},
            headers={"Authorization": "Bearer test-key"},
        )
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


def test_status_endpoint_reports_version_schema_and_counts(tmp_path) -> None:
    import reel_harness.api.app as app_module

    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = client.get("/status")
        assert response.status_code == 200
        body = response.json()
        from reel_harness._version import __version__
        from reel_harness.db.schema import SCHEMA_VERSION

        assert body["version"] == __version__
        assert body["schema_version"] == SCHEMA_VERSION
        assert body["schema_version_expected"] == SCHEMA_VERSION
        assert body["uptime_seconds"] >= 0
        assert body["job_status_counts"].get("QUEUED", 0) >= 1  # create_job() auto-transitions CREATED -> QUEUED
        assert body["stale_job_leases"] == 0
        assert body["stale_publication_leases"] == 0
        assert body["supervisor"] is None  # not running inside `serve`
    finally:
        app.dependency_overrides.clear()
        app_module._supervisor = None


def test_status_endpoint_never_exposes_secrets(tmp_path) -> None:
    import json

    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.get("/status")
        blob = json.dumps(response.json()).lower()
        for forbidden in ("api_key", "access_token", "client_secret", "authorization", "test-key"):
            assert forbidden not in blob
    finally:
        app.dependency_overrides.clear()


def test_status_endpoint_reports_stale_leases(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from reel_harness.core.state_machine import JobStatus

    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k-stale", topic="t")
        with ctx.session_factory() as session:
            from reel_harness.db.models import Job

            db_job = session.get(Job, job.id)
            db_job.status = JobStatus.RENDERING.value
            db_job.locked_by = "dead-worker"
            db_job.heartbeat_at = datetime.now(UTC) - timedelta(seconds=ctx.settings.lease_timeout_seconds + 60)
            session.commit()
        response = client.get("/status")
        assert response.json()["stale_job_leases"] == 1
    finally:
        app.dependency_overrides.clear()


def test_status_endpoint_no_api_key_required(tmp_path) -> None:
    """Deliberately no auth requirement on /status (matches /healthz,
    /readyz) -- it exposes no secret, just operational counts, and an
    operator's monitoring stack should not need a credential just to
    scrape health/status."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.get("/status")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()


def test_metrics_endpoint_returns_prometheus_text(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert "jobs_created_total 1.0" in response.text
        assert "# TYPE jobs_created_total counter" in response.text
    finally:
        app.dependency_overrides.clear()


def test_metrics_endpoint_no_api_key_required(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.get("/metrics")
        assert response.status_code == 200
    finally:
        app.dependency_overrides.clear()
