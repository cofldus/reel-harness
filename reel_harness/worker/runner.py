from __future__ import annotations

import hashlib
import json
import wave
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, select

from reel_harness.core.errors import (
    MissingPrerequisiteError,
    PipelineError,
    ReviewRequiredSignal,
    UnsupportedResumeStageError,
)
from reel_harness.core.state_machine import JobStatus, ReasonCode, Stage, apply_transition
from reel_harness.db.models import Asset, StageRun
from reel_harness.manifest.writer import build_manifest, write_manifest
from reel_harness.observability import log_stage_event
from reel_harness.pipeline import stages
from reel_harness.providers.base import LLMProvider, Publisher, StockMediaProvider, TTSProvider, TTSResult
from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES, STAGE_ENTRY_STATUS, STAGE_ORDER, STAGE_RETRY_POLICY

TTS_VOICE_ID = "fake-voice-1"

# Render metadata persisted alongside the video so a VALIDATE resume in a fresh
# worker process can rebuild RenderOutput without trusting any prior in-memory
# state (see _restore_render).
RENDER_META_REL_PATH = "render/render_meta.json"


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
        try:
            return Stage(job.retry_target_stage)
        except ValueError as exc:
            raise UnsupportedResumeStageError(
                f"unknown resume stage {job.retry_target_stage!r}"
            ) from exc
    return Stage.TOPIC if job.topic is None else Stage.SCRIPT


def _restore_assets(session, job) -> list[stages.AssetFetchResult]:
    """Rebuilds the ASSET stage's results from the Asset rows persisted at ASSET
    success plus the files on disk. Every file is re-hashed against the recorded
    checksum so a missing or corrupted asset fails explicitly instead of feeding
    bad inputs to RENDER."""
    rows = session.execute(
        select(Asset).where(Asset.job_id == job.id).order_by(Asset.scene_index),
    ).scalars().all()
    if not rows:
        raise MissingPrerequisiteError(
            "no persisted assets for this job -- retry from the ASSET stage to re-fetch them"
        )
    scenes = (job.script or {}).get("scenes") or []
    if len(rows) != len(scenes):
        raise MissingPrerequisiteError(
            f"persisted assets ({len(rows)}) do not match script scenes ({len(scenes)}) "
            "-- retry from the ASSET stage"
        )
    results: list[stages.AssetFetchResult] = []
    for row in rows:
        path = Path(row.local_path)
        if not path.is_file():
            raise MissingPrerequisiteError(f"asset file for scene {row.scene_index} is missing on disk")
        if hashlib.sha256(path.read_bytes()).hexdigest() != row.checksum_sha256:
            raise MissingPrerequisiteError(
                f"asset file for scene {row.scene_index} is corrupted (checksum mismatch)"
            )
        results.append(
            stages.AssetFetchResult(
                scene_index=row.scene_index,
                local_path=path,
                checksum_sha256=row.checksum_sha256,
                mime_type=row.mime_type,
                source_url=row.source_url or "",
                author=row.author,
                license_type=row.license_type,
            )
        )
    return results


def _restore_tts(job, providers: ProviderBundle, storage) -> list[TTSResult]:
    """Rebuilds the TTS stage's results from the audio files persisted under
    jobs/{id}/tts/. Duration is re-read from the actual WAV header, never
    guessed."""
    scenes = (job.script or {}).get("scenes") or []
    if not scenes:
        raise MissingPrerequisiteError("job has no persisted script -- retry from the SCRIPT stage")
    results: list[TTSResult] = []
    for index in range(len(scenes)):
        audio_path = storage.job_dir(job.id) / "tts" / f"scene_{index}" / "tts.wav"
        if not audio_path.is_file():
            raise MissingPrerequisiteError(f"tts audio for scene {index} is missing on disk")
        try:
            with wave.open(str(audio_path), "rb") as wav_file:
                duration = wav_file.getnframes() / float(wav_file.getframerate())
        except (OSError, EOFError, wave.Error) as exc:
            raise MissingPrerequisiteError(f"tts audio for scene {index} is unreadable: {exc}") from exc
        results.append(
            TTSResult(
                audio_path=audio_path,
                duration_sec=duration,
                provider_id=providers.tts.provider_id,
                voice_id=TTS_VOICE_ID,
            )
        )
    return results


