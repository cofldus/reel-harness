from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

# Names of scratch files a worker may leave behind mid-write if it crashes or
# loses its lease -- see storage.local.LocalFilesystemStorage.write_bytes_atomic
# and worker.runner's RENDER-stage temp-file convention. Only files matching
# one of these patterns are ever candidates for --repair-safe deletion.
_TEMP_FILE_PATTERNS = ("*.tmp-*", "final-inprogress-*")

# --repair-safe only removes a stale temp file this old -- anything newer
# might be a live worker's in-progress write, never touched.
_STALE_TEMP_AGE_SECONDS = 3600.0

# Statuses a job passes through before ANY stage has written anything to
# disk -- SCRIPT/POLICY only ever touch the DB (Job.script is a JSON
# column, never a file), and the first stage to actually write a file is
# ASSET_FETCHING. A job sitting in one of these statuses (freshly created,
# still queued, or mid-script/policy) legitimately has no job directory
# yet -- that is normal operational state, not a defect, and must never
# be reported as "missing_directory" (found via a real Phase 4B soak test:
# a handful of jobs still queued behind a busy render worker made
# storage-verify FAIL on an otherwise perfectly healthy system).
_PRE_STORAGE_JOB_STATUSES = frozenset({
    "CREATED", "QUEUED", "TOPIC_GENERATING", "SCRIPT_GENERATING", "POLICY_CHECKING",
})


def is_reparse_point(path: Path) -> bool:
    """Same check as publisher.secret_store's -- duplicated rather than
    imported because that module's version is deliberately private to the
    credential-store's own concerns; this one applies the identical logic
    to job storage. A symlink OR a Windows junction/reparse point (which
    Path.is_symlink() alone does not detect)."""
    import os

    if path.is_symlink():
        return True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & 0x400)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass
class StorageIssue:
    kind: str
    job_id: str | None
    detail: str

    def to_dict(self) -> dict:
        return {"kind": self.kind, "job_id": self.job_id, "detail": self.detail}


@dataclass
class StorageVerifyResult:
    jobs_checked: int
    issues: list[StorageIssue] = field(default_factory=list)
    repaired: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "jobs_checked": self.jobs_checked,
            "issues": [i.to_dict() for i in self.issues], "repaired": self.repaired,
        }


def _check_asset_checksums(storage, session, job_id: str, issues: list[StorageIssue]) -> None:
    from reel_harness.db.models import Asset

    assets = session.query(Asset).filter(Asset.job_id == job_id, Asset.is_current.is_(True)).all()
    for asset in assets:
        path = Path(asset.local_path)
        if not path.is_file():
            issues.append(StorageIssue("missing_file", job_id, f"asset {asset.id}: {path} does not exist"))
            continue
        if is_reparse_point(path):
            issues.append(StorageIssue(
                "unsafe_reparse_point", job_id, f"asset {asset.id}: {path} is a symlink/junction",
            ))
            continue
        actual = _sha256_file(path)
        if actual != asset.checksum_sha256:
            issues.append(StorageIssue(
                "checksum_mismatch", job_id,
                f"asset {asset.id}: on-disk checksum {actual[:12]}... != recorded {asset.checksum_sha256[:12]}...",
            ))


def _check_final_video(storage, job, manifest_bytes: bytes | None, issues: list[StorageIssue]) -> None:
    from reel_harness.core.state_machine import JobStatus

    expects_final = job.status in (
        JobStatus.REVIEW_REQUIRED.value, JobStatus.READY.value, JobStatus.COMPLETED.value,
    )
    if not expects_final:
        return
    try:
        final_path = storage.path_for(job.id, "final/final.mp4")
    except Exception:  # noqa: BLE001
        return
    if not final_path.is_file():
        issues.append(StorageIssue("missing_file", job.id, "final/final.mp4 is missing but job status expects it"))
        return
    if is_reparse_point(final_path):
        issues.append(StorageIssue("unsafe_reparse_point", job.id, f"{final_path} is a symlink/junction"))
        return
    if manifest_bytes is None:
        return
    try:
        from reel_harness.manifest.schema import Manifest

        manifest = Manifest.model_validate_json(manifest_bytes)
    except Exception:  # noqa: BLE001 - reported separately by _check_manifest
        return
    if manifest.final_video_checksum_sha256:
        actual = _sha256_file(final_path)
        if actual != manifest.final_video_checksum_sha256:
            expected = manifest.final_video_checksum_sha256
            issues.append(StorageIssue(
                "checksum_mismatch", job.id,
                f"final.mp4 on-disk checksum {actual[:12]}... != manifest {expected[:12]}...",
            ))


