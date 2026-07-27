"""Shared-redaction tests: the same rule set must protect log output AND every
persisted error field (job.failure_summary, StageRun.error_detail) and anything
the API echoes back. All secrets below are obviously-fake placeholders.
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy import select

from reel_harness.core.state_machine import JobStatus
from reel_harness.db.models import Job, StageRun
from reel_harness.observability import redact, register_secret
from reel_harness.providers.base import TTSResult
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.worker.runner import ProviderBundle, run_job

FAKE_KEY = "FAKE-PLACEHOLDER-KEY-0123456789abcdef"


def test_openai_style_key_is_redacted() -> None:
    out = redact("upstream rejected sk-fakefakefakefake1234567890 for this request")
    assert "sk-fakefakefakefake1234567890" not in out
    assert "***REDACTED***" in out


def test_url_query_token_is_redacted_but_param_name_kept() -> None:
    out = redact("GET https://api.example.invalid/v1/tts?api_key=FAKEVALUE123456&scene=2 failed")
    assert "FAKEVALUE123456" not in out
    assert "?api_key=" in out  # message stays diagnosable
    assert "scene=2" in out


def test_json_error_body_fields_are_redacted() -> None:
    out = redact('provider said: {"api_key": "FAKEJSONKEY123", "access_token": "FAKETOKEN456789", "detail": "quota"}')
    assert "FAKEJSONKEY123" not in out
    assert "FAKETOKEN456789" not in out
    assert "quota" in out


def test_basic_auth_payload_is_redacted() -> None:
    out = redact("Authorization: Basic RkFLRVVTRVI6RkFLRVBBU1M=")
    assert "RkFLRVVTRVI6RkFLRVBBU1M=" not in out


def test_case_variants_are_redacted() -> None:
    out = redact("X-API-KEY: FAKEHEADERKEY99 and APIKEY=FAKEUPPER123456")
    assert "FAKEHEADERKEY99" not in out
    assert "FAKEUPPER123456" not in out


def test_overlapping_registered_secrets_leave_no_suffix() -> None:
    register_secret("FAKE-OVERLAP-SECRET-LONGER-abcdef")
    register_secret("FAKE-OVERLAP-SECRET-LONGER")
    out = redact("value=FAKE-OVERLAP-SECRET-LONGER-abcdef tail")
    assert "FAKE-OVERLAP-SECRET-LONGER" not in out
    assert "abcdef tail" not in out or "-abcdef" not in out


def test_short_registered_values_are_ignored() -> None:
    register_secret("key")  # must NOT start mauling the word "key" everywhere
    assert redact("the key insight is retry") == "the key insight is retry"


def test_normal_error_messages_pass_through_unchanged() -> None:
    msg = "asset file for scene 1 is corrupted (checksum mismatch)"
    assert redact(msg) == msg
    assert redact(None) is None


class _LeakyTTSProvider:
    """Fails the way a real HTTP provider might: with connection details,
    query-string credentials, and an Authorization header in the message."""

    provider_id = "fake"

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult:
        raise RuntimeError(
            f"POST https://tts.example.invalid/v1/speak?api_key={FAKE_KEY} returned 401; "
            f"Authorization: Bearer {FAKE_KEY}"
        )


def test_persisted_failure_fields_never_contain_the_secret(
    job_service, channel, session_factory, storage,
) -> None:
    providers = ProviderBundle(
        llm=FakeLLMProvider(), tts=_LeakyTTSProvider(), stock_media=FakeStockMediaProvider(),
    )
    job, _ = job_service.create_job(channel.id, idempotency_key="leaky-1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        run_job(session, db_job, channel, providers, storage)
        assert db_job.status == JobStatus.FAILED.value
        assert FAKE_KEY not in (db_job.failure_summary or "")
        assert "***REDACTED***" in db_job.failure_summary

        tts_run = session.execute(
            select(StageRun).where(StageRun.job_id == job.id, StageRun.stage == "TTS"),
        ).scalar_one()
        assert FAKE_KEY not in (tts_run.error_detail or "")
        assert "***REDACTED***" in tts_run.error_detail


def test_api_job_response_echoes_only_redacted_failure_summary(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from reel_harness.api.app import app, get_context
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    ctx = AppContext(
        settings=Settings(
            database_url=f"sqlite:///{tmp_path / 'api.db'}",
            jobs_dir=tmp_path / "jobs",
            app_api_key="fake-test-api-key",
        ),
    )
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
        job, _ = ctx.jobs.create_job(channel.id, idempotency_key="k1", topic="t")
        providers = ProviderBundle(
            llm=FakeLLMProvider(), tts=_LeakyTTSProvider(), stock_media=FakeStockMediaProvider(),
        )
        with ctx.session_factory() as session:
            db_job = session.get(Job, job.id)
            run_job(session, db_job, channel, providers, ctx.storage)

        response = TestClient(app).get(
            f"/v1/jobs/{job.id}", headers={"Authorization": "Bearer fake-test-api-key"},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == JobStatus.FAILED.value
        assert FAKE_KEY not in (body["failure_summary"] or "")
        assert "***REDACTED***" in body["failure_summary"]
    finally:
        app.dependency_overrides.clear()


def test_user_reject_reason_is_redacted_before_persisting(
    job_service, channel, session_factory,
) -> None:
    from reel_harness.core.state_machine import ReasonCode, apply_transition

    job, _ = job_service.create_job(channel.id, idempotency_key="reject-redact", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.CONTENT_POLICY_REVIEW.value)
        session.commit()

    rejected = job_service.reject(
        job.id, reason=f"pasted by accident: Bearer {FAKE_KEY}", regenerate_from_stage="SCRIPT",
    )
    assert FAKE_KEY not in (rejected.failure_summary or "")
