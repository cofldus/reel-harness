from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from reel_harness.observability import redact

_DEPENDENCY_NAMES = ("fastapi", "sqlalchemy", "pydantic", "pydantic-settings", "httpx", "uvicorn")


class IncidentBundleSecretDetectedError(Exception):
    pass


def _dependency_versions() -> dict[str, str | None]:
    versions: dict[str, str | None] = {}
    for name in _DEPENDENCY_NAMES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _recent_failure_codes(session_factory, limit: int = 20) -> dict:
    from reel_harness.db.models import Job, Publication

    with session_factory() as session:
        job_failures = (
            session.query(Job.id, Job.failure_code)
            .filter(Job.failure_code.isnot(None))
            .order_by(Job.updated_at.desc()).limit(limit).all()
        )
        pub_failures = (
            session.query(Publication.id, Publication.failure_code)
            .filter(Publication.failure_code.isnot(None))
            .order_by(Publication.updated_at.desc()).limit(limit).all()
        )
    return {
        "jobs": [{"job_id": job_id, "failure_code": code} for job_id, code in job_failures],
        "publications": [{"publication_id": pub_id, "failure_code": code} for pub_id, code in pub_failures],
    }


def _journal_integrity(journal_dir: Path) -> dict:
    """Per publication with a journal file: how many raw non-blank lines
    exist vs. how many `PublishJournal.read_events` accepted as valid
    (integrity-checksum-verified) -- a gap between the two means at least
    one record was corrupted or tampered with."""
    if not journal_dir.is_dir():
        return {}
    from reel_harness.publisher.journal import PublishJournal

    journal = PublishJournal(journal_dir)
    results: dict[str, dict] = {}
    for path in sorted(journal_dir.glob("*.jsonl")):
        publication_id = path.stem
        raw_line_count = sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())
        valid_event_count = len(journal.read_events(publication_id))
        results[publication_id] = {
            "raw_line_count": raw_line_count, "valid_event_count": valid_event_count,
            "corrupted": raw_line_count != valid_event_count,
        }
    return results


def _status_counts(session_factory, table: str) -> dict[str, int]:
    from sqlalchemy import text

    with session_factory() as session:
        rows = session.execute(text(f"SELECT status, COUNT(*) FROM {table} GROUP BY status"))
        return {row[0]: row[1] for row in rows}


def _self_secret_scan(blob: str) -> None:
    """A final, independent check over the FULLY ASSEMBLED report text --
    every individual field that could plausibly carry a secret is already
    built from redacted/safe sources, but this catches anything that
    slipped through a gap between them. Reuses the same redact() rule set
    (generic patterns + every secret registered via observability.
    register_secret, which AppContext.__init__ already does for every
    real credential this process holds) rather than a second, possibly
    weaker pattern set."""
    redacted = redact(blob)
    if redacted != blob:
        raise IncidentBundleSecretDetectedError(
            "incident bundle content matched a secret-like pattern after assembly -- refusing to write it"
        )


def build_incident_bundle(ctx, dest_path: Path, log_lines: list[str] | None = None) -> dict:
    """Assembles a diagnostics bundle for offline incident analysis:
    version/schema/config fingerprint, a full local preflight report, DB
    status, job/publication status breakdowns, recent failure codes,
    publish-journal integrity, and dependency/platform versions.
    Deliberately never includes: a token, API key, credential file path,
    full script/prompt text, a signed URL, or media bytes (final video/
    asset/TTS audio) -- none of those are even queried here, and the
    assembled report is independently secret-scanned before being written
    (see _self_secret_scan) rather than trusted by construction alone.
    Caller-supplied `log_lines` (e.g. captured stdout from a `serve`
    session) are redacted individually before inclusion. Written as a zip
    archive via a temp file + os.replace() so a crash mid-write never
    leaves a half-written bundle at the final name."""
    from reel_harness._version import __version__
    from reel_harness.db.schema import SCHEMA_VERSION
    from reel_harness.ops.db_tools import db_status
    from reel_harness.ops.preflight import run_preflight

    preflight_report = run_preflight(ctx.settings, ctx.session_factory, profile="fake")
    db_status_result = db_status(ctx.engine, ctx.settings.database_url)
    redacted_logs = [redact(line) or "" for line in (log_lines or [])]

    report = {
        "generated_at": datetime.now(UTC).isoformat(),
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "config_fingerprint": ctx.config_fingerprint(),
        "preflight": preflight_report.to_dict(),
        "db_status": db_status_result.to_dict(),
        "job_status_counts": _status_counts(ctx.session_factory, "jobs"),
        "publication_status_counts": _status_counts(ctx.session_factory, "publications"),
        "recent_failure_codes": _recent_failure_codes(ctx.session_factory),
        "journal_integrity": _journal_integrity(ctx.publish_journal().root_dir),
        "dependency_versions": _dependency_versions(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "logs": redacted_logs,
    }
    report_blob = json.dumps(report, indent=2, sort_keys=True, default=str)
    _self_secret_scan(report_blob)

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = dest_path.parent / f".{dest_path.name}.tmp-{os.getpid()}"
    try:
        with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("incident_report.json", report_blob)
        os.replace(temp_path, dest_path)
    except BaseException:
        Path(temp_path).unlink(missing_ok=True)
        raise

    return {"path": str(dest_path), "generated_at": report["generated_at"]}
