from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_VERSION_RE = re.compile(r"version\s+(\S+)")

ENV_FFMPEG_PATH = "REEL_HARNESS_FFMPEG_PATH"
ENV_FFPROBE_PATH = "REEL_HARNESS_FFPROBE_PATH"


@dataclass
class ResolvedBinary:
    name: str
    path: Path | None
    version: str | None
    source: str  # "env" | "project_local" | "path" | "not_found"


@dataclass
class DependencyStatus:
    ffmpeg: ResolvedBinary
    ffprobe: ResolvedBinary

    @property
    def ffmpeg_available(self) -> bool:
        return self.ffmpeg.path is not None

    @property
    def ffprobe_available(self) -> bool:
        return self.ffprobe.path is not None

    @property
    def all_available(self) -> bool:
        return self.ffmpeg_available and self.ffprobe_available


def _read_version(path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, timeout=10, shell=False, check=False,
        )
    except OSError:
        return None
    match = _VERSION_RE.search(result.stdout or result.stderr or "")
    return match.group(1) if match else None


def _project_local_candidate(name: str, project_root: Path) -> Path | None:
    bin_dir = project_root / ".tools" / "ffmpeg" / "bin"
    candidates = [bin_dir / name, bin_dir / f"{name}.exe"] if sys.platform == "win32" else [bin_dir / name]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _resolve_binary(name: str, env_var: str, env: Mapping[str, str], project_root: Path) -> ResolvedBinary:
    """Resolution order: REEL_HARNESS_*_PATH env var -> project-local
    .tools/ffmpeg/bin/ -> system PATH. Never installs or downloads anything --
    a miss at every tier means BLOCKED_DEPENDENCY, reported as such, not
    silently worked around.
    """
    explicit = env.get(env_var)
    if explicit:
        explicit_path = Path(explicit)
        if explicit_path.is_file():
            return ResolvedBinary(name, explicit_path, _read_version(explicit_path), source="env")

    local_path = _project_local_candidate(name, project_root)
    if local_path is not None:
        return ResolvedBinary(name, local_path, _read_version(local_path), source="project_local")

    which_path = shutil.which(name)
    if which_path is not None:
        resolved = Path(which_path)
        return ResolvedBinary(name, resolved, _read_version(resolved), source="path")

    return ResolvedBinary(name, None, None, source="not_found")


def check_ffmpeg_available(
    env: Mapping[str, str] | None = None, project_root: Path | None = None,
) -> DependencyStatus:
    """Resolves both binaries fresh every call -- never cache the result, so a
    missing dependency is always reported as BLOCKED_DEPENDENCY at the point of
    use instead of relying on a stale "it was there before" assumption."""
    env = env if env is not None else os.environ
    project_root = project_root if project_root is not None else Path.cwd()
    return DependencyStatus(
        ffmpeg=_resolve_binary("ffmpeg", ENV_FFMPEG_PATH, env, project_root),
        ffprobe=_resolve_binary("ffprobe", ENV_FFPROBE_PATH, env, project_root),
    )
