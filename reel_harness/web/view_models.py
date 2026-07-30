from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from reel_harness.core.service import asset_safe_metadata
from reel_harness.core.state_machine import ALLOWED_TRANSITIONS, TERMINAL_STATUSES, JobStatus
from reel_harness.web.formatting import format_duration, format_elapsed_since
from reel_harness.web.labels import NEEDS_ACTION_STATUSES, job_status_label, stage_label

FINAL_VIDEO_REL_PATH = "final/final.mp4"


@dataclass
class JobSummaryView:
    job_id: str
    topic: str | None
    status_label: str
    stage_label: str | None
    created_at: datetime
    elapsed: str
    is_terminal: bool
    needs_action: bool
    detail_url: str


@dataclass
class StageProgressEntry:
    stage_label: str
    status: str
    attempt: int
    started_at: datetime
    finished_at: datetime | None
    duration: str | None
    error_detail: str | None


@dataclass
class AssetView:
    scene_index: int
    provider: str
    creator: str | None
    license_type: str | None
    attribution_text: str | None
    commercial_use_allowed: bool
    modification_allowed: bool
    width: int | None
    height: int | None
    duration_sec: float | None


@dataclass
class JobDetailView:
    job_id: str
    topic: str | None
    script_title: str | None
    status_label: str
    stage_label: str | None
    reason_code: str | None
    failure_code: str | None
    failure_summary: str | None
    created_at: datetime
    updated_at: datetime
    elapsed: str
    attempt_number: int
    retry_count: int
    stage_progress: list[StageProgressEntry]
    assets: list[AssetView]
    eligibility_reasons: list[str]
    is_terminal: bool
    needs_action: bool
    can_retry: bool
    can_cancel: bool
    can_approve: bool
    can_reject: bool
    can_download: bool
    can_publish: bool
    video_available: bool


@dataclass
class ProviderStatusView:
    kind: str
    provider_id: str
    status: str
    detail: str


@dataclass
class SystemStatusView:
    version: str
    uptime: str
    job_status_counts: dict[str, int]
    publication_status_counts: dict[str, int]
    queue_depth: int
    stale_job_leases: int
    stale_publication_leases: int
    preflight_overall: str
    preflight_checks: list[ProviderStatusView] = field(default_factory=list)


def _job_status(job) -> JobStatus:
    return JobStatus(job.status)


def build_job_summary_view(job) -> JobSummaryView:
    status = _job_status(job)
    return JobSummaryView(
        job_id=job.id,
        topic=job.topic,
        status_label=job_status_label(job.status),
        stage_label=stage_label(job.current_stage),
        created_at=job.created_at,
        elapsed=format_elapsed_since(job.created_at),
        is_terminal=status in TERMINAL_STATUSES,
        needs_action=status in NEEDS_ACTION_STATUSES,
        detail_url=f"/jobs/{job.id}",
    )


def build_stage_progress(stage_runs) -> list[StageProgressEntry]:
    entries = []
    for run in stage_runs:
        duration = None
        if run.finished_at is not None:
            started = run.started_at.replace(tzinfo=UTC) if run.started_at.tzinfo is None else run.started_at
            finished = run.finished_at.replace(tzinfo=UTC) if run.finished_at.tzinfo is None else run.finished_at
            duration = format_duration((finished - started).total_seconds())
        entries.append(StageProgressEntry(
            stage_label=stage_label(run.stage) or run.stage,
            status=run.status,
            attempt=run.attempt,
            started_at=run.started_at,
            finished_at=run.finished_at,
            duration=duration,
            error_detail=run.error_detail,
        ))
    return entries


def build_asset_view(asset) -> AssetView:
    safe = asset_safe_metadata(asset)
    return AssetView(
        scene_index=safe["scene_index"],
        provider=safe["provider"],
        creator=safe["creator"],
        license_type=safe["license_type"],
        attribution_text=safe["attribution_text"],
        commercial_use_allowed=safe["commercial_use_allowed"],
        modification_allowed=safe["modification_allowed"],
        width=safe["width"],
        height=safe["height"],
        duration_sec=safe["duration_sec"],
    )


def build_system_status_view(ctx) -> SystemStatusView:
    """Reuses api.app.collect_status's exact counting logic (no duplicated
    SQL) plus a local-only preflight sweep. Deliberately profile="fake" and
    never run_remote_checks -- production-profile verification and any real
    network call to a publisher stay a deliberate CLI action, never
    something a page load triggers automatically."""
    from reel_harness.api.app import collect_status
    from reel_harness.ops.preflight import run_preflight

    status_data = collect_status(ctx)
    report = run_preflight(ctx.settings, ctx.session_factory, profile="fake")
    checks = [
        ProviderStatusView(kind="preflight", provider_id=c.name, status=c.status, detail=c.detail)
        for c in report.checks
    ]
    return SystemStatusView(
        version=status_data["version"],
        uptime=format_duration(status_data["uptime_seconds"]),
        job_status_counts=status_data["job_status_counts"],
        publication_status_counts=status_data["publication_status_counts"],
        queue_depth=status_data["queue_depth"],
        stale_job_leases=status_data["stale_job_leases"],
        stale_publication_leases=status_data["stale_publication_leases"],
        preflight_overall=report.overall,
        preflight_checks=checks,
    )


def build_job_detail_view(job, stage_runs, assets, storage, eligibility=None) -> JobDetailView:
    """`eligibility` (an EligibilityResult or None) is passed in rather than
    computed here -- it requires a live re-check (re-hashing the final
    video, re-reading manifest.json) that's only worth doing when the job is
    actually COMPLETED; the route decides when to call
    ctx.publications.check_eligibility and passes the result through."""
    status = _job_status(job)
    can_cancel = JobStatus.CANCELLED in ALLOWED_TRANSITIONS.get(status, set())
    video_available = storage.exists(job.id, FINAL_VIDEO_REL_PATH)
    script_title = None
    if isinstance(job.script, dict):
        script_title = job.script.get("title")

    return JobDetailView(
        job_id=job.id,
        topic=job.topic,
        script_title=script_title,
        status_label=job_status_label(job.status),
        stage_label=stage_label(job.current_stage),
        reason_code=job.reason_code,
        failure_code=job.failure_code,
        failure_summary=job.failure_summary,
        created_at=job.created_at,
        updated_at=job.updated_at,
        elapsed=format_elapsed_since(job.created_at),
        attempt_number=job.attempt_number,
        retry_count=job.retry_count,
        stage_progress=build_stage_progress(stage_runs),
        assets=[build_asset_view(a) for a in assets],
        eligibility_reasons=eligibility.reasons if eligibility is not None else [],
        is_terminal=status in TERMINAL_STATUSES,
        needs_action=status in NEEDS_ACTION_STATUSES,
        can_retry=status == JobStatus.FAILED,
        can_cancel=can_cancel,
        can_approve=status == JobStatus.REVIEW_REQUIRED,
        can_reject=status == JobStatus.REVIEW_REQUIRED,
        can_download=video_available,
        can_publish=eligibility.eligible if eligibility is not None else False,
        video_available=video_available,
    )
