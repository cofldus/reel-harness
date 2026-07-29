from __future__ import annotations

import json
import zipfile

import pytest

from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.ops.incident import IncidentBundleSecretDetectedError, build_incident_bundle


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'rh.db'}", jobs_dir=tmp_path / "jobs",
        credential_dir=tmp_path.parent / "creds", app_api_key="a-real-non-placeholder-key-value",
    )
    context = AppContext(settings)
    yield context
    context.engine.dispose()


def _read_report(bundle_path) -> dict:
    with zipfile.ZipFile(bundle_path) as zf:
        return json.loads(zf.read("incident_report.json"))


def test_build_incident_bundle_produces_a_readable_zip(ctx, tmp_path) -> None:
    dest = tmp_path / "incident.zip"
    result = build_incident_bundle(ctx, dest)
    assert dest.exists()
    report = _read_report(dest)
    assert report["app_version"]
    assert report["schema_version"]
    assert "config_fingerprint" in report
    assert "preflight" in report
    assert "db_status" in report
    assert "dependency_versions" in report
    assert report["dependency_versions"]["fastapi"] is not None
    assert result["path"] == str(dest)


def test_incident_bundle_reports_job_and_publication_status_counts(ctx, tmp_path) -> None:
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
    dest = tmp_path / "incident.zip"
    build_incident_bundle(ctx, dest)
    report = _read_report(dest)
    assert report["job_status_counts"].get("QUEUED", 0) >= 1


def test_incident_bundle_reports_recent_failure_codes(ctx, tmp_path) -> None:
    from reel_harness.db.models import Job

    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
    with ctx.session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.status = "FAILED"
        db_job.failure_code = "BLOCKED_DEPENDENCY"
        session.commit()
    dest = tmp_path / "incident.zip"
    build_incident_bundle(ctx, dest)
    report = _read_report(dest)
    codes = [entry["failure_code"] for entry in report["recent_failure_codes"]["jobs"]]
    assert "BLOCKED_DEPENDENCY" in codes


def test_incident_bundle_never_contains_registered_secrets(ctx, tmp_path) -> None:
    from reel_harness.observability import register_secret

    register_secret("super-secret-registered-value-123")
    dest = tmp_path / "incident.zip"
    build_incident_bundle(ctx, dest)
    with zipfile.ZipFile(dest) as zf:
        blob = zf.read("incident_report.json").decode("utf-8")
    assert "super-secret-registered-value-123" not in blob


def test_incident_bundle_redacts_supplied_log_lines(ctx, tmp_path) -> None:
    dest = tmp_path / "incident.zip"
    log_lines = [
        "normal log line",
        'Authorization: Bearer sk-abcdef1234567890abcdef',
        "api_key=my-super-secret-value-here",
    ]
    build_incident_bundle(ctx, dest, log_lines=log_lines)
    report = _read_report(dest)
    joined = " ".join(report["logs"])
    assert "sk-abcdef1234567890abcdef" not in joined
    assert "my-super-secret-value-here" not in joined
    assert "normal log line" in report["logs"]


def test_incident_bundle_self_scan_refuses_to_write_on_detected_secret(ctx, tmp_path, monkeypatch) -> None:
    """If something secret-shaped survives into the assembled report despite
    every individual field being built from safe sources, the bundle must
    never be written -- proven here by forcing redact() to find something
    in an otherwise-clean report via a registered secret that also happens
    to appear in a normal, unredacted field (the db identifier)."""
    from reel_harness.observability import register_secret

    # A registered "secret" that also happens to be a normal db filename --
    # forces _self_secret_scan to trip even though no real leak occurred,
    # deterministically exercising the refuse-to-write path.
    register_secret("rh.dbxxxxxxxx")
    monkeypatch.setattr(
        "reel_harness.ops.db_tools.safe_db_identifier", lambda url: "rh.dbxxxxxxxx",
    )
    dest = tmp_path / "incident.zip"
    with pytest.raises(IncidentBundleSecretDetectedError):
        build_incident_bundle(ctx, dest)
    assert not dest.exists()


def test_incident_bundle_journal_integrity_reports_valid_and_corrupted(ctx, tmp_path) -> None:
    from datetime import UTC, datetime

    journal = ctx.publish_journal()
    journal.append(
        publication_id="pub-1", job_id="job-1", provider="youtube", account_reference="default",
        final_video_checksum="abc", event="upload_completed", timestamp=datetime.now(UTC),
        provider_video_id="vid-1",
    )
    # Corrupt the file by appending a non-JSON line.
    path = journal._path_for("pub-1")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("not valid json\n")

    dest = tmp_path / "incident.zip"
    build_incident_bundle(ctx, dest)
    report = _read_report(dest)
    entry = report["journal_integrity"]["pub-1"]
    assert entry["raw_line_count"] == 2
    assert entry["valid_event_count"] == 1
    assert entry["corrupted"] is True


def test_incident_bundle_is_atomic_on_failure(ctx, tmp_path, monkeypatch) -> None:
    def _boom(*args, **kwargs):
        raise RuntimeError("simulated zip write failure")

    monkeypatch.setattr("zipfile.ZipFile.writestr", _boom)
    dest = tmp_path / "incident.zip"
    with pytest.raises(RuntimeError):
        build_incident_bundle(ctx, dest)
    assert not dest.exists()
    assert list(tmp_path.glob(".incident.zip.tmp-*")) == []
