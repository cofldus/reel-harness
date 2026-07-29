"""TikTok publisher "contract E2E": a real final.mp4 driven through the
FULL publish state machine (worker.publish_runner.run_publication) against
the real TikTokPublisher adapter, with an in-memory stateful fake TikTok
server behind httpx.MockTransport instead of FakePublisher. Exercises real
Content-Range/Content-Length validation, a real byte-for-byte checksum of
everything the "server" received, creator_info fetched fresh before every
session, the documented "cannot resume -- always a fresh session" behavior
(providers.tiktok_publisher.query_upload_offset), idempotent publication
creation, the full Publication status/audit trail, and real (polled, not
immediate) processing completion.

This is NOT a live network test -- see docs/PUBLISHING.md and
tests/unit/test_tiktok_publisher.py for the unit-level adapter contract
tests this builds on. No real TikTok endpoint is ever contacted, and no
step here should ever be described as a live TikTok publish."""
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
from reel_harness.providers.tiktok_publisher import TikTokPublisher
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

FAKE_TOKEN = "FAKE-TIKTOK-ACCESS-TOKEN-CONTRACT-E2E"
CHUNK_SIZE = 131072  # 128 KiB -- small enough that the ~270 KB test clip spans 2+ chunks


def _faststart_mp4_bytes(tmp_path) -> bytes:
    """~20s of testsrc/sine, well over one chunk, so the upload genuinely
    spans two real chunk requests."""
    deps = check_ffmpeg_available()
    out = tmp_path / "tt-e2e.mp4"
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


