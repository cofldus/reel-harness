"""Readiness endpoint: deep checks without any provider network request."""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import text

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.media.deps import check_ffmpeg_available

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _ctx(tmp_path) -> AppContext:
    return AppContext(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'ready.db'}",
            jobs_dir=tmp_path / "jobs",
            app_api_key="fake-test-api-key",
            # Explicit and outside the repo -- tmp_path lives under the OS
            # temp dir, never inside this checkout, so this never touches
            # the real ~/.reel-harness/credentials on the machine running
            # the tests.
            credential_dir=tmp_path / "creds",
        ),
    )


def _get_readyz(ctx) -> tuple[int, dict]:
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/readyz")
        return response.status_code, response.json()
    finally:
        app.dependency_overrides.clear()


def test_ready_on_a_healthy_context(tmp_path) -> None:
    status_code, body = _get_readyz(_ctx(tmp_path))
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["schema"].startswith("ok")
    assert body["checks"]["storage"] == "ok"
    assert body["checks"]["provider"].startswith("ok")
    if FFMPEG_PRESENT:
        assert status_code == 200
        assert body["ready"] is True
    else:
        assert status_code == 503
        assert body["checks"]["ffmpeg"] == "not found"


def test_unsupported_schema_version_is_not_ready(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    with ctx.session_factory() as session:
        session.execute(text("UPDATE schema_migrations SET version = 999"))
        session.commit()
    status_code, body = _get_readyz(ctx)
    assert status_code == 503
    assert body["ready"] is False
    assert "unsupported version 999" in body["checks"]["schema"]


def test_provider_configuration_drift_is_not_ready(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    # Simulate post-startup drift: a real provider selected with no credentials.
    ctx.settings = ctx.settings.model_copy(update={"llm_provider": "openai_compatible"})
    status_code, body = _get_readyz(ctx)
    assert status_code == 503
    assert body["checks"]["provider"].startswith("invalid")
    assert "REEL_HARNESS_LLM" in body["checks"]["provider"]
    # No secret material in the response, ever.
    assert "fake-test-api-key" not in str(body)


def test_healthz_stays_a_shallow_liveness_check(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/healthz")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"
    finally:
        app.dependency_overrides.clear()


def _get_readyz_publisher(ctx, account: str | None = None) -> tuple[int, dict]:
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        params = {"account": account} if account else {}
        response = TestClient(app).get("/readyz/publisher", params=params)
        return response.status_code, response.json()
    finally:
        app.dependency_overrides.clear()


def test_readyz_publisher_ready_without_any_credential(tmp_path) -> None:
    """No account credential configured is reported, not treated as a
    server-side failure -- publishing readiness for an unconfigured account
    is a config gap the operator sees, not a 503."""
    status_code, body = _get_readyz_publisher(_ctx(tmp_path))
    assert body["checks"]["publication_schema"] == "ok"
    assert body["checks"]["credential_backend"] == "ok"
    assert "not configured" in body["checks"]["account_credential"]
    if FFMPEG_PRESENT:
        assert status_code == 200
        assert body["ready"] is True


def test_readyz_publisher_never_exposes_secrets_or_paths(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    status_code, body = _get_readyz_publisher(ctx)
    text_body = str(body)
    assert "access_token" not in text_body
    assert "refresh_token" not in text_body
    assert str(ctx.settings.credential_dir or "") == "" or str(ctx.settings.credential_dir) not in text_body


def test_readyz_publisher_reports_invalid_credential(tmp_path) -> None:
    from datetime import UTC, datetime, timedelta

    from reel_harness.publisher.credentials import OAuthCredential

    ctx = _ctx(tmp_path)
    ctx.credential_backend().save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="s", provider="youtube", account_reference="default",
        invalid=True, last_refresh_error="invalid_grant",
    ))
    status_code, body = _get_readyz_publisher(ctx)
    assert status_code == 503
    assert body["ready"] is False
    assert "invalid" in body["checks"]["account_credential"]
