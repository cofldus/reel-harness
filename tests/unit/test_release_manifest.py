from __future__ import annotations

import json

from reel_harness.ops.release import (
    KNOWN_LIMITATIONS,
    build_release_manifest,
    write_release_manifest,
)


def test_build_release_manifest_reports_version_and_schema(tmp_path) -> None:
    from reel_harness._version import __version__
    from reel_harness.db.schema import SCHEMA_VERSION

    manifest = build_release_manifest(repo_root=tmp_path)
    assert manifest["version"] == __version__
    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["live_verification"] == "not_run"


def test_build_release_manifest_default_live_verification_is_not_run(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    assert manifest["live_verification"] == "not_run"


def test_build_release_manifest_accepts_explicit_live_verification_status(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path, live_verification_status="youtube=PASS,tiktok=PASS")
    assert manifest["live_verification"] == "youtube=PASS,tiktok=PASS"


def test_build_release_manifest_checksums_none_when_artifacts_missing(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    assert manifest["wheel_checksum_sha256"] is None
    assert manifest["sdist_checksum_sha256"] is None
    assert manifest["dependency_lock_checksum_sha256"] is None


def test_build_release_manifest_checksums_real_artifacts(tmp_path) -> None:
    wheel = tmp_path / "reel_harness-0.1.0rc1-py3-none-any.whl"
    wheel.write_bytes(b"fake wheel bytes")
    manifest = build_release_manifest(repo_root=tmp_path, wheel_path=wheel)
    import hashlib

    assert manifest["wheel_checksum_sha256"] == hashlib.sha256(b"fake wheel bytes").hexdigest()


def test_build_release_manifest_embeds_test_summary_verbatim(tmp_path) -> None:
    summary = {"passed": 892, "failed": 0, "skipped": 1}
    manifest = build_release_manifest(repo_root=tmp_path, test_summary=summary)
    assert manifest["test_summary"] == summary


def test_build_release_manifest_includes_known_limitations(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    assert manifest["known_limitations"] == list(KNOWN_LIMITATIONS)
    assert any("Instagram" in item for item in manifest["known_limitations"])


def test_build_release_manifest_includes_supported_providers(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    assert set(manifest["supported_providers"]["publisher"]) == {"youtube", "tiktok", "instagram"}
    assert "fake" in manifest["supported_providers"]["llm"]


def test_build_release_manifest_git_commit_matches_real_repo(tmp_path) -> None:
    """Run against the ACTUAL project repo root (not tmp_path) to prove the
    git-commit lookup does something real, not just handle the no-repo
    case."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    manifest = build_release_manifest(repo_root=repo_root)
    assert manifest["git_commit"] is not None
    assert len(manifest["git_commit"]) == 40  # full SHA


def test_build_release_manifest_git_commit_none_outside_a_repo(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    assert manifest["git_commit"] is None


def test_write_release_manifest_is_atomic_and_readable(tmp_path) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    dest = tmp_path / "out" / "release_manifest.json"
    written = write_release_manifest(manifest, dest)
    assert written == dest
    assert dest.exists()
    on_disk = json.loads(dest.read_text(encoding="utf-8"))
    assert on_disk["version"] == manifest["version"]
    assert list(tmp_path.glob("out/.*.tmp")) == []


def test_write_release_manifest_atomic_on_failure(tmp_path, monkeypatch) -> None:
    manifest = build_release_manifest(repo_root=tmp_path)
    dest = tmp_path / "release_manifest.json"

    original_replace = __import__("os").replace

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr("os.replace", _boom)
    import pytest

    with pytest.raises(RuntimeError):
        write_release_manifest(manifest, dest)
    assert not dest.exists()
    assert list(tmp_path.glob(".release_manifest.json.tmp")) == []
    monkeypatch.setattr("os.replace", original_replace)
