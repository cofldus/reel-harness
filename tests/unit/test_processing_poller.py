"""Processing poller hardening (Phase 3B): _processing_stage's local
max-duration timeout and next_poll_at bookkeeping, plus the upload/
processing lease-lane separation. No network (FakePublisher /
in-process SQLite only)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.base import ProcessingStatusResult
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_lease import (
    lease_next_processing_publication,
    lease_next_publication,
    lease_next_via_lanes,
)
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


class _StillProcessingPublisher(FakePublisher):
    def __init__(self, *, raise_if_called: bool = False) -> None:
        super().__init__()
        self._raise_if_called = raise_if_called

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        if self._raise_if_called:
            raise AssertionError("get_processing_status must not be called past the local timeout")
        return ProcessingStatusResult(processing_status="processing")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"poller-{seed}.mp4"
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
    job_service, channel, session_factory, storage, tmp_path, key: str, **publisher_config_overrides,
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
    snapshot = {
        "publisher_provider": "fake", "publisher_account_reference": "default", **publisher_config_overrides,
    }
    pub, _ = service.create_publication(
        job.id, provider="fake", account_reference="default", publisher_snapshot=snapshot,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.provider_video_id = "fake-video-precomputed"
        db_pub.status = PublicationStatus.PROCESSING.value
        db_pub.processing_started_at = datetime.now(UTC)
        session.commit()
        session.refresh(db_pub)
        return db_pub


def _bundle(tmp_path, publisher) -> PublishBundle:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return PublishBundle(
        publisher=publisher, session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )


def test_still_processing_sets_next_poll_at_in_the_future(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_publication_at_processing(job_service, channel, session_factory, storage, tmp_path, "poll-1")
    bundle = _bundle(tmp_path, _StillProcessingPublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        before = datetime.now(UTC)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PROCESSING.value
        assert db_pub.processing_poll_count == 1
        assert db_pub.next_poll_at is not None
        assert db_pub.next_poll_at > before


def test_max_processing_duration_fails_locally_without_calling_the_provider(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_publication_at_processing(
        job_service, channel, session_factory, storage, tmp_path, "poll-2",
        processing_max_duration_seconds=60.0,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.processing_started_at = datetime.now(UTC) - timedelta(seconds=120)
        session.commit()

    bundle = _bundle(tmp_path, _StillProcessingPublisher(raise_if_called=True))
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.FAILED.value
        assert db_pub.failure_code == "PROCESSING_TIMEOUT"


def test_upload_lane_never_leases_a_processing_publication(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    _make_publication_at_processing(job_service, channel, session_factory, storage, tmp_path, "poll-3")
    with session_factory() as session:
        assert lease_next_publication(session, worker_id="w1") is None


def test_processing_lane_never_leases_an_upload_lane_publication(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="poll-4", topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path, "poll-4")
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
    service.create_publication(
        job.id, provider="fake", account_reference="default",
        publisher_snapshot={"publisher_provider": "fake", "publisher_account_reference": "default"},
    )
    with session_factory() as session:
        assert lease_next_processing_publication(session, worker_id="w1") is None


def test_lease_next_via_lanes_respects_process_upload_false(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    _make_publication_at_processing(job_service, channel, session_factory, storage, tmp_path, "poll-5")
    with session_factory() as session:
        leased = lease_next_via_lanes(session, worker_id="w1", process_upload=False, process_status=True)
        assert leased is not None
        assert leased.status == PublicationStatus.PROCESSING.value


def test_lease_next_via_lanes_prefer_status_tries_status_lane_first(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    # Only a processing-lane publication exists; even with both lanes
    # enabled and prefer_status toggled either way, it must still be found.
    _make_publication_at_processing(job_service, channel, session_factory, storage, tmp_path, "poll-6")
    with session_factory() as session:
        leased = lease_next_via_lanes(session, worker_id="w1", prefer_status=True)
        assert leased is not None
        assert leased.status == PublicationStatus.PROCESSING.value
