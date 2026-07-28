"""publisher.credentials: FileCredentialBackend and InMemoryCredentialBackend
round-trip OAuthCredential correctly, never persist to the jobs DB, and
report absence/presence correctly. No network."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reel_harness.publisher.credentials import (
    FileCredentialBackend,
    InMemoryCredentialBackend,
    OAuthCredential,
)
from reel_harness.publisher.secret_store import FileSecretStore


def _credential(**overrides) -> OAuthCredential:
    defaults = dict(
        access_token="fake-access-token-000000000000",
        refresh_token="fake-refresh-token-000000000000",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        scope="https://www.googleapis.com/auth/youtube.upload",
        provider="youtube", account_reference="default",
        channel_id="UC-fake", channel_title="Fake Channel",
    )
    defaults.update(overrides)
    return OAuthCredential(**defaults)


def test_file_backend_round_trips_a_credential(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    backend = FileCredentialBackend(store)

    assert backend.has_credential("youtube", "default") is False
    assert backend.get_credential("youtube", "default") is None

    cred = _credential()
    backend.save_credential(cred)
    assert backend.has_credential("youtube", "default") is True

    loaded = backend.get_credential("youtube", "default")
    assert loaded is not None
    assert loaded.access_token == cred.access_token
    assert loaded.refresh_token == cred.refresh_token
    assert loaded.channel_id == "UC-fake"
    assert loaded.expires_at is not None


def test_file_backend_revoke_removes_the_credential(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    backend = FileCredentialBackend(store)
    backend.save_credential(_credential())
    backend.revoke_credential("youtube", "default")
    assert backend.has_credential("youtube", "default") is False


def test_different_accounts_are_independent(tmp_path) -> None:
    store = FileSecretStore(tmp_path / "secrets", repo_root=tmp_path / "repo")
    backend = FileCredentialBackend(store)
    backend.save_credential(_credential(account_reference="acct-a", access_token="token-a-000000000"))
    backend.save_credential(_credential(account_reference="acct-b", access_token="token-b-000000000"))
    assert backend.get_credential("youtube", "acct-a").access_token == "token-a-000000000"
    assert backend.get_credential("youtube", "acct-b").access_token == "token-b-000000000"


def test_credential_files_never_land_in_the_jobs_db_directory(tmp_path) -> None:
    repo_root = tmp_path / "repo"
    jobs_dir = repo_root / "jobs"
    jobs_dir.mkdir(parents=True)
    store = FileSecretStore(tmp_path / "secrets", repo_root=repo_root)
    backend = FileCredentialBackend(store)
    backend.save_credential(_credential())
    assert list(jobs_dir.iterdir()) == []


def test_in_memory_backend_never_touches_disk(tmp_path) -> None:
    backend = InMemoryCredentialBackend()
    backend.save_credential(_credential())
    assert backend.has_credential("youtube", "default") is True
    assert list(tmp_path.iterdir()) == []
    backend.revoke_credential("youtube", "default")
    assert backend.has_credential("youtube", "default") is False
