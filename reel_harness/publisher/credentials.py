from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from reel_harness.publisher.secret_store import FileSecretStore

_NAMESPACE = "oauth_credentials"


@dataclass
class OAuthCredential:
    """Never persisted to the jobs DB, a manifest, or a log line -- only to
    a CredentialBackend (see docs/PUBLISHING.md).

    `created_at`/`last_refreshed_at`/`last_refresh_error`/`invalid` exist so
    `publisher-doctor`/`publisher-account-show` can report a credential's
    operational health without ever touching the network (see
    providers.registry._resolve_fresh_youtube_access_token, which is the
    only writer of the refresh-tracking fields). `invalid=True` means the
    last refresh attempt failed in a way that a retry cannot fix on its own
    (e.g. a revoked/expired refresh token) -- re-running `publisher-auth`
    is required, but the record is kept (not deleted) so an operator can
    still see what was connected and when it broke."""

    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    scope: str
    provider: str
    account_reference: str
    # Generic "account identifier"/"display name" slot -- named after
    # YouTube's channel concept (the first provider to use this schema) but
    # reused as-is for every other provider's own account identity (e.g.
    # TikTok's open_id/creator display name) rather than adding
    # provider-specific duplicate fields. See docs/PUBLISHING.md.
    channel_id: str | None = None
    channel_title: str | None = None
    # Some providers' refresh tokens themselves expire (TikTok: 365 days)
    # unlike Google's, which this schema originally modeled and which don't
    # carry a documented expiry -- None means "provider doesn't expire (or
    # doesn't report) refresh token lifetime".
    refresh_expires_at: datetime | None = None
    created_at: datetime | None = None
    last_refreshed_at: datetime | None = None
    last_refresh_error: str | None = None
    invalid: bool = False


def _key(provider: str, account_reference: str) -> str:
    return f"{provider}__{account_reference}"


class CredentialBackend(Protocol):
    def get_credential(self, provider: str, account_reference: str) -> OAuthCredential | None: ...
    def save_credential(self, credential: OAuthCredential) -> None: ...
    def has_credential(self, provider: str, account_reference: str) -> bool: ...
    def revoke_credential(self, provider: str, account_reference: str) -> None: ...
    def list_accounts(self, provider: str) -> list[str]: ...


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
            refresh_expires_at=(
                datetime.fromisoformat(data["refresh_expires_at"]) if data.get("refresh_expires_at") else None
            ),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else None,
            last_refreshed_at=(
                datetime.fromisoformat(data["last_refreshed_at"]) if data.get("last_refreshed_at") else None
            ),
            last_refresh_error=data.get("last_refresh_error"),
            invalid=bool(data.get("invalid", False)),
        )

    def save_credential(self, credential: OAuthCredential) -> None:
        self._store.set(_NAMESPACE, _key(credential.provider, credential.account_reference), {
            "access_token": credential.access_token,
            "refresh_token": credential.refresh_token,
            "expires_at": credential.expires_at.isoformat() if credential.expires_at else None,
            "scope": credential.scope,
            "channel_id": credential.channel_id,
            "channel_title": credential.channel_title,
            "refresh_expires_at": (
                credential.refresh_expires_at.isoformat() if credential.refresh_expires_at else None
            ),
            "created_at": credential.created_at.isoformat() if credential.created_at else None,
            "last_refreshed_at": (
                credential.last_refreshed_at.isoformat() if credential.last_refreshed_at else None
            ),
            "last_refresh_error": credential.last_refresh_error,
            "invalid": credential.invalid,
        })

    def has_credential(self, provider: str, account_reference: str) -> bool:
        return self._store.exists(_NAMESPACE, _key(provider, account_reference))

    def revoke_credential(self, provider: str, account_reference: str) -> None:
        self._store.delete(_NAMESPACE, _key(provider, account_reference))

    def list_accounts(self, provider: str) -> list[str]:
        prefix = f"{provider}__"
        return sorted(
            key[len(prefix):] for key in self._store.list_keys(_NAMESPACE) if key.startswith(prefix)
        )


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

    def list_accounts(self, provider: str) -> list[str]:
        prefix = f"{provider}__"
        return sorted(
            key[len(prefix):] for key in self._data if key.startswith(prefix)
        )
