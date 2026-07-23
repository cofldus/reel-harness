from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from reel_harness.core.errors import PipelineError, ReviewRequiredSignal
from reel_harness.core.state_machine import JobStatus, ReasonCode, Stage, apply_transition
from reel_harness.db.models import StageRun
from reel_harness.manifest.writer import build_manifest, write_manifest
from reel_harness.observability import log_stage_event
from reel_harness.pipeline import stages
from reel_harness.providers.base import LLMProvider, Publisher, StockMediaProvider, TTSProvider
from reel_harness.worker.policy import STAGE_ENTRY_STATUS, STAGE_ORDER, STAGE_RETRY_POLICY

TTS_VOICE_ID = "fake-voice-1"


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class ProviderBundle:
    llm: LLMProvider
    tts: TTSProvider
    stock_media: StockMediaProvider
    publisher: Publisher | None = None


def _start_stage(job) -> Stage:
    if job.status == JobStatus.RETRY_WAIT.value and job.retry_target_stage:
        return Stage(job.retry_target_stage)
    return Stage.TOPIC if job.topic is None else Stage.SCRIPT


def _execute_stage(stage: Stage, job, channel, providers: ProviderBundle, storage, context: dict) -> None:
    if stage is Stage.TOPIC:
        stages.run_topic_generating(job, channel, providers.llm)
    elif stage is Stage.SCRIPT:
        stages.run_script_generating(job, channel, providers.llm)
    elif stage is Stage.POLICY:
        stages.run_policy_checking(job)
    elif stage is Stage.ASSET:
        context["assets"] = stages.run_asset_fetching(job, providers.stock_media, storage)
    elif stage is Stage.TTS:
        context["tts_results"] = stages.run_tts_generating(job, providers.tts, storage)
    elif stage is Stage.RENDER:
        context["render"] = stages.run_rendering(job, context["assets"], context["tts_results"], storage)
    elif stage is Stage.VALIDATE:
        context["validation"] = stages.run_validating(job, context["render"].video_path)
    else:  # pragma: no cover - PUBLISH is not reachable in Phase 0/1
        raise NotImplementedError(f"stage {stage} is not implemented yet")


def _handle_pipeline_error(job, stage: Stage, error: PipelineError, now: datetime) -> None:
    if not error.retryable:
        apply_transition(job, JobStatus.FAILED, failure_code=error.code, failure_summary=str(error)[:500])
        return

    max_retries, backoffs = STAGE_RETRY_POLICY.get(stage, (0, []))
    if job.retry_count >= max_retries:
        apply_transition(
            job, JobStatus.FAILED,
            failure_code="RETRIES_EXHAUSTED",
            failure_summary=f"{stage.value} failed after {job.retry_count} retries: {error}"[:500],
        )
        return

    delay = backoffs[min(job.retry_count, len(backoffs) - 1)] if backoffs else 30
    job.retry_count += 1
    apply_transition(
        job, JobStatus.RETRY_WAIT,
        retry_target_stage=stage.value,
        next_retry_at=now + timedelta(seconds=delay),
        failure_code=error.code,
        failure_summary=str(error)[:500],
    )


def _run_single_stage(session, job, channel, providers, storage, stage: Stage, context: dict) -> bool:
    """Runs one stage; returns True to keep advancing, False if the job landed in
    a stopping state (RETRY_WAIT / FAILED / REVIEW_REQUIRED / CANCELLED)."""
    now = _utcnow()
    attempt = job.retry_count + 1
    apply_transition(job, STAGE_ENTRY_STATUS[stage])
    job.current_stage = stage.value
    job.heartbeat_at = now
    stage_run = StageRun(
        job_id=job.id, stage=stage.value, attempt=attempt, status="running", started_at=now,
    )
    session.add(stage_run)
    session.commit()
    log_stage_event(job_id=job.id, stage=stage.value, attempt=attempt, event="stage_started")

    try:
        _execute_stage(stage, job, channel, providers, storage, context)
    except ReviewRequiredSignal as signal:
        finished_at = _utcnow()
        stage_run.status = "review_required"
        stage_run.finished_at = finished_at
        apply_transition(job, JobStatus.REVIEW_REQUIRED, reason_code=signal.reason_code)
        job.retry_count = 0
        session.commit()
        log_stage_event(
            job_id=job.id, stage=stage.value, attempt=attempt, event="stage_review_required",
            duration_ms=(finished_at - now).total_seconds() * 1000, error_code=signal.reason_code,
        )
        return False
    except PipelineError as error:
        finished_at = _utcnow()
        stage_run.status = "failed"
        stage_run.error_detail = str(error)[:2000]
        stage_run.finished_at = finished_at
        _handle_pipeline_error(job, stage, error, finished_at)
        session.commit()
        log_stage_event(
            job_id=job.id, stage=stage.value, attempt=attempt, event="stage_failed",
            duration_ms=(finished_at - now).total_seconds() * 1000, error_code=error.code,
        )
        return False
    else:
        finished_at = _utcnow()
        stage_run.status = "success"
        stage_run.finished_at = finished_at
        job.retry_count = 0
        session.commit()
        log_stage_event(
            job_id=job.id, stage=stage.value, attempt=attempt, event="stage_succeeded",
            duration_ms=(finished_at - now).total_seconds() * 1000,
        )
        return True


def run_job(session, job, channel, providers: ProviderBundle, storage) -> None:
    """Runs a leased job forward from wherever it currently is, through as many
    stages as succeed in this call, stopping at the first RETRY_WAIT / FAILED /
    REVIEW_REQUIRED / CANCELLED outcome. On full success it writes manifest.json
    (including real render/validation metadata and the final video's checksum)
    and lands in REVIEW_REQUIRED for the operator to approve/reject.
    """
    start_stage = _start_stage(job)
    full_order = [Stage.TOPIC, *STAGE_ORDER] if start_stage is Stage.TOPIC else STAGE_ORDER
    remaining = full_order[full_order.index(start_stage):]

    context: dict = {}
    for stage in remaining:
        if job.cancel_requested:
            apply_transition(job, JobStatus.CANCELLED)
            session.commit()
            return
        if not _run_single_stage(session, job, channel, providers, storage, stage, context):
            return

    final_video_checksum = hashlib.sha256(context["render"].video_path.read_bytes()).hexdigest()
    manifest = build_manifest(
        job, context["assets"], providers.tts.provider_id, TTS_VOICE_ID,
        render=context["render"], validation=context["validation"], final_video_checksum=final_video_checksum,
    )
    write_manifest(storage, job.id, manifest)
    apply_transition(job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.USER_APPROVAL_REQUIRED.value)
    session.commit()
