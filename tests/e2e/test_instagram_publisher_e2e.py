"""Instagram publisher "contract E2E": a real final.mp4 driven through the
FULL publish state machine (worker.publish_runner.run_publication) against
the real InstagramPublisher adapter, with an in-memory stateful fake Meta
server behind httpx.MockTransport instead of FakePublisher. Exercises real
resumable-upload headers (Authorization/offset/file_size), a real byte-
for-byte checksum of everything the "server" received, account_info fetched
fresh before every container, the documented "cannot resume -- retries the
SAME container while nothing was uploaded yet" behavior
(providers.instagram_publisher.query_upload_offset combined with
worker.publish_runner._upload_stage's bytes_uploaded==0 shortcut),
idempotent publication creation, the full Publication status/audit trail,
container status polling, the transparent media_publish call once a
container reaches FINISHED, and the permalink fetch.

This is NOT a live network test -- see docs/PUBLISHING.md and
tests/unit/test_instagram_publisher.py for the unit-level adapter contract
tests this builds on. No real Meta endpoint is ever contacted, and no step
here should ever be described as a real live Instagram publish."""
from __future__ import annotations

import hashlib
import json
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
from reel_harness.providers.instagram_publisher import InstagramPublisher
from reel_harness.providers.registry import publisher_snapshot
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

FAKE_TOKEN = "FAKE-INSTAGRAM-ACCESS-TOKEN-CONTRACT-E2E"
ACCOUNT_ID = "17841400"
API_VERSION = "v25.0"
GRAPH_URL = "https://graph.instagram.com"


def _faststart_mp4_bytes(tmp_path) -> bytes:
    """~4s of testsrc/sine -- above Instagram's documented 3s Reels
    minimum, small enough to keep the test fast."""
    deps = check_ffmpeg_available()
    out = tmp_path / "ig-e2e.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=4:size=320x568:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=60)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


