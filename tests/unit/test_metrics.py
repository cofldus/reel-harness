from __future__ import annotations

from reel_harness.core.state_machine import JobStatus, PublicationStatus, apply_transition
from reel_harness.db.models import Job, Publication
from reel_harness.ops.metrics import collect_metrics, render_prometheus_text


def _samples_by_name(session_factory) -> dict:
    return {s.name: s for s in collect_metrics(session_factory)}


def test_metrics_on_empty_db_are_all_zero(session_factory) -> None:
    samples = _samples_by_name(session_factory)
    assert samples["jobs_created_total"].value == 0
    assert samples["publications_created_total"].value == 0
    assert samples["active_jobs"].value == 0
    assert samples["queue_depth"].value == 0


def test_metrics_count_job_creation(job_service, channel, session_factory) -> None:
    job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    job_service.create_job(channel.id, idempotency_key="k2", topic="t")
    samples = _samples_by_name(session_factory)
    assert samples["jobs_created_total"].value == 2


def test_metrics_count_completed_and_failed_jobs(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.status = JobStatus.COMPLETED.value
        session.commit()
    job2, _ = job_service.create_job(channel.id, idempotency_key="k2", topic="t")
    with session_factory() as session:
        db_job2 = session.get(Job, job2.id)
        db_job2.status = JobStatus.FAILED.value
        session.commit()
    samples = _samples_by_name(session_factory)
    assert samples["jobs_completed_total"].value == 1
    assert samples["jobs_failed_total"].value == 1


def test_metrics_active_jobs_and_queue_depth(job_service, channel, session_factory) -> None:
    active_job, _ = job_service.create_job(channel.id, idempotency_key="k-active", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, active_job.id)
        db_job.status = JobStatus.RENDERING.value
        session.commit()
    queued_job, _ = job_service.create_job(channel.id, idempotency_key="k-queued", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, queued_job.id)
        db_job.status = JobStatus.QUEUED.value
        session.commit()
    samples = _samples_by_name(session_factory)
    assert samples["active_jobs"].value == 1
    assert samples["queue_depth"].value == 1


def test_metrics_count_publications(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        pub = Publication(
            job_id=job.id, provider="youtube", account_reference="default",
            status=PublicationStatus.PUBLISHED.value,
            idempotency_key="pub-1", final_video_checksum="abc123", bytes_uploaded=5000,
        )
        session.add(pub)
        session.commit()
    samples = _samples_by_name(session_factory)
    assert samples["publications_created_total"].value == 1
    assert samples["publications_published_total"].value == 1
    assert samples["upload_bytes_total"].value == 5000


def test_metrics_stage_duration_from_stage_runs(job_service, channel, session_factory) -> None:
    from datetime import UTC, datetime, timedelta

    from reel_harness.db.models import StageRun

    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    started = datetime.now(UTC)
    with session_factory() as session:
        session.add(StageRun(
            job_id=job.id, stage="RENDER", attempt=1, status="succeeded",
            started_at=started, finished_at=started + timedelta(seconds=12),
        ))
        session.commit()
    samples = _samples_by_name(session_factory)
    assert samples["stage_duration_seconds_count"].value == 1
    assert 11.0 <= samples["stage_duration_seconds_sum"].value <= 13.0


def test_metrics_never_include_job_topic_or_title(job_service, channel, session_factory) -> None:
    job_service.create_job(channel.id, idempotency_key="k1", topic="a very specific sensitive topic string")
    text_output = render_prometheus_text(collect_metrics(session_factory))
    assert "a very specific sensitive topic string" not in text_output


def test_render_prometheus_text_format(session_factory) -> None:
    samples = collect_metrics(session_factory)
    text_output = render_prometheus_text(samples)
    assert "# HELP jobs_created_total" in text_output
    assert "# TYPE jobs_created_total counter" in text_output
    assert "jobs_created_total 0.0" in text_output


def test_metrics_apply_transition_updates_active_jobs(job_service, channel, session_factory) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")  # already QUEUED
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        apply_transition(db_job, JobStatus.TOPIC_GENERATING)
        session.commit()
    samples = _samples_by_name(session_factory)
    assert samples["active_jobs"].value == 1
