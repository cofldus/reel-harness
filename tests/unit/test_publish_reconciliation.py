"""core.publish_reconciliation.reconcile_publication: crash-recovery
decision logic (durable journal + read-only provider checks), driven with
FakePublisher/a small scripted variant -- no network. See
tests/e2e/test_youtube_publisher_e2e.py and the Commit 6 crash-recovery
E2E scenarios for the fuller real-adapter version of scenario (A)."""
from __future__ import annotations

import hashlib
from datetime import UTC, datetime

import pytest

from reel_harness.core.errors import ProviderAuthError, TransientProviderError
from reel_harness.core.publish_reconciliation import reconcile_publication
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
from reel_harness.worker.publish_runner import PublishBundle, _create_session_stage, run_publication

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


class _ScriptedPublisher(FakePublisher):
    """A FakePublisher whose get_processing_status/query_upload_offset can
    be scripted to raise a specific error or return a specific result,
    falling back to real FakePublisher behavior otherwise."""

    def __init__(self, *, processing_result=None, processing_error=None) -> None:
        super().__init__()
        self._processing_result = processing_result
        self._processing_error = processing_error

    def get_processing_status(self, provider_video_id: str) -> ProcessingStatusResult:
        if self._processing_error is not None:
            raise self._processing_error
        if self._processing_result is not None:
            return self._processing_result
        return super().get_processing_status(provider_video_id)


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"reconcile-{seed}.mp4"
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


def _bundle(tmp_path, publisher) -> tuple[PublishBundle, FileSecretStore]:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    return PublishBundle(
        publisher=publisher, session_store=UploadSessionStore(store),
        journal=PublishJournal(store.root_dir / "publish_journal"),
    ), store


