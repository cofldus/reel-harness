"""Publication-lease takeover: worker A stalls mid-upload, its lease expires
and is reclaimed, worker B finishes the upload for real; when A finally
wakes up and tries to commit, the fenced commit refuses and A never
overwrites B's result. Mirrors tests/integration/test_asset_lease_takeover.py
for the render pipeline."""
from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime, timedelta

import pytest

from reel_harness.core.publish_service import PublicationService
from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.fake_publisher import FakePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.publish_lease import (
    find_orphaned_active_publications,
    lease_next_publication,
    recover_stale_publications,
    release_publication_lease,
)
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


class _GatedPublisher(FakePublisher):
    """Real FakePublisher behavior, but the first upload_chunk() call blocks
    until released -- simulating a worker stuck mid-upload while its lease
    expires."""

    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()
        self._blocked_once = False

    def upload_chunk(self, session, chunk, start_byte, total_bytes):
        if not self._blocked_once:
            self._blocked_once = True
            self.entered.set()
            assert self.release.wait(timeout=60), "test forgot to release the gated publisher"
        return super().upload_chunk(session, chunk, start_byte, total_bytes)


def _faststart_mp4_bytes(tmp_path) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / "takeover.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def test_expired_lease_during_upload_is_taken_over_and_late_worker_fenced_out(tmp_path) -> None:
    db_path = tmp_path / "pub-takeover.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(session_factory, storage=storage)
    channel = job_service.create_channel(name="mw-pub", niche="cooking", language="en")

    job, _ = job_service.create_job(channel.id, idempotency_key="pub-takeover-1", topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path)
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
    publication, _ = pub_service.create_publication(
        job.id, provider="fake", account_reference="default",
        publisher_snapshot={
            "publisher_provider": "fake", "publisher_account_reference": "default",
            "youtube_chunk_size": 4096,
        },
    )

    secret_store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(secret_store)
    journal = PublishJournal(secret_store.root_dir / "publish_journal")
    gated = _GatedPublisher()
    bundle_a = PublishBundle(publisher=gated, session_store=session_store, journal=journal)

    with session_factory() as session:
        leased = lease_next_publication(session, worker_id="worker-a")
        token_a = leased.lease_token
        assert token_a is not None

    def worker_a() -> None:
        with session_factory() as session:
            pub_a = session.get(Publication, publication.id)
            run_publication(session, pub_a, storage, bundle_a, lease_token=token_a)

    thread_a = threading.Thread(target=worker_a, name="worker-a")
    thread_a.start()
    try:
        assert gated.entered.wait(timeout=30), "worker A never reached upload_chunk"

        future = datetime.now(UTC) + timedelta(hours=1)
        with session_factory() as session:
            recovered = recover_stale_publications(session, lease_timeout_seconds=60, now=future)
            assert recovered == [publication.id]

            leased_b = lease_next_publication(session, worker_id="worker-b", now=future + timedelta(minutes=1))
            assert leased_b is not None and leased_b.id == publication.id
            token_b = leased_b.lease_token
            assert token_b is not None and token_b != token_a

            bundle_b = PublishBundle(
                publisher=FakePublisher(), session_store=UploadSessionStore(secret_store), journal=journal,
            )
            run_publication(session, leased_b, storage, bundle_b, lease_token=token_b)
            status_after_b = leased_b.status
            video_id_b = leased_b.provider_video_id
            release_publication_lease(session, leased_b, lease_token=token_b)
    finally:
        gated.release.set()
        thread_a.join(timeout=60)
    assert not thread_a.is_alive()

    # B's own fresh session (worker A's stale session/session-store entry
    # was overwritten by B's own create_upload_session call) reaches PUBLISHED.
    assert status_after_b == PublicationStatus.PUBLISHED.value
    assert video_id_b is not None

    with session_factory() as session:
        final_pub = session.get(Publication, publication.id)
        assert final_pub.status == status_after_b
        assert final_pub.provider_video_id == video_id_b
        assert final_pub.lease_token is None
        assert find_orphaned_active_publications(session) == []
