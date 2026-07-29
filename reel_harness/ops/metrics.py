from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select

from reel_harness.core.state_machine import JobStatus, PublicationStatus
from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES

# Statuses that mean "waiting for a worker to pick this up," used for
# queue_depth -- a job that is currently ACTIVE (a worker already holds it)
# is not queued, it's in flight.
_QUEUED_JOB_STATUSES = frozenset({JobStatus.QUEUED.value, JobStatus.RETRY_WAIT.value})


@dataclass
class MetricSample:
    name: str
    value: float
    metric_type: str  # "counter" | "gauge"
    help_text: str


def _sample(name: str, value: float, metric_type: str, help_text: str) -> MetricSample:
    return MetricSample(name=name, value=float(value), metric_type=metric_type, help_text=help_text)


def collect_metrics(session_factory) -> list[MetricSample]:
    """Every metric here is DERIVED from current DB state at scrape time,
    not accumulated by in-memory counters instrumented at each call site.
    Deliberate: an in-memory counter resets to zero on every process
    restart (silently under-reporting after any crash/restart, exactly
    the kind of gap a metrics system exists to catch), while these values
    are always the true cumulative count as of right now, recomputed
    fresh -- and this project already has that cumulative history sitting
    in the jobs DB (job/publication/stage-run rows are never deleted).
    The `_total` names are still valid Prometheus counter semantics: a
    terminal-status COUNT(*) only ever grows over time. No provider
    response text, prompt, or script is ever queried here -- only status
    enums, counts, and byte totals."""
    from reel_harness.db.models import Job, Publication, PublicationAuditEvent, StageRun

    with session_factory() as session:
        jobs_created_total = session.execute(select(func.count()).select_from(Job)).scalar_one()
        jobs_completed_total = session.execute(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.COMPLETED.value)
        ).scalar_one()
        jobs_failed_total = session.execute(
            select(func.count()).select_from(Job).where(Job.status == JobStatus.FAILED.value)
        ).scalar_one()
        active_jobs = session.execute(
            select(func.count()).select_from(Job).where(
                Job.status.in_([s.value for s in ACTIVE_STAGE_STATUSES])
            )
        ).scalar_one()
        queue_depth = session.execute(
            select(func.count()).select_from(Job).where(Job.status.in_(_QUEUED_JOB_STATUSES))
        ).scalar_one()
        retries_total = session.execute(select(func.coalesce(func.sum(Job.retry_count), 0))).scalar_one()
        worker_lease_lost_total = session.execute(
            select(func.count()).select_from(StageRun).where(StageRun.status == "lease_lost")
        ).scalar_one()
        provider_errors_total = session.execute(
            select(func.count()).select_from(StageRun).where(StageRun.error_detail.isnot(None))
        ).scalar_one()
        # SQLite has no native duration/interval type; julianday() returns a
        # fractional day count, converted to seconds here rather than
        # relying on strftime('%s', ...) (which truncates to whole seconds
        # and needs an explicit CAST from its TEXT return type).
        stage_duration_count, stage_duration_sum = session.execute(
            select(
                func.count(),
                func.coalesce(
                    func.sum(
                        (func.julianday(StageRun.finished_at) - func.julianday(StageRun.started_at)) * 86400.0
                    ), 0.0,
                ),
            ).select_from(StageRun).where(StageRun.finished_at.isnot(None))
        ).one()

        publications_created_total = session.execute(select(func.count()).select_from(Publication)).scalar_one()
        publications_published_total = session.execute(
            select(func.count()).select_from(Publication).where(
                Publication.status == PublicationStatus.PUBLISHED.value,
            )
        ).scalar_one()
        publications_failed_total = session.execute(
            select(func.count()).select_from(Publication).where(
                Publication.status == PublicationStatus.FAILED.value,
            )
        ).scalar_one()
        upload_bytes_total = session.execute(
            select(func.coalesce(func.sum(Publication.bytes_uploaded), 0))
        ).scalar_one()
        publisher_retries_total = session.execute(
            select(func.coalesce(func.sum(Publication.retry_count), 0))
        ).scalar_one()
        stale_recoveries_total = session.execute(
            select(func.count()).select_from(PublicationAuditEvent).where(
                PublicationAuditEvent.event == "upload_resumed",
            )
        ).scalar_one()

    return [
        _sample("jobs_created_total", jobs_created_total, "counter", "Total jobs ever created"),
        _sample("jobs_completed_total", jobs_completed_total, "counter", "Total jobs that reached COMPLETED"),
        _sample("jobs_failed_total", jobs_failed_total, "counter", "Total jobs that reached FAILED"),
        _sample("active_jobs", active_jobs, "gauge", "Jobs currently in an ACTIVE stage status"),
        _sample("queue_depth", queue_depth, "gauge", "Jobs QUEUED or RETRY_WAIT, awaiting a worker"),
        _sample("retries_total", retries_total, "counter", "Sum of Job.retry_count across all jobs"),
        _sample(
            "stage_duration_seconds_count", stage_duration_count, "counter",
            "Count of completed StageRun rows with a known duration",
        ),
        _sample(
            "stage_duration_seconds_sum", stage_duration_sum, "counter",
            "Sum of StageRun durations in seconds (finished_at - started_at)",
        ),
        _sample(
            "publications_created_total", publications_created_total, "counter", "Total publications ever created",
        ),
        _sample(
            "publications_published_total", publications_published_total, "counter",
            "Total publications that reached PUBLISHED",
        ),
        _sample(
            "publications_failed_total", publications_failed_total, "counter",
            "Total publications that reached FAILED",
        ),
        _sample("upload_bytes_total", upload_bytes_total, "counter", "Sum of Publication.bytes_uploaded"),
        _sample(
            "worker_lease_lost_total", worker_lease_lost_total, "counter",
            "Count of StageRun rows closed as lease_lost",
        ),
        _sample(
            "stale_recoveries_total", stale_recoveries_total, "counter",
            "Count of publication upload_resumed audit events (a proxy for stale-session recovery)",
        ),
        _sample(
            "provider_errors_total", provider_errors_total, "counter",
            "Count of StageRun rows with a non-null error_detail",
        ),
        _sample(
            "publisher_retries_total", publisher_retries_total, "counter", "Sum of Publication.retry_count",
        ),
    ]


def render_prometheus_text(samples: list[MetricSample]) -> str:
    """Dependency-free Prometheus text exposition format (no
    prometheus_client package) -- job topic/title/script text is never a
    metric label anywhere in this module, so there is no high-cardinality
    or sensitive label risk to guard against here."""
    lines = []
    for sample in samples:
        lines.append(f"# HELP {sample.name} {sample.help_text}")
        lines.append(f"# TYPE {sample.name} {sample.metric_type}")
        lines.append(f"{sample.name} {sample.value}")
    return "\n".join(lines) + "\n"
