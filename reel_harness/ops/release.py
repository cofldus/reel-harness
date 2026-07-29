from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

SUPPORTED_PYTHON_VERSIONS = ("3.11", "3.12")
SUPPORTED_PLATFORMS = ("windows", "linux")
SUPPORTED_PROVIDERS = {
    "llm": ("fake", "openai-compatible"),
    "tts": ("fake", "openai-compatible"),
    "asset": ("fake", "pexels"),
    "publisher": ("youtube", "tiktok", "instagram"),
}

# Kept in one place so docs/CHANGELOG.md and the release manifest can never
# silently drift apart -- both read from here.
KNOWN_LIMITATIONS = (
    "Live provider credentials are not configured/verified on this build machine "
    "(see live_verification status below).",
    "Instagram Reels publishing has no private/unlisted option -- every publish is public.",
    "TikTok forces SELF_ONLY visibility on any app that has not passed its own review process.",
    "SQLite/local filesystem storage only -- no PostgreSQL or cloud object storage.",
    "One pre-existing test skip on Windows (symlink-rejection, requires elevated privileges to create).",
    "No remote video/post delete is implemented for any publisher.",
    "No cloud deployment target is implemented -- single-machine, local-first only.",
    "Facebook Reels publishing is not implemented (Instagram Reels only).",
)


class ReleaseManifestError(Exception):
    pass


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit(repo_root: Path) -> str | None:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user input
            ["git", "rev-parse", "HEAD"], cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _optional_checksum(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    return _sha256_file(path)


def build_release_manifest(
    repo_root: Path, wheel_path: Path | None = None, sdist_path: Path | None = None,
    lock_path: Path | None = None, test_summary: dict | None = None,
    live_verification_status: str = "not_run", timestamp: datetime | None = None,
) -> dict:
    """Everything an operator or CI job needs to know about exactly what a
    release artifact is, without re-deriving it -- version/commit/build
    time, schema version, supported Python versions/platforms/providers,
    dependency-lock and wheel/sdist checksums (None, not a guess, when a
    given artifact wasn't built), the test summary the caller supplies,
    a fixed known-limitations list shared with CHANGELOG.md, and the live-
    verification status -- explicitly "not_run" (never silently omitted)
    when nothing was actually run against a real provider."""
    from reel_harness._version import __version__
    from reel_harness.db.schema import SCHEMA_VERSION

    ts = timestamp or datetime.now(UTC)
    return {
        "version": __version__,
        "git_commit": _git_commit(repo_root),
        "build_timestamp": ts.isoformat(),
        "python_versions_supported": list(SUPPORTED_PYTHON_VERSIONS),
        "platforms_supported": list(SUPPORTED_PLATFORMS),
        "schema_version": SCHEMA_VERSION,
        "dependency_lock_checksum_sha256": _optional_checksum(lock_path),
        "wheel_checksum_sha256": _optional_checksum(wheel_path),
        "sdist_checksum_sha256": _optional_checksum(sdist_path),
        "migrations": "additive-only (see db.schema._ADDITIVE_COLUMNS); no Alembic yet",
        "test_summary": test_summary,
        "known_limitations": list(KNOWN_LIMITATIONS),
        "supported_providers": {k: list(v) for k, v in SUPPORTED_PROVIDERS.items()},
        "live_verification": live_verification_status,
    }


def write_release_manifest(manifest: dict, dest_path: Path) -> Path:
    import os

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.parent / f".{dest_path.name}.tmp"
    try:
        temp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temp_path, dest_path)
    except BaseException:
        temp_path.unlink(missing_ok=True)
        raise
    return dest_path
