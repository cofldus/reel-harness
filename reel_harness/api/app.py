from __future__ import annotations

import os
import time
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
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
# Set by ops.supervisor.Supervisor._start_api() when the API is running
# inside `reel-harness serve` -- used only to enrich /status with live
# component/fatal-error state; None when the API runs standalone (e.g.
# under a bare uvicorn invocation with no supervisor), which /status
# reports honestly rather than guessing.
_supervisor: object | None = None
_process_started_at = time.monotonic()


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


@app.get("/readyz/publisher")
def readyz_publisher(account: str = "default", ctx: AppContext = Depends(get_context)) -> JSONResponse:
    """Publisher-specific readiness: publication schema, credential-backend
    reachability, the selected account's token state, publisher registry,
    worker config, and the media toolchain -- all checked locally. Never
    makes a real YouTube API call (that's `publisher-doctor --check-remote`
    or `provider-smoke publisher youtube`); never exposes a secret, token,
    or local credential path."""
    checks: dict[str, str] = {}
    ready = True

    try:
        with ctx.session_factory() as session:
            session.execute(text("SELECT 1 FROM publications LIMIT 1"))
            session.execute(text("SELECT 1 FROM publication_audit_events LIMIT 1"))
        checks["publication_schema"] = "ok"
    except Exception as exc:  # noqa: BLE001 - readiness must report, not raise
        checks["publication_schema"] = f"error: {type(exc).__name__}"
        ready = False

    from reel_harness.publisher.secret_store import SecretStoreError

    backend = None
    try:
        backend = ctx.credential_backend()
        checks["credential_backend"] = "ok"
    except SecretStoreError as exc:
        checks["credential_backend"] = f"error: {exc}"
        ready = False

    if backend is not None:
        cred = backend.get_credential("youtube", account)
        if cred is None:
            checks["account_credential"] = f"not configured (account={account!r})"
        elif cred.invalid:
            checks["account_credential"] = "invalid -- last refresh failed, re-run publisher-auth"
            ready = False
        else:
            checks["account_credential"] = "ok"
            checks["refresh_token"] = "present" if cred.refresh_token else "missing"

    try:
        from reel_harness.providers.youtube_publisher import UPLOAD_ENDPOINT  # noqa: F401

        checks["publisher_registry"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["publisher_registry"] = f"error: {type(exc).__name__}"
        ready = False

    chunk_size = ctx.settings.youtube_upload_chunk_size
    checks["publication_worker_config"] = (
        "ok" if chunk_size > 0 and chunk_size % 262144 == 0 else "invalid upload chunk size"
    )
    if checks["publication_worker_config"] != "ok":
        ready = False
    checks["public_upload_feature_flag"] = str(ctx.settings.allow_public_upload)

    deps = check_ffmpeg_available()
    checks["ffmpeg"] = "ok" if deps.ffmpeg_available else "not found"
    checks["ffprobe"] = "ok" if deps.ffprobe_available else "not found"
    if not deps.all_available:
        ready = False

    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ready": ready, "checks": checks},
    )


