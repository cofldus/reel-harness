"""Instagram-specific coverage for core.publish_reconciliation and the
Publication idempotency guarantee. Provider registry/worker wiring lands in
a later Phase 3D commit, so publications here are constructed directly
(bypassing PublicationService.create_publication's capability gate, which
does not yet recognize "instagram") -- reconcile_publication itself is
already fully provider-generic (driven only through the Publisher
Protocol), so this exercises it the same way the existing YouTube/TikTok
reconciliation tests do: a real adapter (InstagramPublisher +
httpx.MockTransport) as bundle.publisher, no network."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy.exc import IntegrityError

from reel_harness.core.publish_reconciliation import reconcile_publication
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.instagram_publisher import InstagramPublisher, InstagramReelsOptions
from reel_harness.publisher.journal import PublishJournal
from reel_harness.publisher.secret_store import FileSecretStore
from reel_harness.publisher.session_store import UploadSessionStore
from reel_harness.worker.publish_runner import PublishBundle

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

FAKE_TOKEN = "FAKE-INSTAGRAM-RECONCILE-ACCESS-TOKEN"
ACCOUNT_ID = "17841400"
API_VERSION = "v25.0"


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"instagram-reconcile-{seed}.mp4"
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


def _make_completed_job(job_service, channel, session_factory, storage, tmp_path, key: str):
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
    return job, checksum


def _make_instagram_publication(
    session_factory, job, checksum, *, account_reference: str = "acct-1",
    status: str = PublicationStatus.READY_TO_UPLOAD.value, platform_options: dict | None = None,
) -> str:
    with session_factory() as session:
        pub = Publication(
            job_id=job.id, provider="instagram", account_reference=account_reference,
            status=status, privacy_status="PUBLIC",
            idempotency_key=f"instagram:{account_reference}:{job.id}:{checksum}",
            final_video_checksum=checksum,
            publisher_config={
                "publisher_provider": "instagram", "publisher_account_reference": account_reference,
            },
            metadata_snapshot={
                "title": "A short-form video", "description": "", "tags": [], "category_id": "",
                "privacy_status": "PUBLIC", "made_for_kids": False,
                "platform_options": platform_options or InstagramReelsOptions().as_platform_options(),
            },
        )
        session.add(pub)
        session.commit()
        return pub.id


def _bundle(tmp_path, publisher) -> PublishBundle:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return PublishBundle(
        publisher=publisher, session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    )


def _instagram_publisher(handler, **overrides) -> InstagramPublisher:
    defaults: dict = dict(
        access_token_provider=lambda: FAKE_TOKEN, graph_url="https://graph.instagram.com",
        api_version=API_VERSION, account_id=ACCOUNT_ID, max_retries=1, retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return InstagramPublisher(transport=httpx.MockTransport(handler), **defaults)


def _identity_response(**overrides) -> dict:
    data = dict(id=ACCOUNT_ID, username="my_reel_account", account_type="BUSINESS")
    data.update(overrides)
    return data


def _limit_response(quota_usage: int = 3, quota_total: int = 100) -> dict:
    return {"data": [{"quota_usage": quota_usage, "config": {"quota_total": quota_total}}]}


def _account_info_handler(quota_usage: int = 3, quota_total: int = 100, account_type: str = "BUSINESS"):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response(quota_usage, quota_total))
        return httpx.Response(200, json=_identity_response(account_type=account_type))
    return handler


# -- idempotency (DB-level guarantee, per-provider-scoped) -------------------------------------------------------

def test_idempotency_constraint_is_scoped_per_provider(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """The same (account, job, checksum) tuple must be independently
    publishable to youtube/tiktok AND instagram -- the unique constraint
    includes `provider`, so these never collide with each other."""
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "idem-1")
    with session_factory() as session:
        session.add(Publication(
            job_id=job.id, provider="youtube", account_reference="acct-1", privacy_status="private",
            idempotency_key=f"youtube:acct-1:{job.id}:{checksum}", final_video_checksum=checksum,
        ))
        session.add(Publication(
            job_id=job.id, provider="instagram", account_reference="acct-1", privacy_status="PUBLIC",
            idempotency_key=f"instagram:acct-1:{job.id}:{checksum}", final_video_checksum=checksum,
        ))
        session.commit()

    with session_factory() as session:
        count = session.query(Publication).filter(Publication.job_id == job.id).count()
        assert count == 2


def test_duplicate_instagram_publication_for_the_same_tuple_violates_the_unique_constraint(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """The DB constraint (not an application-level check) is what actually
    prevents a duplicate upload target -- see Publication's own docstring."""
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "idem-2")
    _make_instagram_publication(session_factory, job, checksum)
    with session_factory() as session:
        session.add(Publication(
            job_id=job.id, provider="instagram", account_reference="acct-1", privacy_status="PUBLIC",
            idempotency_key=f"instagram:acct-1:{job.id}:{checksum}", final_video_checksum=checksum,
        ))
        with pytest.raises(IntegrityError):
            session.commit()


