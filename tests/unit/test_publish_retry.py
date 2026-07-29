"""core.publish_retry.retry_publication: manual-retry policy for stuck
publications. No network (FakePublisher only)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from reel_harness.core.publish_retry import PublicationRetryError, retry_publication
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle, _create_session_stage

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"retry-{seed}.mp4"
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


def _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, key: str) -> Publication:
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
    return pub


def _bundle(tmp_path) -> PublishBundle:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return PublishBundle(
        publisher=FakePublisher(), session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )


def _set_status(session_factory, pub_id: str, status: PublicationStatus, **fields) -> None:
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        db_pub.status = status.value
        for key, value in fields.items():
            setattr(db_pub, key, value)
        session.commit()


@pytest.mark.parametrize("terminal_status,expected_phrase", [
    (PublicationStatus.PUBLISHED, "already PUBLISHED"),
    (PublicationStatus.CANCELLED, "CANCELLED"),
    (PublicationStatus.REVIEW_REQUIRED, "REVIEW_REQUIRED"),
])
def test_refuses_non_retryable_terminal_statuses(
    job_service, channel, session_factory, storage, tmp_path, terminal_status, expected_phrase,
) -> None:
    key = f"retry-{terminal_status}"
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, key)
    _set_status(session_factory, pub.id, terminal_status)
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        with pytest.raises(PublicationRetryError) as exc_info:
            retry_publication(session, db_pub, storage)
        assert expected_phrase in exc_info.value.reasons[0]


def test_refuses_an_active_status_and_points_to_reconcile(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-active")
    _set_status(session_factory, pub.id, PublicationStatus.UPLOADING, upload_session_reference=pub.id)
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        with pytest.raises(PublicationRetryError) as exc_info:
            retry_publication(session, db_pub, storage)
        assert "publication-reconcile" in exc_info.value.reasons[0]


def test_refuses_an_unknown_from_stage(job_service, channel, session_factory, storage, tmp_path) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-badstage")
    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        with pytest.raises(PublicationRetryError, match="unknown --from-stage"):
            retry_publication(session, db_pub, storage, from_stage="NOT_A_STAGE")


def test_refuses_processing_stage_without_a_known_video_id(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-noproc")
    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        with pytest.raises(PublicationRetryError, match="PROCESSING requires"):
            retry_publication(session, db_pub, storage, from_stage="PROCESSING")


def test_auto_target_is_ready_to_upload_when_nothing_started(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-auto1")
    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = retry_publication(session, db_pub, storage)
        assert result.target_status == PublicationStatus.READY_TO_UPLOAD.value
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.retry_target_status == PublicationStatus.READY_TO_UPLOAD.value
        assert db_pub.next_retry_at is not None


def test_auto_target_is_uploading_when_a_session_exists(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-auto2")
    bundle = _bundle(tmp_path)
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        total_bytes = (storage.job_dir(job.id) / "final" / "final.mp4").stat().st_size
        _create_session_stage(session, db_pub, job, storage, bundle, None, total_bytes, None)
    _set_status(session_factory, pub.id, PublicationStatus.AUTH_REQUIRED, failure_code="UPSTREAM_AUTH",
                failure_summary="token expired")

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = retry_publication(session, db_pub, storage)
        assert result.target_status == PublicationStatus.UPLOADING.value
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value


def test_auto_target_is_processing_when_a_video_id_is_known(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-auto3")
    _set_status(
        session_factory, pub.id, PublicationStatus.QUOTA_BLOCKED,
        provider_video_id="fake-video-123", failure_code="UPSTREAM_QUOTA_EXCEEDED", failure_summary="quota",
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = retry_publication(session, db_pub, storage)
        assert result.target_status == PublicationStatus.PROCESSING.value
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value


def test_explicit_from_stage_overrides_auto_selection(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-explicit")
    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = retry_publication(session, db_pub, storage, from_stage="SESSION")
        assert result.target_status == PublicationStatus.READY_TO_UPLOAD.value


def test_retry_from_retry_wait_brings_the_timer_forward(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-wait")
    far_future = datetime.now(UTC) + timedelta(hours=6)
    _set_status(
        session_factory, pub.id, PublicationStatus.RETRY_WAIT,
        retry_target_status=PublicationStatus.READY_TO_UPLOAD.value, next_retry_at=far_future,
        failure_code="X", failure_summary="x",
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = retry_publication(session, db_pub, storage)
        assert result.target_status == PublicationStatus.READY_TO_UPLOAD.value
        assert db_pub.next_retry_at < far_future


def test_refuses_retry_when_the_job_is_no_longer_eligible(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-ineligible")
    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        (storage.job_dir(job.id) / "final" / "final.mp4").unlink()

        with pytest.raises(PublicationRetryError) as exc_info:
            retry_publication(session, db_pub, storage)
        assert "FINAL_VIDEO_MISSING" in exc_info.value.reasons


def test_refuses_retry_when_the_metadata_fingerprint_no_longer_matches(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "retry-fingerprint")
    bundle = _bundle(tmp_path)
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        total_bytes = (storage.job_dir(job.id) / "final" / "final.mp4").stat().st_size
        _create_session_stage(session, db_pub, job, storage, bundle, None, total_bytes, None)
        assert db_pub.metadata_fingerprint is not None

    _set_status(session_factory, pub.id, PublicationStatus.FAILED, failure_code="X", failure_summary="x")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        # Something about the pinned metadata policy changed since the
        # session was created (e.g. the channel niche/category default).
        db_pub.publisher_config = {**(db_pub.publisher_config or {}), "youtube_category_id": "27"}
        session.commit()

        with pytest.raises(PublicationRetryError, match="no longer matches"):
            retry_publication(session, db_pub, storage)
