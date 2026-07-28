from __future__ import annotations

import os

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import text

from reel_harness.bootstrap import AppContext
from reel_harness.config import ProviderConfigurationError, validate_provider_settings
from reel_harness.core.publish_service import (
    PublicationInvalidActionError,
    PublicationNotEligibleError,
    PublicationNotFoundError,
)
from reel_harness.core.service import InvalidActionError, JobNotFoundError, asset_safe_metadata
from reel_harness.db.schema import SCHEMA_VERSION
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
    """Liveness: the process answers. Deep checks live in /readyz."""
    deps = check_ffmpeg_available()
    return {
        "status": "ok",
        "database": "ok",
        "ffmpeg_available": deps.ffmpeg_available,
        "ffprobe_available": deps.ffprobe_available,
    }


@app.get("/readyz")
def readyz(ctx: AppContext = Depends(get_context)) -> JSONResponse:
    """Readiness: DB reachable, schema supported, storage root usable, the
    selected provider's configuration valid (checked locally -- no network
    request to the provider), and the media toolchain resolved. Responses
    contain check names and short error classes only -- never secrets."""
    checks: dict[str, str] = {}
    ready = True

    try:
        with ctx.session_factory() as session:
            session.execute(text("SELECT 1"))
            version = session.execute(text("SELECT version FROM schema_migrations")).scalar_one()
        checks["database"] = "ok"
        if version == SCHEMA_VERSION:
            checks["schema"] = f"ok (v{version})"
        else:
            checks["schema"] = f"unsupported version {version} (expected {SCHEMA_VERSION})"
            ready = False
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        checks["database"] = f"error: {type(exc).__name__}"
        checks.setdefault("schema", "unknown")
        ready = False

    root = ctx.storage.root_dir
    if root.is_dir() and os.access(root, os.W_OK):
        checks["storage"] = "ok"
    else:
        checks["storage"] = "storage root missing or not writable"
        ready = False

    try:
        validate_provider_settings(ctx.settings)
        checks["provider"] = f"ok ({ctx.settings.llm_provider})"
    except ProviderConfigurationError as exc:
        checks["provider"] = f"invalid: {exc}"
        ready = False

    deps = check_ffmpeg_available()
    checks["ffmpeg"] = "ok" if deps.ffmpeg_available else "not found"
    checks["ffprobe"] = "ok" if deps.ffprobe_available else "not found"
    if not deps.all_available:
        ready = False

    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ready": ready, "checks": checks},
    )


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


@app.get("/v1/jobs/{job_id}/assets", dependencies=[Depends(require_api_key)])
def get_job_assets(job_id: str, ctx: AppContext = Depends(get_context)) -> list[dict]:
    """Safe per-scene asset metadata for the job's current attempt (provider,
    creator, license, dimensions, checksum prefix). Never a local filesystem
    path or a signed/temporary download URL -- see
    core.service.asset_safe_metadata."""
    try:
        assets = ctx.jobs.get_current_assets(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    return [asset_safe_metadata(a) for a in assets]


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


class CreatePublicationRequest(BaseModel):
    provider: str
    account_reference: str
    privacy_status: str = "private"
    confirm_public_upload: bool = False
    dry_run: bool = False


class PublicationResponse(BaseModel):
    publication_id: str | None = None
    job_id: str
    provider: str
    status: str
    privacy_status: str
    provider_video_id: str | None = None
    publication_url: str | None = None
    bytes_uploaded: int = 0
    total_bytes: int | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    dry_run: bool = False
    eligible: bool | None = None
    eligibility_reasons: list[str] = []


def _to_publication_response(pub, dry_run: bool = False, eligibility=None) -> PublicationResponse:
    return PublicationResponse(
        publication_id=pub.id if pub is not None else None,
        job_id=pub.job_id if pub is not None else "",
        provider=pub.provider if pub is not None else "",
        status=pub.status if pub is not None else "",
        privacy_status=pub.privacy_status if pub is not None else "private",
        provider_video_id=pub.provider_video_id if pub is not None else None,
        publication_url=pub.publication_url if pub is not None else None,
        bytes_uploaded=pub.bytes_uploaded if pub is not None else 0,
        total_bytes=pub.total_bytes if pub is not None else None,
        failure_code=pub.failure_code if pub is not None else None,
        failure_summary=pub.failure_summary if pub is not None else None,
        dry_run=dry_run,
        eligible=eligibility.eligible if eligibility is not None else None,
        eligibility_reasons=eligibility.reasons if eligibility is not None else [],
    )


@app.post(
    "/v1/jobs/{job_id}/publications", status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def create_publication(
    job_id: str, request: CreatePublicationRequest, ctx: AppContext = Depends(get_context),
) -> PublicationResponse:
    """Never blocks on the upload itself -- this only creates the Publication
    row (or, for dry_run, only re-checks eligibility) and returns
    immediately; a publisher worker performs the actual upload
    asynchronously (see worker.publish_runner). Never exposes a provider
    secret, an upload session URL, or a local filesystem path."""
    if request.dry_run:
        try:
            eligibility = ctx.publications.check_eligibility(job_id)
        except PublicationNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
        return _to_publication_response(None, dry_run=True, eligibility=eligibility)
    try:
        pub, eligibility = ctx.publications.create_publication(
            job_id, provider=request.provider, account_reference=request.account_reference,
            privacy_status=request.privacy_status, confirm_public_upload=request.confirm_public_upload,
            public_upload_enabled=ctx.settings.allow_public_upload,
        )
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except PublicationNotEligibleError as exc:
        raise HTTPException(
            status_code=409, detail={"eligible": False, "reasons": exc.reasons},
        ) from exc
    except PublicationInvalidActionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _to_publication_response(pub, eligibility=eligibility)


@app.get("/v1/publications/{publication_id}", dependencies=[Depends(require_api_key)])
def get_publication(publication_id: str, ctx: AppContext = Depends(get_context)) -> PublicationResponse:
    try:
        pub = ctx.publications.get_publication(publication_id)
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}") from exc
    return _to_publication_response(pub)


@app.post("/v1/publications/{publication_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_publication(publication_id: str, ctx: AppContext = Depends(get_context)) -> PublicationResponse:
    try:
        pub = ctx.publications.cancel_publication(publication_id)
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}") from exc
    except PublicationInvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_publication_response(pub)
