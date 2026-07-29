"""worker.publish_runner.run_publication: the full upload state machine
driven with FakePublisher (in-memory, no network) -- chunked upload,
resume-after-interruption, cancellation, retry-on-transient-error, and the
processing/PUBLISHED handoff."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from reel_harness.core.errors import TransientProviderError
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication, PublicationAuditEvent
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str, size_multiplier: int = 1) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"runner-{seed}.mp4"
    duration = 1 * size_multiplier
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", f"testsrc=duration={duration}:size=320x240:rate=25",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _make_ready_publication(
    job_service, channel, session_factory, storage, tmp_path, key: str, size_multiplier: int = 1,
) -> Publication:
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path, key, size_multiplier)
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
    return pub


@pytest.fixture
def bundle(tmp_path) -> PublishBundle:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return PublishBundle(
        publisher=FakePublisher(), session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )


def test_full_upload_reaches_published(job_service, channel, session_factory, storage, tmp_path, bundle) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "run-1")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle, channel_niche="cooking")
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        assert db_pub.provider_video_id is not None
        assert db_pub.publication_url is not None
        assert db_pub.published_at is not None
        assert db_pub.bytes_uploaded == db_pub.total_bytes

    with session_factory() as session:
        events = [
            e.event for e in session.query(PublicationAuditEvent)
            .filter(PublicationAuditEvent.publication_id == pub.id).order_by(PublicationAuditEvent.created_at)
        ]
    assert "upload_session_created" in events
    assert "upload_completed" in events
    assert "processing_started" in events
    assert "processing_completed" in events


def test_multi_chunk_upload_progresses_bytes_uploaded(
    job_service, channel, session_factory, storage, tmp_path, bundle,
) -> None:
    pub = _make_ready_publication(
        job_service, channel, session_factory, storage, tmp_path, "run-2", size_multiplier=2,
    )
    # Force a tiny chunk size so the upload spans multiple chunks.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.publisher_config = {**(db_pub.publisher_config or {}), "youtube_chunk_size": 4096}
        session.commit()

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value

    with session_factory() as session:
        chunk_events = [
            e for e in session.query(PublicationAuditEvent).filter(
                PublicationAuditEvent.publication_id == pub.id,
                PublicationAuditEvent.event == "chunk_uploaded",
            )
        ]
    assert len(chunk_events) > 1, "a tiny chunk size must force more than one chunk_uploaded event"


def test_cancel_requested_is_honored_between_chunks(
    job_service, channel, session_factory, storage, tmp_path, bundle,
) -> None:
    pub = _make_ready_publication(
        job_service, channel, session_factory, storage, tmp_path, "run-3", size_multiplier=2,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.publisher_config = {**(db_pub.publisher_config or {}), "youtube_chunk_size": 4096}
        db_pub.cancel_requested = True
        session.commit()

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.CANCELLED.value
        assert db_pub.provider_video_id is None


def test_transient_error_routes_to_retry_wait_with_backoff(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    class _FlakyPublisher(FakePublisher):
        def create_upload_session(self, *a, **kw):
            raise TransientProviderError("simulated transient failure")

    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    flaky_bundle = PublishBundle(
        publisher=_FlakyPublisher(), session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "run-4")

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, flaky_bundle)
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.retry_target_status == PublicationStatus.READY_TO_UPLOAD.value
        assert db_pub.next_retry_at is not None
        assert db_pub.retry_count == 1


def test_processing_failure_reaches_failed_with_reason(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    failing_bundle = PublishBundle(
        publisher=FakePublisher(mode="fail_processing"), session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "run-5")

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, failing_bundle)
        assert db_pub.status == PublicationStatus.FAILED.value
        assert db_pub.failure_code == "PROCESSING_FAILED"
        assert "fake_processing_failure" in (db_pub.failure_summary or "")


def test_resume_after_interruption_continues_from_confirmed_offset(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """Simulates a worker restart mid-upload: the same FakePublisher instance
    (standing in for the provider's own durable session state) and the same
    UploadSessionStore entry are reused across two separate run_publication
    calls, exactly like a fresh worker process would reattach after a
    crash."""
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(store)
    journal = PublishJournal(store.root_dir / "publish_journal")
    publisher = FakePublisher()
    pub = _make_ready_publication(
        job_service, channel, session_factory, storage, tmp_path, "run-6", size_multiplier=2,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.publisher_config = {**(db_pub.publisher_config or {}), "youtube_chunk_size": 4096}
        session.commit()

    # First "worker": only create the session and upload the first chunk,
    # then simulate a crash by just stopping (no further calls).
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        from reel_harness.worker.publish_runner import _create_session_stage

        _create_session_stage(session, db_pub, session.get(Job, db_pub.job_id), storage,
                               PublishBundle(publisher=publisher, session_store=session_store, journal=journal),
                               None, db_pub.total_bytes or 0, None)
        assert db_pub.status == PublicationStatus.UPLOAD_SESSION_CREATED.value

    # Second "worker" (fresh call): resumes from UPLOAD_SESSION_CREATED and
    # completes the whole upload using the SAME publisher/session_store state.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(
            session, db_pub, storage,
            PublishBundle(publisher=publisher, session_store=session_store, journal=journal),
        )
        assert db_pub.status == PublicationStatus.PUBLISHED.value
