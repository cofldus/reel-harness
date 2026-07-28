"""publish-job (--dry-run and real) and provider-smoke publisher youtube CLI
wiring. Fake provider / no credentials configured -- no network, matching
tests/unit/test_publisher_run_cli.py's isolation pattern."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from reel_harness.cli import main as cli_main
from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus, apply_transition
from reel_harness.db.models import Asset, Job
from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.storage.local import LocalFilesystemStorage

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'publish-job-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def _faststart_mp4_bytes(tmp_path) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / "publish-job-cli.mp4"
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


def _make_completed_job_id(tmp_path, key: str) -> str:
    """Builds a fully COMPLETED, approved, manifest-backed job -- eligible for
    publish -- using its own engine/storage (not the CLI's), exactly like
    test_publisher_run_cli._make_ready_publication_id does for a Publication.
    Called BEFORE _isolate()/chdir so ffmpeg's project-local resolution tier
    still sees the real project root."""
    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'publish-job-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="cooking", language="en")
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")

    video_bytes = _faststart_mp4_bytes(tmp_path)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        for status in (
            JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING, JobStatus.ASSET_FETCHING,
            JobStatus.TTS_GENERATING, JobStatus.RENDERING, JobStatus.VALIDATING,
        ):
            apply_transition(db_job, status)
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
    return job.id


def test_publish_job_dry_run_reports_eligible_and_never_creates_a_publication(
    monkeypatch, tmp_path, capsys,
) -> None:
    job_id = _make_completed_job_id(tmp_path, "dry-run-1")
    _isolate(monkeypatch, tmp_path)

    exit_code = cli_main.main(["publish-job", job_id, "--provider", "fake", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["dry_run"] is True
    assert payload["eligible"] is True
    assert payload["eligibility_reasons"] == []
    assert payload["metadata_preview"]["title"]
    assert payload["video_file_size_bytes"] > 0
    assert payload["public_upload_allowed"] is True  # requested privacy defaults to private


def test_publish_job_dry_run_job_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publish-job", "does-not-exist", "--provider", "fake", "--dry-run"]) == 1
    assert "not found" in capsys.readouterr().err


def test_publish_job_dry_run_public_without_confirmation_is_not_allowed(monkeypatch, tmp_path, capsys) -> None:
    job_id = _make_completed_job_id(tmp_path, "dry-run-2")
    _isolate(monkeypatch, tmp_path)

    exit_code = cli_main.main(["publish-job", job_id, "--provider", "fake", "--privacy", "public", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload["public_upload_allowed"] is False


def test_publish_job_real_run_creates_a_publication(monkeypatch, tmp_path, capsys) -> None:
    job_id = _make_completed_job_id(tmp_path, "real-run-1")
    _isolate(monkeypatch, tmp_path)

    exit_code = cli_main.main(["publish-job", job_id, "--provider", "fake"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["job_id"] == job_id
    assert payload["provider"] == "fake"
    assert payload["status"] == "READY_TO_UPLOAD"

    # Calling it again with the same job/checksum is idempotent -- same
    # publication id, not a duplicate.
    exit_code_again = cli_main.main(["publish-job", job_id, "--provider", "fake"])
    payload_again = json.loads(capsys.readouterr().out)
    assert exit_code_again == 0
    assert payload_again["publication_id"] == payload["publication_id"]


def test_publish_job_real_run_job_not_found(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publish-job", "does-not-exist", "--provider", "fake"]) == 1
    assert "not found" in capsys.readouterr().err


def test_provider_smoke_publisher_youtube_not_run_without_client_credentials(
    monkeypatch, tmp_path, capsys,
) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["provider-smoke", "publisher", "youtube"])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "NOT RUN" in out
    assert "credentials not configured" in out


def test_provider_smoke_publisher_youtube_not_run_without_saved_credential(
    monkeypatch, tmp_path, capsys,
) -> None:
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("REEL_HARNESS_YOUTUBE_CLIENT_SECRET", "test-client-secret")
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["provider-smoke", "publisher", "youtube"])
    out = capsys.readouterr().out
    assert exit_code == 2
    assert "NOT RUN" in out


def test_provider_smoke_publisher_requires_youtube_positional(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["provider-smoke", "publisher"]) == 2
    assert "usage" in capsys.readouterr().err
