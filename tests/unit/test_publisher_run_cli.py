"""publisher-run-once / publisher-run CLI wiring (in-process, fake provider,
no network) -- exercises the real AppContext.bundle_for_publication /
channel_niche_for_job implementations, not mocks."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from reel_harness.cli import main as cli_main
from reel_harness.core.publish_service import PublicationService
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
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'pub-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    # Outside tmp_path on purpose: once we chdir into tmp_path below, it IS
    # "the repository" as far as publisher.secret_store.resolve_secret_dir is
    # concerned, and a credential dir under it would be correctly refused.
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def _faststart_mp4_bytes(tmp_path) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / "cli-pub.mp4"
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


def _make_ready_publication_id(tmp_path) -> str:
    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'pub-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="cooking", language="en")
    job, _ = job_service.create_job(channel.id, idempotency_key="cli-pub-1", topic="t")

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

    pub_service = PublicationService(factory, storage)
    pub, _ = pub_service.create_publication(
        job.id, provider="fake", account_reference="default",
        publisher_snapshot={"publisher_provider": "fake", "publisher_account_reference": "default"},
    )
    return pub.id


def test_publisher_run_once_no_publication_ready(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    assert cli_main.main(["publisher-run-once"]) == 0
    assert "no publication ready" in capsys.readouterr().out


def test_publisher_run_once_completes_a_real_publication(monkeypatch, tmp_path, capsys) -> None:
    # Build the fixture data (which shells out to real ffmpeg, resolved via
    # the project-local .tools/ffmpeg/bin tier relative to the CURRENT cwd)
    # before _isolate() chdirs into tmp_path -- ffmpeg resolution is relative
    # to Path.cwd() and _make_ready_publication_id doesn't need the
    # DATABASE_URL/JOBS_DIR env vars _isolate sets (it builds its own engine
    # from an explicit path).
    pub_id = _make_ready_publication_id(tmp_path)
    _isolate(monkeypatch, tmp_path)

    assert cli_main.main(["publisher-run-once"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["publication_id"] == pub_id
    assert payload["status"] == "PUBLISHED"
    assert payload["provider_video_id"] is not None


def test_publisher_run_daemon_idle_exits_cleanly(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publisher-run", "--idle-exit-after", "0.1", "--poll-interval", "0.05"])
    assert exit_code == 0