def collect_status(ctx: AppContext) -> dict:
    """The DB-query/counting logic behind /status -- factored out so the web
    UI's dashboard/system-status pages can reuse the exact same counts
    in-process instead of re-deriving them (or making an HTTP self-call).
    Never a secret, token, script, prompt, or job topic/title -- only counts
    and status enum values."""
    from datetime import UTC, datetime, timedelta

    from reel_harness._version import __version__
    from reel_harness.core.state_machine import PUBLICATION_ACTIVE_STATUSES, JobStatus
    from reel_harness.db.models import Job, Publication
    from reel_harness.worker.policy import ACTIVE_STAGE_STATUSES

    with ctx.session_factory() as session:
        schema_version = session.execute(text("SELECT version FROM schema_migrations")).scalar_one_or_none()
        job_status_counts: dict[str, int] = {}
        for row in session.execute(text("SELECT status, COUNT(*) FROM jobs GROUP BY status")):
            job_status_counts[row[0]] = row[1]
        publication_status_counts: dict[str, int] = {}
        for row in session.execute(text("SELECT status, COUNT(*) FROM publications GROUP BY status")):
            publication_status_counts[row[0]] = row[1]

        cutoff = datetime.now(UTC) - timedelta(seconds=ctx.settings.lease_timeout_seconds)
        stale_job_leases = session.query(Job).filter(
            Job.locked_by.isnot(None), Job.status.in_([s.value for s in ACTIVE_STAGE_STATUSES]),
            Job.heartbeat_at < cutoff,
        ).count()
        stale_publication_leases = session.query(Publication).filter(
            Publication.locked_by.isnot(None),
            Publication.status.in_([s.value for s in PUBLICATION_ACTIVE_STATUSES]),
            Publication.heartbeat_at < cutoff,
        ).count()

    supervisor_status: dict | None = None
    if _supervisor is not None:
        supervisor_status = {
            "components": _supervisor.component_status(),  # type: ignore[attr-defined]
            "fatal_errors": dict(_supervisor.fatal_errors),  # type: ignore[attr-defined]
        }

    return {
        "version": __version__,
        "config_fingerprint": ctx.config_fingerprint(),
        "schema_version": schema_version,
        "schema_version_expected": SCHEMA_VERSION,
        "uptime_seconds": round(time.monotonic() - _process_started_at, 3),
        "job_status_counts": job_status_counts,
        "publication_status_counts": publication_status_counts,
        "queue_depth": job_status_counts.get(JobStatus.QUEUED.value, 0) + job_status_counts.get(
            JobStatus.RETRY_WAIT.value, 0,
        ),
        "stale_job_leases": stale_job_leases,
        "stale_publication_leases": stale_publication_leases,
        "supervisor": supervisor_status,
    }


@app.get("/status")
def get_status(ctx: AppContext = Depends(get_context)) -> dict:
    """Operational status for dashboards/incident triage -- version, config
    fingerprint, schema, uptime, job/publication status breakdowns, stale
    lease counts, and (only when running inside `reel-harness serve`) live
    component/fatal-error state."""
    return collect_status(ctx)


@app.get("/metrics")
def get_metrics(ctx: AppContext = Depends(get_context)) -> PlainTextResponse:
    """Prometheus text-exposition format, dependency-free (no
    prometheus_client package) -- see ops.metrics for what each metric
    means and why every value is derived fresh from DB state at scrape
    time rather than an in-memory counter. No job topic/title/script text
    is ever a metric value or label. No API key required, matching
    /healthz /readyz /status."""
    from reel_harness.ops.metrics import collect_metrics, render_prometheus_text

    samples = collect_metrics(ctx.session_factory)
    return PlainTextResponse(render_prometheus_text(samples), media_type="text/plain; version=0.0.4")


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


class RejectJobRequest(BaseModel):
    reason: str
    regenerate_from_stage: str


@app.post("/v1/jobs/{job_id}/reject", dependencies=[Depends(require_api_key)])
def reject_job(job_id: str, request: RejectJobRequest, ctx: AppContext = Depends(get_context)) -> JobResponse:
    try:
        job = ctx.jobs.reject(job_id, reason=request.reason, regenerate_from_stage=request.regenerate_from_stage)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(job)


class RetryJobRequest(BaseModel):
    stage: str


@app.post("/v1/jobs/{job_id}/retry", dependencies=[Depends(require_api_key)])
def retry_job(job_id: str, request: RetryJobRequest, ctx: AppContext = Depends(get_context)) -> JobResponse:
    try:
        job = ctx.jobs.retry_from_stage(job_id, stage=request.stage)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _to_response(job)


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int


@app.get("/v1/jobs", dependencies=[Depends(require_api_key)])
def list_jobs(
    status_filter: str | None = None, limit: int = 50, offset: int = 0, ctx: AppContext = Depends(get_context),
) -> JobListResponse:
    """`status_filter` (query param name `status_filter`, not `status`, to
    avoid colliding with FastAPI's own `status` module import in this file)
    matches one exact JobStatus value; omit for all statuses."""
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must not be negative")
    jobs = ctx.jobs.list_jobs(status=status_filter, limit=limit, offset=offset)
    total = ctx.jobs.count_jobs(status=status_filter)
    return JobListResponse(jobs=[_to_response(j) for j in jobs], total=total, limit=limit, offset=offset)


