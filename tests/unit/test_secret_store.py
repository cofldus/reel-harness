"""publisher.secret_store.FileSecretStore: repository-external path
enforcement, symlink rejection, and get/set/delete roundtrip. No network."""
from __future__ import annotations

import pytest

from reel_harness.publisher.secret_store import FileSecretStore, SecretStoreError, resolve_secret_dir


def test_resolve_secret_dir_rejects_a_path_inside_the_repo(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    inside = repo_root / ".secrets"
    with pytest.raises(SecretStoreError, match="inside the repository"):
        resolve_secret_dir(inside, repo_root)


def test_resolve_secret_dir_accepts_a_path_outside_the_repo(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "elsewhere" / "creds"
    resolved = resolve_secret_dir(outside, repo_root)
    assert resolved == outside.resolve()


def test_resolve_secret_dir_default_is_outside_any_repo(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    resolved = resolve_secret_dir(None, repo_root)
    with pytest.raises(ValueError):
        resolved.relative_to(repo_root.resolve())


def test_set_get_delete_roundtrip(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    assert store.get("ns", "k1") is None
    assert store.exists("ns", "k1") is False

    store.set("ns", "k1", {"a": 1, "b": "two"})
    assert store.exists("ns", "k1") is True
    assert store.get("ns", "k1") == {"a": 1, "b": "two"}

    store.delete("ns", "k1")
    assert store.get("ns", "k1") is None


def test_namespaces_are_isolated(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    store.set("oauth_credentials", "youtube__default", {"token": "a"})
    store.set("upload_sessions", "youtube__default", {"token": "b"})
    assert store.get("oauth_credentials", "youtube__default") == {"token": "a"}
    assert store.get("upload_sessions", "youtube__default") == {"token": "b"}


def test_path_traversal_in_key_is_rejected(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    with pytest.raises(SecretStoreError):
        store.get("ns", "../../etc/passwd")
    with pytest.raises(SecretStoreError):
        store.set("../escape", "k", {"x": 1})


def test_symlinked_secret_file_is_refused(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    real_target = tmp_path / "outside-target.json"
    real_target.write_text('{"leaked": true}', encoding="utf-8")

    link_path = store.root_dir / "ns" / "linked.json"
    link_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        link_path.symlink_to(real_target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted in this environment")

    with pytest.raises(SecretStoreError, match="symlink"):
        store.get("ns", "linked")


def test_corrupted_json_reads_as_missing_not_a_crash(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    path = store.root_dir / "ns" / "bad.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not valid json", encoding="utf-8")
    assert store.get("ns", "bad") is None
