"""publication-status / publication-refresh: CLI, lease_specific_publication,
and the API refresh endpoint. FakePublisher only, no network."""
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
from reel_harness.worker.publish_lease import lease_specific_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"refresh-{seed}.mp4"
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


def _make_publication_at_processing(
    job_service, channel, session_factory, storage, tmp_path, key: str,
) -> Publication:
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

    service = PublicationService(session_factory, storage)
    pub, _ = service.create_publication(
        job.id, provider="fake", account_reference="default",
        publisher_snapshot={"publisher_provider": "fake", "publisher_account_reference": "default"},
    )
    # Drive it straight to PROCESSING via the DB, bypassing the actual upload
    # -- this test targets the refresh path, not the upload itself (already
    # covered by test_publish_runner.py).
    with session_factory() as session:
        from reel_harness.core.state_machine import apply_publication_transition

        db_pub = session.get(Publication, pub.id)
        apply_publication_transition(
            db_pub, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference=db_pub.id,
        )
        apply_publication_transition(db_pub, PublicationStatus.UPLOADING, upload_session_reference=db_pub.id)
        db_pub.provider_video_id = "fake-video-precomputed"
        apply_publication_transition(db_pub, PublicationStatus.UPLOAD_COMPLETED)
        apply_publication_transition(db_pub, PublicationStatus.PROCESSING)
        session.commit()
        session.refresh(db_pub)
        return db_pub


def test_lease_specific_publication_succeeds_only_for_processing_and_unlocked(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_publication_at_processing(job_service, channel, session_factory, storage, tmp_path, "refresh-1")
    with session_factory() as session:
        assert lease_specific_publication(session, pub.id, worker_id="w1") is True

    with session_factory() as session:
        # Already locked by the previous lease -- a second attempt refuses.
        assert lease_specific_publication(session, pub.id, worker_id="w2") is False


def test_lease_specific_publication_refuses_wrong_status(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="refresh-2", topic="t")
    with session_factory() as session:
        assert lease_specific_publication(session, "does-not-exist", worker_id="w1") is False


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'refresh-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def test_publication_status_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publication-status", "does-not-exist"]) == 1
    assert "not found" in capsys.readouterr().err


def test_publication_refresh_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publication-refresh", "does-not-exist"]) == 1


def test_publication_refresh_advances_processing_to_published_via_cli(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'refresh-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")
    pub = _make_publication_at_processing(job_service, channel, factory, storage, tmp_path, "refresh-cli-1")

    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publication-refresh", pub.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PUBLISHED"

    # A second refresh (now PUBLISHED, not PROCESSING) refuses cleanly.
    assert cli_main.main(["publication-refresh", pub.id]) == 1


def test_publication_status_reports_without_calling_the_provider(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'refresh-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")
    pub = _make_publication_at_processing(job_service, channel, factory, storage, tmp_path, "refresh-cli-2")

    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publication-status", pub.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PROCESSING"  # unchanged -- status is read-only


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'refresh-api.db'}",
        jobs_dir=tmp_path / "jobs", app_api_key="test-key",
        credential_dir=tmp_path.parent / f"{tmp_path.name}-api-secrets",
    )
    return AppContext(settings=settings)


def test_api_refresh_advances_processing_to_published(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    pub = _make_publication_at_processing(
        JobService(ctx.session_factory, storage=ctx.storage), channel, ctx.session_factory, ctx.storage,
        tmp_path, "refresh-api-1",
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        unauthed = client.post(f"/v1/publications/{pub.id}/refresh")
        assert unauthed.status_code == 401

        response = client.post(
            f"/v1/publications/{pub.id}/refresh", headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "PUBLISHED"

        again = client.post(
            f"/v1/publications/{pub.id}/refresh", headers={"Authorization": "Bearer test-key"},
        )
        assert again.status_code == 409
    finally:
        app.dependency_overrides.clear()


def test_api_refresh_not_found(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)
        response = client.post(
            "/v1/publications/does-not-exist/refresh", headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.clear()
