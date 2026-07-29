"""Backup/restore E2E: a real job (driven through the full pipeline with
real ffmpeg, reaching a real final.mp4 on disk) plus a publication row are
backed up into a bundle, the original storage/DB root is isolated away
(renamed, not deleted -- proving restore doesn't need the original to
still exist), the bundle is restored into a BRAND NEW root, and every
piece of data is confirmed present there: DB rows, file checksums,
manifest.json, and the publish journal. A worker "restart" (a fresh
engine/session_factory against the restored DB) still sees everything.
Credential non-inclusion and archive-traversal safety are also proven
here against a REAL bundle (test_backup_bundle.py already covers
traversal against synthetic archives at the unit level)."""
from __future__ import annotations

import hashlib
import tarfile

import pytest

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.schema import create_engine_from_url, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.ops.backup_bundle import BackupBundleError, backup_create, backup_inspect, backup_restore
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider
from reel_harness.publisher.journal import PublishJournal
from reel_harness.storage.local import LocalFilesystemStorage
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg for an actual render")


def test_backup_restore_e2e_full_round_trip(tmp_path) -> None:
    original_root = tmp_path / "original"
    original_root.mkdir()
    db_path = original_root / "rh.db"
    jobs_root = original_root / "jobs"
    journal_dir = original_root / "journal"
    database_url = f"sqlite:///{db_path}"

    from reel_harness.db.schema import init_db

    engine = create_engine_from_url(database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(jobs_root)

    from reel_harness.core.service import JobService

    job_service = JobService(session_factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="cooking", language="en")

    # 1. job created, 2. render completed (real ffmpeg via run_job)
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="fried rice")
    providers = ProviderBundle(llm=FakeLLMProvider(), tts=FakeTTSProvider(), stock_media=FakeStockMediaProvider())
    with session_factory() as session:
        from reel_harness.db.models import Channel as ChannelModel
        from reel_harness.db.models import Job as JobModel

        db_job = session.get(JobModel, job.id)
        db_channel = session.get(ChannelModel, channel.id)
        run_job(session, db_job, db_channel, providers, storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value

    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    assert final_path.is_file()
    original_video_bytes = final_path.read_bytes()
    original_checksum = hashlib.sha256(original_video_bytes).hexdigest()

    # 3. publication created (direct insert -- see docs/STATUS.md: Fake
    # asset licenses are permanently publish-ineligible by design, so the
    # normal eligibility-gated create_publication() path cannot be used
    # here; a real Publication row is all this test needs).
    from reel_harness.db.models import Publication

    with session_factory() as session:
        pub = Publication(
            job_id=job.id, provider="youtube", account_reference="default", status="PUBLISHED",
            privacy_status="private", idempotency_key="pub-1",
            final_video_checksum=original_checksum, bytes_uploaded=len(original_video_bytes),
        )
        session.add(pub)
        session.commit()
        pub_id = pub.id

    journal = PublishJournal(journal_dir)
    from datetime import UTC, datetime

    journal.append(
        publication_id=pub_id, job_id=job.id, provider="youtube", account_reference="default",
        final_video_checksum=original_checksum, event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="real-video-id-123",
    )

    # 4. backup bundle created
    bundle_path = tmp_path / "bundle.tar.gz"
    config_fingerprint = {"app_version": "0.1.0rc1", "llm_provider": "fake"}
    backup_create(database_url, jobs_root, journal_dir, config_fingerprint, bundle_path)

    inspected = backup_inspect(bundle_path)
    assert inspected["manifest"]["config_fingerprint"] == config_fingerprint

    # credential non-inclusion: no member name looks like a credential/
    # journal-adjacent secret path.
    with tarfile.open(bundle_path, "r:gz") as tar:
        names = tar.getnames()
    for name in names:
        assert "credential" not in name.lower()
        assert ".env" not in name
        assert "oauth" not in name.lower()

    # 5. original isolated away (renamed, not deleted)
    isolated_root = tmp_path / "original_isolated"
    engine.dispose()
    original_root.rename(isolated_root)
    assert not original_root.exists()

    # 6. restore into a BRAND NEW root
    restored_root = tmp_path / "restored"
    restored_db_path = restored_root / "rh.db"
    restored_jobs_root = restored_root / "jobs"
    restored_journal_dir = restored_root / "journal"
    restored_database_url = f"sqlite:///{restored_db_path}"
    result = backup_restore(
        bundle_path, restored_jobs_root, restored_database_url, restored_journal_dir, confirm_restore=True,
    )
    assert result["restored"] is True

    # 7. DB verify
    from reel_harness.ops.db_tools import db_verify

    restored_engine = create_engine_from_url(restored_database_url)
    restored_session_factory = make_session_factory(restored_engine)
    verify_result = db_verify(restored_engine, restored_session_factory)
    assert verify_result.ok is True

    # 8. checksums -- the restored final.mp4 is byte-identical
    restored_storage = LocalFilesystemStorage(restored_jobs_root)
    restored_final_path = restored_storage.job_dir(job.id) / "final" / "final.mp4"
    assert restored_final_path.is_file()
    assert hashlib.sha256(restored_final_path.read_bytes()).hexdigest() == original_checksum

    # 9. manifest.json present and valid
    from reel_harness.manifest.schema import Manifest

    manifest_bytes = restored_storage.read_bytes(job.id, "manifest.json")
    restored_manifest = Manifest.model_validate_json(manifest_bytes)
    assert restored_manifest.final_video_checksum_sha256 == original_checksum

    # 10. journals present and integrity-verifiable
    restored_journal = PublishJournal(restored_journal_dir)
    events = restored_journal.read_events(pub_id)
    assert len(events) == 1
    assert events[0]["provider_video_id"] == "real-video-id-123"

    # 11. "worker restart" -- yet another fresh engine/session_factory
    restored_engine.dispose()
    restarted_engine = create_engine_from_url(restored_database_url)
    restarted_session_factory = make_session_factory(restarted_engine)

    # 12. job/publication queryable after the "restart"
    with restarted_session_factory() as session:
        from reel_harness.db.models import Job as JobModel2
        from reel_harness.db.models import Publication as PublicationModel2

        restored_job = session.get(JobModel2, job.id)
        restored_pub = session.get(PublicationModel2, pub_id)
        assert restored_job is not None
        assert restored_job.status == JobStatus.REVIEW_REQUIRED.value
        assert restored_pub is not None
        assert restored_pub.status == "PUBLISHED"
        assert restored_pub.final_video_checksum == original_checksum
    restarted_engine.dispose()


def test_backup_restore_e2e_refuses_a_malicious_archive_and_touches_nothing(tmp_path) -> None:
    """A real, structurally valid-looking but maliciously-crafted archive
    (path traversal) must be refused before any extraction touches the
    destination -- exercised here against backup_restore's real
    destination-writing path, not just backup_inspect's read-only one
    (see test_backup_bundle.py for the broader unit-level matrix)."""
    import io

    malicious = tmp_path / "evil.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        payload = b"pwned"
        info = tarfile.TarInfo(name="../../escape.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    dest_jobs = tmp_path / "dest_jobs"
    with pytest.raises(BackupBundleError, match="traversal"):
        backup_restore(
            malicious, dest_jobs, f"sqlite:///{tmp_path / 'x.db'}", tmp_path / "journal", confirm_restore=True,
        )
    assert not dest_jobs.exists()
    assert not (tmp_path / "escape.txt").exists()
