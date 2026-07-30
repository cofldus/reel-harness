from __future__ import annotations

import re

from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_IDEMPOTENCY_INPUT_RE = re.compile(r'name="idempotency_key" value="([^"]+)"')


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'web-test.db'}",
        jobs_dir=tmp_path / "jobs",
        app_api_key="a-real-non-placeholder-test-key",
    )
    return AppContext(settings=settings)


def _extract_csrf_and_idempotency(html: str) -> tuple[str, str]:
    csrf_match = _CSRF_INPUT_RE.search(html)
    idem_match = _IDEMPOTENCY_INPUT_RE.search(html)
    assert csrf_match, "csrf_token hidden field not found in rendered form"
    assert idem_match, "idempotency_key hidden field not found in rendered form"
    return csrf_match.group(1), idem_match.group(1)


def _set_status(ctx: AppContext, job_id: str, status: str, **extra) -> None:
    from reel_harness.db.models import Job

    with ctx.session_factory() as session:
        db_job = session.get(Job, job_id)
        db_job.status = status
        for key, value in extra.items():
            setattr(db_job, key, value)
        session.commit()


def test_dashboard_renders(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/")
        assert response.status_code == 200
        assert "새 영상 만들기" in response.text
        assert "rh_csrf" in response.cookies
    finally:
        app.dependency_overrides.clear()


def test_dashboard_shows_empty_state_with_no_jobs(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/")
        assert "아직 만든 영상이 없습니다" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_list_renders_and_filters(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="topic one")
        client = TestClient(app)
        response = client.get("/jobs")
        assert response.status_code == 200
        assert "topic one" in response.text

        filtered = client.get("/jobs?status_filter=FAILED")
        assert filtered.status_code == 200
        assert "해당 상태의 작업이 없습니다" in filtered.text
    finally:
        app.dependency_overrides.clear()


def test_new_job_form_renders_with_csrf_and_idempotency_fields(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/jobs/new")
        assert response.status_code == 200
        _extract_csrf_and_idempotency(response.text)  # raises if missing
        assert 'value="real" disabled' in response.text  # not configured on a fresh test Settings
    finally:
        app.dependency_overrides.clear()


def test_new_job_form_hides_fake_profile_by_default(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/jobs/new")
        assert 'value="fake"' not in response.text
    finally:
        app.dependency_overrides.clear()


def test_new_job_form_shows_fake_profile_when_env_flag_set(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("REEL_HARNESS_UI_SHOW_FAKE_PROFILE", "true")
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/jobs/new")
        assert 'value="fake"' in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_job_without_csrf_token_is_rejected(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).post("/jobs", data={
            "topic": "t", "language": "ko", "duration_seconds": 30, "style": "general",
            "provider_profile": "demo", "idempotency_key": "k1",
        })
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_create_job_with_valid_csrf_redirects_to_detail(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, idempotency_key = _extract_csrf_and_idempotency(form_page.text)

        response = client.post("/jobs", data={
            "topic": "김치찌개", "language": "ko", "duration_seconds": 30, "style": "cooking",
            "provider_profile": "demo", "idempotency_key": idempotency_key, "csrf_token": csrf_token,
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/jobs/")

        detail = client.get(response.headers["location"])
        assert detail.status_code == 200
        assert "김치찌개" in detail.text
    finally:
        app.dependency_overrides.clear()


def test_create_job_validation_errors_preserve_input(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, idempotency_key = _extract_csrf_and_idempotency(form_page.text)

        response = client.post("/jobs", data={
            "topic": "", "language": "ko", "duration_seconds": 30, "style": "cooking",
            "provider_profile": "demo", "idempotency_key": idempotency_key, "csrf_token": csrf_token,
        })
        assert response.status_code == 422
        assert "주제를 입력해주세요" in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_job_real_profile_refused_when_not_configured(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, idempotency_key = _extract_csrf_and_idempotency(form_page.text)

        response = client.post("/jobs", data={
            "topic": "t", "language": "ko", "duration_seconds": 30, "style": "cooking",
            "provider_profile": "real", "idempotency_key": idempotency_key, "csrf_token": csrf_token,
        })
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_job_detail_404_for_unknown_job(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/jobs/does-not-exist")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_job_status_fragment_returns_partial(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = TestClient(app).get(f"/jobs/{job.id}/status")
        assert response.status_code == 200
        assert 'id="job-status"' in response.text
        assert "<html" not in response.text  # a fragment, not a full page
    finally:
        app.dependency_overrides.clear()


def test_job_cancel_action_requires_csrf_and_succeeds_with_it(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)

        unauthed = client.post(f"/jobs/{job.id}/cancel")
        assert unauthed.status_code == 403

        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)
        response = client.post(
            f"/jobs/{job.id}/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert "취소됨" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_approve_refuses_when_not_review_required(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)

        response = client.post(f"/jobs/{job.id}/approve", headers={"X-CSRF-Token": csrf_token})
        assert response.status_code == 409  # job is QUEUED, not REVIEW_REQUIRED
    finally:
        app.dependency_overrides.clear()


def test_job_reject_without_csrf_is_rejected(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = TestClient(app).post(
            f"/jobs/{job.id}/reject", data={"reason": "not good", "regenerate_from_stage": "SCRIPT"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_job_reject_refuses_when_not_review_required(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)

        response = client.post(
            f"/jobs/{job.id}/reject", data={"reason": "not good", "regenerate_from_stage": "SCRIPT"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 409  # job is QUEUED, not REVIEW_REQUIRED
    finally:
        app.dependency_overrides.clear()


def test_job_reject_succeeds_when_review_required(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        _set_status(ctx, job.id, "REVIEW_REQUIRED", reason_code="USER_APPROVAL_REQUIRED")
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)

        response = client.post(
            f"/jobs/{job.id}/reject", data={"reason": "not good", "regenerate_from_stage": "SCRIPT"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert "재시도 대기 중" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_retry_without_csrf_is_rejected(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = TestClient(app).post(f"/jobs/{job.id}/retry", data={"stage": "SCRIPT"})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_job_retry_refuses_when_not_failed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)

        response = client.post(
            f"/jobs/{job.id}/retry", data={"stage": "SCRIPT"}, headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 409  # job is QUEUED, not FAILED
    finally:
        app.dependency_overrides.clear()


def test_job_retry_succeeds_when_failed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        _set_status(ctx, job.id, "FAILED", failure_code="X", failure_summary="boom")
        client = TestClient(app)
        form_page = client.get("/jobs/new")
        csrf_token, _ = _extract_csrf_and_idempotency(form_page.text)

        response = client.post(
            f"/jobs/{job.id}/retry", data={"stage": "SCRIPT"}, headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert "재시도 대기 중" in response.text
    finally:
        app.dependency_overrides.clear()


def test_status_fragment_response_carries_a_real_csrf_token_not_empty(tmp_path) -> None:
    """Regression test for a bug found by independent review: the status
    fragment (what every htmx poll AND every action response re-renders)
    must carry a real csrf_token into its own action forms' hidden fields
    -- not just the first full-page render -- or the no-JS <form> fallback
    silently 403s after the very first poll."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)
        client.get("/jobs/new")  # establishes the CSRF cookie in the client's jar

        fragment = client.get(f"/jobs/{job.id}/status")
        assert fragment.status_code == 200
        match = _CSRF_INPUT_RE.search(fragment.text)
        assert match, "fragment never renders a csrf_token hidden field at all"
        assert match.group(1), "fragment's csrf_token hidden field is empty"
    finally:
        app.dependency_overrides.clear()


def test_job_video_not_yet_available_returns_409(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = TestClient(app).get(f"/jobs/{job.id}/video")
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_job_video_streams_with_range_support(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        final_dir = ctx.storage.job_dir(job.id) / "final"
        final_dir.mkdir(parents=True)
        video_bytes = b"0123456789" * 100
        (final_dir / "final.mp4").write_bytes(video_bytes)

        client = TestClient(app)
        full = client.get(f"/jobs/{job.id}/video")
        assert full.status_code == 200
        assert full.headers["content-type"] == "video/mp4"
        assert full.headers["content-disposition"].startswith("inline")
        assert full.content == video_bytes

        ranged = client.get(f"/jobs/{job.id}/video", headers={"Range": "bytes=0-9"})
        assert ranged.status_code == 206
        assert ranged.content == video_bytes[:10]
        assert ranged.headers["content-range"] == f"bytes 0-9/{len(video_bytes)}"

        download = client.get(f"/jobs/{job.id}/video?download=1")
        assert download.headers["content-disposition"].startswith("attachment")
    finally:
        app.dependency_overrides.clear()


def test_video_route_never_accepts_a_client_supplied_path(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/jobs/../../etc/passwd/video")
        assert response.status_code in (404, 409)  # never a 200 leaking an arbitrary file
    finally:
        app.dependency_overrides.clear()


def test_system_status_page_renders(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/system")
        assert response.status_code == 200
        assert "demo_tts" in response.text
    finally:
        app.dependency_overrides.clear()


def test_settings_guide_page_never_exposes_secret_values(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/settings")
        assert response.status_code == 200
        assert "REEL_HARNESS_LLM_API_KEY" in response.text  # the env var NAME is fine
        assert ctx.settings.app_api_key not in response.text  # the actual key value is never fine
    finally:
        app.dependency_overrides.clear()


def test_every_response_has_security_headers(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]
    finally:
        app.dependency_overrides.clear()


def test_static_files_are_served(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        css = client.get("/static/app.css")
        assert css.status_code == 200
        htmx = client.get("/static/htmx.min.js")
        assert htmx.status_code == 200
    finally:
        app.dependency_overrides.clear()
