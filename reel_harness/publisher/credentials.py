from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from reel_harness.publisher.secret_store import FileSecretStore

_NAMESPACE = "oauth_credentials"


@dataclass
class OAuthCredential:
    """Never persisted to the jobs DB, a manifest, or a log line -- only to
    a CredentialBackend (see docs/PUBLISHING.md)."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str
    provider: str
    account_reference: str
    channel_id: str | None = None
    channel_title: str | None = None


def _key(provider: str, account_reference: str) -> str:
    return f"{provider}__{account_reference}"


class CredentialBackend(Protocol):
    def get_credential(self, provider: str, account_reference: str) -> OAuthCredential | None: ...
    def save_credential(self, credential: OAuthCredential) -> None: ...
    def has_credential(self, provider: str, account_reference: str) -> bool: ...
    def revoke_credential(self, provider: str, account_reference: str) -> None: ...


class FileCredentialBackend:
    """Repository-external, file-based CredentialBackend built on
    FileSecretStore -- see docs/PUBLISHING.md for the security model this
    is a deliberate, documented starting point for (not a substitute for a
    real OS keychain / cloud secret manager)."""

    def __init__(self, store: FileSecretStore) -> None:
        self._store = store

    def get_credential(self, provider: str, account_reference: str) -> OAuthCredential | None:
        data = self._store.get(_NAMESPACE, _key(provider, account_reference))
        if data is None:
            return None
        return OAuthCredential(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            expires_at=datetime.fromisoformat(data["expires_at"]) if data.get("expires_at") else None,
            scope=data.get("scope", ""), provider=provider, account_reference=account_reference,
            channel_id=data.get("channel_id"), channel_title=data.get("channel_title"),
        )

    def save_credential(self, credential: OAuthCredential) -> None:
        self._store.set(_NAMESPACE, _key(credential.provider, credential.account_reference), {
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "scope": credential.scope,
            "channel_id": credential.channel_id,
            "channel_title": credential.channel_title,
        })

    def has_credential(self, provider: str, account_reference: str) -> bool:
        return self._store.exists(_NAMESPACE, _key(provider, account_reference))

    def revoke_credential(self, provider: str, account_reference: str) -> None:
        self._store.delete(_NAMESPACE, _key(provider, account_reference))


class InMemoryCredentialBackend:
    """Test-only CredentialBackend -- never touches disk."""

    def __init__(self) -> None:
        self._data: dict[str, OAuthCredential] = {}

    def get_credential(self, provider: str, account_reference: str) -> OAuthCredential | None:
        return self._data.get(_key(provider, account_reference))

    def save_credential(self, credential: OAuthCredential) -> None:
        self._data[_key(credential.provider, credential.account_reference)] = credential

    def has_credential(self, provider: str, account_reference: str) -> bool:
        return _key(provider, account_reference) in self._data

    def revoke_credential(self, provider: str, account_reference: str) -> None:
        self._data.pop(_key(provider, account_reference), None)
