"""worker.publish_daemon.PublisherDaemon: idle exit, one-shot leasing,
max-publications, stop-on-error. FakePublisher only, no network."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_daemon import PublisherDaemon, PublisherDaemonConfig
from reel_harness.worker.publish_runner import PublishBundle

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"daemon-{seed}.mp4"
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


def _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, key: str):
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


def _daemon(session_factory, storage, tmp_path, **config_overrides) -> PublisherDaemon:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")

    def bundle_for_publication(_pub):
        return PublishBundle(publisher=FakePublisher(), session_store=UploadSessionStore(store))

    def channel_niche_for_job(_job):
        return "cooking"

    config = PublisherDaemonConfig(
        worker_id="test-publisher-daemon", poll_interval_seconds=0.05, lease_timeout_seconds=60,
        **config_overrides,
    )
    return PublisherDaemon(session_factory, storage, bundle_for_publication, channel_niche_for_job, config)


def test_idle_exit_when_nothing_to_lease(session_factory, storage, tmp_path) -> None:
    daemon = _daemon(session_factory, storage, tmp_path, idle_exit_after_seconds=0.1)
    exit_code = daemon.run()
    assert exit_code == 0
    assert daemon.stop_reason == "idle_exit"
    assert daemon.publications_processed == 0


def test_processes_one_publication_then_idle_exits(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "daemon-1")
    daemon = _daemon(session_factory, storage, tmp_path, idle_exit_after_seconds=0.1)
    exit_code = daemon.run()
    assert exit_code == 0
    assert daemon.publications_processed == 1

    from reel_harness.db.models import Publication

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.status == PublicationStatus.PUBLISHED.value


def test_max_publications_stops_after_the_configured_count(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    for i in range(3):
        _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, f"daemon-max-{i}")
    daemon = _daemon(session_factory, storage, tmp_path, max_publications=2, idle_exit_after_seconds=5)
    exit_code = daemon.run()
    assert exit_code == 0
    assert daemon.stop_reason == "max_publications"
    assert daemon.publications_processed == 2


def test_stop_on_error_exits_fatal_after_first_failed_publication(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    from reel_harness.db.models import Publication

    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "daemon-fail")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.publisher_config = {**(db_pub.publisher_config or {}), "publisher_provider": "fake"}
        session.commit()

    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")

    def bundle_for_publication(_pub):
        return PublishBundle(
            publisher=FakePublisher(mode="fail_processing"), session_store=UploadSessionStore(store),
        )

    config = PublisherDaemonConfig(
        worker_id="fail-daemon", poll_interval_seconds=0.05, lease_timeout_seconds=60,
        stop_on_error=True, idle_exit_after_seconds=5,
    )
    daemon = PublisherDaemon(session_factory, storage, bundle_for_publication, lambda j: None, config)
    exit_code = daemon.run()
    assert exit_code == 1
    assert daemon.stop_reason == "stop_on_error"

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.status == PublicationStatus.FAILED.value
