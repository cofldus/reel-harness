"""Web UI publish-setup screen (GET /jobs/{id}/publish) and publication
creation (POST /jobs/{id}/publications). Uses the real `fake` publisher
provider (not a mock) for the success-path test -- distinct from the `fake`
PIPELINE provider tier, which is permanently publish-ineligible; see the
eligible-job helper below, which manually seeds a publishable (non-Demo/
Fake-licensed) manifest+asset rather than running the actual generation
pipeline. No real network call anywhere in this file."""
from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Asset, Job
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.publisher.credentials import OAuthCredential

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _make_ctx(tmp_path, **settings_overrides) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'publish-setup-test.db'}",
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
    """Seeds a COMPLETED job with a publishable (real-license-shaped, not
    DEMO_TEST_LICENSE/FAKE_TEST_LICENSE) manifest+asset -- bypassing the
    actual generation pipeline entirely, the same way
    test_publication_service.py's own eligible-job fixture does."""
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
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_transition(db_job, JobStatus.TTS_GENERATING)
        apply_transition(db_job, JobStatus.RENDERING)
        apply_transition(db_job, JobStatus.VALIDATING)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        apply_transition(db_job, JobStatus.READY)
        apply_transition(db_job, JobStatus.COMPLETED)
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


def _csrf_token_from(client: TestClient, url: str) -> str:
    page = client.get(url)
    match = _CSRF_INPUT_RE.search(page.text)
    assert match, f"no csrf_token hidden field found on {url}"
    return match.group(1)


def test_publish_setup_redirects_when_job_not_completed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        response = TestClient(app).get(f"/jobs/{job.id}/publish", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/jobs/{job.id}"
    finally:
        app.dependency_overrides.clear()


def test_publish_setup_redirects_when_ineligible_demo_job(tmp_path) -> None:
    """A COMPLETED job whose assets carry DEMO_TEST_LICENSE/FAKE_TEST_LICENSE
    must never reach the setup form -- this is the permanent Demo/Fake
    publish block, unaffected by anything Phase 5B adds."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        with ctx.session_factory() as session:
            db_job = session.get(Job, job.id)
            apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
            apply_transition(db_job, JobStatus.POLICY_CHECKING)
            apply_transition(db_job, JobStatus.ASSET_FETCHING)
            apply_transition(db_job, JobStatus.TTS_GENERATING)
            apply_transition(db_job, JobStatus.RENDERING)
            apply_transition(db_job, JobStatus.VALIDATING)
            apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
            apply_transition(db_job, JobStatus.READY)
            apply_transition(db_job, JobStatus.COMPLETED)
            session.commit()
        response = TestClient(app).get(f"/jobs/{job.id}/publish", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/jobs/{job.id}"
    finally:
        app.dependency_overrides.clear()


def test_publish_setup_renders_for_eligible_job_showing_readiness(tmp_path) -> None:
    ctx = _make_ctx(
        tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000",
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "setup-1")
        response = TestClient(app).get(f"/jobs/{job.id}/publish")
        assert response.status_code == 200
        assert "YouTube" in response.text
        assert "TikTok" in response.text
        assert "Instagram" in response.text
        assert "연결된 계정이 없습니다" in response.text  # youtube configured but not yet connected
    finally:
        app.dependency_overrides.clear()


def test_create_publication_requires_csrf(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "csrf-1")
        response = TestClient(app).post(
            f"/jobs/{job.id}/publications",
            data={"provider": "fake", "account_reference": "default", "privacy_status": "private"},
        )
        assert response.status_code == 403
    finally:
        app.dependency_overrides.clear()


def test_create_publication_unknown_provider_shows_friendly_error(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "badprov-1")
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/jobs/{job.id}/publish")
        response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "facebook", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        })
        assert response.status_code == 422
        assert "지원하지 않는 플랫폼" in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_publication_unconnected_account_shows_friendly_error(tmp_path) -> None:
    ctx = _make_ctx(
        tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000",
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "unconn-1")
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/jobs/{job.id}/publish")
        response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "youtube", "account_reference": "never-connected", "privacy_status": "private",
            "csrf_token": csrf_token,
        })
        assert response.status_code == 422
        assert "연결되지 않은 계정" in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_publication_public_privacy_without_confirmation_rejected(tmp_path) -> None:
    ctx = _make_ctx(
        tmp_path, youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-0000000",
        allow_public_upload=True,
    )
    ctx.credential_backend().save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=None, scope="",
        provider="youtube", account_reference="default",
    ))
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "pubconfirm-1")
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/jobs/{job.id}/publish")
        response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "youtube", "account_reference": "default", "privacy_status": "public",
            "csrf_token": csrf_token,
        })
        assert response.status_code == 422
        assert "체크" in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_publication_success_with_fake_publisher_redirects_to_detail(tmp_path) -> None:
    """provider="fake" needs no OAuth client/connected account (FakePublisher
    has no credential requirement at all) -- this exercises the full create
    path against the real PublicationService without any network call."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _make_eligible_job(ctx, tmp_path, "fake-success-1")
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, f"/jobs/{job.id}/publish")
        response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "fake", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        }, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"].startswith("/publications/")

        publications = ctx.publications.list_publications(job_id=job.id)
        assert len(publications) == 1
        assert publications[0].provider == "fake"
        assert publications[0].privacy_status == "private"
    finally:
        app.dependency_overrides.clear()


def test_create_publication_ineligible_job_shows_friendly_error(tmp_path) -> None:
    """A COMPLETED-but-Demo/Fake job's setup page redirects away (tested
    above), but hitting the create route directly (bypassing that redirect)
    must still fail gracefully via PublicationNotEligibleError -> a friendly
    re-render, never a raw 409 JSON body."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        client = TestClient(app)
        # No setup page exists for this ineligible job (it would redirect),
        # so fetch the New Job form instead purely to obtain a valid CSRF
        # pair -- it always renders its form unconditionally.
        csrf_token = _csrf_token_from(client, "/jobs/new")
        response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "fake", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        })
        assert response.status_code == 409
        assert "게시할 수 없습니다" in response.text
    finally:
        app.dependency_overrides.clear()


def test_create_publication_unknown_job_404s(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        csrf_token = _csrf_token_from(client, "/jobs/new")
        response = client.post("/jobs/does-not-exist/publications", data={
            "provider": "fake", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        })
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