class _FakeTikTokServer:
    """A minimal, stateful stand-in for TikTok's Content Posting API
    (creator_info/init/upload/status), wired in via httpx.MockTransport.
    Validates the SAME wire contract the real adapter speaks (Content-
    Range, Content-Length, chunk offsets, the {data, error} envelope)
    rather than just returning canned responses.

    `fail_chunk_on_session` fails the SECOND chunk PUT (so the first chunk's
    progress genuinely persists) attempted against the session with that
    number (1-indexed, in creation order) -- modeling a transient mid-
    upload failure."""

    def __init__(
        self, expected_bytes: bytes, fail_chunk_on_session: int | None, polls_before_success: int,
    ) -> None:
        self.expected_bytes = expected_bytes
        self.fail_chunk_on_session = fail_chunk_on_session
        self.polls_before_success = polls_before_success
        self.sessions: dict[str, dict] = {}
        self.publishes: dict[str, dict] = {}
        self.session_creations = 0
        self.creator_info_calls = 0
        self.last_post_info: dict | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "POST" and url.endswith("/v2/post/publish/creator_info/query/"):
            return self._creator_info(request)
        if request.method == "POST" and url.endswith("/v2/post/publish/video/init/"):
            return self._create_session(request)
        if request.method == "POST" and url.endswith("/v2/post/publish/status/fetch/"):
            return self._processing_status(request)
        if request.method == "PUT" and url in self.sessions:
            return self._chunk(request, url)
        raise AssertionError(f"unexpected request to fake tiktok server: {request.method} {url}")

    def _creator_info(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        self.creator_info_calls += 1
        return httpx.Response(200, json={
            "data": {
                "creator_username": "creator1", "creator_nickname": "Creator One",
                "privacy_level_options": ["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
                "comment_disabled": False, "duet_disabled": False, "stitch_disabled": False,
                "max_video_post_duration_sec": 300,
            },
            "error": {"code": "ok", "message": "", "log_id": "creator-info"},
        })

    def _create_session(self, request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        body = json.loads(request.content)
        self.last_post_info = body["post_info"]
        total = body["source_info"]["video_size"]
        assert total == len(self.expected_bytes)
        self.session_creations += 1
        session_number = self.session_creations
        publish_id = f"fake-publish-id-{session_number}"
        upload_url = f"https://upload.tiktokapis.invalid/video/{uuid.uuid4()}"
        self.sessions[upload_url] = {
            "received": bytearray(), "total": total, "publish_id": publish_id,
            "chunk_attempts": 0, "session_number": session_number,
        }
        self.publishes[publish_id] = {"polls": 0}
        return httpx.Response(200, json={
            "data": {"publish_id": publish_id, "upload_url": upload_url},
            "error": {"code": "ok", "message": "", "log_id": "init"},
        })

    def _chunk(self, request: httpx.Request, upload_url: str) -> httpx.Response:
        sess = self.sessions[upload_url]
        sess["chunk_attempts"] += 1
        content_range = request.headers["content-range"]
        assert request.headers["content-type"] == "video/mp4"
        range_part, total_part = content_range[len("bytes "):].split("/")
        start_s, end_s = range_part.split("-")
        start, end = int(start_s), int(end_s)
        total = int(total_part)
        assert total == sess["total"], "chunk Content-Range total does not match the session's declared size"
        body = request.content
        assert len(body) == end - start + 1, "chunk body length does not match its own declared Content-Range"
        assert len(request.headers["content-length"]) > 0
        assert int(request.headers["content-length"]) == len(body)
        assert start == len(sess["received"]), (
            f"chunk start byte {start} does not match the server's confirmed offset {len(sess['received'])} "
            "-- a real server would reject this as a protocol violation"
        )

        if self.fail_chunk_on_session == sess["session_number"] and sess["chunk_attempts"] == 2:
            return httpx.Response(500)

        sess["received"].extend(body)
        if len(sess["received"]) >= total:
            assert bytes(sess["received"]) == self.expected_bytes, (
                "bytes actually received by the fake server do not match the source file"
            )
        return httpx.Response(200)

    def _processing_status(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        publish_id = body["publish_id"]
        entry = self.publishes.get(publish_id)
        assert entry is not None, f"processing status queried for an unknown publish_id {publish_id!r}"
        entry["polls"] += 1
        if entry["polls"] < self.polls_before_success:
            return httpx.Response(200, json={
                "data": {"status": "PROCESSING_UPLOAD"},
                "error": {"code": "ok", "message": "", "log_id": "status"},
            })
        return httpx.Response(200, json={
            "data": {"status": "PUBLISH_COMPLETE", "publicly_available_post_id": f"post-{publish_id}"},
            "error": {"code": "ok", "message": "", "log_id": "status"},
        })


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
        validation=ValidationInfo(duration_sec=20.0, video_codec="h264", audio_codec="aac", has_audio_stream=True),
        final_video_checksum_sha256=checksum,
        approval=ApprovalInfo(decision="approve", decided_at=datetime.now(UTC)),
    )
    write_manifest(storage, job.id, manifest)
    return job, checksum


def test_tiktok_publisher_contract_e2e(job_service, channel, session_factory, storage, tmp_path) -> None:
    video_bytes = _faststart_mp4_bytes(tmp_path)
    job, _checksum = _make_completed_job(job_service, channel, session_factory, storage, video_bytes, "tt-e2e-1")

    settings = Settings(
        tiktok_client_key="test-client-key", tiktok_client_secret="test-client-secret",
        tiktok_redirect_uri="https://example.invalid/callback", tiktok_upload_chunk_size=CHUNK_SIZE,
    )
    snapshot = publisher_snapshot(settings, "tiktok", "default")
    assert "client_secret" not in json.dumps(snapshot).lower()
    assert "access_token" not in json.dumps(snapshot).lower()

    pub_service = PublicationService(session_factory, storage)
    pub, eligibility = pub_service.create_publication(
        job.id, provider="tiktok", account_reference="default", publisher_snapshot=snapshot,
        confirm_platform_options=True,
    )
    assert eligibility.eligible, eligibility.reasons
    assert pub.privacy_status == "SELF_ONLY"  # the provider's own most-restrictive default

    # Idempotency: a second create_publication call for the same
    # (provider, account, job, checksum) returns the SAME row, never a
    # duplicate upload target.
    pub_again, _ = pub_service.create_publication(
        job.id, provider="tiktok", account_reference="default", publisher_snapshot=snapshot,
        confirm_platform_options=True,
    )
    assert pub_again.id == pub.id

    server = _FakeTikTokServer(expected_bytes=video_bytes, fail_chunk_on_session=1, polls_before_success=2)
    publisher = TikTokPublisher(
        access_token_provider=lambda: FAKE_TOKEN, base_url="https://open.tiktokapis.com", chunk_size=CHUNK_SIZE,
        transport=httpx.MockTransport(server.handle), max_retries=1, retry_backoff_seconds=0.0,
    )
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(store)
    journal = PublishJournal(store.root_dir / "publish_journal")
    bundle = PublishBundle(publisher=publisher, session_store=session_store, journal=journal)

    # First run: creator_info confirmed fresh, session 1 created (publish_id
    # known immediately -- persisted onto provider_video_id right away,
    # never waiting for upload completion like YouTube's), chunk 1 uploads
    # fine, the (simulated) transient failure hits chunk 2 -> RETRY_WAIT.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle, channel_niche="cooking")
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.retry_target_status == PublicationStatus.UPLOADING.value
        assert db_pub.bytes_uploaded == CHUNK_SIZE
        assert db_pub.provider_video_id == "fake-publish-id-1"  # closed the crash-recovery gap immediately

    assert server.session_creations == 1
    assert server.creator_info_calls == 1

    # Second run (a fresh lease, exactly like a resumed worker): TikTok's
    # adapter has no documented way to query a confirmed offset (see
    # providers.tiktok_publisher.query_upload_offset), so this does NOT
    # resume session 1 -- it self-heals into a BRAND NEW session (a fresh
    # creator_info check, a new publish_id) and re-uploads the ENTIRE file
    # from byte 0, completing the upload and taking the first processing
    # poll (still "processing") all within this one call.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PROCESSING.value
        assert db_pub.bytes_uploaded == len(video_bytes)
        assert db_pub.provider_video_id == "fake-publish-id-2"  # superseded session 1's id

    assert server.session_creations == 2
    assert server.creator_info_calls == 2  # re-checked fresh before the second session too, never cached
    # Session 1's upload_url only ever received its first (successful)
    # chunk before the failure -- it is now permanently abandoned, exactly
    # as documented: TikTok publications can't resume, only restart.
    session_1_url = next(u for u, s in server.sessions.items() if s["session_number"] == 1)
    assert len(server.sessions[session_1_url]["received"]) == CHUNK_SIZE
    session_2_url = next(u for u, s in server.sessions.items() if s["session_number"] == 2)
    assert bytes(server.sessions[session_2_url]["received"]) == video_bytes

    # Third run: second processing poll reports PUBLISH_COMPLETE ->
    # PUBLISHED. Upload completion alone never reaches PUBLISHED without
    # this real poll.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        assert db_pub.published_at is not None
        # Never fabricated -- see providers.tiktok_publisher.get_processing_status's docstring.
        assert db_pub.publication_url is None

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
        # The safe, most-restrictive platform_options were actually sent.
        snapshot_options = db_pub.metadata_snapshot["platform_options"]
        assert snapshot_options["disable_comment"] is True
        assert snapshot_options["disable_duet"] is True
        assert snapshot_options["disable_stitch"] is True
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

    assert server.last_post_info is not None
    assert server.last_post_info["privacy_level"] == "SELF_ONLY"
    assert server.last_post_info["disable_comment"] is True
    assert server.last_post_info["disable_duet"] is True
    assert server.last_post_info["disable_stitch"] is True
    # The post text is deterministically built from the manifest -- never a
    # job id, local path, API key, or raw provider response.
    assert "reel-harness" not in server.last_post_info["title"].lower()
    assert str(job.id) not in server.last_post_info["title"]

    # The durable journal (see publisher.journal) recorded the completion
    # fact independently of the DB, with the real (superseding) publish_id
    # -- this is what a crash-recovery reconcile would read if the DB
    # commit itself had never happened.
    journal_events = journal.read_events(pub.id)
    completed_events = [e for e in journal_events if e["event"] == "upload_completed"]
    assert len(completed_events) == 1
    assert completed_events[0]["provider_video_id"] == "fake-publish-id-2"
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.metadata_fingerprint is not None
    journal_text = json.dumps(journal_events).lower()
    for forbidden in ("access_token", "refresh_token", "authorization", "bearer"):
        assert forbidden not in journal_text

    publisher.close()
