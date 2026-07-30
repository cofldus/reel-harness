from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from reel_harness.api.app import get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import ProviderConfigurationError, validate_provider_settings
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.core.state_machine import JobStatus
from reel_harness.web.dependencies import (
    CSRF_COOKIE_NAME,
    ensure_csrf_cookie,
    get_templates,
    require_csrf,
)
from reel_harness.web.forms import ALLOWED_LANGUAGES, ALLOWED_STYLES, validate_new_job_form
from reel_harness.web.view_models import (
    build_job_detail_view,
    build_job_summary_view,
    build_system_status_view,
)

router = APIRouter()

FINAL_VIDEO_REL_PATH = "final/final.mp4"
_FAKE_PROFILE_ENV_FLAG = "REEL_HARNESS_UI_SHOW_FAKE_PROFILE"


def _set_csrf_cookie(response, token: str) -> None:
    response.set_cookie(
        CSRF_COOKIE_NAME, token, samesite="strict", httponly=False, path="/",
    )


def _render(request: Request, name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    templates = get_templates()
    csrf_token = ensure_csrf_cookie(request)
    response = templates.TemplateResponse(
        request, name, {**context, "csrf_token": csrf_token}, status_code=status_code,
    )
    if request.cookies.get(CSRF_COOKIE_NAME) != csrf_token:
        _set_csrf_cookie(response, csrf_token)
    return response


def _real_provider_readiness(settings) -> tuple[bool, str | None]:
    """Whether the Real provider profile is actually usable right now -- an
    independent check against the SPECIFIC real provider names (openai-
    compatible LLM/TTS, Pexels assets), not just "no exception at current
    settings" (which fake/demo always trivially satisfy)."""
    real_settings = settings.model_copy(update={
        "llm_provider": "openai-compatible", "tts_provider": "openai-compatible", "asset_provider": "pexels",
    })
    try:
        validate_provider_settings(real_settings)
    except ProviderConfigurationError as exc:
        return False, str(exc)
    return True, None


def _fake_profile_visible(settings) -> bool:
    import os

    return os.environ.get(_FAKE_PROFILE_ENV_FLAG, "").strip().lower() in ("1", "true", "yes")


def _get_or_create_web_channel(ctx: AppContext, style: str, language: str):
    """One channel per (style, language) combination, reused across jobs --
    channels aren't a concept the 6-screen MVP UI exposes directly, so this
    keeps them from proliferating one-per-job while still giving
    ChannelContext (niche/language) something meaningful for prompt
    generation."""
    from sqlalchemy import select

    from reel_harness.db.models import Channel

    name = f"web-{style}-{language}"
    with ctx.session_factory() as session:
        existing = session.execute(select(Channel).where(Channel.name == name)).scalar_one_or_none()
        if existing is not None:
            session.expunge(existing)
            return existing
    return ctx.jobs.create_channel(name=name, niche=style, language=language)


def _provider_snapshot_for_profile(ctx: AppContext, profile: str) -> dict:
    from reel_harness.providers.registry import provider_snapshot

    name_map = {
        "demo": {"llm_provider": "demo", "tts_provider": "demo", "asset_provider": "demo"},
        "real": {
            "llm_provider": "openai-compatible", "tts_provider": "openai-compatible", "asset_provider": "pexels",
        },
        "fake": {"llm_provider": "fake", "tts_provider": "fake", "asset_provider": "fake"},
    }
    overridden = ctx.settings.model_copy(update=name_map[profile])
    return provider_snapshot(overridden)


# --- Pages -----------------------------------------------------------------


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    status_view = build_system_status_view(ctx)
    recent_jobs = [build_job_summary_view(j) for j in ctx.jobs.list_jobs(limit=5)]
    return _render(request, "dashboard.html", {"status": status_view, "recent_jobs": recent_jobs})


_FILTERABLE_STATUSES = (
    None, JobStatus.QUEUED.value, JobStatus.COMPLETED.value, JobStatus.FAILED.value,
    JobStatus.CANCELLED.value, JobStatus.REVIEW_REQUIRED.value,
)
_PAGE_SIZE = 20


@router.get("/jobs", response_class=HTMLResponse)
def job_list(
    request: Request, status_filter: str | None = None, page: int = 1,
    ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    if status_filter not in _FILTERABLE_STATUSES:
        status_filter = None
    page = max(page, 1)
    offset = (page - 1) * _PAGE_SIZE
    jobs = ctx.jobs.list_jobs(status=status_filter, limit=_PAGE_SIZE, offset=offset)
    total = ctx.jobs.count_jobs(status=status_filter)
    return _render(request, "jobs_list.html", {
        "jobs": [build_job_summary_view(j) for j in jobs],
        "status_filter": status_filter, "page": page,
        "has_next": offset + _PAGE_SIZE < total, "has_prev": page > 1, "total": total,
    })


@router.get("/jobs/new", response_class=HTMLResponse)
def new_job_form(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    real_ready, real_reason = _real_provider_readiness(ctx.settings)
    return _render(request, "job_new.html", {
        "languages": ALLOWED_LANGUAGES, "styles": ALLOWED_STYLES,
        "real_ready": real_ready, "real_reason": real_reason,
        "fake_visible": _fake_profile_visible(ctx.settings),
        "idempotency_key": str(uuid.uuid4()),
        "errors": {}, "values": {},
    })


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
def job_detail(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    stage_runs = ctx.jobs.get_stage_runs(job_id)
    assets = ctx.jobs.get_current_assets(job_id)
    eligibility = None
    if job.status == JobStatus.COMPLETED.value:
        eligibility = ctx.publications.check_eligibility(job_id)
    view = build_job_detail_view(job, stage_runs, assets, ctx.storage, eligibility=eligibility)
    return _render(request, "job_detail.html", {"job": view})


@router.get("/system", response_class=HTMLResponse)
def system_status(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _render(request, "system_status.html", {"status": build_system_status_view(ctx)})


@router.get("/settings", response_class=HTMLResponse)
def settings_guide(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    settings = ctx.settings
    real_ready, real_reason = _real_provider_readiness(settings)
    cards = [
        {"name": "Demo Mode", "configured": True, "detail": "API 키 불필요 -- 항상 사용 가능"},
        {
            "name": "OpenAI-compatible LLM", "configured": bool(settings.llm_api_key.get_secret_value()),
            "detail": "REEL_HARNESS_LLM_PROVIDER=openai_compatible, REEL_HARNESS_LLM_BASE_URL, "
                      "REEL_HARNESS_LLM_MODEL, REEL_HARNESS_LLM_API_KEY",
        },
        {
            "name": "OpenAI-compatible TTS", "configured": bool(settings.tts_api_key.get_secret_value()),
            "detail": "REEL_HARNESS_TTS_PROVIDER=openai_compatible, REEL_HARNESS_TTS_BASE_URL, "
                      "REEL_HARNESS_TTS_VOICE, REEL_HARNESS_TTS_API_KEY",
        },
        {
            "name": "Pexels (stock media)", "configured": bool(settings.asset_api_key.get_secret_value()),
            "detail": "REEL_HARNESS_ASSET_PROVIDER=pexels, REEL_HARNESS_ASSET_API_KEY",
        },
        {
            "name": "YouTube", "configured": bool(settings.youtube_client_secret.get_secret_value()),
            "detail": "REEL_HARNESS_YOUTUBE_CLIENT_ID, REEL_HARNESS_YOUTUBE_CLIENT_SECRET, "
                      "then `reel-harness publisher-auth youtube`",
        },
        {
            "name": "TikTok", "configured": bool(settings.tiktok_client_secret.get_secret_value()),
            "detail": "REEL_HARNESS_TIKTOK_CLIENT_KEY, REEL_HARNESS_TIKTOK_CLIENT_SECRET, "
                      "REEL_HARNESS_TIKTOK_REDIRECT_URI, then `reel-harness publisher-auth tiktok`",
        },
        {
            "name": "Instagram", "configured": bool(settings.instagram_app_secret.get_secret_value()),
            "detail": "REEL_HARNESS_INSTAGRAM_APP_ID, REEL_HARNESS_INSTAGRAM_APP_SECRET, "
                      "REEL_HARNESS_INSTAGRAM_REDIRECT_URI, then `reel-harness publisher-auth instagram`",
        },
    ]
    return _render(
        request, "settings.html", {"cards": cards, "real_ready": real_ready, "real_reason": real_reason},
    )


# --- Job creation ------------------------------------------------------------


@router.post("/jobs", dependencies=[Depends(require_csrf)], response_model=None)
def create_job(
    request: Request,
    topic: str = Form(""), language: str = Form(""), duration_seconds: int = Form(0),
    style: str = Form(""), provider_profile: str = Form(""),
    burn_subtitles: bool = Form(False), idempotency_key: str = Form(""),
    ctx: AppContext = Depends(get_context),
) -> HTMLResponse | RedirectResponse:
    # Every field defaults to an empty/zero value (never Form(...) / required)
    # so an empty submission reaches validate_new_job_form's own friendly
    # per-field error page instead of FastAPI's generic 422 JSON body --
    # "never trust the browser alone" means our own validation is what
    # actually decides accept/reject, not a framework-level required check.
    if not idempotency_key:
        idempotency_key = str(uuid.uuid4())
    result = validate_new_job_form(
        topic=topic, language=language, duration_seconds=duration_seconds, style=style,
        provider_profile=provider_profile, burn_subtitles=burn_subtitles,
    )
    if not result.ok or result.value is None:
        real_ready, real_reason = _real_provider_readiness(ctx.settings)
        return _render(request, "job_new.html", {
            "languages": ALLOWED_LANGUAGES, "styles": ALLOWED_STYLES,
            "real_ready": real_ready, "real_reason": real_reason,
            "fake_visible": _fake_profile_visible(ctx.settings),
            "idempotency_key": idempotency_key,
            "errors": result.errors,
            "values": {
                "topic": topic, "language": language, "duration_seconds": duration_seconds,
                "style": style, "provider_profile": provider_profile, "burn_subtitles": burn_subtitles,
            },
        }, status_code=422)

    value = result.value
    if value.provider_profile == "real":
        real_ready, _ = _real_provider_readiness(ctx.settings)
        if not real_ready:
            raise HTTPException(status_code=409, detail="real provider profile is not configured")
    if value.provider_profile == "fake" and not _fake_profile_visible(ctx.settings):
        raise HTTPException(status_code=403, detail="fake provider profile is not enabled")

    channel = _get_or_create_web_channel(ctx, value.style, value.language)
    snapshot = _provider_snapshot_for_profile(ctx, value.provider_profile)
    job, _replay = ctx.jobs.create_job(
        channel.id, idempotency_key=idempotency_key, topic=value.topic, provider_snapshot=snapshot,
    )
    return RedirectResponse(url=f"/jobs/{job.id}", status_code=303)


# --- Job actions (fragments) -------------------------------------------------


def _status_fragment(request: Request, job_id: str, ctx: AppContext) -> HTMLResponse:
    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    stage_runs = ctx.jobs.get_stage_runs(job_id)
    assets = ctx.jobs.get_current_assets(job_id)
    eligibility = None
    if job.status == JobStatus.COMPLETED.value:
        eligibility = ctx.publications.check_eligibility(job_id)
    view = build_job_detail_view(job, stage_runs, assets, ctx.storage, eligibility=eligibility)
    # Reuses _render (not a bare TemplateResponse) specifically so
    # csrf_token is threaded through here too -- this fragment is what
    # every htmx poll AND every cancel/approve/reject/retry action response
    # swaps into the page, so its own action forms' hidden csrf_token
    # fields need a real value on every render, not just the page's first
    # one (the no-JS <form> fallback POSTs that hidden field directly;
    # htmx's own requests are separately covered by the X-CSRF-Token header
    # set once on <body>, which is why a missing token here was previously
    # easy to miss under normal JS-enabled testing -- found by independent
    # review, reproduced, and fixed here).
    return _render(request, "fragments/job_status.html", {"job": view})


@router.get("/jobs/{job_id}/status", response_class=HTMLResponse)
def job_status_fragment(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    return _status_fragment(request, job_id, ctx)


@router.post("/jobs/{job_id}/cancel", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def job_cancel(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    try:
        ctx.jobs.request_cancel(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_fragment(request, job_id, ctx)


@router.post("/jobs/{job_id}/approve", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def job_approve(request: Request, job_id: str, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    try:
        ctx.jobs.approve(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_fragment(request, job_id, ctx)


@router.post("/jobs/{job_id}/reject", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def job_reject(
    request: Request, job_id: str, reason: str = Form(...), regenerate_from_stage: str = Form(...),
    ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    try:
        ctx.jobs.reject(job_id, reason=reason, regenerate_from_stage=regenerate_from_stage)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_fragment(request, job_id, ctx)


@router.post("/jobs/{job_id}/retry", response_class=HTMLResponse, dependencies=[Depends(require_csrf)])
def job_retry(
    request: Request, job_id: str, stage: str = Form(...), ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    try:
        ctx.jobs.retry_from_stage(job_id, stage=stage)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except InvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _status_fragment(request, job_id, ctx)


# --- Video streaming ---------------------------------------------------------


@router.get("/jobs/{job_id}/video")
def job_video(job_id: str, download: int = 0, ctx: AppContext = Depends(get_context)):
    import logging

    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc

    if not ctx.storage.exists(job_id, FINAL_VIDEO_REL_PATH):
        if job.status == JobStatus.COMPLETED.value:
            logging.getLogger(__name__).warning(
                "job %s is COMPLETED but final.mp4 is missing (storage inconsistency)", job_id,
            )
            raise HTTPException(status_code=404, detail="final video missing (storage inconsistency)")
        raise HTTPException(status_code=409, detail=f"video not yet available (status={job.status})")

    path = ctx.storage.path_for(job_id, FINAL_VIDEO_REL_PATH)
    disposition = "attachment" if download else "inline"
    return FileResponse(
        path, media_type="video/mp4",
        headers={"Content-Disposition": f'{disposition}; filename="{job_id}.mp4"'},
    )