class CreatePublicationRequest(BaseModel):
    provider: str
    account_reference: str
    # None -> the provider's own most-restrictive default (see
    # providers.base.PublisherCapabilities.default_privacy); never a
    # hardcoded YouTube-shaped default.
    privacy_status: str | None = None
    confirm_public_upload: bool = False
    confirm_platform_options: bool = False
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
            confirm_platform_options=request.confirm_platform_options,
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


@app.post("/v1/publications/{publication_id}/refresh", dependencies=[Depends(require_api_key)])
def refresh_publication(publication_id: str, ctx: AppContext = Depends(get_context)) -> PublicationResponse:
    """Re-polls a single PROCESSING publication's status out of turn (see
    cli.main.cmd_publication_refresh). Briefly leases just this publication
    so a concurrent publisher-run daemon cycle can never race this request;
    if it isn't PROCESSING and unlocked, refuses with 409 rather than
    silently doing nothing."""
    from reel_harness.db.models import Job, Publication
    from reel_harness.worker.publish_lease import lease_specific_publication, release_publication_lease
    from reel_harness.worker.publish_runner import run_publication

    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}")
        if not lease_specific_publication(session, publication_id, worker_id="api-refresh"):
            raise HTTPException(
                status_code=409,
                detail=f"publication is not currently PROCESSING and unlocked (status={pub.status})",
            )
        lease_token = pub.lease_token
        job = session.get(Job, pub.job_id)
        channel_niche = ctx.channel_niche_for_job(job)
        bundle = ctx.bundle_for_publication(pub)
        try:
            run_publication(
                session, pub, ctx.storage, bundle, channel_niche=channel_niche, lease_token=lease_token,
            )
        finally:
            release_publication_lease(session, pub, lease_token=lease_token)
            close = getattr(bundle.publisher, "close", None)
            if callable(close):
                close()
    return _to_publication_response(pub)


@app.post("/v1/publications/{publication_id}/reconcile", dependencies=[Depends(require_api_key)])
def reconcile_publication_endpoint(publication_id: str, ctx: AppContext = Depends(get_context)) -> dict:
    """Confirms a publication's local state against the provider (recovering
    a provider_video_id the DB never committed, distinguishing an expired
    upload session from an incomplete one, etc.) via a read-only call --
    never starts a new upload. See core.publish_reconciliation for the full
    outcome list. Response contains only the outcome, safe reasons, and a
    provider_video_id -- no secret, token, or local path."""
    from reel_harness.core.publish_reconciliation import reconcile_publication
    from reel_harness.db.models import Publication

    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}")
        bundle = ctx.bundle_for_publication(pub)
        try:
            result = reconcile_publication(session, pub, bundle)
        finally:
            close = getattr(bundle.publisher, "close", None)
            if callable(close):
                close()
    return result.to_dict()


class RetryPublicationRequest(BaseModel):
    from_stage: str | None = None


@app.post("/v1/publications/{publication_id}/retry", dependencies=[Depends(require_api_key)])
def retry_publication_endpoint(
    publication_id: str, request: RetryPublicationRequest | None = None, ctx: AppContext = Depends(get_context),
) -> dict:
    """Manually retries a stuck publication (see core.publish_retry for the
    full policy -- eligibility and metadata-fingerprint are always re-
    verified first). Never uploads anything itself; a 409 with structured
    reasons means the retry was refused, not that anything failed."""
    from reel_harness.core.publish_retry import PublicationRetryError, retry_publication
    from reel_harness.db.models import Publication

    from_stage = request.from_stage if request is not None else None
    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}")
        try:
            result = retry_publication(session, pub, ctx.storage, from_stage=from_stage)
        except PublicationRetryError as exc:
            raise HTTPException(status_code=409, detail={"reasons": exc.reasons}) from exc
    return {"retried": True, **result.to_dict()}


class PublicationListResponse(BaseModel):
    publications: list[PublicationResponse]
    total: int
    limit: int
    offset: int


