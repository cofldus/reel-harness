from __future__ import annotations

import re
from pathlib import Path

_JOB_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class InvalidJobIdError(Exception):
    pass


class PathTraversalError(Exception):
    pass


class LocalFilesystemStorage:
    """StorageBackend that confines every read/write under `root_dir/{job_id}/`.

    job_id must look like a UUID (server-generated, never user-supplied text) and
    every rel_path is resolved and checked to still live under the job directory,
    so `../../..` style escapes and symlink tricks are rejected before any I/O.
    """

    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir.resolve()
        self.root_dir.mkdir(parents=True, exist_ok=True)

    def job_dir(self, job_id: str) -> Path:
        if not _JOB_ID_RE.match(job_id):
            raise InvalidJobIdError(f"job_id must be a UUID, got: {job_id!r}")
        candidate = (self.root_dir / job_id).resolve()
        if not candidate.is_relative_to(self.root_dir):
            raise PathTraversalError(f"job_id resolves outside storage root: {job_id!r}")
        return candidate

    def path_for(self, job_id: str, rel_path: str) -> Path:
        job_dir = self.job_dir(job_id)
        candidate = (job_dir / rel_path).resolve()
        if not candidate.is_relative_to(job_dir):
            raise PathTraversalError(f"rel_path escapes job directory: {rel_path!r}")
        return candidate

    def write_bytes(self, job_id: str, rel_path: str, data: bytes) -> Path:
        path = self.path_for(job_id, rel_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def read_bytes(self, job_id: str, rel_path: str) -> bytes:
        return self.path_for(job_id, rel_path).read_bytes()

    def exists(self, job_id: str, rel_path: str) -> bool:
        try:
            return self.path_for(job_id, rel_path).exists()
        except PathTraversalError:
            return False