def _check_manifest(storage, job_id: str, issues: list[StorageIssue]) -> bytes | None:
    if not storage.exists(job_id, "manifest.json"):
        return None
    raw = storage.read_bytes(job_id, "manifest.json")
    try:
        from reel_harness.manifest.schema import Manifest

        Manifest.model_validate_json(raw)
    except Exception as exc:  # noqa: BLE001
        issues.append(StorageIssue("corrupt_manifest", job_id, f"manifest.json failed to validate: {exc}"))
        return None
    return raw


def _find_stale_temp_files(job_dir: Path, now: float) -> list[Path]:
    stale = []
    for pattern in _TEMP_FILE_PATTERNS:
        for path in job_dir.rglob(pattern):
            if not path.is_file():
                continue
            try:
                age = now - path.stat().st_mtime
            except OSError:
                continue
            if age >= _STALE_TEMP_AGE_SECONDS:
                stale.append(path)
    return stale


def storage_verify(storage, session_factory, repair_safe: bool = False) -> StorageVerifyResult:
    """Read-only by default: walks every job directory under storage.root_dir
    and cross-checks it against the DB (asset/final-video checksums, manifest
    validity, unsafe symlinks/junctions, orphan directories with no matching
    Job row, stale leaked temp files). `repair_safe=True` additionally
    deletes ONLY temp files matching storage.local's own known scratch-file
    naming convention that are older than an hour (never a live worker's
    in-progress write) -- it never touches final.mp4, rewrites a manifest,
    changes a Publication's status, or retries a remote upload; anything
    else stays reported, not auto-fixed."""
    from sqlalchemy import select

    from reel_harness.db.models import Job

    issues: list[StorageIssue] = []
    repaired: list[str] = []
    now = time.time()

    with session_factory() as session:
        db_job_ids = set(session.execute(select(Job.id)).scalars())
        jobs = session.query(Job).all()
        jobs_by_id = {job.id: job for job in jobs}

        for job in jobs:
            job_dir = storage.job_dir(job.id)
            if not job_dir.is_dir():
                if job.status not in _PRE_STORAGE_JOB_STATUSES:
                    issues.append(StorageIssue("missing_directory", job.id, f"{job_dir} does not exist"))
                continue
            _check_asset_checksums(storage, session, job.id, issues)
            manifest_bytes = _check_manifest(storage, job.id, issues)
            _check_final_video(storage, job, manifest_bytes, issues)

    root = storage.root_dir
    if root.is_dir():
        for entry in root.iterdir():
            if not entry.is_dir():
                continue
            if entry.name not in db_job_ids:
                issues.append(StorageIssue(
                    "orphan_directory", None, f"{entry} has no matching job in the database",
                ))
                continue
            stale = _find_stale_temp_files(entry, now)
            for temp_path in stale:
                issues.append(StorageIssue(
                    "stale_temp_file", entry.name, f"{temp_path} (age >= {_STALE_TEMP_AGE_SECONDS:.0f}s)",
                ))
                if repair_safe:
                    try:
                        temp_path.unlink()
                        repaired.append(str(temp_path))
                    except OSError:
                        pass

    return StorageVerifyResult(jobs_checked=len(jobs_by_id), issues=issues, repaired=repaired)