def test_different_checksum_after_a_rerender_is_a_genuinely_new_tuple(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "idem-3")
    _make_instagram_publication(session_factory, job, checksum)
    with session_factory() as session:
        session.add(Publication(
            job_id=job.id, provider="instagram", account_reference="acct-1", privacy_status="PUBLIC",
            idempotency_key=f"instagram:acct-1:{job.id}:different-checksum",
            final_video_checksum="different-checksum",
        ))
        session.commit()  # different checksum -> not a constraint violation
    with session_factory() as session:
        assert session.query(Publication).filter(Publication.job_id == job.id).count() == 2


# -- reconciliation -------------------------------------------------------

def test_already_consistent_when_container_id_already_known_and_confirmed(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-1")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.PROCESSING.value,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        db_pub.provider_video_id = "container-id-1"
        session.commit()

    bundle = _bundle(tmp_path, _instagram_publisher(lambda r: httpx.Response(200, json={
        "status_code": "IN_PROGRESS",
    })))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "already_consistent"
        assert result.provider_video_id == "container-id-1"


def test_remote_video_missing_when_a_previously_known_container_id_stops_resolving(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-2")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.PROCESSING.value,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        db_pub.provider_video_id = "container-id-gone"
        session.commit()

    bundle = _bundle(tmp_path, _instagram_publisher(lambda r: httpx.Response(500)))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "remote_video_missing"
        assert result.provider_video_id == "container-id-gone"


def test_credentials_unavailable_when_container_status_check_fails_auth(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-3")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.PROCESSING.value,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        db_pub.provider_video_id = "container-id-1"
        session.commit()

    bundle = _bundle(tmp_path, _instagram_publisher(lambda r: httpx.Response(401)))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "credentials_unavailable"


def test_upload_session_expired_for_an_instagram_publication_mid_upload(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """InstagramPublisher.query_upload_offset always raises
    UploadSessionExpiredError (no documented offset-query endpoint -- see
    docs/PUBLISHING.md), so the existing generic reconciliation path
    always resolves a mid-upload Instagram publication with a known
    session reference to upload_session_expired, never a guessed partial
    offset."""
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-4")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.UPLOADING.value,
    )
    bundle = _bundle(tmp_path, _instagram_publisher(lambda r: httpx.Response(200)))
    bundle.session_store.set(pub_id, f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/container-id-1")
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        db_pub.total_bytes = 1000
        session.commit()

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "upload_session_expired"
    assert bundle.session_store.get(pub_id) is None  # cleared so a fresh container is safe to create


def test_publishing_limit_reached_when_account_info_shows_quota_exhausted(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """A stuck publication (never even created a container) whose
    account has reached Instagram's documented 100-posts-per-24-hours
    limit gets a specific, actionable outcome instead of a generic
    upload_incomplete."""
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-5")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.READY_TO_UPLOAD.value,
    )
    bundle = _bundle(tmp_path, _instagram_publisher(
        _account_info_handler(quota_usage=100, quota_total=100),
    ))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "publishing_limit_reached"


def test_upload_incomplete_when_nothing_publishing_limit_related_explains_the_stall(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    """The proactive account-info check must never mask an ordinary
    'just hasn't been retried yet' case."""
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-6")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.READY_TO_UPLOAD.value,
    )
    bundle = _bundle(tmp_path, _instagram_publisher(_account_info_handler()))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "upload_incomplete"


def test_manual_review_required_when_account_no_longer_professional(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-7")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.READY_TO_UPLOAD.value,
    )
    bundle = _bundle(tmp_path, _instagram_publisher(
        _account_info_handler(account_type="PERSONAL"),
    ))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "manual_review_required"


def test_eligibility_check_does_not_block_on_a_transient_account_info_error(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    job, checksum = _make_completed_job(job_service, channel, session_factory, storage, tmp_path, "rec-ig-8")
    pub_id = _make_instagram_publication(
        session_factory, job, checksum, status=PublicationStatus.READY_TO_UPLOAD.value,
    )
    bundle = _bundle(tmp_path, _instagram_publisher(lambda r: httpx.Response(500)))
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "upload_incomplete"  # falls through, never blocked by a transient hiccup
