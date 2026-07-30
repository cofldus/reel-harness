from __future__ import annotations

from datetime import UTC, datetime, timedelta

from reel_harness.publisher.oauth_common import generate_state
from reel_harness.publisher.secret_store import FileSecretStore

_NAMESPACE = "oauth_pending_flow"

# How long a browser-initiated OAuth connect flow has to complete before its
# pending state is treated as expired. Generous enough for a real user to
# authorize on a slow connection, short enough that an abandoned flow's PKCE
# verifier doesn't sit around indefinitely.
DEFAULT_TTL_SECONDS = 600


class OAuthFlowStore:
    """Transient, single-use state for a browser-driven OAuth connect flow
    (the PKCE verifier + which provider/account_reference it's for),
    bridging the POST /connect request (which generates it) and the GET
    /callback request (which consumes it) -- a browser redirect round-trip
    a CLI process's in-memory PKCE state can't survive.

    Deliberately NOT a DB table: this is discard-and-retry state, same
    posture as publisher.session_store.UploadSessionStore's own "if
    missing, just start over" philosophy, and it's meaningless after the
    flow completes or the process restarts. Stored in the same
    repository-external FileSecretStore as OAuth credentials and upload
    sessions, in its own namespace, so it inherits the same symlink/
    junction rejection for free -- but never in the same namespace as
    actual credentials, since this data has a different (much shorter)
    lifetime and security posture.

    Keyed by `state` ONLY (never by (provider, account_reference)) --
    concurrent or abandoned-then-retried flows for the same account must
    coexist independently rather than overwrite each other, which is
    exactly the state-fixation footgun a "latest wins" keying scheme would
    reintroduce."""

    def __init__(self, store: FileSecretStore, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
        self._store = store
        self._ttl_seconds = ttl_seconds

    def create(self, provider: str, account_reference: str, verifier: str) -> str:
        state = generate_state()
        self._store.set(_NAMESPACE, state, {
            "provider": provider,
            "account_reference": account_reference,
            "verifier": verifier,
            "created_at": datetime.now(UTC).isoformat(),
        })
        return state

    def pop(self, state: str) -> dict | None:
        """Single-use: the entry is gone after this call regardless of
        outcome. Returns None for a missing, malformed, or expired entry --
        the caller (the /callback route) treats all three identically, a
        friendly "connection expired, try again" redirect, never a stack
        trace."""
        data = self._store.pop(_NAMESPACE, state)
        if data is None:
            return None
        try:
            created_at = datetime.fromisoformat(data["created_at"])
        except (KeyError, TypeError, ValueError):
            return None
        if datetime.now(UTC) - created_at > timedelta(seconds=self._ttl_seconds):
            return None
        return data