def test_already_consistent_for_a_terminal_published_publication(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-1")
    bundle, _ = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "already_consistent"
        assert result.provider_video_id == db_pub.provider_video_id


def test_manual_review_required_when_locked_by_an_active_worker(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-2")
    bundle, _ = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        db_pub.locked_by = "some-other-worker"
        session.commit()
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "manual_review_required"
        assert "locked" in result.reasons[0]


def test_upload_session_expired_when_the_session_is_unknown_to_the_provider(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-3")
    creator_bundle, store = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        total_bytes = (storage.job_dir(job.id) / "final" / "final.mp4").stat().st_size
        _create_session_stage(session, db_pub, job, storage, creator_bundle, None, total_bytes, None)
        assert db_pub.status == PublicationStatus.UPLOAD_SESSION_CREATED.value
        # Simulate a crash right after: still "in flight" per the state
        # machine's own book-keeping.
        db_pub.status = PublicationStatus.UPLOADING.value
        session.commit()

    # A fresh publisher instance never saw this session reference -- this
    # is exactly what "the provider says the session is gone" looks like.
    fresh_bundle = PublishBundle(
        publisher=FakePublisher(), session_store=creator_bundle.session_store, journal=creator_bundle.journal,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = reconcile_publication(session, db_pub, fresh_bundle)
        assert result.outcome == "upload_session_expired"


def test_upload_incomplete_when_the_provider_confirms_a_partial_offset(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-4")
    bundle, _ = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        total_bytes = (storage.job_dir(job.id) / "final" / "final.mp4").stat().st_size
        _create_session_stage(session, db_pub, job, storage, bundle, None, total_bytes, None)
        db_pub.status = PublicationStatus.UPLOADING.value
        session.commit()

        # Same publisher instance still knows the session; 0 bytes received
        # so far is a genuinely partial, safely-resumable upload.
        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "upload_incomplete"


def test_ambiguous_when_provider_reports_complete_but_nothing_local_confirms_it(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-5")
    publisher = FakePublisher()
    bundle, _ = _bundle(tmp_path, publisher)
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        job = session.get(Job, db_pub.job_id)
        total_bytes = (storage.job_dir(job.id) / "final" / "final.mp4").stat().st_size
        _create_session_stage(session, db_pub, job, storage, bundle, None, total_bytes, None)
        db_pub.status = PublicationStatus.UPLOADING.value
        session.commit()

        # Upload the whole file directly through the publisher (bypassing
        # _upload_stage entirely) so the provider's own state says
        # "complete", but neither the DB nor the journal ever recorded it --
        # the one case a crash could leave truly ambiguous.
        final_path = storage.job_dir(db_pub.job_id) / "final" / "final.mp4"
        video_bytes = final_path.read_bytes()
        handle = bundle.session_store.get(db_pub.id)
        from reel_harness.providers.base import UploadSessionHandle
        session_handle = UploadSessionHandle(
            session_reference=handle, total_bytes=len(video_bytes), chunk_size=262144,
        )
        publisher.upload_chunk(session_handle, video_bytes, 0, len(video_bytes))

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "ambiguous_remote_state"
        assert db_pub.provider_video_id is None  # never auto-repaired from ambiguity alone


def _run_to_published_then_simulate_a_lost_commit(job_service, channel, session_factory, storage, tmp_path, key):
    """Runs a publication all the way to PUBLISHED for real (so the durable
    journal has a genuine upload_completed record with a real provider_video_id
    and the session store/journal files exist on disk), then hand-resets the
    DB row back to UPLOADING with no provider_video_id -- reproducing exactly
    what a crash between the provider's response and the DB commit leaves
    behind, without needing to actually kill a process mid-test."""
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, key)
    bundle, _ = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        recovered_video_id = db_pub.provider_video_id
        assert recovered_video_id is not None

        db_pub.status = PublicationStatus.UPLOADING.value
        db_pub.provider_video_id = None
        db_pub.publication_url = None
        db_pub.bytes_uploaded = 0
        db_pub.published_at = None
        session.commit()
    return pub.id, recovered_video_id, bundle


def test_recovered_remote_video_from_the_durable_journal_after_a_simulated_crash(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub_id, recovered_video_id, bundle = _run_to_published_then_simulate_a_lost_commit(
        job_service, channel, session_factory, storage, tmp_path, "rec-6",
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        assert db_pub.status == PublicationStatus.UPLOADING.value
        assert db_pub.provider_video_id is None

        result = reconcile_publication(session, db_pub, bundle)
        assert result.outcome == "recovered_remote_video"
        assert result.provider_video_id == recovered_video_id
        assert db_pub.provider_video_id == recovered_video_id
        assert db_pub.status == PublicationStatus.PUBLISHED.value  # FakePublisher confirms "succeeded"


def test_manual_review_required_when_the_journal_video_id_cannot_be_confirmed(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub_id, recovered_video_id, bundle = _run_to_published_then_simulate_a_lost_commit(
        job_service, channel, session_factory, storage, tmp_path, "rec-7",
    )
    unconfirmable_bundle = PublishBundle(
        publisher=_ScriptedPublisher(processing_error=TransientProviderError("fake: not found")),
        session_store=bundle.session_store, journal=bundle.journal,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, unconfirmable_bundle)
        assert result.outcome == "manual_review_required"
        assert db_pub.provider_video_id is None  # never repaired from an unconfirmed candidate


def test_credentials_unavailable_when_confirming_the_journal_video_id_fails_auth(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub_id, recovered_video_id, bundle = _run_to_published_then_simulate_a_lost_commit(
        job_service, channel, session_factory, storage, tmp_path, "rec-8",
    )
    auth_broken_bundle = PublishBundle(
        publisher=_ScriptedPublisher(processing_error=ProviderAuthError("fake: token invalid")),
        session_store=bundle.session_store, journal=bundle.journal,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub_id)
        result = reconcile_publication(session, db_pub, auth_broken_bundle)
        assert result.outcome == "credentials_unavailable"


def test_remote_video_missing_when_a_previously_known_video_id_stops_resolving(
    job_service, channel, session_factory, storage, tmp_path,
) -> None:
    pub = _make_ready_publication(job_service, channel, session_factory, storage, tmp_path, "rec-9")
    bundle, _ = _bundle(tmp_path, FakePublisher())
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        run_publication(session, db_pub, storage, bundle)
        assert db_pub.status == PublicationStatus.PUBLISHED.value
        # PUBLISHED is terminal (already_consistent short-circuits before
        # ever checking provider_video_id) -- roll back to PROCESSING, which
        # still has a known provider_video_id and DOES get re-confirmed.
        db_pub.status = PublicationStatus.PROCESSING.value
        db_pub.published_at = None
        session.commit()

    broken_bundle = PublishBundle(
        publisher=_ScriptedPublisher(processing_error=TransientProviderError("fake: gone")),
        session_store=bundle.session_store, journal=bundle.journal,
    )
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        result = reconcile_publication(session, db_pub, broken_bundle)
        assert result.outcome == "remote_video_missing"
        assert result.provider_video_id == db_pub.provider_video_id