def _restore_render(job, storage) -> stages.RenderOutput:
    """Rebuilds RenderOutput for a VALIDATE resume from the actual final.mp4 on
    disk plus the render metadata persisted at RENDER success. VALIDATE then
    re-runs ffprobe against the real file -- nothing is trusted from memory."""
    video_path = storage.job_dir(job.id) / "final" / "final.mp4"
    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise MissingPrerequisiteError(
            "final/final.mp4 is missing or empty -- retry from the RENDER stage"
        )
    if not storage.exists(job.id, RENDER_META_REL_PATH):
        raise MissingPrerequisiteError(
            "render/render_meta.json is missing -- retry from the RENDER stage"
        )
    try:
        meta = json.loads(storage.read_bytes(job.id, RENDER_META_REL_PATH))
        return stages.RenderOutput(
            video_path=video_path,
            ffmpeg_version=meta["ffmpeg_version"],
            width=int(meta["width"]),
            height=int(meta["height"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MissingPrerequisiteError(f"render metadata is corrupted: {exc}") from exc


def _restore_context(session, job, providers: ProviderBundle, storage, full_order: list[Stage],
                     start_stage: Stage) -> dict:
    """Rebuilds the inter-stage context for a resume purely from the DB and the
    job's storage directory. A resumed worker never trusts a previous worker
    process's memory; anything a remaining stage (or the final manifest) needs
    is either restored here or fails with MISSING_PREREQUISITE."""
    context: dict = {}
    idx = full_order.index(start_stage)

    def _completed(stage: Stage) -> bool:
        return stage in full_order and idx > full_order.index(stage)

    if _completed(Stage.SCRIPT) and not job.script:
        raise MissingPrerequisiteError("job has no persisted script -- retry from the SCRIPT stage")
    if _completed(Stage.ASSET):
        context["assets"] = _restore_assets(session, job)
    if start_stage is Stage.RENDER:
        context["tts_results"] = _restore_tts(job, providers, storage)
    if start_stage is Stage.VALIDATE:
        context["render"] = _restore_render(job, storage)
    return context


def _execute_stage(session, stage: Stage, job, channel, providers: ProviderBundle, storage,
                   context: dict) -> None:
    if stage is Stage.TOPIC:
        stages.run_topic_generating(job, channel, providers.llm)
    elif stage is Stage.SCRIPT:
        stages.run_script_generating(job, channel, providers.llm)
    elif stage is Stage.POLICY:
        stages.run_policy_checking(job)
    elif stage is Stage.ASSET:
        context["assets"] = stages.run_asset_fetching(job, providers.stock_media, storage)
        # Replace (not append) this job's Asset rows so a later resume restores
        # exactly the current attempt's assets. StageRun history is unaffected.
        session.execute(delete(Asset).where(Asset.job_id == job.id))
        for result in context["assets"]:
            session.add(
                Asset(
                    job_id=job.id,
                    scene_index=result.scene_index,
                    source_provider=providers.stock_media.provider_id,
                    source_url=result.source_url,
                    author=result.author,
                    license_type=result.license_type,
                    local_path=str(result.local_path),
                    checksum_sha256=result.checksum_sha256,
                    mime_type=result.mime_type,
                )
            )
    elif stage is Stage.TTS:
        context["tts_results"] = stages.run_tts_generating(job, providers.tts, storage)
    elif stage is Stage.RENDER:
        render = stages.run_rendering(job, context["assets"], context["tts_results"], storage)
        context["render"] = render
        storage.write_bytes(
            job.id,
            RENDER_META_REL_PATH,
            json.dumps(
                {"ffmpeg_version": render.ffmpeg_version, "width": render.width, "height": render.height},
            ).encode("utf-8"),
        )
    elif stage is Stage.VALIDATE:
        context["validation"] = stages.run_validating(
            job, context["render"].video_path,
            expected_width=context["render"].width, expected_height=context["render"].height,
        )
    else:  # pragma: no cover - PUBLISH resume is rejected before this point
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


def _next_attempt_number(session, job, stage: Stage) -> int:
    """Attempt numbering comes from the StageRun history itself, not from
    retry_count (which resets on reject/manual retry): re-running a stage for
    any reason always records max(previous attempt) + 1."""
    previous = session.execute(
        select(func.max(StageRun.attempt)).where(StageRun.job_id == job.id, StageRun.stage == stage.value),
    ).scalar_one()
    return (previous or 0) + 1


def _run_single_stage(session, job, channel, providers, storage, stage: Stage, context: dict) -> bool:
    """Runs one stage; returns True to keep advancing, False if the job landed in
    a stopping state (RETRY_WAIT / FAILED / REVIEW_REQUIRED / CANCELLED)."""
    now = _utcnow()
    attempt = _next_attempt_number(session, job, stage)
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
        _execute_stage(session, stage, job, channel, providers, storage, context)
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


def _fail_resume(session, job, error: PipelineError) -> None:
    """A resume could not even start (unsupported target stage or missing/corrupt
    prerequisite artifacts). The job moves to an explicit FAILED with the cause
    in failure_code/failure_summary -- never left ACTIVE, never crashed out of."""
    apply_transition(job, JobStatus.FAILED, failure_code=error.code, failure_summary=str(error)[:500])
    session.commit()
    log_stage_event(
        job_id=job.id, stage=job.retry_target_stage or "RESUME", attempt=0,
        event="resume_failed", error_code=error.code,
    )


def _handle_unexpected_error(session, job, error: Exception) -> None:
    """Last-resort safety boundary for non-PipelineError exceptions. Guarantees
    the job is never persisted as ACTIVE + unlocked: the session is rolled back,
    any running StageRun is closed as failed, and the job moves to FAILED with a
    stable failure code. The summary carries only the exception type and a short
    message -- no traceback, no request bodies. Never re-raises."""
    try:
        session.rollback()
    except Exception:  # pragma: no cover - session teardown must not mask the outcome
        pass

    now = _utcnow()
    stage_run = session.execute(
        select(StageRun)
        .where(StageRun.job_id == job.id, StageRun.status == "running")
        .order_by(StageRun.started_at.desc())
        .limit(1),
    ).scalar_one_or_none()
    if stage_run is not None:
        stage_run.status = "failed"
        stage_run.error_detail = f"{type(error).__name__}: {str(error)[:500]}"
        stage_run.finished_at = now

    summary = f"unexpected {type(error).__name__}: {str(error)[:300]}"
    current = JobStatus(job.status)
    if current in ACTIVE_STAGE_STATUSES or current is JobStatus.RETRY_WAIT:
        apply_transition(
            job, JobStatus.FAILED,
            failure_code="UNEXPECTED_PIPELINE_ERROR", failure_summary=summary,
        )
    else:
        # e.g. still QUEUED (leaseable again) -- record the cause without forcing
        # an invalid transition.
        job.failure_code = "UNEXPECTED_PIPELINE_ERROR"
        job.failure_summary = summary
    session.commit()
    log_stage_event(
        job_id=job.id, stage=job.current_stage or "NONE",
        attempt=stage_run.attempt if stage_run is not None else 0,
        event="stage_unexpected_error", error_code="UNEXPECTED_PIPELINE_ERROR",
    )


def run_job(session, job, channel, providers: ProviderBundle, storage) -> None:
    """Runs a leased job forward from wherever it currently is, through as many
    stages as succeed in this call, stopping at the first RETRY_WAIT / FAILED /
    REVIEW_REQUIRED / CANCELLED outcome. On full success it writes manifest.json
    (including real render/validation metadata and the final video's checksum,
    recomputed from the file on disk) and lands in REVIEW_REQUIRED for the
    operator to approve/reject.

    Resumes (RETRY_WAIT with a retry_target_stage) rebuild all inter-stage
    context from the DB and job storage -- a fresh worker process never depends
    on a previous process's memory. Unexpected exceptions are converted to an
    explicit FAILED via _handle_unexpected_error; this function does not raise
    (KeyboardInterrupt/SystemExit still propagate).
    """
    try:
        _run_job_impl(session, job, channel, providers, storage)
    except Exception as error:  # noqa: BLE001 - safety net: a job must never stay ACTIVE + unlocked
        _handle_unexpected_error(session, job, error)


def _run_job_impl(session, job, channel, providers: ProviderBundle, storage) -> None:
    try:
        start_stage = _start_stage(job)
        full_order = [Stage.TOPIC, *STAGE_ORDER] if start_stage is Stage.TOPIC else STAGE_ORDER
        if start_stage not in full_order:
            raise UnsupportedResumeStageError(f"cannot resume from stage {start_stage.value}")
        context = _restore_context(session, job, providers, storage, full_order, start_stage)
    except PipelineError as error:
        _fail_resume(session, job, error)
        return

    remaining = full_order[full_order.index(start_stage):]
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