class _FakeInstagramServer:
    """A minimal, stateful stand-in for Meta's Content Publishing API
    (account info, publishing limit, container init, rupload binary
    upload, container status, media_publish, permalink), wired in via
    httpx.MockTransport. Validates the SAME wire contract the real adapter
    speaks (Authorization/offset/file_size headers, the {data}/{error}
    envelope shapes) rather than just returning canned responses.

    `fail_first_upload_attempt` fails the very first attempt to upload
    bytes to ANY container (once, globally) -- modeling a transient
    mid-upload failure. Since Instagram's upload is always a single
    whole-file request (no confirmed chunking -- see docs/PUBLISHING.md),
    this always happens before any bytes are confirmed received, so the
    retry reuses the SAME container rather than creating a new one."""

    def __init__(self, expected_bytes: bytes, fail_first_upload_attempt: bool, polls_before_finished: int) -> None:
        self.expected_bytes = expected_bytes
        self.fail_first_upload_attempt = fail_first_upload_attempt
        self.polls_before_finished = polls_before_finished
        self.containers: dict[str, dict] = {}
        self.upload_urls: dict[str, str] = {}
        self.media: dict[str, dict] = {}
        self.session_creations = 0
        self.account_info_calls = 0
        self.upload_attempts = 0
        self.publish_calls = 0
        self.last_post_info: dict | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        path = request.url.path
        if "rupload.facebook.com" in url:
            return self._upload(request)
        if request.method == "GET" and path == f"/{API_VERSION}/{ACCOUNT_ID}" and "fields=id" in url:
            return self._account_info(request)
        if request.method == "GET" and path.endswith("/content_publishing_limit"):
            return self._publishing_limit(request)
        if request.method == "POST" and path == f"/{API_VERSION}/{ACCOUNT_ID}/media":
            return self._create_container(request)
        if request.method == "POST" and path == f"/{API_VERSION}/{ACCOUNT_ID}/media_publish":
            return self._media_publish(request)
        if request.method == "GET" and path.lstrip("/").split("/", 1)[-1] in self.containers:
            return self._container_status(request)
        if request.method == "GET" and path.lstrip("/").split("/", 1)[-1] in self.media:
            return self._permalink(request)
        raise AssertionError(f"unexpected request to fake instagram server: {request.method} {url}")

    def _account_info(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("access_token") == FAKE_TOKEN
        self.account_info_calls += 1
        return httpx.Response(200, json={
            "id": ACCOUNT_ID, "username": "my_reel_account", "account_type": "BUSINESS",
        })

    def _publishing_limit(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"quota_usage": 2, "config": {"quota_total": 100}}]})

    def _create_container(self, request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("access_token") == FAKE_TOKEN
        import urllib.parse

        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        self.last_post_info = body
        self.session_creations += 1
        container_id = f"fake-container-{self.session_creations}"
        upload_url = f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/{container_id}"
        self.containers[container_id] = {"received": bytearray(), "polls": 0, "status": "IN_PROGRESS_UPLOAD"}
        self.upload_urls[upload_url] = container_id
        return httpx.Response(200, json={"id": container_id})

    def _upload(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        container_id = self.upload_urls[url]
        sess = self.containers[container_id]
        assert request.headers["authorization"] == f"OAuth {FAKE_TOKEN}"
        assert request.headers["offset"] == "0"
        assert request.headers["file_size"] == str(len(self.expected_bytes))
        self.upload_attempts += 1

        if self.fail_first_upload_attempt and self.upload_attempts == 1:
            return httpx.Response(500)

        sess["received"] = bytearray(request.content)
        assert bytes(sess["received"]) == self.expected_bytes, (
            "bytes actually received by the fake server do not match the source file"
        )
        sess["status"] = "IN_PROGRESS"
        return httpx.Response(200, json={"success": True, "message": "Upload successful."})

    def _container_status(self, request: httpx.Request) -> httpx.Response:
        container_id = request.url.path.lstrip("/").split("/", 1)[-1]
        sess = self.containers[container_id]
        if not sess["received"]:
            return httpx.Response(200, json={"status_code": "IN_PROGRESS"})
        sess["polls"] += 1
        if sess["polls"] < self.polls_before_finished:
            return httpx.Response(200, json={"status_code": "IN_PROGRESS"})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    def _media_publish(self, request: httpx.Request) -> httpx.Response:
        import urllib.parse

        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        container_id = body["creation_id"]
        self.publish_calls += 1
        media_id = f"fake-media-{container_id}"
        self.media[media_id] = {"container_id": container_id}
        self.containers[container_id]["status"] = "PUBLISHED"
        return httpx.Response(200, json={"id": media_id})

    def _permalink(self, request: httpx.Request) -> httpx.Response:
        media_id = request.url.path.lstrip("/").split("/", 1)[-1]
        return httpx.Response(200, json={"permalink": f"https://www.instagram.com/reel/{media_id}-shortcode/"})


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
        validation=ValidationInfo(duration_sec=4.0, video_codec="h264", audio_codec="aac", has_audio_stream=True),
        final_video_checksum_sha256=checksum,
        approval=ApprovalInfo(decision="approve", decided_at=datetime.now(UTC)),
    )
    write_manifest(storage, job.id, manifest)
    return job, checksum


def test_instagram_publisher_contract_e2e(job_service, channel, session_factory, storage, tmp_path) -> None:
    video_bytes = _faststart_mp4_bytes(tmp_path)
    job, _checksum = _make_completed_job(job_service, channel, session_factory, storage, video_bytes, "ig-e2e-1")

    settings = Settings(
        instagram_app_id="test-app-id", instagram_app_secret="test-app-secret",
        instagram_redirect_uri="https://example.invalid/callback",
    )
    snapshot = publisher_snapshot(settings, "instagram", "default")
    assert "app_secret" not in json.dumps(snapshot).lower()
    assert "access_token" not in json.dumps(snapshot).lower()

    pub_service = PublicationService(session_factory, storage)
    pub, eligibility = pub_service.create_publication(
        job.id, provider="instagram", account_reference="default", publisher_snapshot=snapshot,
        confirm_public_upload=True, public_upload_enabled=True, confirm_platform_options=True,
    )
    assert eligibility.eligible, eligibility.reasons
    assert pub.privacy_status == "PUBLIC"  # instagram's only privacy value -- every publish is public

    # Idempotency: a second create_publication call for the same
    # (provider, account, job, checksum) returns the SAME row, never a
    # duplicate upload target.
    pub_again, _ = pub_service.create_publication(
        job.id, provider="instagram", account_reference="default", publisher_snapshot=snapshot,
        confirm_public_upload=True, public_upload_enabled=True, confirm_platform_options=True,
    )
    assert pub_again.id == pub.id

    server = _FakeInstagramServer(
        expected_bytes=video_bytes, fail_first_upload_attempt=True, polls_before_finished=2,
    )
    publisher = InstagramPublisher(
        access_token_provider=lambda: FAKE_TOKEN, graph_url=GRAPH_URL, api_version=API_VERSION,
        account_id=ACCOUNT_ID, transport=httpx.MockTransport(server.handle),
        max_retries=1, retry_backoff_seconds=0.0,
    )
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    session_store = UploadSessionStore(store)
    journal = PublishJournal(store.root_dir / "publish_journal")
    bundle = PublishBundle(publisher=publisher, session_store=session_store, journal=journal)

    # First run: account_info confirmed fresh, container 1 created
    # (container_id known immediately -- persisted onto provider_video_id
    # right away, never waiting for upload completion like YouTube's),
    # the single whole-file upload attempt (the simulated transient
    # failure) fails -> RETRY_WAIT. Nothing was ever actually received by
    # the server, so bytes_uploaded stays 0.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle, channel_niche="cooking")
        assert db_pub.status == PublicationStatus.RETRY_WAIT.value
        assert db_pub.retry_target_status == PublicationStatus.UPLOADING.value
        assert db_pub.bytes_uploaded == 0
        assert db_pub.provider_video_id == "fake-container-1"  # closed the crash-recovery gap immediately

    assert server.session_creations == 1
    assert server.account_info_calls == 1

    # Second run (a fresh lease, exactly like a resumed worker): since
    # bytes_uploaded is still 0, worker.publish_runner._upload_stage never
    # even asks query_upload_offset (which would raise
    # UploadSessionExpiredError anyway -- see
    # providers.instagram_publisher) -- it just reuses container 1's own
    # upload_url directly. This time the whole-file upload succeeds,
    # completing the upload and taking the first processing poll (still
    # "processing", container not yet FINISHED) all within this one call.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PROCESSING.value
        assert db_pub.bytes_uploaded == len(video_bytes)
        assert db_pub.provider_video_id == "fake-container-1"  # the SAME container -- never abandoned

    assert server.session_creations == 1  # never created a second container
    # account_info/eligibility is checked fresh once, immediately before the
    # (irreversible) container is created -- mirroring the already-shipped
    # TikTok pattern (_verify_platform_options, called from
    # _create_session_stage/_start_new_session only) -- never re-queried on
    # a later resume that reuses the SAME still-live container, since no new
    # irreversible action is being taken.
    assert server.account_info_calls == 1
    assert bytes(server.containers["fake-container-1"]["received"]) == video_bytes

    # Third run: second container-status poll reports FINISHED ->
    # media_publish is called transparently inside get_processing_status
    # -> PUBLISHED, with the real permalink fetched as a follow-up call.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        assert db_pub.published_at is not None
        assert db_pub.publication_url == "https://www.instagram.com/reel/fake-media-fake-container-1-shortcode/"

    assert server.publish_calls == 1  # never re-published on a later reconcile/poll

    # The real upload URL is never persisted to the DB, and is cleaned up
    # from the (repository-external) session store once the upload
    # completes.
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.upload_session_reference == db_pub.id
        assert "https://" not in (db_pub.upload_session_reference or "")
        config_text = json.dumps(db_pub.publisher_config or {}).lower()
        for forbidden in ("access_token", "app_secret", "authorization", "bearer", "oauth "):
            assert forbidden not in config_text
        # The safe, most-restrictive platform_options were actually sent.
        snapshot_options = db_pub.metadata_snapshot["platform_options"]
        assert snapshot_options["share_to_feed"] is False
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
    assert server.last_post_info["media_type"] == "REELS"
    assert server.last_post_info["upload_type"] == "resumable"
    assert server.last_post_info["share_to_feed"] == "false"
    # The caption is deterministically built from the manifest -- never a
    # job id, local path, API key, or raw provider response.
    assert "reel-harness" not in server.last_post_info["caption"].lower()
    assert str(job.id) not in server.last_post_info["caption"]

    # The durable journal (see publisher.journal) recorded the completion
    # fact independently of the DB, with the real container id -- this is
    # what a crash-recovery reconcile would read if the DB commit itself
    # had never happened.
    journal_events = journal.read_events(pub.id)
    completed_events = [e for e in journal_events if e["event"] == "upload_completed"]
    assert len(completed_events) == 1
    assert completed_events[0]["provider_video_id"] == "fake-container-1"
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        assert db_pub.metadata_fingerprint is not None
    journal_text = json.dumps(journal_events).lower()
    for forbidden in ("access_token", "app_secret", "authorization", "bearer"):
        assert forbidden not in journal_text

    publisher.close()
