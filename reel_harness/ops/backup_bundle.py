from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from reel_harness.ops.db_tools import sqlite_path_from_url
from reel_harness.ops.storage_tools import is_reparse_point

_MANIFEST_NAME = "bundle_manifest.json"
_CHECKSUMS_NAME = "checksums.json"

# Defends a restore against a maliciously (or corruptly) huge archive: a
# single member this large, or a whole archive this large, is refused
# before any bytes are extracted -- see backup_inspect/backup_restore.
_MAX_MEMBER_SIZE_BYTES = 2 * 1024 * 1024 * 1024
_MAX_TOTAL_SIZE_BYTES = 20 * 1024 * 1024 * 1024


class BackupBundleError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_online_backup(src_db_path: Path, dest_path: Path) -> None:
    src_conn = sqlite3.connect(str(src_db_path))
    try:
        dst_conn = sqlite3.connect(str(dest_path))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
    finally:
        src_conn.close()


def _add_file(tar: tarfile.TarFile, path: Path, arcname: str, checksums: dict[str, str]) -> None:
    tar.add(path, arcname=arcname, recursive=False)
    checksums[arcname] = _sha256_file(path)


@dataclass
class BackupBundleManifest:
    app_version: str
    schema_version: int
    created_at: str
    config_fingerprint: dict

    def to_dict(self) -> dict:
        return {
            "app_version": self.app_version, "schema_version": self.schema_version,
            "created_at": self.created_at, "config_fingerprint": self.config_fingerprint,
        }


def backup_create(
    database_url: str, jobs_root: Path, journal_dir: Path, config_fingerprint: dict, dest_path: Path,
    timestamp: datetime | None = None,
) -> dict:
    """A single portable archive for moving/backing up an operator's whole
    local deployment: the SQLite database (via the same online-backup API
    db-backup uses -- safe to take while the DB is in use), the jobs
    storage tree (final videos, assets, TTS audio, manifests), and the
    durable publish journal. Deliberately EXCLUDES OAuth tokens/API keys/
    .env/the rest of the credential backend/ffmpeg binaries/caches/logs --
    see docs/OPERATIONS.md for the separate credential backup policy.
    Every file is content-checksummed into checksums.json inside the
    archive; symlinks/junctions are skipped entirely rather than
    followed. Written to a temp file in the destination directory and
    os.replace()'d into place, so a crash mid-archive never leaves a
    half-written bundle at the final name."""
    from reel_harness._version import __version__
    from reel_harness.db.schema import SCHEMA_VERSION

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.parent / f".{dest_path.name}.tmp-{uuid.uuid4().hex[:8]}"

    ts = timestamp or datetime.now(UTC)
    manifest = BackupBundleManifest(
        app_version=__version__, schema_version=SCHEMA_VERSION,
        created_at=ts.isoformat(), config_fingerprint=config_fingerprint,
    )
    checksums: dict[str, str] = {}

    try:
        with tempfile.TemporaryDirectory() as scratch:
            scratch_dir = Path(scratch)
            db_snapshot = scratch_dir / "db.sqlite3"
            _sqlite_online_backup(sqlite_path_from_url(database_url), db_snapshot)
            manifest_path = scratch_dir / _MANIFEST_NAME
            manifest_path.write_text(json.dumps(manifest.to_dict(), indent=2), encoding="utf-8")

            with tarfile.open(temp_path, "w:gz") as tar:
                _add_file(tar, db_snapshot, "db.sqlite3", checksums)
                _add_file(tar, manifest_path, _MANIFEST_NAME, checksums)
                for label, root in (("jobs", jobs_root), ("journal", journal_dir)):
                    if not root.is_dir():
                        continue
                    for path in sorted(root.rglob("*")):
                        if not path.is_file() or is_reparse_point(path):
                            continue
                        arcname = f"{label}/{path.relative_to(root).as_posix()}"
                        _add_file(tar, path, arcname, checksums)
                checksums_bytes = json.dumps(checksums, indent=2, sort_keys=True).encode("utf-8")
                checksums_tmp = scratch_dir / _CHECKSUMS_NAME
                checksums_tmp.write_bytes(checksums_bytes)
                tar.add(checksums_tmp, arcname=_CHECKSUMS_NAME, recursive=False)
        os.replace(temp_path, dest_path)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise

    return {
        "path": str(dest_path), "file_count": len(checksums), "bundle_checksum_sha256": _sha256_file(dest_path),
        **manifest.to_dict(),
    }


