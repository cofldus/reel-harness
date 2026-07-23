from __future__ import annotations

import sys
from pathlib import Path

from reel_harness.media import deps


def test_check_ffmpeg_available_reports_missing(monkeypatch) -> None:
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    status = deps.check_ffmpeg_available(env={}, project_root=Path("/nonexistent-root"))
    assert status.ffmpeg_available is False
    assert status.ffprobe_available is False
    assert status.all_available is False
    assert status.ffmpeg.source == "not_found"


def test_check_ffmpeg_available_reports_present(monkeypatch) -> None:
    monkeypatch.setattr(deps.shutil, "which", lambda name: f"/usr/bin/{name}")
    status = deps.check_ffmpeg_available(env={}, project_root=Path("/nonexistent-root"))
    assert status.ffmpeg_available is True
    assert status.ffprobe_available is True
    assert status.all_available is True
    assert status.ffmpeg.source == "path"


def test_check_ffmpeg_available_reflects_real_environment() -> None:
    # No mocking here: documents what this actual dev machine has installed.
    # As of Phase 0/1 this machine does NOT have ffmpeg/ffprobe on PATH (nor an
    # env var override, nor a .tools/ffmpeg/bin copy) -- see docs/STATUS.md for
    # what that blocks.
    status = deps.check_ffmpeg_available()
    assert isinstance(status.ffmpeg_available, bool)
    assert isinstance(status.ffprobe_available, bool)


def test_env_var_path_is_used_when_it_points_at_a_real_file(tmp_path) -> None:
    fake_ffmpeg = tmp_path / "custom_ffmpeg"
    fake_ffmpeg.write_text("not a real binary, just needs to exist")
    env = {deps.ENV_FFMPEG_PATH: str(fake_ffmpeg)}
    resolved = deps._resolve_binary("ffmpeg", deps.ENV_FFMPEG_PATH, env, tmp_path)
    assert resolved.path == fake_ffmpeg
    assert resolved.source == "env"


def test_project_local_tools_dir_is_used_when_no_env_var(tmp_path) -> None:
    bin_dir = tmp_path / ".tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    local_ffmpeg = bin_dir / name
    local_ffmpeg.write_text("not a real binary, just needs to exist")

    resolved = deps._resolve_binary("ffmpeg", deps.ENV_FFMPEG_PATH, {}, tmp_path)
    assert resolved.path == local_ffmpeg
    assert resolved.source == "project_local"


def test_env_var_takes_priority_over_project_local_tools_dir(tmp_path) -> None:
    bin_dir = tmp_path / ".tools" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    name = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    (bin_dir / name).write_text("local copy -- must NOT win over an explicit env var")

    env_ffmpeg = tmp_path / "env_ffmpeg"
    env_ffmpeg.write_text("explicit override")

    env = {deps.ENV_FFMPEG_PATH: str(env_ffmpeg)}
    resolved = deps._resolve_binary("ffmpeg", deps.ENV_FFMPEG_PATH, env, tmp_path)
    assert resolved.path == env_ffmpeg
    assert resolved.source == "env"


def test_path_is_used_only_as_a_last_resort(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(deps.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    resolved = deps._resolve_binary("ffmpeg", deps.ENV_FFMPEG_PATH, {}, tmp_path)
    assert resolved.path == Path("/usr/bin/ffmpeg")
    assert resolved.source == "path"


def test_not_found_anywhere_reports_not_found_source_and_no_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    resolved = deps._resolve_binary("ffmpeg", deps.ENV_FFMPEG_PATH, {}, tmp_path)
    assert resolved.path is None
    assert resolved.version is None
    assert resolved.source == "not_found"


def test_read_version_parses_version_token_from_stdout(monkeypatch) -> None:
    class _FakeCompletedProcess:
        stdout = "ffmpeg version 6.1.1-essentials_build Copyright (c) 2000-2023 the FFmpeg developers\n"
        stderr = ""

    monkeypatch.setattr(deps.subprocess, "run", lambda *a, **k: _FakeCompletedProcess())
    assert deps._read_version(Path("whatever")) == "6.1.1-essentials_build"


def test_read_version_returns_none_when_binary_cannot_be_run(monkeypatch) -> None:
    def _raise(*args, **kwargs):
        raise OSError("no such file or directory")

    monkeypatch.setattr(deps.subprocess, "run", _raise)
    assert deps._read_version(Path("missing-binary")) is None
