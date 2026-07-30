from __future__ import annotations

from reel_harness.db.models import Job
from reel_harness.web.view_models import (
    build_job_detail_view,
    build_job_summary_view,
    build_system_status_view,
)


def _set_status(session_factory, job_id: str, status: str, **extra) -> None:
    with session_factory() as session:
        db_job = session.get(Job, job_id)
        db_job.status = status
        for key, value in extra.items():
            setattr(db_job, key, value)
        session.commit()


def test_job_summary_view_labels_and_flags_for_queued(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    fresh = job_service.get_job(job.id)
    view = build_job_summary_view(fresh)
    assert view.job_id == job.id
    assert view.status_label == "대기 중"
    assert view.is_terminal is False
    assert view.needs_action is False
    assert view.detail_url == f"/jobs/{job.id}"


def test_job_summary_view_needs_action_for_failed(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    _set_status(session_factory, job.id, "FAILED", failure_code="X", failure_summary="boom")
    view = build_job_summary_view(job_service.get_job(job.id))
    assert view.needs_action is True
    assert view.is_terminal is False


def test_job_summary_view_terminal_for_completed(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    _set_status(session_factory, job.id, "COMPLETED")
    view = build_job_summary_view(job_service.get_job(job.id))
    assert view.is_terminal is True
    assert view.needs_action is False


def test_job_detail_view_can_retry_only_when_failed(job_service, channel, session_factory, storage) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    _set_status(session_factory, job.id, "FAILED", failure_code="X", failure_summary="boom")
    fresh = job_service.get_job(job.id)
    view = build_job_detail_view(fresh, [], [], storage)
    assert view.can_retry is True
    assert view.can_approve is False
    assert view.can_reject is False


def test_job_detail_view_can_approve_and_reject_only_when_review_required(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    _set_status(session_factory, job.id, "REVIEW_REQUIRED", reason_code="USER_APPROVAL_REQUIRED")
    fresh = job_service.get_job(job.id)
    view = build_job_detail_view(fresh, [], [], storage)
    assert view.can_approve is True
    assert view.can_reject is True
    assert view.can_retry is False


def test_job_detail_view_needs_action_and_terminal_flags(job_service, channel, session_factory, storage) -> None:
    """These drive the status fragment's own polling-stop condition
    (fragments/job_status.html) -- must be real booleans on JobDetailView,
    not derived from a localized label string in the template."""
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    fresh = job_service.get_job(job.id)  # QUEUED: still active
    view = build_job_detail_view(fresh, [], [], storage)
    assert view.is_terminal is False
    assert view.needs_action is False

    _set_status(session_factory, job.id, "REVIEW_REQUIRED", reason_code="USER_APPROVAL_REQUIRED")
    review_view = build_job_detail_view(job_service.get_job(job.id), [], [], storage)
    assert review_view.is_terminal is False
    assert review_view.needs_action is True

    _set_status(session_factory, job.id, "COMPLETED")
    done_view = build_job_detail_view(job_service.get_job(job.id), [], [], storage)
    assert done_view.is_terminal is True


def test_job_detail_view_can_cancel_derived_from_allowed_transitions(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    fresh = job_service.get_job(job.id)  # QUEUED -- CANCELLED is an allowed transition
    assert build_job_detail_view(fresh, [], [], storage).can_cancel is True

    _set_status(session_factory, job.id, "COMPLETED")
    completed = job_service.get_job(job.id)  # COMPLETED -- no transitions allowed at all
    assert build_job_detail_view(completed, [], [], storage).can_cancel is False


def test_job_detail_view_video_available_reflects_real_file_existence(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    fresh = job_service.get_job(job.id)
    assert build_job_detail_view(fresh, [], [], storage).video_available is False

    final_dir = storage.job_dir(job.id) / "final"
    final_dir.mkdir(parents=True)
    (final_dir / "final.mp4").write_bytes(b"fake video bytes")
    refreshed = job_service.get_job(job.id)
    view = build_job_detail_view(refreshed, [], [], storage)
    assert view.video_available is True
    assert view.can_download is True


def test_job_detail_view_script_title_from_job_script_column(
    job_service, channel, session_factory, storage,
) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "나의 제목", "scenes": []}
        session.commit()
    fresh = job_service.get_job(job.id)
    view = build_job_detail_view(fresh, [], [], storage)
    assert view.script_title == "나의 제목"


def test_system_status_view_reflects_job_counts(tmp_path) -> None:
    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'view-model-test.db'}",
        jobs_dir=tmp_path / "jobs", app_api_key="a-real-non-placeholder-test-key",
    )
    ctx = AppContext(settings=settings)
    channel = ctx.jobs.create_channel(name="c", niche="n", language="en")
    for i in range(2):
        ctx.jobs.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")

    view = build_system_status_view(ctx)
    assert view.job_status_counts.get("QUEUED") == 2
    assert view.preflight_overall in ("PASS", "WARN", "FAIL")
    assert any(c.provider_id == "demo_tts" for c in view.preflight_checks)


def test_job_detail_view_never_exposes_local_path_or_secret_shaped_fields() -> None:
    import dataclasses

    from reel_harness.web.view_models import JobDetailView

    field_names = {f.name for f in dataclasses.fields(JobDetailView)}
    forbidden_substrings = ("local_path", "secret", "token", "api_key")
    for name in field_names:
        for bad in forbidden_substrings:
            assert bad not in name.lower(), f"JobDetailView.{name} looks unsafe to expose"
