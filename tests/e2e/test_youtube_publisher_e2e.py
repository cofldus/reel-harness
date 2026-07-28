"""YouTube publisher "contract E2E": a real final.mp4 driven through the
FULL resumable-upload state machine (worker.publish_runner.run_publication)
against the real YouTubePublisher adapter, with an in-memory stateful fake
YouTube server behind httpx.MockTransport instead of FakePublisher. Exercises
real Content-Range/Content-Length validation, a real byte-for-byte checksum
of everything the "server" received, a transient mid-upload failure and
resume from the provider's own confirmed offset, idempotent publication
creation, the full Publication status/audit trail, and real (polled, not
immediate) processing completion.

This is NOT a live network test -- see docs/PUBLISHING.md and
tests/unit/test_youtube_publisher.py for the unit-level adapter contract
tests this builds on. No real Google endpoint is ever contacted."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime

import httpx
import pytest

from reel_harness.config import Settings
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication, PublicationAuditEvent
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.registry import publisher_snapshot
from reel_harness.providers.youtube_publisher import UPLOAD_ENDPOINT, VIDEOS_ENDPOINT, YouTubePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

FAKE_TOKEN = "FAKE-YOUTUBE-ACCESS-TOKEN-CONTRACT-E2E"
CHUNK_SIZE = 262144  # protocol minimum granularity -- see providers.youtube_publisher


def _faststart_mp4_bytes(tmp_path) -> bytes:
    """~20s of testsrc/sine, well over one chunk (262144 B), so the upload
    genuinely spans two real chunk requests."""
    deps = check_ffmpeg_available()
    out = tmp_path / "yt-e2e.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=20:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=20",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=60)
    assert result.returncode == 0, result.stderr
    video_bytes = out.read_bytes()
    assert len(video_bytes) > CHUNK_SIZE, "fixture must span more than one chunk for this test to be meaningful"
    return video_bytes


class _FakeYouTubeServer:
    """A minimal, stateful stand-in for the real YouTube Data API v3
    resumable-upload + processing endpoints, wired in via
    httpx.MockTransport. Validates the SAME wire contract the real adapter
    speaks (Content-Range, Content-Length, chunk offsets) rather than just
    returning canned responses -- a malformed request from the adapter fails
    loudly here via assertion, not silently."""

    def __init__(self, expected_bytes: bytes, fail_chunk_attempt: int | None, polls_before_success: int) -> None:
        self.expected_bytes = expected_bytes
        self.fail_chunk_attempt = fail_chunk_attempt
        self.polls_before_success = polls_before_success
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.session_creations = 0
        self.chunk_attempts = 0
        self.last_metadata_body: dict | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.startswith(UPLOAD_ENDPOINT):
            return self._create_session(request)
        if request.method == "GET" and url.startswith(VIDEOS_ENDPOINT):
            return self._processing_status(request)
        if request.method == "PUT" and url in self.sessions:
            return self._chunk_or_offset(request, url)
        raise AssertionError(f"unexpected request to fake youtube server: {request.method} {url}")

    def _create_session(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        total = int(request.headers["x-upload-content-length"])
        assert total == len(self.expected_bytes)
        self.last_metadata_body = json.loads(request.content)
        self.session_creations += 1
        session_id = f"https://upload.example.invalid/session/{uuid.uuid4()}"
        self.sessions[session_id] = {"received": bytearray(), "total": total}
        return httpx.Response(200, headers={"location": session_id})

    def _chunk_or_offset(self, request: httpx.Request, session_id: str) -> httpx.Response:
        sess = self.sessions[session_id]
        content_range = request.headers["content-range"]
        if content_range.startswith("bytes */"):
            received = len(sess["received"])
            if received == 0:
                return httpx.Response(308)
            return httpx.Response(308, headers={"range": f"bytes=0-{received - 1}"})

        self.chunk_attempts += 1
        range_part, total_part = content_range[len("bytes "):].split("/")
        start_s, end_s = range_part.split("-")
        start, end = int(start_s), int(end_s)
        total = int(total_part)
        assert total == sess["total"], "chunk Content-Range total does not match the session's declared size"
        body = request.content
        assert len(body) == end - start + 1, "chunk body length does not match its own declared Content-Range"
        assert start == len(sess["received"]), (
            f"chunk start byte {start} does not match the server's confirmed offset {len(sess['received'])} "
            "-- a real server would reject this as a protocol violation"
        )

        if self.fail_chunk_attempt is not None and self.chunk_attempts == self.fail_chunk_attempt:
            self.fail_chunk_attempt = None  # only fail once
            return httpx.Response(500)

        sess["received"].extend(body)
        if len(sess["received"]) < total:
            return httpx.Response(308, headers={"range": f"bytes=0-{len(sess['received']) - 1}"})

        assert bytes(sess["received"]) == self.expected_bytes, (
            "bytes actually received by the fake server do not match the source file -- a real chunk-checksum "
            "mismatch would mean corrupted or reordered data"
        )
        video_id = f"fake-yt-video-{len(self.videos) + 1}"
        self.videos[video_id] = {"polls": 0}
        return httpx.Response(200, json={"id": video_id})

    def _processing_status(self, request: httpx.Request) -> httpx.Response:
        video_id = request.url.params.get("id")
        entry = self.videos.get(video_id)
        assert entry is not None, f"processing status queried for an unknown video id {video_id!r}"
        entry["polls"] += 1
        if entry["polls"] < self.polls_before_success:
            return httpx.Response(200, json={"items": [{
                "status": {"uploadStatus": "uploaded", "privacyStatus": "private"},
                "processingDetails": {"processingStatus": "processing"},
            }]})
        return httpx.Response(200, json={"items": [{
            "status": {"uploadStatus": "processed", "privacyStatus": "private"},
            "processingDetails": {"processingStatus": "succeeded"},
        }]})


def _make_completed_job(job_service, channel, session_factory, storage, video_bytes: bytes, key: str) -> tuple:
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="how to fold a burrito")
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
        job_id=job.id, created_at=datetime.now(UTC), topic="how to fold a burrito", script_title="Burrito Folding",
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
    return job, checksum


def test_youtube_publisher_contract_e2e(job_service, channel, session_factory, storage, tmp_path) -> None:
    video_bytes = _faststart_mp4_bytes(tmp_path)
    job, _checksum = _make_completed_job(job_service, channel, session_factory, storage, video_bytes, "yt-e2e-1")

    settings = Settings(youtube_client_id="test-client-id", youtube_client_secret="test-client-secret",
                        youtube_upload_chunk_size=CHUNK_SIZE)
    snapshot = publisher_snapshot(settings, "youtube", "default")
    assert "client_secret" not in json.dumps(snapshot).lower()
    assert "access_token" not in json.dumps(snapshot).lower()

    pub_service = PublicationService(session_factory, storage)
    pub, eligibility = pub_service.create_publication(
        job.id, provider="youtube", account_reference="default", publisher_snapshot=snapshot,
    )
    assert eligibility.eligible, eligibility.reasons

    # Idempotency: a second create_publication call for the same
    # (provider, account, job, checksum) returns the SAME row, never a
    # duplicate upload target.
    pub_again, _ = pub_service.create_publication(
        job.id, provider="youtube", account_reference="default", publisher_snapshot=snapshot,
    )
    assert pub_again.id == pub.id

    server = _FakeYouTubeServer(expected_bytes=video_bytes, fail_chunk_attempt=2, polls_before_success=2)
    publisher = YouTubePublisher(
        access_token_provider=lambda: FAKE_TOKEN, chunk_size=CHUNK_SIZE,
        transport=httpx.MockTransport(server.handle), max_retries=1, retry_backoff_seconds=0.0,
    )
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(store)
    journal = PublishJournal(store.root_dir / "publish_journal")
    bundle = PublishBundle(publisher=publisher, session_store=session_store, journal=journal)

    # First run: chunk 1 uploads fine; the (simulated) transient failure hits
    # the second chunk, so this call ends in RETRY_WAIT with the first
    # chunk's progress already durably persisted.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle, channel_niche="cooking")
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.retry_target_status == PublicationStatus.UPLOADING.value
        assert db_pub.bytes_uploaded == CHUNK_SIZE

    # Second run (a fresh lease, exactly like a resumed worker): queries the
    # provider's own confirmed offset before resuming -- must NOT re-send
    # the already-confirmed first chunk -- then completes the upload and
    # takes the first processing poll (still "processing").
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PROCESSING.value
        assert db_pub.bytes_uploaded == len(video_bytes)
        assert db_pub.provider_video_id is not None

    # Third run: second processing poll reports success -> PUBLISHED. Upload
    # completion alone never reaches PUBLISHED without this real poll.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        assert db_pub.published_at is not None
        assert db_pub.publication_url == f"https://www.youtube.com/watch?v={db_pub.provider_video_id}"

    # Exactly one real upload session was ever created -- the retry resumed
    # the SAME session rather than starting a new one, minimizing redundant
    # retransmission.
    assert server.session_creations == 1
    assert server.chunk_attempts == 3  # chunk1 ok, chunk2 fails once, chunk2 retried ok

    session_key = next(iter(server.sessions))
    assert bytes(server.sessions[session_key]["received"]) == video_bytes

    # The real session URI is never persisted to the DB, and is cleaned up
    # from the (repository-external) session store once the upload
    # completes.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.upload_session_reference == db_pub.id
        assert "https://" not in (db_pub.upload_session_reference or "")
        config_text = json.dumps(db_pub.publisher_config or {}).lower()
        for forbidden in ("access_token", "refresh_token", "client_secret", "authorization", "bearer"):
            assert forbidden not in config_text
    assert session_store.get(pub.id) is None

    with session_factory() as session:
        events = [
            e.event for e in session.query(PublicationAuditEvent)
            .filter(PublicationAuditEvent.publication_id == pub.id).order_by(PublicationAuditEvent.created_at)
        ]
    for expected_event in (
        "eligibility_checked", "publication_created", "upload_session_created", "chunk_uploaded",
        "publication_failed", "upload_completed", "processing_started", "processing_completed",
    ):
        assert expected_event in events, f"missing audit event {expected_event!r}: {events}"

    assert server.last_metadata_body is not None
    assert server.last_metadata_body["status"]["privacyStatus"] == "private"

    # The durable journal (see publisher.journal) recorded the completion
    # fact independently of the DB, with the real provider_video_id -- this
    # is what a crash-recovery reconcile would read if the DB commit itself
    # had never happened.
    journal_events = journal.read_events(pub.id)
    completed_events = [e for e in journal_events if e["event"] == "upload_completed"]
    assert len(completed_events) == 1
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert completed_events[0]["provider_video_id"] == db_pub.provider_video_id
        assert db_pub.metadata_fingerprint is not None
    journal_text = json.dumps(journal_events).lower()
    for forbidden in ("access_token", "refresh_token", "authorization", "bearer"):
        assert forbidden not in journal_text
