"""Web UI publication action routes: cancel/retry/refresh/reconcile
(reel_harness/web/router.py). Uses the real `fake` publisher provider
(needs no OAuth account) -- no network call, no mock. Mirrors
tests/unit/test_web_routes.py's job-action test shape: CSRF gating +
success + 409-on-violated-precondition for each action."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_publication_transition
from reel_harness.core.state_machine import apply_transition as apply_job_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _make_ctx(tmp_path, **settings_overrides) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'pub-actions-test.db'}",
        jobs_dir=tmp_path / "jobs",
        credential_dir=tmp_path / "credentials",
        app_api_key="a-real-non-placeholder-test-key",
        **settings_overrides,
    )
    return AppContext(settings=settings)


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"eligible-{seed}.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _make_eligible_job(ctx: AppContext, tmp_path, key: str) -> Job:
    channel = ctx.jobs.create_channel(name=f"c-{key}", niche="n", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key=key, topic=f"topic-{key}")
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    final_path = ctx.storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with ctx.session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        apply_job_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_job_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_job_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_job_transition(db_job, JobStatus.TTS_GENERATING)
        apply_job_transition(db_job, JobStatus.RENDERING)
        apply_job_transition(db_job, JobStatus.VALIDATING)
        apply_job_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        apply_job_transition(db_job, JobStatus.READY)
        apply_job_transition(db_job, JobStatus.COMPLETED)
        session.add(Asset(
            job_id=job.id, scene_index=0, source_provider="pexels", local_path=str(final_path),
            checksum_sha256=checksum, mime_type="video/mp4", license_type="CC-BY-4.0",
            commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
        ))
        session.commit()

    manifest = Manifest(
        job_id=job.id, created_at=datetime.now(UTC), topic="t", script_title="T",
        llm=LLMInfo(provider_id="fake", model_id="m", prompt_version="v"),
        tts=TTSInfo(provider_id="fake", voice_id="v1"),
        assets=[AssetInfo(
            scene_index=0, source_url="https://example.invalid/page", author="Creator",
            license_type="CC-BY-4.0", checksum_sha256=checksum,
            commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
        )],
        validation=ValidationInfo(duration_sec=5.0, video_codec="h264", audio_codec="aac", has_audio_stream=True),
        final_video_checksum_sha256=checksum,
        approval=ApprovalInfo(decision="approve", decided_at=datetime.now(UTC)),
    )
    write_manifest(ctx.storage, job.id, manifest)
    return ctx.jobs.get_job(job.id)


def _make_publication(ctx: AppContext, tmp_path, key: str) -> Publication:
    job = _make_eligible_job(ctx, tmp_path, key)
    pub, _ = ctx.publications.create_publication(job.id, provider="fake", account_reference="default")
    return pub


def _set_publication_status(ctx: AppContext, publication_id: str, status: PublicationStatus, **extra) -> None:
    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        apply_publication_transition(pub, status, **extra)
        session.commit()


def _csrf_token_from(client: TestClient, url: str) -> str:
    page = client.get(url)
    match = _CSRF_INPUT_RE.search(page.text)
    assert match, f"no csrf_token hidden field found on {url}"
    return match.group(1)


def test_cancel_requires_csrf(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "cancel-csrf-1")
        response = TestClient(app).post(f"/publications/{pub.id}/cancel")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_cancel_succeeds_when_allowed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "cancel-1")  # READY_TO_UPLOAD -- cancellable
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/publications/{pub.id}")
        response = client.post(
            f"/publications/{pub.id}/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert "취소됨" in response.text

        refreshed = ctx.publications.get_publication(pub.id)
        assert refreshed.status == PublicationStatus.CANCELLED.value
    finally:
        app.dependency_overrides.clear()


def test_cancel_refused_when_already_published(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "cancel-2")
        _set_publication_status(
            ctx, pub.id, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference="ref-1",
        )
        _set_publication_status(ctx, pub.id, PublicationStatus.UPLOADING, upload_session_reference="ref-1")
        _set_publication_status(ctx, pub.id, PublicationStatus.UPLOAD_COMPLETED)
        _set_publication_status(ctx, pub.id, PublicationStatus.PROCESSING)
        _set_publication_status(ctx, pub.id, PublicationStatus.PUBLISHED)

        client = TestClient(app)
        # A PUBLISHED publication offers no action forms at all (nothing is
        # legally clickable) -- fetch a token from a page that always has
        # one instead of this publication's own now-action-free page.
        csrf_token = _csrf_token_from(client, "/jobs/new")
        response = client.post(
            f"/publications/{pub.id}/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_cancel_refused_when_failed(tmp_path) -> None:
    """Regression: FAILED must be refused cleanly (see the
    core.publish_service fix this same session found), not crash."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "cancel-3")
        _set_publication_status(ctx, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="boom")

        client = TestClient(app)
        csrf_token = _csrf_token_from(client, "/jobs/new")
        response = client.post(
            f"/publications/{pub.id}/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_cancel_unknown_publication_404s(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, "/jobs/new")
        response = client.post(
            "/publications/does-not-exist/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_retry_requires_csrf(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "retry-csrf-1")
        response = TestClient(app).post(f"/publications/{pub.id}/retry", data={"from_stage": ""})
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_retry_refuses_when_not_retryable(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "retry-1")  # READY_TO_UPLOAD -- not in the retryable set
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/publications/{pub.id}")
        response = client.post(
            f"/publications/{pub.id}/retry", data={"from_stage": "", "csrf_token": csrf_token},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_retry_succeeds_when_failed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "retry-2")
        _set_publication_status(ctx, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="boom")

        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/publications/{pub.id}")
        response = client.post(
            f"/publications/{pub.id}/retry", data={"from_stage": "", "csrf_token": csrf_token},
        )
        assert response.status_code == 200

        refreshed = ctx.publications.get_publication(pub.id)
        assert refreshed.status == PublicationStatus.RETRY_WAIT.value
    finally:
        app.dependency_overrides.clear()


def test_refresh_requires_csrf(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "refresh-csrf-1")
        response = TestClient(app).post(f"/publications/{pub.id}/refresh")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_refresh_refuses_when_not_processing(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "refresh-1")  # READY_TO_UPLOAD, not PROCESSING
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/publications/{pub.id}")
        response = client.post(
            f"/publications/{pub.id}/refresh", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_reconcile_requires_csrf(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "reconcile-csrf-1")
        response = TestClient(app).post(f"/publications/{pub.id}/reconcile")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_reconcile_on_non_terminal_publication_shows_outcome(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "reconcile-1")
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/publications/{pub.id}")
        response = client.post(
            f"/publications/{pub.id}/reconcile", headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 200
        assert "동기화 결과" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_detail_shows_publish_button_when_eligible(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "jobdetail-1")
        response = TestClient(app).get(f"/jobs/{job.id}")
        assert response.status_code == 200
        assert f"/jobs/{job.id}/publish" in response.text
        assert "아직 게시된 항목이 없습니다" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_detail_lists_existing_publications(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "jobdetail-2")
        response = TestClient(app).get(f"/jobs/{pub.job_id}")
        assert response.status_code == 200
        assert f"/publications/{pub.id}" in response.text
    finally:
        app.dependency_overrides.clear()


def test_job_detail_hides_publish_button_for_ineligible_demo_job(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        with ctx.session_factory() as session:
            db_job = session.get(Job, job.id)
            apply_job_transition(db_job, JobStatus.SCRIPT_GENERATING)
            apply_job_transition(db_job, JobStatus.POLICY_CHECKING)
            apply_job_transition(db_job, JobStatus.ASSET_FETCHING)
            apply_job_transition(db_job, JobStatus.TTS_GENERATING)
            apply_job_transition(db_job, JobStatus.RENDERING)
            apply_job_transition(db_job, JobStatus.VALIDATING)
            apply_job_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
            apply_job_transition(db_job, JobStatus.READY)
            apply_job_transition(db_job, JobStatus.COMPLETED)
            session.commit()
        response = TestClient(app).get(f"/jobs/{job.id}")
        assert response.status_code == 200
        assert f"/jobs/{job.id}/publish" not in response.text
    finally:
        app.dependency_overrides.clear()
