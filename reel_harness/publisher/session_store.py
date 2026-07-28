from __future__ import annotations

from reel_harness.publisher.secret_store import FileSecretStore

_NAMESPACE = "upload_sessions"


class UploadSessionStore:
    """Stores the real resumable-upload session URI (a bearer-style
    capability URL -- see docs/PUBLISHING.md's resumable upload protocol
    notes) OUTSIDE the jobs DB, keyed by publication_id, in the same
    repository-external secret directory OAuth credentials use. Never
    logged, never in an API response, never written to
    Publication.upload_session_reference (which stores only this store's
    key, an opaque local reference -- never the URI itself).

    If an entry is missing when a worker needs it (different machine, the
    secret store was cleared, or the process crashed before persisting it),
    the only safe recourse is to start a brand-new upload session rather
    than guess at a URI -- see worker.publish_runner. That re-uploads the
    file but never risks silently reusing or leaking a stale capability
    URL.
    """

    def __init__(self, store: FileSecretStore) -> None:
        self._store = store

    def get(self, publication_id: str) -> str | None:
        data = self._store.get(_NAMESPACE, publication_id)
        return data["session_uri"] if data else None

    def set(self, publication_id: str, session_uri: str) -> None:
        self._store.set(_NAMESPACE, publication_id, {"session_uri": session_uri})

    def delete(self, publication_id: str) -> None:
        self._store.delete(_NAMESPACE, publication_id)
