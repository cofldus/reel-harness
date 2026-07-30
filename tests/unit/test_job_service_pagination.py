from __future__ import annotations

from reel_harness.core.state_machine import Stage


def test_list_jobs_default_is_unbounded(job_service, channel) -> None:
    for i in range(5):
        job_service.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")
    jobs = job_service.list_jobs()
    assert len(jobs) == 5


def test_list_jobs_respects_limit_and_offset(job_service, channel) -> None:
    for i in range(5):
        job_service.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")
    page1 = job_service.list_jobs(limit=2, offset=0)
    page2 = job_service.list_jobs(limit=2, offset=2)
    assert len(page1) == 2
    assert len(page2) == 2
    assert {j.id for j in page1}.isdisjoint({j.id for j in page2})


def test_list_jobs_status_filter_combines_with_pagination(job_service, channel) -> None:
    for i in range(3):
        job_service.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")
    jobs = job_service.list_jobs(status="QUEUED", limit=2)
    assert len(jobs) == 2
    assert all(j.status == "QUEUED" for j in jobs)


def test_count_jobs_matches_status_filter(job_service, channel) -> None:
    for i in range(3):
        job_service.create_job(channel.id, idempotency_key=f"k{i}", topic=f"t{i}")
    assert job_service.count_jobs() == 3
    assert job_service.count_jobs(status="QUEUED") == 3
    assert job_service.count_jobs(status="COMPLETED") == 0


def test_get_stage_runs_empty_before_any_stage_executes(job_service, channel) -> None:
    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    assert job_service.get_stage_runs(job.id) == []


def test_get_stage_runs_ordered_oldest_first(job_service, channel, session_factory) -> None:
    from datetime import UTC, datetime, timedelta

    from reel_harness.db.models import Job, StageRun

    job, _ = job_service.create_job(channel.id, idempotency_key="k1", topic="t")
    with session_factory() as session:
        db_job = session.get(Job, job.id)
        base = datetime.now(UTC)
        session.add(StageRun(
            job_id=db_job.id, stage=Stage.POLICY.value, attempt=1, status="success",
            started_at=base + timedelta(seconds=5), finished_at=base + timedelta(seconds=6),
        ))
        session.add(StageRun(
            job_id=db_job.id, stage=Stage.SCRIPT.value, attempt=1, status="success",
            started_at=base, finished_at=base + timedelta(seconds=1),
        ))
        session.commit()

    runs = job_service.get_stage_runs(job.id)
    assert [r.stage for r in runs] == [Stage.SCRIPT.value, Stage.POLICY.value]
