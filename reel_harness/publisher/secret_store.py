from __future__ import annotations

import json
import os
import stat
from pathlib import Path


class SecretStoreError(Exception):
    pass


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_reparse_point(path: Path) -> bool:
    """True for a symlink OR a Windows junction/mount-point reparse point.
    Both redirect a path to somewhere else, but `Path.is_symlink()` alone
    does NOT detect a junction -- and unlike a symlink, a junction can be
    created on Windows without Developer Mode or administrator privileges,
    so checking only for symlinks leaves a real bypass on exactly the
    platform this project runs on most. `st_file_attributes`/
    `st_reparse_tag` are Windows-only `os.stat_result` fields (absent on
    POSIX, where symlinks are the only redirection mechanism this needs to
    catch)."""
    if path.is_symlink():
        return True
    try:
        attributes = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def resolve_secret_dir(configured: Path | None, repo_root: Path) -> Path:
    """Resolves the secret directory, refusing any path inside the
    repository -- the whole point of keeping OAuth credentials and upload
    session references out of the repo is that a repo-internal path could
    end up committed or synced somewhere it shouldn't."""
    base = configured or (Path.home() / ".reel-harness" / "credentials")
    resolved = base.expanduser().resolve()
    repo_resolved = repo_root.resolve()
    try:
        resolved.relative_to(repo_resolved)
    except ValueError:
        return resolved
    raise SecretStoreError(
        f"credential directory {resolved} is inside the repository ({repo_resolved}) -- "
        "refusing to store secrets where they could be committed"
    )


class FileSecretStore:
    """A small JSON-per-key secret store rooted OUTSIDE the repository.

    This is NOT a substitute for a real OS keychain or cloud secret
    manager -- it is the documented, deliberate starting point for a
    local-first single-user tool (see docs/PUBLISHING.md for the security
    model and limitations). Used for OAuth credentials and, separately
    namespaced, upload-session references (publisher.session_store) so
    neither ever touches the jobs DB, a manifest, or a log line.

    Directory/file permissions are set to owner-only where the platform
    supports POSIX modes; on Windows (this project's primary dev platform)
    that's advisory only -- NTFS ACLs are not managed here, which is a
    documented limitation, not a silent gap.
    """

    def __init__(self, root_dir: Path | None = None, repo_root: Path | None = None) -> None:
        self._root = resolve_secret_dir(root_dir, repo_root or Path.cwd())
        self._root.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(self._root):
            raise SecretStoreError(
                f"refusing to use {self._root} as the secret root -- it is a symlink or junction/reparse point"
            )
        self._chmod_private(self._root)

    @property
    def root_dir(self) -> Path:
        return self._root

    @staticmethod
    def _chmod_private(path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, stat.S_IRWXU)

    def _path_for(self, namespace: str, key: str) -> Path:
        if not namespace or any(c in namespace for c in ("/", "\\", "..")):
            raise SecretStoreError(f"invalid secret namespace: {namespace!r}")
        if not key or any(c in key for c in ("/", "\\", "..")):
            raise SecretStoreError(f"invalid secret key: {key!r}")
        ns_dir = self._root / namespace
        ns_dir.mkdir(parents=True, exist_ok=True)
        if _is_reparse_point(ns_dir):
            raise SecretStoreError(f"refusing to use {ns_dir} -- it is a symlink or junction/reparse point")
        self._chmod_private(ns_dir)
        path = ns_dir / f"{key}.json"
        if _is_reparse_point(path):
            raise SecretStoreError(f"refusing to follow a symlink or junction/reparse point at {path}")
        return path

    def get(self, namespace: str, key: str) -> dict | None:
        path = self._path_for(namespace, key)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def set(self, namespace: str, key: str, value: dict) -> None:
        path = self._path_for(namespace, key)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(value), encoding="utf-8")
        if os.name != "nt":
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)

    def delete(self, namespace: str, key: str) -> None:
        self._path_for(namespace, key).unlink(missing_ok=True)

    def exists(self, namespace: str, key: str) -> bool:
        return self._path_for(namespace, key).is_file()

    def list_keys(self, namespace: str) -> list[str]:
        """Lists every key currently stored in `namespace` (e.g. every saved
        OAuth account alias) -- never the values. Returns an empty list for
        a namespace that has never been written to."""
        if not namespace or any(c in namespace for c in ("/", "\\", "..")):
            raise SecretStoreError(f"invalid secret namespace: {namespace!r}")
        ns_dir = self._root / namespace
        if not ns_dir.is_dir() or _is_reparse_point(ns_dir):
            return []
        return sorted(
            p.stem for p in ns_dir.glob("*.json")
            if p.is_file() and not _is_reparse_point(p)
        )
