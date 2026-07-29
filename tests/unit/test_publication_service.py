"""core.publish_service.PublicationService: eligibility gate, idempotent
creation, public-upload double confirmation, cancel policy. No network."""
from __future__ import annotations

import hashlib
import threading
from datetime import UTC, datetime

import pytest

from reel_harness.core.publish_service import (
    PublicationInvalidActionError,
    PublicationNotEligibleError,
    PublicationNotFoundError,
    PublicationService,
)
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available

pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"eligible-{seed}.mp4"
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


def _make_eligible_job(job_service, channel, session_factory, storage, tmp_path, key: str = "pub-svc-1"):
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_transition(db_job, JobStatus.TTS_GENERATING)
        apply_transition(db_job, JobStatus.RENDERING)
        apply_transition(db_job, JobStatus.VALIDATING)
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
    return job


@pytest.fixture
def publish_service(session_factory, storage):
    return PublicationService(session_factory, storage)


def test_create_publication_for_eligible_job_reaches_ready_to_upload(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    pub, eligibility = publish_service.create_publication(
        job.id, provider="youtube", account_reference="acct-1",
        publisher_snapshot={"provider": "youtube", "adapter_version": "v1"},
    )
    assert eligibility.eligible is True
    assert pub.status == PublicationStatus.READY_TO_UPLOAD.value
    assert pub.privacy_status == "private"  # default, never public
    assert pub.publisher_config == {"provider": "youtube", "adapter_version": "v1"}


def test_ineligible_job_raises_with_structured_reasons(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="pub-svc-2", topic="t")
    with pytest.raises(PublicationNotEligibleError) as exc_info:
        publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    assert "JOB_NOT_COMPLETED" in exc_info.value.reasons
    assert "MANIFEST_MISSING" in exc_info.value.reasons


def test_unknown_job_id_raises_not_found(publish_service) -> None:
    with pytest.raises(PublicationNotFoundError):
        publish_service.create_publication("does-not-exist", provider="youtube", account_reference="a")


def test_second_call_with_same_tuple_returns_the_existing_publication(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    first, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    second, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    assert first.id == second.id

    with session_factory() as session:
        count = session.query(Publication).filter(Publication.job_id == job.id).count()
    assert count == 1


def test_concurrent_create_publication_calls_yield_exactly_one_row(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    results: list = []
    errors: list = []

    def _create() -> None:
        try:
            pub, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
            results.append(pub.id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_create) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, errors
    assert len(set(results)) == 1, "concurrent creation must converge on exactly one Publication"
    with session_factory() as session:
        count = session.query(Publication).filter(Publication.job_id == job.id).count()
    assert count == 1


def test_public_privacy_requires_both_confirm_flag_and_feature_flag(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    with pytest.raises(PublicationInvalidActionError):
        publish_service.create_publication(
            job.id, provider="youtube", account_reference="acct-1", privacy_status="public",
        )
    with pytest.raises(PublicationInvalidActionError):
        publish_service.create_publication(
            job.id, provider="youtube", account_reference="acct-1", privacy_status="public",
            confirm_public_upload=True, public_upload_enabled=False,
        )
    with pytest.raises(PublicationInvalidActionError):
        publish_service.create_publication(
            job.id, provider="youtube", account_reference="acct-1", privacy_status="public",
            confirm_public_upload=False, public_upload_enabled=True,
        )
    pub, _ = publish_service.create_publication(
        job.id, provider="youtube", account_reference="acct-1", privacy_status="public",
        confirm_public_upload=True, public_upload_enabled=True,
    )
    assert pub.privacy_status == "public"


def test_unknown_privacy_status_rejected(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    with pytest.raises(PublicationInvalidActionError):
        publish_service.create_publication(
            job.id, provider="youtube", account_reference="acct-1", privacy_status="everyone-in-the-world",
        )


def test_platform_options_confirmation_required_when_capability_demands_it(
    job_service, channel, session_factory, storage, publish_service, tmp_path, monkeypatch,
) -> None:
    """No real adapter sets requires_user_confirmation=True yet (that lands
    with the TikTok adapter, whose direct-publish options -- comments/duet/
    stitch/commercial-disclosure -- must be reviewed before every publish).
    Simulated here via a capability override so the gate itself is proven
    correct ahead of that adapter existing."""
    import dataclasses

    from reel_harness.providers import registry as registry_module

    real_capabilities = registry_module.provider_capabilities

    def _fake_capabilities(provider: str):
        caps = real_capabilities(provider)
        if provider == "youtube":
            return dataclasses.replace(caps, requires_user_confirmation=True)
        return caps

    monkeypatch.setattr(registry_module, "provider_capabilities", _fake_capabilities)

    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    with pytest.raises(PublicationInvalidActionError):
        publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")

    pub, _ = publish_service.create_publication(
        job.id, provider="youtube", account_reference="acct-1", confirm_platform_options=True,
    )
    assert pub.status == PublicationStatus.READY_TO_UPLOAD.value


def test_cancel_before_upload_is_immediate(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    pub, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    cancelled = publish_service.cancel_publication(pub.id)
    assert cancelled.status == PublicationStatus.CANCELLED.value


def test_cancel_during_upload_only_flags_does_not_change_status(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    pub, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        from reel_harness.core.state_machine import apply_publication_transition

        apply_publication_transition(
            db_pub, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference="ref-1",
        )
        apply_publication_transition(db_pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")
        db_pub.locked_by = "worker-a"
        session.commit()

    flagged = publish_service.cancel_publication(pub.id)
    assert flagged.status == PublicationStatus.UPLOADING.value
    assert flagged.cancel_requested is True


def test_cancel_refused_once_published(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    pub, _ = publish_service.create_publication(job.id, provider="youtube", account_reference="acct-1")
    with session_factory() as session:
        db_pub = session.get(Publication, pub.id)
        from reel_harness.core.state_machine import apply_publication_transition

        apply_publication_transition(
            db_pub, PublicationStatus.UPLOAD_SESSION_CREATED, upload_session_reference="ref-1",
        )
        apply_publication_transition(db_pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")
        apply_publication_transition(db_pub, PublicationStatus.UPLOAD_COMPLETED)
        apply_publication_transition(db_pub, PublicationStatus.PROCESSING)
        apply_publication_transition(db_pub, PublicationStatus.PUBLISHED)
        session.commit()

    with pytest.raises(PublicationInvalidActionError):
        publish_service.cancel_publication(pub.id)


def test_dry_run_style_check_eligibility_never_persists_anything(
    job_service, channel, session_factory, storage, publish_service, tmp_path,
) -> None:
    job = _make_eligible_job(job_service, channel, session_factory, storage, tmp_path)
    result = publish_service.check_eligibility(job.id)
    assert result.eligible is True
    with session_factory() as session:
        assert session.query(Publication).filter(Publication.job_id == job.id).count() == 0
