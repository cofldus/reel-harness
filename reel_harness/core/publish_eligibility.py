from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Asset
from reel_harness.manifest.schema import (
    MANIFEST_SCHEMA_VERSION,
    NON_PUBLISHABLE_LICENSES,
    Manifest,
)
from reel_harness.media import ffprobe_validate

# Bounds mirrored from ffprobe_validate.DEFAULT_VALIDATION_POLICY -- publish
# eligibility re-checks against the SAME production-safe policy the render
# pipeline's own VALIDATE stage enforces, not a looser one.
_POLICY = ffprobe_validate.DEFAULT_VALIDATION_POLICY


@dataclass
class EligibilityResult:
    """Structured re-verification result. `eligible` is only ever True when
    `reasons` is empty -- ambiguous/missing data always fails closed. Every
    check reads from disk/DB fresh; nothing here trusts an in-memory
    assumption about a prior check (see core.publish_service)."""

    eligible: bool
    reasons: list[str] = field(default_factory=list)
    manifest: Manifest | None = None
    final_video_checksum: str | None = None

    def to_dict(self) -> dict:
        return {"eligible": self.eligible, "reasons": list(self.reasons)}


def check_publish_eligibility(session, job, storage) -> EligibilityResult:
    """Re-verifies, from the actual job row, actual manifest.json on disk,
    actual final.mp4 bytes, and actual current Asset DB rows, whether this
    job may be published -- never trusts a manifest field or an earlier
    check's cached conclusion. Called at publication creation time (see
    core.publish_service.PublicationService.create_publication) so a race
    between an operator's approval getting revoked/replaced and an upload
    starting cannot slip through.
    """
    reasons: list[str] = []

    if job.status != JobStatus.COMPLETED.value:
        reasons.append("JOB_NOT_COMPLETED")

    if not storage.exists(job.id, "manifest.json"):
        reasons.append("MANIFEST_MISSING")
        return EligibilityResult(False, reasons)

    raw = storage.read_bytes(job.id, "manifest.json")
    try:
        manifest = Manifest.model_validate_json(raw)
    except ValidationError:
        reasons.append("MANIFEST_UNPARSEABLE")
        return EligibilityResult(False, reasons)

    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        reasons.append("MANIFEST_SCHEMA_UNSUPPORTED")

    if manifest.approval.decision != "approve":
        reasons.append("APPROVAL_MISSING")
    if manifest.approval.decided_at is None:
        reasons.append("APPROVAL_TIMESTAMP_MISSING")

    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_video_checksum: str | None = None
    if not final_path.is_file() or final_path.stat().st_size == 0:
        reasons.append("FINAL_VIDEO_MISSING")
    else:
        final_video_checksum = hashlib.sha256(final_path.read_bytes()).hexdigest()
        if manifest.final_video_checksum_sha256 is None:
            reasons.append("FINAL_VIDEO_CHECKSUM_MISSING")
        elif final_video_checksum != manifest.final_video_checksum_sha256:
            # The only way this happens post-approval is a bug or tampering:
            # COMPLETED jobs have no path back to RENDERING (see
            # core.state_machine.ALLOWED_TRANSITIONS), so this is the
            # concrete enforcement of "no render/manifest change since
            # approval", not just a sanity check.
            reasons.append("FINAL_VIDEO_CHECKSUM_MISMATCH")

        _append_technical_validation_reasons(manifest, final_path, reasons)

    if not manifest.assets:
        reasons.append("NO_ASSETS")
    for asset_info in manifest.assets:
        if asset_info.license_type is None or asset_info.license_type in NON_PUBLISHABLE_LICENSES:
            _append_once(reasons, "ASSET_LICENSE_NOT_PUBLISHABLE")
        if not asset_info.commercial_use_allowed:
            _append_once(reasons, "ASSET_COMMERCIAL_USE_NOT_ALLOWED")
        if not asset_info.modification_allowed:
            _append_once(reasons, "ASSET_MODIFICATION_NOT_ALLOWED")
        if not asset_info.attribution_text:
            _append_once(reasons, "ASSET_ATTRIBUTION_MISSING")

    _append_current_asset_disk_reasons(session, job.id, reasons)

    return EligibilityResult(
        eligible=len(reasons) == 0, reasons=reasons,
        manifest=manifest, final_video_checksum=final_video_checksum,
    )


def _append_technical_validation_reasons(manifest: Manifest, final_path: Path, reasons: list[str]) -> None:
    validation = manifest.validation
    if validation.video_codec != _POLICY.video_codec:
        reasons.append("VIDEO_CODEC_NOT_SUPPORTED")
    if validation.audio_codec != _POLICY.audio_codec:
        reasons.append("AUDIO_CODEC_NOT_SUPPORTED")
    if not validation.has_audio_stream:
        reasons.append("NO_AUDIO_STREAM")
    if validation.duration_sec is None or not (
        _POLICY.min_duration_sec <= validation.duration_sec <= _POLICY.max_duration_sec
    ):
        reasons.append("DURATION_OUT_OF_RANGE")
    if _POLICY.require_faststart and not ffprobe_validate.has_faststart(final_path):
        reasons.append("FASTSTART_MISSING")


def _append_current_asset_disk_reasons(session, job_id: str, reasons: list[str]) -> None:
    """Defense-in-depth beyond trusting manifest.json: re-reads the current
    Asset DB rows (is_current=True) and re-hashes the actual files on disk,
    the same discipline worker.runner._restore_assets already applies before
    a resume."""
    from sqlalchemy import select

    rows = session.execute(
        select(Asset).where(Asset.job_id == job_id, Asset.is_current.is_(True)),
    ).scalars().all()
    for row in rows:
        path = Path(row.local_path)
        if not path.is_file():
            _append_once(reasons, "ASSET_FILE_MISSING")
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.checksum_sha256:
            _append_once(reasons, "ASSET_CHECKSUM_MISMATCH")


def _append_once(reasons: list[str], code: str) -> None:
    if code not in reasons:
        reasons.append(code)
