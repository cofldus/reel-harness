from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import shutil
import uuid
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
from reel_harness.observability import log_stage_event, redact
from reel_harness.pipeline import stages
from reel_harness.providers.base import LLMProvider, Publisher, StockMediaProvider, TTSProvider, TTSResult
from reel_harness.worker.lease import assert_lease
from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES, STAGE_ENTRY_STATUS, STAGE_ORDER, STAGE_RETRY_POLICY


class LeaseLostSignal(Exception):
    """Internal worker outcome: this worker's lease was reclaimed while it was
    executing. Not a job failure -- the new lease owner is responsible for the
    job now; this worker must stop without writing any job state."""

TTS_VOICE_ID = "fake-voice-1"

# Render metadata persisted alongside the video so a VALIDATE resume in a fresh
# worker process can rebuild RenderOutput without trusting any prior in-memory
# state (see _restore_render).
RENDER_META_REL_PATH = "render/render_meta.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _persisted_error_text(text: object, limit: int) -> str:
    """Every error string persisted to the DB (failure_summary, error_detail)
    goes through the shared redaction rules first -- provider exception messages
    may embed Authorization headers, URLs with tokens, or raw API keys."""
    return (redact(str(text)) or "")[:limit]


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
                voice_id=getattr(providers.tts, "voice_id", None) or TTS_VOICE_ID,
                checksum_sha256=hashlib.sha256(audio_path.read_bytes()).hexdigest(),
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
                   context: dict, lease_token: str | None = None) -> None:
    if stage is Stage.TOPIC:
        stages.run_topic_generating(job, channel, providers.llm)
    elif stage is Stage.SCRIPT:
        stages.run_script_generating(job, channel, providers.llm)
    elif stage is Stage.POLICY:
        stages.run_policy_checking(job)
    elif stage is Stage.ASSET:
        # Same fenced-promote pattern as TTS/RENDER: search/select/download
        # into a worker-private temp root, then move each scene's asset to
        # the official assets/scene_i/ location only while the lease is
        # provably held. A fenced-out worker cleans up only its own temp tree
        # and never touches the official assets or the Asset table.
        official_root = storage.job_dir(job.id) / "assets"
        temp_root = storage.job_dir(job.id) / f"assets-inprogress-{uuid.uuid4().hex}"
        search_policy = (getattr(job, "provider_config", None) or {}).get("asset_search_policy")
        try:
            fetched_assets = stages.run_asset_fetching(
                job, providers.stock_media, storage, dest_root=temp_root, search_policy=search_policy,
            )
            if not assert_lease(session, job.id, lease_token):
                session.rollback()
                raise LeaseLostSignal(stage.value)
            promoted_assets: list[stages.AssetFetchResult] = []
            for fetched in fetched_assets:
                official_dir = official_root / fetched.local_path.parent.name
                official_dir.mkdir(parents=True, exist_ok=True)
                official_path = official_dir / fetched.local_path.name
                os.replace(fetched.local_path, official_path)
                promoted_assets.append(dataclasses.replace(fetched, local_path=official_path))
            context["assets"] = promoted_assets
            # Replace (not append) this job's Asset rows so a later resume
            # restores exactly the current attempt's assets. StageRun history
            # is unaffected. This is inside the same transaction _fenced_commit
            # (called by the caller after this returns) commits -- so on a lost
            # lease at that later point, session.rollback() undoes these writes
            # too, in addition to the assert_lease check already done above.
            session.execute(delete(Asset).where(Asset.job_id == job.id))
            for fetched in promoted_assets:
                session.add(
                    Asset(
                        job_id=job.id,
                        scene_index=fetched.scene_index,
                        source_provider=providers.stock_media.provider_id,
                        source_url=fetched.source_url,
                        author=fetched.author,
                        license_type=fetched.license_type,
                        local_path=str(fetched.local_path),
                        checksum_sha256=fetched.checksum_sha256,
                        mime_type=fetched.mime_type,
                    )
                )
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
    elif stage is Stage.TTS:
        # Same fenced-promote pattern as RENDER: synthesize (and validate/
        # normalize, for real providers) into a worker-private temp root, then
        # move each scene's audio to the official tts/scene_i/ location only
        # while the lease is provably held. A fenced-out worker cleans up only
        # its own temp tree and never touches the official audio.
        official_root = storage.job_dir(job.id) / "tts"
        temp_root = storage.job_dir(job.id) / f"tts-inprogress-{uuid.uuid4().hex}"
        try:
            results = stages.run_tts_generating(job, providers.tts, storage, dest_root=temp_root)
            if not assert_lease(session, job.id, lease_token):
                session.rollback()
                raise LeaseLostSignal(stage.value)
            promoted: list[TTSResult] = []
            for result in results:
                official_dir = official_root / result.audio_path.parent.name
                official_dir.mkdir(parents=True, exist_ok=True)
                official_path = official_dir / result.audio_path.name
                os.replace(result.audio_path, official_path)
                checksum = result.checksum_sha256 or hashlib.sha256(
                    official_path.read_bytes()
                ).hexdigest()
                promoted.append(
                    dataclasses.replace(result, audio_path=official_path, checksum_sha256=checksum)
                )
            context["tts_results"] = promoted
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
    elif stage is Stage.RENDER:
        # Render into a worker-private temp file, then promote it to the
        # official final.mp4 only while the lease is provably held (the guarded
        # UPDATE in assert_lease keeps SQLite's write lock until this stage's
        # commit), so a fenced-out worker can never clobber the new owner's
        # output. On lease loss only our own temp file is cleaned up.
        official_path = storage.job_dir(job.id) / "final" / "final.mp4"
        temp_path = official_path.parent / f"final-inprogress-{uuid.uuid4().hex}.mp4"
        render = stages.run_rendering(
            job, context["assets"], context["tts_results"], storage, output_path=temp_path,
        )
        if not assert_lease(session, job.id, lease_token):
            session.rollback()
            temp_path.unlink(missing_ok=True)
            raise LeaseLostSignal(stage.value)
        os.replace(temp_path, official_path)
        # The old manifest described the video that was just replaced -- remove
        # it immediately so a stale checksum/validation block can never be read
        # as describing the new render. A fresh manifest is written only after
        # VALIDATE succeeds.
        (official_path.parent.parent / "manifest.json").unlink(missing_ok=True)
        context["render"] = stages.RenderOutput(
            video_path=official_path, ffmpeg_version=render.ffmpeg_version,
            width=render.width, height=render.height,
        )
        storage.write_bytes_atomic(
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
        apply_transition(
            job, JobStatus.FAILED,
            failure_code=error.code, failure_summary=_persisted_error_text(error, 500),
        )
        return

    max_retries, backoffs = STAGE_RETRY_POLICY.get(stage, (0, []))
    if job.retry_count >= max_retries:
        apply_transition(
            job, JobStatus.FAILED,
            failure_code="RETRIES_EXHAUSTED",
            failure_summary=_persisted_error_text(
                f"{stage.value} failed after {job.retry_count} retries: {error}", 500,
            ),
        )
        return

    delay = backoffs[min(job.retry_count, len(backoffs) - 1)] if backoffs else 30
    job.retry_count += 1
    apply_transition(
        job, JobStatus.RETRY_WAIT,
        retry_target_stage=stage.value,
        next_retry_at=now + timedelta(seconds=delay),
        failure_code=error.code,
        failure_summary=_persisted_error_text(error, 500),
    )


def _next_attempt_number(session, job, stage: Stage) -> int:
    """Attempt numbering comes from the StageRun history itself, not from
    retry_count (which resets on reject/manual retry): re-running a stage for
    any reason always records max(previous attempt) + 1."""
    previous = session.execute(
        select(func.max(StageRun.attempt)).where(StageRun.job_id == job.id, StageRun.stage == stage.value),
    ).scalar_one()
    return (previous or 0) + 1


def _abandon_stage_run(session, stage_run_id: str, note: str) -> None:
    """Closes this worker's own (already committed) StageRun row after a lost
    lease. Touches only the stage_runs table -- never the fenced jobs row."""
    run = session.get(StageRun, stage_run_id)
    if run is not None and run.status == "running":
        run.status = "lease_lost"
        run.error_detail = note
        run.finished_at = _utcnow()
        session.commit()


def _fenced_commit(session, job, lease_token: str | None, stage: Stage) -> None:
    """Commits the pending stage result iff this worker still owns the lease.
    On a lost lease the transaction is rolled back untouched and LeaseLostSignal
    is raised -- no status change, no StageRun success, no manifest."""
    if not assert_lease(session, job.id, lease_token):
        session.rollback()
        raise LeaseLostSignal(stage.value)
    session.commit()


def _run_single_stage(session, job, channel, providers, storage, stage: Stage, context: dict,
                      lease_token: str | None = None) -> bool:
    """Runs one stage; returns True to keep advancing, False if the job landed in
    a stopping state (RETRY_WAIT / FAILED / REVIEW_REQUIRED / CANCELLED).
    Raises LeaseLostSignal if this worker's lease was reclaimed."""
    now = _utcnow()
    attempt = _next_attempt_number(session, job, stage)
    apply_transition(job, STAGE_ENTRY_STATUS[stage])
    job.current_stage = stage.value
    job.heartbeat_at = now
    stage_run = StageRun(
        job_id=job.id, stage=stage.value, attempt=attempt, status="running", started_at=now,
    )
    session.add(stage_run)
    _fenced_commit(session, job, lease_token, stage)
    stage_run_id = stage_run.id
    log_stage_event(job_id=job.id, stage=stage.value, attempt=attempt, event="stage_started")

    try:
        _execute_stage(session, stage, job, channel, providers, storage, context, lease_token)
    except LeaseLostSignal:
        # In-stage fence failure (TTS/RENDER promote): the worker-private temp
        # outputs were already cleaned by the stage; close our own StageRun and
        # stop without touching the job row.
        _abandon_stage_run(session, stage_run_id, "lease lost during stage execution")
        raise
    except ReviewRequiredSignal as signal:
        finished_at = _utcnow()
        stage_run.status = "review_required"
        stage_run.finished_at = finished_at
        apply_transition(job, JobStatus.REVIEW_REQUIRED, reason_code=signal.reason_code)
        job.retry_count = 0
        try:
            _fenced_commit(session, job, lease_token, stage)
        except LeaseLostSignal:
            _abandon_stage_run(session, stage_run_id, "lease lost before review_required commit")
            raise
        log_stage_event(
            job_id=job.id, stage=stage.value, attempt=attempt, event="stage_review_required",
            duration_ms=(finished_at - now).total_seconds() * 1000, error_code=signal.reason_code,
        )
        return False
    except PipelineError as error:
        finished_at = _utcnow()
        stage_run.status = "failed"
        stage_run.error_detail = _persisted_error_text(error, 2000)
        stage_run.finished_at = finished_at
        _handle_pipeline_error(job, stage, error, finished_at)
        try:
            _fenced_commit(session, job, lease_token, stage)
        except LeaseLostSignal:
            _abandon_stage_run(session, stage_run_id, "lease lost before failure commit")
            raise
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
        try:
            _fenced_commit(session, job, lease_token, stage)
        except LeaseLostSignal:
            _abandon_stage_run(session, stage_run_id, "lease lost before success commit")
            raise
        log_stage_event(
            job_id=job.id, stage=stage.value, attempt=attempt, event="stage_succeeded",
            duration_ms=(finished_at - now).total_seconds() * 1000,
        )
        return True


def _fail_resume(session, job, error: PipelineError, lease_token: str | None = None) -> None:
    """A resume could not even start (unsupported target stage or missing/corrupt
    prerequisite artifacts). The job moves to an explicit FAILED with the cause
    in failure_code/failure_summary -- never left ACTIVE, never crashed out of."""
    apply_transition(
        job, JobStatus.FAILED,
        failure_code=error.code, failure_summary=_persisted_error_text(error, 500),
    )
    if not assert_lease(session, job.id, lease_token):
        session.rollback()
        raise LeaseLostSignal("RESUME")
    session.commit()
    log_stage_event(
        job_id=job.id, stage=job.retry_target_stage or "RESUME", attempt=0,
        event="resume_failed", error_code=error.code,
    )


def _handle_unexpected_error(session, job, error: Exception, lease_token: str | None = None) -> None:
    """Last-resort safety boundary for non-PipelineError exceptions. Guarantees
    the job is never persisted as ACTIVE + unlocked: the session is rolled back,
    any running StageRun is closed as failed, and the job moves to FAILED with a
    stable failure code. The summary carries only the exception type and a short
    message -- no traceback, no request bodies. Never re-raises.

    If this worker's lease was reclaimed while it was failing, only its own
    StageRun row is closed -- the job now belongs to the new owner."""
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
    stage_run_id = stage_run.id if stage_run is not None else None
    stage_run_attempt = stage_run.attempt if stage_run is not None else 0
    if stage_run is not None:
        stage_run.status = "failed"
        stage_run.error_detail = f"{type(error).__name__}: {_persisted_error_text(error, 500)}"
        stage_run.finished_at = now

    summary = f"unexpected {type(error).__name__}: {_persisted_error_text(error, 300)}"
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

    # Single fenced commit: if the lease was reclaimed while this worker was
    # failing, drop every job mutation and only close our own StageRun row.
    if not assert_lease(session, job.id, lease_token):
        session.rollback()
        if stage_run_id is not None:
            _abandon_stage_run(session, stage_run_id, f"lease lost during {type(error).__name__}")
        log_stage_event(
            job_id=job.id, stage=job.current_stage or "NONE", attempt=stage_run_attempt,
            event="lease_lost", error_code="LEASE_LOST",
        )
        return
    session.commit()
    log_stage_event(
        job_id=job.id, stage=job.current_stage or "NONE",
        attempt=stage_run_attempt,
        event="stage_unexpected_error", error_code="UNEXPECTED_PIPELINE_ERROR",
    )


def run_job(session, job, channel, providers: ProviderBundle, storage,
            lease_token: str | None = None) -> None:
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

    When lease_token is given (the CLI worker always passes it), every job-state
    commit is fenced on the token: if the lease is reclaimed mid-run, this
    worker stops without writing any job state, manifest, or official output.
    """
    try:
        _run_job_impl(session, job, channel, providers, storage, lease_token)
    except LeaseLostSignal as signal:
        session.rollback()
        log_stage_event(
            job_id=job.id, stage=str(signal) or "UNKNOWN", attempt=0,
            event="lease_lost", error_code="LEASE_LOST",
        )
    except Exception as error:  # noqa: BLE001 - safety net: a job must never stay ACTIVE + unlocked
        _handle_unexpected_error(session, job, error, lease_token)


def _run_job_impl(session, job, channel, providers: ProviderBundle, storage,
                  lease_token: str | None = None) -> None:
    try:
        start_stage = _start_stage(job)
        full_order = [Stage.TOPIC, *STAGE_ORDER] if start_stage is Stage.TOPIC else STAGE_ORDER
        if start_stage not in full_order:
            raise UnsupportedResumeStageError(f"cannot resume from stage {start_stage.value}")
        context = _restore_context(session, job, providers, storage, full_order, start_stage)
    except LeaseLostSignal:
        raise
    except PipelineError as error:
        _fail_resume(session, job, error, lease_token)
        return

    remaining = full_order[full_order.index(start_stage):]
    for stage in remaining:
        if job.cancel_requested:
            if job.status != JobStatus.CANCELLED.value:
                apply_transition(job, JobStatus.CANCELLED)
                _fenced_commit(session, job, lease_token, stage)
            return
        if not _run_single_stage(session, job, channel, providers, storage, stage, context, lease_token):
            return

    final_video_checksum = hashlib.sha256(context["render"].video_path.read_bytes()).hexdigest()
    tts_results = context.get("tts_results") or []
    manifest = build_manifest(
        job, context["assets"], providers.tts.provider_id,
        (tts_results[0].voice_id if tts_results else TTS_VOICE_ID),
        render=context["render"], validation=context["validation"], final_video_checksum=final_video_checksum,
        tts_results=tts_results, tts_model=getattr(providers.tts, "model_id", None),
    )
    # Fence BEFORE writing the manifest file: assert_lease's guarded UPDATE
    # holds SQLite's write lock from here through the commit, so a takeover can
    # neither slip between the manifest write and the status transition nor be
    # overwritten by a fenced-out worker.
    if not assert_lease(session, job.id, lease_token):
        session.rollback()
        raise LeaseLostSignal("MANIFEST")
    write_manifest(storage, job.id, manifest)
    apply_transition(job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.USER_APPROVAL_REQUIRED.value)
    session.commit()
