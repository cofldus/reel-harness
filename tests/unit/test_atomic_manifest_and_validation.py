"""Atomic manifest writes and the hardened validation policy."""
from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime

import pytest

from reel_harness.manifest.schema import LLMInfo, Manifest, TTSInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.ffprobe_validate import (
    DEFAULT_VALIDATION_POLICY,
    ValidationPolicy,
    ValidationResult,
    check_against_policy,
    has_faststart,
)


def _job_id() -> str:
    return str(uuid.uuid4())


def _manifest(job_id: str, topic: str = "t") -> Manifest:
    return Manifest(
        job_id=job_id, created_at=datetime.now(UTC), topic=topic, script_title="s",
        llm=LLMInfo(provider_id="fake", model_id="m", prompt_version="v1"),
        tts=TTSInfo(provider_id="fake", voice_id="fake-voice-1"),
        assets=[],
    )


def test_atomic_write_roundtrip_leaves_no_temp_files(storage) -> None:
    job_id = _job_id()
    path = storage.write_bytes_atomic(job_id, "manifest.json", b'{"ok": true}')
    assert path.read_bytes() == b'{"ok": true}'
    leftovers = [p for p in path.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_failed_replace_preserves_existing_file_and_cleans_temp(storage, monkeypatch) -> None:
    job_id = _job_id()
    storage.write_bytes_atomic(job_id, "manifest.json", b'{"version": 1}')

    def _boom(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _boom)
    with pytest.raises(OSError):
        storage.write_bytes_atomic(job_id, "manifest.json", b'{"version": 2}')
    monkeypatch.undo()

    target = storage.path_for(job_id, "manifest.json")
    assert json.loads(target.read_bytes()) == {"version": 1}, "old manifest must survive a failed write"
    leftovers = [p for p in target.parent.iterdir() if ".tmp-" in p.name]
    assert leftovers == []


def test_serialize_failure_never_touches_the_existing_manifest(storage) -> None:
    job_id = _job_id()
    write_manifest(storage, job_id, _manifest(job_id, topic="original"))

    class _UnserializableManifest(Manifest):
        def model_dump_json(self, *args, **kwargs):  # type: ignore[override]
            raise ValueError("simulated serialization failure")

    broken = _UnserializableManifest(**_manifest(job_id, topic="broken").model_dump())
    with pytest.raises(ValueError):
        write_manifest(storage, job_id, broken)

    saved = json.loads(storage.read_bytes(job_id, "manifest.json"))
    assert saved["topic"] == "original"
    target = storage.path_for(job_id, "manifest.json")
    assert [p for p in target.parent.iterdir() if ".tmp-" in p.name] == []


def test_repeated_writers_always_leave_parseable_json(storage) -> None:
    job_id = _job_id()
    for i in range(20):
        write_manifest(storage, job_id, _manifest(job_id, topic=f"rev-{i}"))
        parsed = json.loads(storage.read_bytes(job_id, "manifest.json"))
        assert parsed["topic"] == f"rev-{i}"


def _result(**overrides) -> ValidationResult:
    base = dict(
        width=360, height=640, duration_sec=8.0,
        video_codec="h264", has_audio_stream=True, audio_codec="aac",
    )
    base.update(overrides)
    return ValidationResult(**base)


def test_policy_passes_a_conforming_render() -> None:
    assert check_against_policy(_result(), 360, 640, DEFAULT_VALIDATION_POLICY) == []


def test_policy_reports_every_violated_condition_together() -> None:
    failures = check_against_policy(
        _result(video_codec="vp9", has_audio_stream=False, duration_sec=0.4),
        360, 640, DEFAULT_VALIDATION_POLICY,
    )
    joined = "; ".join(failures)
    assert "video codec" in joined
    assert "no audio stream" in joined
    assert "duration" in joined
    assert len(failures) == 3


def test_policy_rejects_wrong_audio_codec_and_resolution() -> None:
    failures = check_against_policy(_result(audio_codec="mp3", width=720), 360, 640, DEFAULT_VALIDATION_POLICY)
    assert any("audio codec" in f for f in failures)
    assert any("resolution" in f for f in failures)


def test_policy_duration_bounds_are_configurable() -> None:
    tight = ValidationPolicy(min_duration_sec=10.0, max_duration_sec=20.0)
    assert any("duration" in f for f in check_against_policy(_result(duration_sec=8.0), 360, 640, tight))
    assert check_against_policy(_result(duration_sec=15.0), 360, 640, tight) == []


def test_has_faststart_orders_atoms_correctly(tmp_path) -> None:
    good = tmp_path / "good.mp4"
    good.write_bytes(b"xxxxmoovxxxxmdatxxxx")
    assert has_faststart(good) is True

    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"xxxxmdatxxxxmoovxxxx")
    assert has_faststart(bad) is False

    missing = tmp_path / "missing.mp4"
    missing.write_bytes(b"no atoms at all")
    assert has_faststart(missing) is False


def test_run_validating_rejects_missing_or_empty_file(tmp_path) -> None:
    from reel_harness.core.errors import ValidationFailedError
    from reel_harness.pipeline.stages import run_validating

    with pytest.raises(ValidationFailedError, match="missing or empty"):
        run_validating(None, tmp_path / "does-not-exist.mp4")

    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(ValidationFailedError, match="missing or empty"):
        run_validating(None, empty)
