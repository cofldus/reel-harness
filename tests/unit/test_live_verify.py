from __future__ import annotations

import pytest

from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.ops.live_verify import (
    LiveVerificationLog,
    LiveVerificationRecord,
    LiveVerifyError,
    run_read_only_live_verify,
)


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


def test_read_only_live_verify_reports_not_configured_without_credentials(ctx) -> None:
    records = run_read_only_live_verify(ctx, providers=("youtube", "tiktok", "instagram"))
    assert len(records) == 3
    for record in records:
        assert record.outcome == "NOT_CONFIGURED"
        assert record.verification_type == "read_only"


def test_read_only_live_verify_continues_past_a_missing_provider(ctx) -> None:
    """No credential for youtube must never abort the sweep for tiktok/
    instagram -- each provider is checked independently."""
    records = run_read_only_live_verify(ctx, providers=("youtube", "tiktok", "instagram"))
    providers_checked = {r.provider for r in records}
    assert providers_checked == {"youtube", "tiktok", "instagram"}


def test_read_only_live_verify_scoped_to_requested_providers(ctx) -> None:
    records = run_read_only_live_verify(ctx, providers=("tiktok",))
    assert len(records) == 1
    assert records[0].provider == "tiktok"


def test_live_verification_record_never_contains_forbidden_fields(ctx) -> None:
    records = run_read_only_live_verify(ctx, providers=("youtube",))
    payload = records[0].to_dict()
    assert "access_token" not in payload
    assert "refresh_token" not in payload
    assert "credential" not in payload


def test_live_verification_log_round_trips(tmp_path) -> None:
    log = LiveVerificationLog(tmp_path / "live_verification")
    record = LiveVerificationRecord(
        provider="youtube", account_alias="default", verification_type="read_only",
        started_at="2026-01-01T00:00:00+00:00", completed_at="2026-01-01T00:00:01+00:00",
        outcome="NOT_CONFIGURED", application_version="0.1.0rc1", config_fingerprint_hash="abc123",
    )
    log.append(record)
    stored = log.read_all()
    assert len(stored) == 1
    assert stored[0]["provider"] == "youtube"
    assert stored[0]["outcome"] == "NOT_CONFIGURED"


def test_live_verification_log_is_append_only(tmp_path) -> None:
    log = LiveVerificationLog(tmp_path / "live_verification")
    for i in range(3):
        log.append(LiveVerificationRecord(
            provider="youtube", account_alias="default", verification_type="read_only",
            started_at="2026-01-01T00:00:00+00:00", completed_at=None, outcome="NOT_CONFIGURED",
            application_version="0.1.0rc1", config_fingerprint_hash="abc123", detail=f"run-{i}",
        ))
    stored = log.read_all()
    assert len(stored) == 3
    assert [r["detail"] for r in stored] == ["run-0", "run-1", "run-2"]


def test_live_verification_log_refuses_forbidden_content(tmp_path) -> None:
    log = LiveVerificationLog(tmp_path / "live_verification")
    record = LiveVerificationRecord(
        provider="youtube", account_alias="default", verification_type="read_only",
        started_at="2026-01-01T00:00:00+00:00", completed_at=None, outcome="PASS",
        application_version="0.1.0rc1", config_fingerprint_hash="abc123",
        detail="leaked access_token=abcdef",
    )
    with pytest.raises(LiveVerifyError):
        log.append(record)
    assert log.read_all() == []


def test_live_verification_log_rooted_outside_repository(tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    log_dir = tmp_path / "external" / "live_verification"
    log = LiveVerificationLog(log_dir)
    log.append(LiveVerificationRecord(
        provider="tiktok", account_alias="default", verification_type="read_only",
        started_at="2026-01-01T00:00:00+00:00", completed_at=None, outcome="NOT_CONFIGURED",
        application_version="0.1.0rc1", config_fingerprint_hash="abc123",
    ))
    assert log_dir.is_dir()
    assert not (log_dir).is_relative_to(repo)
