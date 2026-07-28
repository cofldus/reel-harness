from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from reel_harness.bootstrap import AppContext
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.media.deps import check_ffmpeg_available

app = FastAPI(title="Reel Harness API")
_ctx: AppContext | None = None


def get_context() -> AppContext:
    global _ctx
    if _ctx is None:
        _ctx = AppContext()
    return _ctx


def require_api_key(
    authorization: str | None = Header(default=None), ctx: AppContext = Depends(get_context),
) -> None:
    expected = f"Bearer {ctx.settings.app_api_key}"
    if authorization != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing or invalid API key")


class CreateJobRequest(BaseModel):
    channel_id: str
    idempotency_key: str
    topic: str | None = None


class JobResponse(BaseModel):
    job_id: str
    status: str
    current_stage: str | None
    idempotent_replay: bool = False
    # failure_code/failure_summary are persisted already-redacted (see
    # reel_harness.observability.redact), so echoing them here never exposes a
    # provider secret.
    failure_code: str | None = None
    failure_summary: str | None = None
    reason_code: str | None = None


def _to_response(job, idempotent_replay: bool = False) -> JobResponse:
    return JobResponse(
        job_id=job.id, status=job.status, current_stage=job.current_stage, idempotent_replay=idempotent_replay,
        failure_code=job.failure_code, failure_summary=job.failure_summary, reason_code=job.reason_code,
    )


@app.get("/healthz")
def healthz(ctx: AppContext = Depends(get_context)) -> dict:
    deps = check_ffmpeg_available()
    return {
        "status": "ok",
        "database": "ok",
        "ffmpeg_available": deps.ffmpeg_available,
        "ffprobe_available": deps.ffprobe_available,
    }


@app.post("/v1/jobs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_api_key)])
def create_job(request: CreateJobRequest, ctx: AppContext = Depends(get_context)) -> JobResponse:
    job, replay = ctx.jobs.create_job(request.channel_id, request.idempotency_key, request.topic)
    return _to_response(job, idempotent_replay=replay)


@app.get("/v1/jobs/{job_id}", dependencies=[Depends(require_api_key)])
def get_job(job_id: str, ctx: AppContext = Depends(get_context)) -> JobResponse:
    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    return _to_response(job)


@app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_job(job_id: str, ctx: AppContext = Depends(get_context)) -> JobResponse:
    try:
        job = ctx.jobs.request_cancel(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(job)


@app.post("/v1/jobs/{job_id}/approve", dependencies=[Depends(require_api_key)])
def approve_job(job_id: str, ctx: AppContext = Depends(get_context)) -> JobResponse:
    try:
        job = ctx.jobs.approve(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(job)
