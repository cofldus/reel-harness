"""publication-retry: CLI and the API endpoint wiring. Core policy is
covered by tests/unit/test_publish_retry.py; this file only proves the
CLI/API surface calls it correctly. FakePublisher only, no network."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.cli import main as cli_main
from reel_harness.config import Settings
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"retry-cli-{seed}.mp4"
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


def _make_failed_publication(job_service, channel, session_factory, storage, tmp_path, key: str) -> Publication:
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        for status in (
            JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING, JobStatus.ASSET_FETCHING,
            JobStatus.TTS_GENERATING, JobStatus.RENDERING, JobStatus.VALIDATING,
        ):
            apply_transition(db_job, status)
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
    write_manifest(storage, job.id, manifest)

    pub_service = PublicationService(session_factory, storage)
    pub, _ = pub_service.create_publication(
        job.id, provider="fake", account_reference="default",
        publisher_snapshot={"publisher_provider": "fake", "publisher_account_reference": "default"},
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.status = PublicationStatus.FAILED.value
        db_pub.failure_code = "SIMULATED"
        db_pub.failure_summary = "simulated failure for retry tests"
        session.commit()
    return pub


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'retry-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def test_publication_retry_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publication-retry", "does-not-exist"]) == 1
    assert "not found" in capsys.readouterr().err


def test_publication_retry_succeeds_via_cli(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'retry-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")
    pub = _make_failed_publication(job_service, channel, factory, storage, tmp_path, "retry-cli-1")

    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publication-retry", pub.id])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["retried"] is True
    assert payload["target_status"] == PublicationStatus.READY_TO_UPLOAD.value


def test_publication_retry_refusal_reports_reasons_and_exits_nonzero(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'retry-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")
    pub = _make_failed_publication(job_service, channel, factory, storage, tmp_path, "retry-cli-2")
    with factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.status = PublicationStatus.PUBLISHED.value
        db_pub.published_at = datetime.now(UTC)
        session.commit()

    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publication-retry", pub.id])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["retried"] is False
    assert "already PUBLISHED" in payload["reasons"][0]


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'retry-api.db'}",
        jobs_dir=tmp_path / "jobs", app_api_key="test-key",
        credential_dir=tmp_path.parent / f"{tmp_path.name}-api-secrets",
    )
    return AppContext(settings=settings)


def test_api_retry_not_found(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/publications/does-not-exist/retry", headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()


def test_api_retry_requires_auth(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post("/v1/publications/does-not-exist/retry")
        assert response.status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_api_retry_succeeds(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    pub = _make_failed_publication(
        JobService(ctx.session_factory, storage=ctx.storage), channel, ctx.session_factory, ctx.storage,
        tmp_path, "retry-api-1",
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post(
            f"/v1/publications/{pub.id}/retry", headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        assert response.json()["retried"] is True
    finally:
        app.dependency_overrides.clear()


def test_api_retry_refusal_returns_409_with_reasons(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    pub = _make_failed_publication(
        JobService(ctx.session_factory, storage=ctx.storage), channel, ctx.session_factory, ctx.storage,
        tmp_path, "retry-api-2",
    )
    with ctx.session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.status = PublicationStatus.CANCELLED.value
        session.commit()

    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post(
            f"/v1/publications/{pub.id}/retry", headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 409
        assert "CANCELLED" in response.json()["detail"]["reasons"][0]
    finally:
        app.dependency_overrides.clear()


def test_api_retry_accepts_an_explicit_from_stage(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    pub = _make_failed_publication(
        JobService(ctx.session_factory, storage=ctx.storage), channel, ctx.session_factory, ctx.storage,
        tmp_path, "retry-api-3",
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post(
            f"/v1/publications/{pub.id}/retry", headers={"Authorization": "Bearer test-key"},
            json={"from_stage": "SESSION"},
        )
        assert response.status_code == 200
        assert response.json()["target_status"] == PublicationStatus.READY_TO_UPLOAD.value
    finally:
        app.dependency_overrides.clear()
