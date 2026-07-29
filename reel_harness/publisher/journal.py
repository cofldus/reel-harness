from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

# Fields a journal record must NEVER contain: an OAuth token, the full
# resumable upload session URI, an Authorization header, or a full provider
# response body. Callers pass only safe identifiers (see
# safe_session_reference_hash below for the session URI's safe stand-in).
_FORBIDDEN_SUBSTRINGS = ("access_token", "refresh_token", "authorization", "bearer ")


class PublishJournalError(Exception):
    pass


def safe_session_reference_hash(session_reference: str) -> str:
    """A stand-in for the real (bearer-style capability) upload session URI
    that is safe to persist: the URI itself is never written to the
    journal, only this one-way hash of it, which is enough to notice "this
    is the same session as before" without ever being usable to make a
    request."""
    return hashlib.sha256(session_reference.encode("utf-8")).hexdigest()[:32]


def _record_checksum(record: dict) -> str:
    payload = json.dumps(record, sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class PublishJournal:
    """Append-only, per-publication durable log of the handful of events
    where a crash between "the provider told us X happened" and "the DB
    reflects X" could cause a duplicate upload or a stuck publication --
    principally upload_completed (the moment a provider_video_id becomes
    known). Every append is fsync'd before returning, so a write that
    returns successfully survives a process crash immediately after.

    Deliberately separate from PublicationAuditEvent (the DB-backed audit
    trail): the DB audit log is only as durable as the DB transaction that
    writes it, which is exactly the gap this journal exists to close. This
    journal is the more paranoid, crash-first record; the DB audit trail is
    the queryable, ordinary-operations one.

    Never stores a token, the real upload session URI (only its hash via
    safe_session_reference_hash), an Authorization header, or a full
    provider response body -- see _FORBIDDEN_SUBSTRINGS, checked on every
    append as a defense-in-depth assertion, not just relied on by
    convention.
    """

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root_dir(self) -> Path:
        """Exposed for ops tooling (backup-create/backup-restore) that needs
        to locate the journal directory on disk -- never used to read a
        credential; the journal itself never contains one (see append()'s
        _FORBIDDEN_SUBSTRINGS check)."""
        return self._root

    def _path_for(self, publication_id: str) -> Path:
        if not publication_id or any(c in publication_id for c in ("/", "\\", "..")):
            raise PublishJournalError(f"invalid publication id: {publication_id!r}")
        return self._root / f"{publication_id}.jsonl"

    def append(
        self, *, publication_id: str, job_id: str, provider: str, account_reference: str,
        final_video_checksum: str, event: str, timestamp: datetime,
        safe_session_hash: str | None = None, provider_video_id: str | None = None,
        provider_request_id: str | None = None, detail: dict | None = None,
    ) -> None:
        record = {
            "publication_id": publication_id,
            "job_id": job_id,
            "provider": provider,
            "account_reference": account_reference,
            "final_video_checksum": final_video_checksum,
            "event": event,
            "safe_session_hash": safe_session_hash,
            "provider_video_id": provider_video_id,
            "provider_request_id": provider_request_id,
            "detail": detail or {},
            "timestamp": timestamp.isoformat(),
        }
        blob = json.dumps(record, sort_keys=True)
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            if forbidden in blob.lower():
                raise PublishJournalError(
                    f"refusing to write a publish-journal record containing {forbidden!r} -- "
                    "this looks like a credential or session URL leak, not a safe identifier"
                )
        record["integrity_checksum"] = _record_checksum(record)

        path = self._path_for(publication_id)
        line = json.dumps(record, sort_keys=True) + "\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def read_events(self, publication_id: str) -> list[dict]:
        """Every well-formed, integrity-checksum-verified record for this
        publication, in append order. A corrupted or tampered line is
        skipped rather than raised -- reconciliation must never crash on a
        damaged journal, only treat it as missing information."""
        path = self._path_for(publication_id)
        if not path.is_file():
            return []
        events: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            checksum = record.pop("integrity_checksum", None)
            if checksum is None or _record_checksum(record) != checksum:
                continue
            record["integrity_checksum"] = checksum
            events.append(record)
        return events
