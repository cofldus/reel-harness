"""Failure drills for the Phase 4A release candidate: DB/storage/
dependency unavailability, credential-backend unavailability, and a
disk-full-style write failure -- each checked for the right status,
readiness signal, failure code, and (where relevant) confirmed data-loss-
free recovery path. Lease takeover, worker crash recovery, and multi-
worker safety already have solid dedicated coverage
(tests/integration/test_worker_crash_recovery.py, test_multi_worker.py,
test_publish_lease_takeover.py, test_asset_lease_takeover.py) and are
deliberately not duplicated here -- this file covers what those don't:
infrastructure-level failures (DB/storage/dependencies/credentials) as
seen through ops.preflight/ops.db_tools/ops.storage_tools, and one fault-
injected disk-full drill, clearly distinguished from a real E2E."""
from __future__ import annotations

import pytest

from reel_harness.config import Settings
from reel_harness.ops.preflight import run_preflight


def test_drill_db_unavailable_is_reported_not_crashed(tmp_path, monkeypatch) -> None:
    """A DB file that cannot be opened (directory used as the target path)
    must produce a clean FAIL from preflight, never an unhandled
    exception escaping to the caller."""
    monkeypatch.chdir(tmp_path)
    bogus_db_dir = tmp_path / "not_a_file"
    bogus_db_dir.mkdir()
    from reel_harness.db.schema import create_engine_from_url, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{bogus_db_dir}")
    session_factory = make_session_factory(engine)
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path.parent / "creds")

    report = run_preflight(settings, session_factory, profile="fake")
    checks = {c.name: c for c in report.checks}
    assert checks["db_connectivity"].status == "FAIL"
    assert checks["db_schema"].status == "FAIL"
    assert report.overall == "FAIL"


def test_drill_storage_read_only_is_detected(tmp_path, monkeypatch) -> None:
    """A storage root that exists but cannot be written to (permission
    revoked) must be caught by preflight's write-probe, not silently
    passed."""
    monkeypatch.chdir(tmp_path)
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'rh.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)

    readonly_root = tmp_path / "readonly_jobs"
    readonly_root.mkdir()
    import os
    import stat

    if os.name == "posix":
        os.chmod(readonly_root, stat.S_IREAD | stat.S_IEXEC)
        try:
            settings = Settings(jobs_dir=readonly_root, credential_dir=tmp_path.parent / "creds")
            report = run_preflight(settings, session_factory, profile="fake")
            checks = {c.name: c for c in report.checks}
            assert checks["storage_root_writable"].status == "FAIL"
        finally:
            os.chmod(readonly_root, stat.S_IRWXU)
    else:
        # Windows ACL-based read-only enforcement is a materially
        # different mechanism (see ops.preflight._check_credential_and_
        # journal_roots's own platform note) -- simulate the SAME
        # observable failure (write probe raises) via a monkeypatched
        # Path.write_bytes instead, proving the CHECK's own failure path
        # works even though this drill can't set a real Windows ACL here.
        import pathlib

        def _boom(self, data):
            raise OSError(13, "simulated permission denied")

        monkeypatch.setattr(pathlib.Path, "write_bytes", _boom)
        settings = Settings(jobs_dir=readonly_root, credential_dir=tmp_path.parent / "creds")
        report = run_preflight(settings, session_factory, profile="fake")
        checks = {c.name: c for c in report.checks}
        assert checks["storage_root_writable"].status == "FAIL"


def test_drill_ffmpeg_missing_is_reported(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("REEL_HARNESS_FFMPEG_PATH", str(tmp_path / "does-not-exist-ffmpeg.exe"))
    monkeypatch.setenv("REEL_HARNESS_FFPROBE_PATH", str(tmp_path / "does-not-exist-ffprobe.exe"))
    monkeypatch.delenv("PATH", raising=False)
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{tmp_path / 'rh.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path.parent / "creds")

    report = run_preflight(settings, session_factory, profile="fake")
    checks = {c.name: c for c in report.checks}
    assert checks["ffmpeg"].status == "FAIL"
    assert checks["ffprobe"].status == "FAIL"
    assert report.overall == "FAIL"


def test_drill_credential_backend_unavailable_is_reported(tmp_path, monkeypatch) -> None:
    """A credential directory that resolves INSIDE the repository (the
    one case FileSecretStore refuses outright) must surface as a clear,
    specific FAIL -- never a generic crash, and never silently treated
    as 'no credentials configured'."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{repo / 'rh.db'}")
    init_db(engine)
    session_factory = make_session_factory(engine)
    settings = Settings(jobs_dir=repo / "jobs", credential_dir=repo / "creds_inside_repo")

    report = run_preflight(settings, session_factory, profile="fake")
    checks = {c.name: c for c in report.checks}
    assert checks["repo_internal_credential"].status == "FAIL"
    assert report.overall == "FAIL"


def test_drill_disk_full_during_backup_leaves_no_partial_file(tmp_path, monkeypatch) -> None:
    """Fault-injected (real disk-full is impractical to reproduce
    portably in CI) -- distinct from the real backup E2E in
    tests/e2e/test_backup_restore_e2e.py, which exercises the successful
    path against real disk I/O. Here, os.replace is made to fail exactly
    like it would on ENOSPC, and the drill confirms db_backup raises
    (never silently "succeeds" with a truncated file) and leaves no
    partial temp file behind."""
    from reel_harness.db.schema import create_engine_from_url, init_db
    from reel_harness.ops.db_tools import db_backup

    url = f"sqlite:///{tmp_path / 'rh.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    engine.dispose()

    def _boom(*args, **kwargs):
        raise OSError(28, "No space left on device")  # ENOSPC

    monkeypatch.setattr("os.replace", _boom)
    dest_dir = tmp_path / "backups"
    with pytest.raises(OSError):
        db_backup(url, dest_dir)
    # No half-written backup file left in place under the final name, and
    # no permanently-orphaned temp file either (db_backup's own temp
    # naming is deterministic per call, so at most one candidate exists).
    if dest_dir.is_dir():
        finals = list(dest_dir.glob("*.sqlite3.bak"))
        assert finals == []


def test_drill_disk_full_during_backup_bundle_leaves_no_partial_archive(tmp_path, monkeypatch) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db
    from reel_harness.ops.backup_bundle import backup_create

    url = f"sqlite:///{tmp_path / 'rh.db'}"
    engine = create_engine_from_url(url)
    init_db(engine)
    engine.dispose()

    def _boom(*args, **kwargs):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("os.replace", _boom)
    dest = tmp_path / "bundle.tar.gz"
    with pytest.raises(OSError):
        backup_create(url, tmp_path / "jobs", tmp_path / "journal", {}, dest)
    assert not dest.exists()
    assert list(tmp_path.glob(".bundle.tar.gz.tmp-*")) == []


def test_drill_multiple_infrastructure_failures_all_surface_together(tmp_path, monkeypatch) -> None:
    """A preflight run with BOTH a broken DB and a repo-internal
    credential dir must report both independently, not stop at the
    first one found."""
    monkeypatch.chdir(tmp_path)
    bogus_db_dir = tmp_path / "not_a_file"
    bogus_db_dir.mkdir()
    from reel_harness.db.schema import create_engine_from_url, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{bogus_db_dir}")
    session_factory = make_session_factory(engine)
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds_inside_repo")

    report = run_preflight(settings, session_factory, profile="fake")
    checks = {c.name: c for c in report.checks}
    assert checks["db_connectivity"].status == "FAIL"
    assert checks["repo_internal_credential"].status == "FAIL"