@app.get("/v1/publications", dependencies=[Depends(require_api_key)])
def list_publications(
    job_id: str | None = None, provider: str | None = None, status_filter: str | None = None,
    limit: int = 50, offset: int = 0, ctx: AppContext = Depends(get_context),
) -> PublicationListResponse:
    """`status_filter` (query param name `status_filter`, not `status`, to
    avoid colliding with FastAPI's own `status` module import in this file,
    same naming choice as GET /v1/jobs). Additive endpoint -- ctx.publications
    already supported all of this filtering; only limit/offset pagination
    and this HTTP surface are new (see PublicationService.list_publications/
    count_publications)."""
    if not (1 <= limit <= 200):
        raise HTTPException(status_code=422, detail="limit must be between 1 and 200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must not be negative")
    pubs = ctx.publications.list_publications(
        job_id=job_id, provider=provider, status=status_filter, limit=limit, offset=offset,
    )
    total = ctx.publications.count_publications(job_id=job_id, provider=provider, status=status_filter)
    return PublicationListResponse(
        publications=[_to_publication_response(p) for p in pubs], total=total, limit=limit, offset=offset,
    )


# --- Fable cinematic projects (Phase F4) -----------------------------------
# Every route here calls the SAME FableService methods the CLI already uses;
# no domain logic lives in this layer. The response models are deliberately
# explicit rather than serialized ORM objects, so a column added to
# StoryProject can never leak through the API by accident.


class FableProjectResponse(BaseModel):
    project_id: str
    title: str
    status: str
    aspect_ratio: str
    takes_per_shot: int | None = None
    idempotent_replay: bool = False
    failure_code: str | None = None
    failure_summary: str | None = None


class FableBudgetResponse(BaseModel):
    limit_amount: float | None
    currency: str | None
    spent_amount: float
    remaining_amount: float | None
    # Non-zero means `spent_amount` is a LOWER BOUND: some completed
    # generation reported no price. Surfaced rather than hidden.
    unpriced_take_count: int


class FableCharacterResponse(BaseModel):
    character_id: str
    name: str
    adult_confirmed: bool
    reference_approved: bool
    reference_images: dict
    reference_failure_code: str | None = None
    reference_failure_summary: str | None = None


class FableTakeResponse(BaseModel):
    take_id: str
    attempt_number: int
    status: str
    selected: bool
    cost_amount: float | None = None
    cost_currency: str | None = None


class FableShotResponse(BaseModel):
    shot_id: str
    shot_order: int
    status: str
    action: str | None = None
    duration_sec: float | None = None
    failure_code: str | None = None
    takes: list[FableTakeResponse]


class FableCostEstimateResponse(BaseModel):
    # False means `amount` is NOT a total -- see `detail`.
    known: bool
    amount: float | None
    currency: str | None
    shot_count: int
    unpriced_shot_count: int
    detail: str


class CreateFableProjectRequest(BaseModel):
    title: str
    source_text: str
    idempotency_key: str
    language: str = "ko"
    genre: str | None = None
    tone: str | None = None
    target_duration_sec: int = 60
    aspect_ratio: str = "9:16"
    takes_per_shot: int | None = None


class SetFableBudgetRequest(BaseModel):
    # None clears the limit, which re-closes the paid-generation gate and
    # un-spends nothing.
    limit_amount: float | None = None
    currency: str | None = None


def _to_fable_project(project, idempotent_replay: bool = False) -> FableProjectResponse:
    return FableProjectResponse(
        project_id=project.id, title=project.title, status=project.status,
        aspect_ratio=project.aspect_ratio, takes_per_shot=project.takes_per_shot,
        idempotent_replay=idempotent_replay,
        failure_code=project.failure_code, failure_summary=project.failure_summary,
    )


def _to_fable_character(character) -> FableCharacterResponse:
    return FableCharacterResponse(
        character_id=character.id, name=character.name,
        adult_confirmed=character.adult_confirmed,
        reference_approved=character.reference_approved,
        reference_images=character.reference_images or {},
        reference_failure_code=character.reference_failure_code,
        reference_failure_summary=character.reference_failure_summary,
    )


def _fable_project_or_404(ctx: AppContext, project_id: str):
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        return ctx.fable.get_project(project_id)
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"fable project not found: {project_id}") from exc