def _validate_member(member: tarfile.TarInfo) -> None:
    """Applied to every member BEFORE anything is extracted -- refuses
    absolute paths, `..` traversal, symlinks/hardlinks (an archive should
    never need one; backup_create never writes one), and any non-regular-
    file/non-directory member (device nodes, fifos, ...). Independent of
    Python's own 3.12+ extractall(filter=...) so this hardening does not
    depend on which patched-in Python 3.11.x subversion is running it."""
    name = member.name
    if not name or name.startswith(("/", "\\")):
        raise BackupBundleError(f"refusing archive member with an absolute path: {name!r}")
    if ":" in name.split("/")[0] and len(name.split("/")[0]) == 2:
        # A Windows drive-letter-style prefix ("C:...") smuggled into a
        # POSIX-style tar member name.
        raise BackupBundleError(f"refusing archive member with a drive-letter path: {name!r}")
    if ".." in PurePosixPath(name).parts:
        raise BackupBundleError(f"refusing archive member with path traversal: {name!r}")
    if member.issym() or member.islnk():
        raise BackupBundleError(f"refusing archive member that is a symlink/hardlink: {name!r}")
    if not (member.isfile() or member.isdir()):
        raise BackupBundleError(f"refusing archive member of unsupported type: {name!r}")
    if member.size > _MAX_MEMBER_SIZE_BYTES:
        raise BackupBundleError(f"archive member {name!r} exceeds the per-file size cap")


def _validated_members(tar: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = tar.getmembers()
    total = 0
    for member in members:
        _validate_member(member)
        total += member.size
        if total > _MAX_TOTAL_SIZE_BYTES:
            raise BackupBundleError("archive exceeds the total size cap -- possible archive bomb")
    return members


def backup_inspect(bundle_path: Path) -> dict:
    """Read-only: validates every member (never extracts) and reports the
    bundle's manifest, file count, total size, and checksum count -- safe
    to run against an untrusted/unverified bundle file."""
    with tarfile.open(bundle_path, "r:gz") as tar:
        members = _validated_members(tar)
        total_size = sum(m.size for m in members)
        try:
            manifest_member = tar.getmember(_MANIFEST_NAME)
            checksums_member = tar.getmember(_CHECKSUMS_NAME)
        except KeyError as exc:
            raise BackupBundleError(f"bundle is missing a required member: {exc}") from exc
        manifest_file = tar.extractfile(manifest_member)
        checksums_file = tar.extractfile(checksums_member)
        if manifest_file is None or checksums_file is None:
            raise BackupBundleError("bundle manifest/checksums member could not be read")
        manifest = json.loads(manifest_file.read())
        checksums = json.loads(checksums_file.read())
    return {
        "manifest": manifest, "file_count": len(members), "total_size_bytes": total_size,
        "checksums_recorded": len(checksums),
    }


def backup_restore(
    bundle_path: Path, dest_jobs_root: Path, dest_database_url: str, dest_journal_dir: Path,
    confirm_restore: bool,
) -> dict:
    """Destructive (overwrites the destination jobs tree, journal, and
    database), so it requires explicit confirmation. Every member is
    validated (see _validate_member) and extracted to a private scratch
    directory FIRST, its content checksummed against checksums.json, and
    only THEN moved into the real destinations -- a corrupt or malicious
    bundle never partially overwrites live data."""
    if not confirm_restore:
        raise BackupBundleError("backup-restore requires --confirm-restore -- this overwrites the destination")

    with tarfile.open(bundle_path, "r:gz") as tar:
        members = _validated_members(tar)
        with tempfile.TemporaryDirectory() as scratch:
            scratch_dir = Path(scratch)
            tar.extractall(scratch_dir, members=members)  # noqa: S202 - members pre-validated above

            checksums_path = scratch_dir / _CHECKSUMS_NAME
            if not checksums_path.is_file():
                raise BackupBundleError("extracted bundle is missing checksums.json")
            checksums = json.loads(checksums_path.read_text(encoding="utf-8"))
            for arcname, expected in checksums.items():
                candidate = scratch_dir / arcname
                if not candidate.is_file():
                    raise BackupBundleError(f"checksum manifest references missing file: {arcname}")
                actual = _sha256_file(candidate)
                if actual != expected:
                    raise BackupBundleError(f"checksum mismatch for {arcname} -- bundle may be corrupt")

            manifest = json.loads((scratch_dir / _MANIFEST_NAME).read_text(encoding="utf-8"))

            dest_jobs_root.mkdir(parents=True, exist_ok=True)
            src_jobs = scratch_dir / "jobs"
            if src_jobs.is_dir():
                for entry in src_jobs.iterdir():
                    target = dest_jobs_root / entry.name
                    if target.is_dir():
                        shutil.rmtree(target)
                    elif target.exists():
                        target.unlink()
                    shutil.move(str(entry), str(target))

            dest_journal_dir.mkdir(parents=True, exist_ok=True)
            src_journal = scratch_dir / "journal"
            if src_journal.is_dir():
                for entry in src_journal.iterdir():
                    target = dest_journal_dir / entry.name
                    if target.is_file():
                        target.unlink()
                    shutil.move(str(entry), str(target))

            db_path = sqlite_path_from_url(dest_database_url)
            db_path.parent.mkdir(parents=True, exist_ok=True)
            temp_db_path = db_path.parent / f".{db_path.name}.restoring-{uuid.uuid4().hex}"
            shutil.copy2(scratch_dir / "db.sqlite3", temp_db_path)
            os.replace(temp_db_path, db_path)

    return {"restored": True, "restored_from": str(bundle_path), "manifest": manifest}
