"""publication-list: filters and safe-field output. FakePublisher only,
no network."""
from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest

from reel_harness.cli import main as cli_main
from reel_harness.core.publish_service import PublicationService
from reel_harness.core.service import JobService
from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Asset, Job, Publication
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"list-{seed}.mp4"
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


def _make_publication(job_service, channel, session_factory, storage, tmp_path, key: str, account="default"):
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic="t")
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    final_path = storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with session_factory() as session:
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

    pub_service = PublicationService(session_factory, storage)
    pub, _ = pub_service.create_publication(
        job.id, provider="fake", account_reference=account,
        publisher_snapshot={"publisher_provider": "fake", "publisher_account_reference": account},
    )
    return pub


def _isolate(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{(tmp_path / 'list-cli.db').as_posix()}")
    monkeypatch.setenv("JOBS_DIR", str(tmp_path / "jobs"))
    monkeypatch.setenv("REEL_HARNESS_CREDENTIAL_DIR", str(tmp_path.parent / f"{tmp_path.name}-secrets"))
    monkeypatch.chdir(tmp_path)


def test_publication_list_empty(monkeypatch, tmp_path, capsys) -> None:
    _isolate(monkeypatch, tmp_path)
    exit_code = cli_main.main(["publication-list"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload == []


def test_publication_list_filters_by_account_and_status(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'list-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")

    pub_a = _make_publication(job_service, channel, factory, storage, tmp_path, "list-a", account="acct-a")
    pub_b = _make_publication(job_service, channel, factory, storage, tmp_path, "list-b", account="acct-b")
    with factory() as session:
        db_pub_b = session.get(Publication, pub_b.id)
        db_pub_b.status = PublicationStatus.FAILED.value
        db_pub_b.failure_code = "X"
        db_pub_b.failure_summary = "x"
        session.commit()

    _isolate(monkeypatch, tmp_path)

    exit_code = cli_main.main(["publication-list"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert {row["publication_id"] for row in payload} == {pub_a.id, pub_b.id}

    exit_code = cli_main.main(["publication-list", "--account", "acct-a"])
    payload = json.loads(capsys.readouterr().out)
    assert [row["publication_id"] for row in payload] == [pub_a.id]

    exit_code = cli_main.main(["publication-list", "--failed-only"])
    payload = json.loads(capsys.readouterr().out)
    assert [row["publication_id"] for row in payload] == [pub_b.id]

    exit_code = cli_main.main(["publication-list", "--status", "FAILED"])
    payload = json.loads(capsys.readouterr().out)
    assert [row["publication_id"] for row in payload] == [pub_b.id]

    exit_code = cli_main.main(["publication-list", "--job-id", pub_a.job_id])
    payload = json.loads(capsys.readouterr().out)
    assert [row["publication_id"] for row in payload] == [pub_a.id]


def test_publication_list_never_exposes_secrets_or_local_paths(monkeypatch, tmp_path, capsys) -> None:
    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory
    from reel_harness.storage.local import LocalFilesystemStorage

    engine = create_engine_from_url(f"sqlite:///{(tmp_path / 'list-cli.db').as_posix()}")
    init_db(engine)
    factory = make_session_factory(engine)
    storage = LocalFilesystemStorage(tmp_path / "jobs")
    job_service = JobService(factory, storage=storage)
    channel = job_service.create_channel(name="c", niche="n", language="en")
    _make_publication(job_service, channel, factory, storage, tmp_path, "list-safe")

    _isolate(monkeypatch, tmp_path)
    cli_main.main(["publication-list"])
    out = capsys.readouterr().out
    payload = json.loads(out)
    for row in payload:
        assert "upload_session_reference" not in row
        assert "publisher_config" not in row
        assert "metadata_snapshot" not in row
    assert "access_token" not in out
    assert "refresh_token" not in out
    assert str(tmp_path) not in out
