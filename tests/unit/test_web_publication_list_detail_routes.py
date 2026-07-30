"""Web UI publication list (/publications), detail (/publications/{id}), and
status polling fragment (/publications/{id}/status). Uses the `fake`
publisher provider (needs no OAuth account) to create real Publication rows
via the actual service layer -- no network call, no mock."""
from __future__ import annotations

import hashlib
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


def _make_ctx(tmp_path, **settings_overrides) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'pub-list-detail-test.db'}",
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


def _make_eligible_job(ctx: AppContext, tmp_path, key: str, topic: str = "topic") -> Job:
    channel = ctx.jobs.create_channel(name=f"c-{key}", niche="n", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key=key, topic=topic)
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
        job_id=job.id, created_at=datetime.now(UTC), topic=topic, script_title="T",
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


def _make_publication(ctx: AppContext, tmp_path, key: str, topic: str = "topic") -> Publication:
    job = _make_eligible_job(ctx, tmp_path, key, topic=topic)
    pub, _ = ctx.publications.create_publication(job.id, provider="fake", account_reference="default")
    return pub


def test_publications_list_shows_empty_state(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/publications")
        assert response.status_code == 200
        assert "해당 상태의 게시가 없습니다" in response.text
    finally:
        app.dependency_overrides.clear()


def test_publications_list_shows_created_publication_with_job_topic(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        _make_publication(ctx, tmp_path, "list-1", topic="김치찌개 영상")
        response = TestClient(app).get("/publications")
        assert response.status_code == 200
        assert "김치찌개 영상" in response.text
        assert "Fake" in response.text
    finally:
        app.dependency_overrides.clear()


def test_publications_list_filters_by_status(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        _make_publication(ctx, tmp_path, "filter-1", topic="첫번째")
        client = TestClient(app)
        filtered = client.get("/publications?status_filter=PUBLISHED")
        assert "해당 상태의 게시가 없습니다" in filtered.text

        unfiltered = client.get("/publications?status_filter=READY_TO_UPLOAD")
        assert "첫번째" in unfiltered.text
    finally:
        app.dependency_overrides.clear()


def test_publications_list_paginates(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        for i in range(3):
            _make_publication(ctx, tmp_path, f"page-{i}", topic=f"topic-{i}")
        client = TestClient(app)
        response = client.get("/publications")
        assert response.status_code == 200
        assert "총 3개" in response.text
    finally:
        app.dependency_overrides.clear()


def test_publication_detail_404_for_unknown_publication(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        response = TestClient(app).get("/publications/does-not-exist")
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_publication_detail_renders_status_and_provider(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "detail-1", topic="상세 테스트")
        response = TestClient(app).get(f"/publications/{pub.id}")
        assert response.status_code == 200
        assert "상세 테스트" in response.text
        assert "Fake" in response.text
        assert pub.id in response.text
    finally:
        app.dependency_overrides.clear()


def test_publication_status_fragment_returns_partial(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "fragment-1")
        response = TestClient(app).get(f"/publications/{pub.id}/status")
        assert response.status_code == 200
        assert 'id="publication-status"' in response.text
        assert "<html" not in response.text  # a fragment, not a full page
    finally:
        app.dependency_overrides.clear()


def test_publication_status_fragment_keeps_polling_while_active(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "polling-1")  # READY_TO_UPLOAD -- active, not terminal
        response = TestClient(app).get(f"/publications/{pub.id}/status")
        assert f"/publications/{pub.id}/status" in response.text
        assert "hx-trigger" in response.text
    finally:
        app.dependency_overrides.clear()


def test_publication_status_fragment_stops_polling_once_published(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "polling-2")
        with ctx.session_factory() as session:
            db_pub = session.get(Publication, pub.id)
            apply_publication_transition(
                db_pub, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference="ref-1",
            )
            apply_publication_transition(db_pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")
            apply_publication_transition(db_pub, PublicationStatus.UPLOAD_COMPLETED)
            apply_publication_transition(db_pub, PublicationStatus.PROCESSING)
            apply_publication_transition(db_pub, PublicationStatus.PUBLISHED)
            session.commit()

        response = TestClient(app).get(f"/publications/{pub.id}/status")
        assert "hx-trigger" not in response.text
        assert "게시 완료" in response.text
    finally:
        app.dependency_overrides.clear()


def test_publication_status_fragment_stops_polling_once_failed(tmp_path) -> None:
    """FAILED is a needs-action status (per PUBLICATION_NEEDS_ACTION_STATUSES,
    mirroring publish_retry's real retryable set), not just PUBLICATION_TERMINAL_STATUSES."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        pub = _make_publication(ctx, tmp_path, "polling-3")
        with ctx.session_factory() as session:
            db_pub = session.get(Publication, pub.id)
            apply_publication_transition(
                db_pub, PublicationStatus.FAILED, failure_code="X", failure_summary="boom",
            )
            session.commit()

        response = TestClient(app).get(f"/publications/{pub.id}/status")
        assert "hx-trigger" not in response.text
        assert "boom" in response.text
    finally:
        app.dependency_overrides.clear()
