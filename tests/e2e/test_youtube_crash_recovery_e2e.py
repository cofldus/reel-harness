"""YouTube publisher crash-recovery contract E2E (Phase 3B, Commit 6):
five scenarios driving the REAL YouTubePublisher adapter and a real
ffmpeg-built final.mp4 against a stateful fake YouTube server behind
httpx.MockTransport -- no real network. Builds on
tests/e2e/test_youtube_publisher_e2e.py's contract-E2E pattern; this file
is specifically about what happens when a worker process dies at each of
the riskiest moments.

Scenario map (see docs/PUBLISHING.md and the module docstrings on
core.publish_reconciliation / worker.publish_runner for the mechanisms
these exercise):
  A. Provider succeeds, DB commit is lost -> journal-based recovery, no
     duplicate upload.
  B. Upload session expires mid-upload -> a fresh session is created and
     the file is re-sent from scratch; still only one video is ever
     created.
  C. The provider's completion response is lost in transit (provider
     actually finished, client sees a transient error) -> reconciliation
     reports ambiguous_remote_state and never guesses.
  D. A worker crashes while a publication is PROCESSING -> stale-lease
     recovery reclaims it and a fresh worker finishes the job.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from reel_harness.config import Settings
from reel_harness.core.publish_reconciliation import reconcile_publication
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.base import UploadSessionHandle
from reel_harness.providers.registry import publisher_snapshot
from reel_harness.providers.youtube_publisher import UPLOAD_ENDPOINT, VIDEOS_ENDPOINT, YouTubePublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_lease import (
    lease_next_processing_publication,
    recover_stale_publications,
)
from reel_harness.worker.publish_runner import PublishBundle, _create_session_stage, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

FAKE_TOKEN = "FAKE-YOUTUBE-ACCESS-TOKEN-CRASH-RECOVERY"
CHUNK_SIZE = 262144


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"crash-{seed}.mp4"
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
    assert len(video_bytes) > CHUNK_SIZE
    return video_bytes


class _FakeYouTubeServer:
    """Like tests/e2e/test_youtube_publisher_e2e.py's fake server, extended
    with session expiry and a "response lost after server-side success"
    mode -- the two extra failure shapes these crash-recovery scenarios
    need."""

    def __init__(self, expected_bytes: bytes, polls_before_success: int = 1) -> None:
        self.expected_bytes = expected_bytes
        self.polls_before_success = polls_before_success
        self.sessions: dict[str, dict] = {}
        self.videos: dict[str, dict] = {}
        self.session_creations = 0
        self.chunk_attempts = 0
        self.drop_response_on_completion = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.startswith(UPLOAD_ENDPOINT):
            return self._create_session(request)
        if request.method == "GET" and url.startswith(VIDEOS_ENDPOINT):
            return self._processing_status(request)
        if request.method == "PUT":
            return self._chunk_or_offset(request, url)
        raise AssertionError(f"unexpected request to fake youtube server: {request.method} {url}")

    def _create_session(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        total = int(request.headers["x-upload-content-length"])
        self.session_creations += 1
        session_id = f"https://upload.example.invalid/session/{uuid.uuid4()}"
        self.sessions[session_id] = {"received": bytearray(), "total": total, "video_id": None}
        return httpx.Response(200, headers={"location": session_id})

    def expire_session(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def _chunk_or_offset(self, request: httpx.Request, session_id: str) -> httpx.Response:
        sess = self.sessions.get(session_id)
        if sess is None:
            return httpx.Response(404)  # expired/unknown session

        content_range = request.headers["content-range"]
        if content_range.startswith("bytes */"):
            received = len(sess["received"])
            if received >= sess["total"]:
                return httpx.Response(200, json={"id": sess["video_id"]})
            if received == 0:
                return httpx.Response(308)
            return httpx.Response(308, headers={"range": f"bytes=0-{received - 1}"})

        self.chunk_attempts += 1
        range_part, total_part = content_range[len("bytes "):].split("/")
        start_s, end_s = range_part.split("-")
        start, end = int(start_s), int(end_s)
        total = int(total_part)
        body = request.content
        assert len(body) == end - start + 1
        assert start == len(sess["received"]), (
            f"chunk start byte {start} does not match the server's confirmed offset {len(sess['received'])}"
        )
        sess["received"].extend(body)

        if len(sess["received"]) < total:
            return httpx.Response(308, headers={"range": f"bytes=0-{len(sess['received']) - 1}"})

        assert bytes(sess["received"]) == self.expected_bytes
        video_id = f"fake-yt-video-{len(self.videos) + 1}"
        sess["video_id"] = video_id
        self.videos[video_id] = {"polls": 0}
        if self.drop_response_on_completion:
            # The provider's own state now reflects success (video minted
            # above) but the client never sees this response -- exactly
            # what a dropped connection after a real server-side commit
            # looks like.
            raise httpx.ReadTimeout("simulated: response lost after the server already completed the upload")
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


def _make_completed_job(job_service, channel, session_factory, storage, video_bytes: bytes, key: str) -> Job:
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
    return job


def _setup(job_service, channel, session_factory, storage, tmp_path, key, server, chunk_size=CHUNK_SIZE):
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    job = _make_completed_job(job_service, channel, session_factory, storage, video_bytes, key)

    settings = Settings(youtube_client_id="test-client-id", youtube_client_secret="test-client-secret",
                        youtube_upload_chunk_size=chunk_size)
    snapshot = publisher_snapshot(settings, "youtube", "default")
    pub_service = PublicationService(session_factory, storage)
    pub, _ = pub_service.create_publication(
        job.id, provider="youtube", account_reference="default", publisher_snapshot=snapshot,
    )

    publisher = YouTubePublisher(
        access_token_provider=lambda: FAKE_TOKEN, chunk_size=chunk_size,
        transport=httpx.MockTransport(server.handle), max_retries=1, retry_backoff_seconds=0.0,
    )
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(store)
    journal = PublishJournal(store.root_dir / "publish_journal")
    bundle = PublishBundle(publisher=publisher, session_store=session_store, journal=journal)
    return pub, bundle, video_bytes


def test_scenario_a_provider_success_then_lost_db_commit_recovers_without_duplicate(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    server = _FakeYouTubeServer(expected_bytes=b"", polls_before_success=1)
    pub, bundle, video_bytes = _setup(
        job_service, channel, session_factory, storage, tmp_path, "scenario-a", server,
    )
    server.expected_bytes = video_bytes

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        real_video_id = db_pub.provider_video_id
        assert real_video_id is not None

        # Simulate: the upload genuinely succeeded and the journal recorded
        # it, but the process crashed before this DB transaction ever
        # committed the fact -- hand-roll exactly that post-crash state.
        db_pub.status = PublicationStatus.UPLOADING.value
        db_pub.provider_video_id = None
        db_pub.publication_url = None
        db_pub.published_at = None
        session.commit()

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "recovered_remote_video"
        assert result.provider_video_id == real_video_id
        assert db_pub.status == PublicationStatus.PUBLISHED.value

    assert server.session_creations == 1  # never re-uploaded


def test_scenario_b_upload_session_expiry_creates_a_fresh_session_and_only_one_video(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    server = _FakeYouTubeServer(expected_bytes=b"")
    pub, bundle, video_bytes = _setup(
        job_service, channel, session_factory, storage, tmp_path, "scenario-b", server,
    )
    server.expected_bytes = video_bytes

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        _create_session_stage(session, db_pub, job, storage, bundle, None, len(video_bytes), None)
        assert db_pub.status == PublicationStatus.UPLOAD_SESSION_CREATED.value

        # Genuine partial progress: the first chunk actually lands.
        session_ref = bundle.session_store.get(db_pub.id)
        handle = UploadSessionHandle(
            session_reference=session_ref, total_bytes=len(video_bytes), chunk_size=CHUNK_SIZE,
        )
        first_chunk = video_bytes[:CHUNK_SIZE]
        result = bundle.publisher.upload_chunk(handle, first_chunk, 0, len(video_bytes))
        assert not result.completed
        db_pub.bytes_uploaded = result.bytes_uploaded
        db_pub.status = PublicationStatus.UPLOADING.value
        session.commit()

        # Now the session expires (e.g. it outlived its lifetime, or the
        # provider otherwise invalidated it) before the rest was sent.
        server.expire_session(session_ref)

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value

    assert server.session_creations == 2  # the expired one, then a fresh one
    assert len(server.videos) == 1  # exactly one video was ever created


def test_scenario_c_ambiguous_completion_never_auto_repairs_or_duplicates(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    server = _FakeYouTubeServer(expected_bytes=b"")
    pub, bundle, video_bytes = _setup(
        job_service, channel, session_factory, storage, tmp_path, "scenario-c", server,
    )
    server.expected_bytes = video_bytes
    server.drop_response_on_completion = True

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle, channel_niche="cooking")
        # The completing chunk's response was lost -> a transient error ->
        # RETRY_WAIT, even though the provider actually finished.
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.provider_video_id is None

    journal_events = bundle.journal.read_events(pub.id)
    assert not any(e["event"] == "upload_completed" for e in journal_events)

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        # The provider reports "already complete" (offset query -> None),
        # but nothing local ever captured a video id for it -- the upload
        # loop has nothing left to do and the publication stays stuck.
        assert db_pub.status == PublicationStatus.UPLOADING.value
        assert db_pub.provider_video_id is None

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "ambiguous_remote_state"
        assert db_pub.provider_video_id is None  # never guessed

    assert server.session_creations == 1  # no duplicate upload was ever attempted
    assert len(server.videos) == 1


def test_scenario_d_processing_worker_crash_is_recovered_by_a_fresh_worker(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    server = _FakeYouTubeServer(expected_bytes=b"", polls_before_success=1)
    pub, bundle, video_bytes = _setup(
        job_service, channel, session_factory, storage, tmp_path, "scenario-d", server,
    )
    server.expected_bytes = video_bytes

    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        # A worker seizes the lease and reaches PROCESSING but then dies
        # before completing the poll -- simulate by hand-setting a stale
        # lock (a real worker would hold locked_by/heartbeat_at throughout).
        assert db_pub.status == PublicationStatus.PUBLISHED.value

    # Rebuild a fresh scenario landing exactly at PROCESSING (not PUBLISHED
    # yet) so there's real recovery work to do.
    server2 = _FakeYouTubeServer(expected_bytes=b"", polls_before_success=2)
    pub2, bundle2, video_bytes2 = _setup(
        job_service, channel, session_factory, storage, tmp_path, "scenario-d2", server2,
    )
    server2.expected_bytes = video_bytes2
    with session_factory() as session:
        db_pub2 = session.get(Publication, pub2.id)
        run_publication(session, db_pub2, storage, bundle2)
        assert db_pub2.status == PublicationStatus.PROCESSING.value  # first poll only, not yet succeeded

        # Simulate the worker that reached PROCESSING crashing: it never
        # released its lease.
        db_pub2.locked_by = "dead-worker"
        db_pub2.heartbeat_at = datetime.now(UTC) - timedelta(hours=1)
        session.commit()

        far_future = datetime.now(UTC) + timedelta(hours=1)
        recovered = recover_stale_publications(session, lease_timeout_seconds=60, now=far_future)
        assert recovered == [pub2.id]
        session.refresh(db_pub2)
        assert db_pub2.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub2.retry_target_status == PublicationStatus.PROCESSING.value

        # recover_stale_publications set next_retry_at to far_future plus a
        # backoff -- lease strictly after that.
        lease_now = far_future + timedelta(minutes=15)
        leased = lease_next_processing_publication(session, worker_id="fresh-worker", now=lease_now)
        assert leased is not None and leased.id == pub2.id
        lease_token = leased.lease_token
        run_publication(session, leased, storage, bundle2, lease_token=lease_token)
        assert leased.status == PublicationStatus.PUBLISHED.value
