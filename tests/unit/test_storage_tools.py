from __future__ import annotations

import hashlib
import time

from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Asset, Job
from reel_harness.ops.storage_tools import storage_verify


def _make_job_with_asset(job_service, channel, session_factory, storage, video_bytes: bytes) -> str:
    job, _ = job_service.create_job(channel.id, idempotency_key="k", topic="t")
    checksum = hashlib.sha256(video_bytes).hexdigest()
    path = storage.job_dir(job.id) / "assets" / "scene_0" / "a.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(video_bytes)
    with session_factory() as session:
        session.add(Asset(
            job_id=job.id, scene_index=0, source_provider="fake", local_path=str(path),
            checksum_sha256=checksum, mime_type="image/png", license_type="FAKE_TEST_LICENSE",
        ))
        session.commit()
    return job.id


def test_storage_verify_ok_on_fresh_empty_storage(job_service, channel, session_factory, storage) -> None:
    result = storage_verify(storage, session_factory)
    assert result.ok is True
    assert result.jobs_checked == 0


def test_storage_verify_passes_with_matching_asset_checksum(
    job_service, channel, session_factory, storage,
) -> None:
    _make_job_with_asset(job_service, channel, session_factory, storage, b"real bytes")
    result = storage_verify(storage, session_factory)
    assert result.ok is True
    assert result.jobs_checked == 1


def test_storage_verify_detects_checksum_mismatch(job_service, channel, session_factory, storage) -> None:
    job_id = _make_job_with_asset(job_service, channel, session_factory, storage, b"real bytes")
    with session_factory() as session:
        asset = session.query(Asset).filter(Asset.job_id == job_id).one()
        path = asset.local_path
    from pathlib import Path

    Path(path).write_bytes(b"tampered bytes")
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "checksum_mismatch" for i in result.issues)


def test_storage_verify_detects_missing_asset_file(job_service, channel, session_factory, storage) -> None:
    job_id = _make_job_with_asset(job_service, channel, session_factory, storage, b"real bytes")
    with session_factory() as session:
        asset = session.query(Asset).filter(Asset.job_id == job_id).one()
        path = asset.local_path
    from pathlib import Path

    Path(path).unlink()
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "missing_file" for i in result.issues)


def test_storage_verify_detects_orphan_directory(job_service, channel, session_factory, storage) -> None:
    orphan_dir = storage.root_dir / "12345678-1234-1234-1234-123456789012"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "final").mkdir()
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "orphan_directory" for i in result.issues)


def test_storage_verify_detects_corrupt_manifest(job_service, channel, session_factory, storage) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k", topic="t")
    storage.write_bytes(job.id, "manifest.json", b"{not valid json")
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "corrupt_manifest" for i in result.issues)


def test_storage_verify_detects_missing_final_video_when_expected(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k", topic="t")
    # Force the job directory to exist (a job with no writes yet has none)
    # without ever writing final.mp4, so this isolates "directory exists,
    # final.mp4 specifically missing" from the separate missing_directory case.
    (storage.job_dir(job.id) / "render").mkdir(parents=True)
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        for status in (
            JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING, JobStatus.ASSET_FETCHING,
            JobStatus.TTS_GENERATING, JobStatus.RENDERING, JobStatus.VALIDATING,
        ):
            apply_transition(db_job, status)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        session.commit()
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "missing_file" and "final.mp4" in i.detail for i in result.issues)


def test_storage_verify_repair_safe_removes_only_stale_temp_files(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k", topic="t")
    job_dir = storage.job_dir(job.id)
    (job_dir / "final").mkdir(parents=True)
    stale_temp = job_dir / "final" / "final.mp4.tmp-abc123"
    fresh_temp = job_dir / "final" / "final.mp4.tmp-fresh"
    stale_temp.write_bytes(b"leftover")
    fresh_temp.write_bytes(b"leftover")
    old_time = time.time() - 7200
    import os

    os.utime(stale_temp, (old_time, old_time))

    result = storage_verify(storage, session_factory, repair_safe=True)
    assert not stale_temp.exists()
    assert fresh_temp.exists()  # too recent to touch
    assert str(stale_temp) in result.repaired


def test_storage_verify_never_repairs_without_the_flag(job_service, channel, session_factory, storage) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k", topic="t")
    job_dir = storage.job_dir(job.id)
    (job_dir / "final").mkdir(parents=True)
    stale_temp = job_dir / "final" / "final.mp4.tmp-abc123"
    stale_temp.write_bytes(b"leftover")
    old_time = time.time() - 7200
    import os

    os.utime(stale_temp, (old_time, old_time))

    result = storage_verify(storage, session_factory, repair_safe=False)
    assert stale_temp.exists()
    assert result.repaired == []
    assert any(i.kind == "stale_temp_file" for i in result.issues)


def test_storage_verify_never_flags_a_freshly_queued_job_as_missing_directory(
    job_service, channel, session_factory, storage,
) -> None:
    """A job that was just created and is still QUEUED (or earlier) has
    legitimately written nothing to disk yet -- SCRIPT/POLICY only touch
    the DB, and the first stage to write a real file is ASSET_FETCHING.
    Found via a real Phase 4B soak test: a handful of jobs still queued
    behind a busy render worker made storage-verify FAIL on an otherwise
    perfectly healthy system."""
    job_service.create_job(channel.id, idempotency_key="k1", topic="t")  # QUEUED, no directory written
    result = storage_verify(storage, session_factory)
    assert result.ok is True
    assert result.issues == []


def test_storage_verify_still_flags_missing_directory_past_asset_fetching(
    job_service, channel, session_factory, storage,
) -> None:
    """Once a job has progressed past the point where SOME file should
    exist (ASSET_FETCHING or later), a genuinely missing directory is
    still a real defect and must still be reported."""
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        for status in (JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING, JobStatus.ASSET_FETCHING):
            apply_transition(db_job, status)
        session.commit()
    # No directory was ever created for this job despite being past
    # ASSET_FETCHING -- a real inconsistency.
    result = storage_verify(storage, session_factory)
    assert result.ok is False
    assert any(i.kind == "missing_directory" for i in result.issues)
