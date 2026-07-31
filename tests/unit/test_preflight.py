from __future__ import annotations

from reel_harness.config import Settings
from reel_harness.ops.preflight import PreflightReport, run_preflight


def _checks_by_name(report: PreflightReport) -> dict:
    return {c.name: c for c in report.checks}


def test_preflight_fake_profile_passes_on_a_healthy_fresh_db(session_factory, tmp_path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    settings = Settings(jobs_dir=repo / "jobs", credential_dir=tmp_path / "creds")
    report = run_preflight(settings, session_factory, profile="fake")
    checks = _checks_by_name(report)
    assert checks["config_parse"].status == "PASS"
    assert checks["db_connectivity"].status == "PASS"
    assert checks["db_schema"].status == "PASS"
    assert checks["storage_root_writable"].status == "PASS"
    assert checks["repo_internal_credential"].status == "PASS"
    assert checks["runtime_dependencies"].status == "PASS"
    assert checks["provider_registry"].status == "PASS"
    # overall may still be WARN/FAIL from ffmpeg not being resolvable on
    # this machine, or free disk space -- assert only what this test
    # actually controls, not overall.


def test_preflight_rejects_unknown_profile(session_factory) -> None:
    import pytest

    with pytest.raises(ValueError):
        run_preflight(Settings(), session_factory, profile="staging")


def test_preflight_detects_repo_internal_credential_dir(session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    # A repo-internal credential dir under the (monkeypatched) cwd.
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds_inside_repo")
    report = run_preflight(settings, session_factory, profile="fake")
    checks = _checks_by_name(report)
    assert checks["repo_internal_credential"].status == "FAIL"
    assert report.overall == "FAIL"


def test_preflight_production_profile_escalates_placeholder_api_key(
    session_factory, tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds",
        app_api_key="changeme-local-dev-key",
    )
    fake_report = run_preflight(settings, session_factory, profile="fake")
    prod_report = run_preflight(settings, session_factory, profile="production")
    assert _checks_by_name(fake_report)["api_authentication"].status == "WARN"
    assert _checks_by_name(prod_report)["api_authentication"].status == "FAIL"
    assert prod_report.overall == "FAIL"


def test_preflight_production_profile_escalates_placeholder_secret(session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds",
        llm_provider="openai_compatible", llm_base_url="https://x.example.com", llm_model="m",
        llm_api_key="changeme",
    )
    report = run_preflight(settings, session_factory, profile="production")
    assert _checks_by_name(report)["secret_placeholder"].status == "FAIL"


def test_preflight_public_upload_flag_without_any_publisher_warns(session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds", allow_public_upload=True,
    )
    report = run_preflight(settings, session_factory, profile="fake")
    assert _checks_by_name(report)["public_upload_feature_flag"].status == "WARN"


def test_preflight_public_upload_flag_with_publisher_configured_passes(
    session_factory, tmp_path, monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds", allow_public_upload=True,
        youtube_client_id="client", youtube_client_secret="secret-value-long-enough",
    )
    report = run_preflight(settings, session_factory, profile="fake")
    assert _checks_by_name(report)["public_upload_feature_flag"].status == "PASS"


def test_preflight_paid_generation_flag_defaults_to_disabled(session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds")
    report = run_preflight(settings, session_factory, profile="fake")
    check = _checks_by_name(report)["paid_generation_feature_flag"]
    assert check.status == "PASS"
    assert "disabled" in check.detail


def test_preflight_paid_generation_enabled_with_only_free_tiers_warns(
    session_factory, tmp_path, monkeypatch,
) -> None:
    """A switch about money that is on with nothing behind it is the state
    an operator most easily forgets before selecting a real adapter."""
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds",
        allow_paid_generation=True,
    )
    report = run_preflight(settings, session_factory, profile="fake")
    assert _checks_by_name(report)["paid_generation_feature_flag"].status == "WARN"


def test_preflight_bad_schema_fails(tmp_path) -> None:
    from sqlalchemy import text

    from reel_harness.db.schema import create_engine_from_url, init_db, make_session_factory

    engine = create_engine_from_url(f"sqlite:///{tmp_path / 't.db'}")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text("UPDATE schema_migrations SET version = 999"))
    session_factory = make_session_factory(engine)
    report = run_preflight(Settings(jobs_dir=tmp_path / "jobs"), session_factory, profile="fake")
    assert _checks_by_name(report)["db_schema"].status == "FAIL"


def test_preflight_overall_is_worst_of_all_checks(session_factory, tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(jobs_dir=tmp_path / "jobs", credential_dir=tmp_path / "creds_inside_repo")
    report = run_preflight(settings, session_factory, profile="fake")
    assert report.overall == "FAIL"
    assert report.to_dict()["overall"] == "FAIL"
