from __future__ import annotations

import tarfile

import pytest

from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.ops.backup_bundle import (
    BackupBundleError,
    backup_create,
    backup_inspect,
    backup_restore,
)


@pytest.fixture
def source_layout(tmp_path):
    db_path = tmp_path / "source.db"
    engine = create_engine_from_url(f"sqlite:///{db_path}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    jobs_root = tmp_path / "jobs"
    job_dir = jobs_root / "11111111-1111-1111-1111-111111111111"
    (job_dir / "final").mkdir(parents=True)
    (job_dir / "final" / "final.mp4").write_bytes(b"fake video bytes")
    (job_dir / "manifest.json").write_text('{"job_id": "x"}')

    journal_dir = tmp_path / "journal"
    journal_dir.mkdir()
    (journal_dir / "pub-1.jsonl").write_text('{"event": "upload_completed"}\n')

    return {
        "database_url": f"sqlite:///{db_path}", "jobs_root": jobs_root, "journal_dir": journal_dir,
        "engine": engine, "session_factory": session_factory,
    }


def test_backup_create_produces_inspectable_bundle(source_layout, tmp_path) -> None:
    dest = tmp_path / "bundle.tar.gz"
    result = backup_create(
        source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"],
        {"app_version": "0.1.0rc1"}, dest,
    )
    assert dest.exists()
    assert result["file_count"] >= 4  # db, manifest, final.mp4, manifest.json, journal entry, checksums

    inspected = backup_inspect(dest)
    assert inspected["manifest"]["config_fingerprint"] == {"app_version": "0.1.0rc1"}
    assert inspected["checksums_recorded"] == result["file_count"]


def test_backup_bundle_never_includes_credential_paths(source_layout, tmp_path) -> None:
    dest = tmp_path / "bundle.tar.gz"
    backup_create(
        source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"],
        {}, dest,
    )
    with tarfile.open(dest, "r:gz") as tar:
        names = tar.getnames()
    for name in names:
        assert "credential" not in name.lower()
        assert ".env" not in name
        assert "oauth" not in name.lower()


def test_backup_restore_requires_confirmation(source_layout, tmp_path) -> None:
    dest = tmp_path / "bundle.tar.gz"
    backup_create(
        source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"], {}, dest,
    )
    with pytest.raises(BackupBundleError, match="confirm"):
        backup_restore(
            dest, tmp_path / "restored_jobs", f"sqlite:///{tmp_path / 'restored.db'}",
            tmp_path / "restored_journal", confirm_restore=False,
        )


def test_backup_restore_round_trips_content(source_layout, tmp_path) -> None:
    dest = tmp_path / "bundle.tar.gz"
    backup_create(
        source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"], {}, dest,
    )
    restored_jobs = tmp_path / "restored_jobs"
    restored_db_path = tmp_path / "restored.db"
    restored_journal = tmp_path / "restored_journal"
    result = backup_restore(
        dest, restored_jobs, f"sqlite:///{restored_db_path}", restored_journal, confirm_restore=True,
    )
    assert result["restored"] is True
    assert restored_db_path.exists()
    final_video = restored_jobs / "11111111-1111-1111-1111-111111111111" / "final" / "final.mp4"
    assert final_video.read_bytes() == b"fake video bytes"
    assert (restored_journal / "pub-1.jsonl").exists()

    restored_engine = create_engine_from_url(f"sqlite:///{restored_db_path}")
    from sqlalchemy import text

    with restored_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM schema_migrations")).scalar_one() == 1


def test_backup_restore_refuses_tampered_checksum(source_layout, tmp_path) -> None:
    dest = tmp_path / "bundle.tar.gz"
    backup_create(
        source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"], {}, dest,
    )
    # Tamper: flip bytes in the middle of the (gzip-compressed) archive --
    # appending junk after the stream is silently tolerated by gzip readers,
    # so this must corrupt actual content, not just trailing bytes.
    with open(dest, "r+b") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(size // 2)
        handle.write(b"\xff" * 16)

    with pytest.raises(Exception):  # noqa: B017 - either BackupBundleError or a tarfile read error, both refusals
        backup_restore(
            dest, tmp_path / "restored_jobs", f"sqlite:///{tmp_path / 'restored.db'}",
            tmp_path / "restored_journal", confirm_restore=True,
        )
    assert not (tmp_path / "restored_jobs").exists()


def test_backup_inspect_refuses_path_traversal_member(tmp_path) -> None:
    malicious = tmp_path / "evil.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        import io

        payload = b"pwned"
        info = tarfile.TarInfo(name="../../etc/evil.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupBundleError, match="traversal"):
        backup_inspect(malicious)


def test_backup_inspect_refuses_absolute_path_member(tmp_path) -> None:
    malicious = tmp_path / "evil_abs.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        import io

        payload = b"pwned"
        info = tarfile.TarInfo(name="/etc/passwd")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupBundleError, match="absolute"):
        backup_inspect(malicious)


def test_backup_inspect_refuses_symlink_member(tmp_path) -> None:
    malicious = tmp_path / "evil_symlink.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        info = tarfile.TarInfo(name="jobs/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)

    with pytest.raises(BackupBundleError, match="symlink"):
        backup_inspect(malicious)


def test_backup_restore_never_extracts_malicious_archive(tmp_path) -> None:
    malicious = tmp_path / "evil.tar.gz"
    with tarfile.open(malicious, "w:gz") as tar:
        import io

        payload = b"pwned"
        info = tarfile.TarInfo(name="../../escape.txt")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    dest_jobs = tmp_path / "dest_jobs"
    with pytest.raises(BackupBundleError, match="traversal"):
        backup_restore(
            malicious, dest_jobs, f"sqlite:///{tmp_path / 'x.db'}", tmp_path / "journal",
            confirm_restore=True,
        )
    assert not dest_jobs.exists()
    assert not (tmp_path.parent / "escape.txt").exists()


def test_backup_create_is_atomic_on_failure(source_layout, tmp_path, monkeypatch) -> None:
    dest = tmp_path / "bundle.tar.gz"

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure mid-archive")

    import reel_harness.ops.backup_bundle as backup_bundle_module

    monkeypatch.setattr(backup_bundle_module, "_sqlite_online_backup", _boom)
    with pytest.raises(RuntimeError):
        backup_create(
            source_layout["database_url"], source_layout["jobs_root"], source_layout["journal_dir"], {}, dest,
        )
    assert not dest.exists()
    assert list(tmp_path.glob(".bundle.tar.gz.tmp-*")) == []


def test_backup_inspect_refuses_archive_bomb(tmp_path, monkeypatch) -> None:
    import reel_harness.ops.backup_bundle as backup_bundle_module

    monkeypatch.setattr(backup_bundle_module, "_MAX_TOTAL_SIZE_BYTES", 100)
    bomb = tmp_path / "bomb.tar.gz"
    with tarfile.open(bomb, "w:gz") as tar:
        import io

        payload = b"x" * 1000
        info = tarfile.TarInfo(name="big.bin")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    with pytest.raises(BackupBundleError, match="size cap"):
        backup_inspect(bomb)