@app.post("/v1/fable/projects", status_code=status.HTTP_201_CREATED,
          dependencies=[Depends(require_api_key)])
def create_fable_project(
    request: CreateFableProjectRequest, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    try:
        project, replay = ctx.fable.create_project(
            title=request.title, source_text=request.source_text,
            idempotency_key=request.idempotency_key, language=request.language,
            genre=request.genre, tone=request.tone,
            target_duration_sec=request.target_duration_sec,
            aspect_ratio=request.aspect_ratio, takes_per_shot=request.takes_per_shot,
        )
    except InvalidActionError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _to_fable_project(project, idempotent_replay=replay)


@app.get("/v1/fable/projects", dependencies=[Depends(require_api_key)])
def list_fable_projects(ctx: AppContext = Depends(get_context)) -> list[FableProjectResponse]:
    return [_to_fable_project(p) for p in ctx.fable.list_projects()]


@app.get("/v1/fable/projects/{project_id}", dependencies=[Depends(require_api_key)])
def get_fable_project(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    return _to_fable_project(_fable_project_or_404(ctx, project_id))


@app.get("/v1/fable/projects/{project_id}/shots", dependencies=[Depends(require_api_key)])
def get_fable_shots(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> list[FableShotResponse]:
    _fable_project_or_404(ctx, project_id)
    return [
        FableShotResponse(
            shot_id=shot.id, shot_order=shot.shot_order, status=shot.status,
            action=shot.action, duration_sec=shot.duration_sec,
            failure_code=shot.failure_code,
            takes=[
                FableTakeResponse(
                    take_id=take.id, attempt_number=take.attempt_number, status=take.status,
                    selected=take.selected, cost_amount=take.cost_amount,
                    cost_currency=take.cost_currency,
                )
                for take in ctx.fable.shot_takes(shot.id)
            ],
        )
        for shot in ctx.fable.project_shots(project_id)
    ]


@app.get("/v1/fable/projects/{project_id}/characters", dependencies=[Depends(require_api_key)])
def get_fable_characters(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> list[FableCharacterResponse]:
    _fable_project_or_404(ctx, project_id)
    return [_to_fable_character(c) for c in ctx.fable.project_characters(project_id)]


@app.get("/v1/fable/projects/{project_id}/budget", dependencies=[Depends(require_api_key)])
def get_fable_budget(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableBudgetResponse:
    _fable_project_or_404(ctx, project_id)
    budget = ctx.fable.budget_status(project_id)
    return FableBudgetResponse(
        limit_amount=budget.limit_amount, currency=budget.currency,
        spent_amount=budget.spent_amount, remaining_amount=budget.remaining_amount,
        unpriced_take_count=budget.unpriced_take_count,
    )


@app.put("/v1/fable/projects/{project_id}/budget", dependencies=[Depends(require_api_key)])
def set_fable_budget(
    project_id: str, request: SetFableBudgetRequest, ctx: AppContext = Depends(get_context),
) -> FableBudgetResponse:
    _fable_project_or_404(ctx, project_id)
    try:
        ctx.fable.set_budget(project_id, request.limit_amount, request.currency)
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return get_fable_budget(project_id, ctx)


@app.get("/v1/fable/projects/{project_id}/estimate", dependencies=[Depends(require_api_key)])
def estimate_fable_cost(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableCostEstimateResponse:
    """Read-only: prices the shot plan, approves nothing, spends nothing."""
    _fable_project_or_404(ctx, project_id)
    try:
        estimate = ctx.fable.estimate_cost(project_id)
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return FableCostEstimateResponse(
        known=estimate.known, amount=estimate.amount, currency=estimate.currency,
        shot_count=estimate.shot_count, unpriced_shot_count=estimate.unpriced_shot_count,
        detail=estimate.detail,
    )


def _fable_action(ctx: AppContext, project_id: str, action, *args) -> FableProjectResponse:
    """Every project-level action shares one error contract: 404 for a
    project that does not exist, 409 for one the action is not valid on
    right now, and 502 for a provider that failed underneath."""
    from reel_harness.core.errors import PipelineError
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        return _to_fable_project(action(project_id, *args))
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"fable project not found: {project_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.code}: {exc}") from exc


@app.post("/v1/fable/projects/{project_id}/adapt", dependencies=[Depends(require_api_key)])
def adapt_fable_project(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    return _fable_action(ctx, project_id, ctx.fable.adapt_project)


@app.post("/v1/fable/projects/{project_id}/references", dependencies=[Depends(require_api_key)])
def generate_fable_references(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    """CASTING -> CHARACTER_REVIEW. Spends money on a paid tier, and is
    gated exactly as the CLI is (the double gate plus the project's
    budget) -- this route adds no new permission."""
    return _fable_action(ctx, project_id, ctx.fable.generate_references)


@app.post("/v1/fable/characters/{character_id}/approve", dependencies=[Depends(require_api_key)])
def approve_fable_reference(
    character_id: str, ctx: AppContext = Depends(get_context),
) -> FableCharacterResponse:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        return _to_fable_character(ctx.fable.approve_reference(character_id))
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"character not found: {character_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/v1/fable/characters/{character_id}/reject", dependencies=[Depends(require_api_key)])
def reject_fable_reference(
    character_id: str, ctx: AppContext = Depends(get_context),
) -> FableCharacterResponse:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        return _to_fable_character(ctx.fable.reject_reference(character_id))
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"character not found: {character_id}") from exc


class ApproveFableGateRequest(BaseModel):
    # The gate being approved, named explicitly so a client can never
    # advance a project by accident -- exactly like the CLI's --step.
    step: str


@app.post("/v1/fable/projects/{project_id}/approve", dependencies=[Depends(require_api_key)])
def approve_fable_gate(
    project_id: str, request: ApproveFableGateRequest, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    actions = {
        "story": ctx.fable.approve_story,
        "characters": ctx.fable.approve_characters,
        "shots": ctx.fable.approve_shots,
        "final": ctx.fable.approve_final,
    }
    action = actions.get(request.step)
    if action is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown step {request.step!r} (expected one of: {', '.join(sorted(actions))})",
        )
    return _fable_action(ctx, project_id, action)


@app.post("/v1/fable/takes/{take_id}/select", dependencies=[Depends(require_api_key)])
def select_fable_take(take_id: str, ctx: AppContext = Depends(get_context)) -> dict:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        shot = ctx.fable.select_take(take_id)
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"take not found: {take_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"shot_id": shot.id, "status": shot.status}


@app.post("/v1/fable/projects/{project_id}/render", dependencies=[Depends(require_api_key)])
def render_fable_final(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    from reel_harness.core.errors import PipelineError
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        ctx.fable.render_final(project_id)
    except FableProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"fable project not found: {project_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status_code=502, detail=f"{exc.code}: {exc}") from exc
    return _to_fable_project(ctx.fable.get_project(project_id))


@app.post("/v1/fable/projects/{project_id}/cancel", dependencies=[Depends(require_api_key)])
def cancel_fable_project(
    project_id: str, ctx: AppContext = Depends(get_context),
) -> FableProjectResponse:
    return _fable_action(ctx, project_id, ctx.fable.cancel_project)


# --- Web UI (reel_harness.web) ---------------------------------------------
# Mounted here (not inside reel_harness.web itself) so this module stays the
# single place that ever constructs FastAPI() -- ops.supervisor.Supervisor
# imports this module and uses `app` as-is, so anything registered on it at
# import time (this include_router/mount call, evaluated once when this
# module is first imported) is picked up automatically with zero supervisor
# changes needed.

@app.middleware("http")
async def _security_headers_middleware(request, call_next):
    """Applies to every response, /v1/* and the web UI alike -- CSP has no
    'unsafe-inline' for script/style since every script/style the web UI
    ships is a static file (see web/static/), never an inline <script> tag
    or a CDN reference."""
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "media-src 'self'; frame-ancestors 'none'"
    )
    return response


from reel_harness.web.router import router as _ui_router  # noqa: E402

app.include_router(_ui_router)

_static_dir = Path(__file__).resolve().parent.parent / "web" / "static"
if _static_dir.is_dir():
    app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")
