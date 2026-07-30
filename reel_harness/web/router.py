from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from reel_harness.api.app import get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import ProviderConfigurationError, validate_provider_settings
from reel_harness.core.publish_service import (
    PublicationInvalidActionError,
    PublicationNotEligibleError,
    PublicationNotFoundError,
)
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.core.state_machine import JobStatus
from reel_harness.web.dependencies import (
    CSRF_COOKIE_NAME,
    ensure_csrf_cookie,
    get_templates,
    require_csrf,
)
from reel_harness.web.forms import ALLOWED_LANGUAGES, ALLOWED_STYLES, validate_new_job_form
from reel_harness.web.publication_forms import validate_create_publication_form
from reel_harness.web.publication_view_models import (
    build_publication_detail_view,
    build_publication_summary_view,
    build_publish_setup_view,
    build_publisher_account_view,
)
from reel_harness.web.view_models import (
    build_job_detail_view,
    build_job_summary_view,
    build_system_status_view,
)

router = APIRouter()

FINAL_VIDEO_REL_PATH = "final/final.mp4"
_FAKE_PROFILE_ENV_FLAG = "REEL_HARNESS_UI_SHOW_FAKE_PROFILE"

# The only three publisher ids a browser can ever connect an OAuth account
# for -- "fake" has no OAuth client at all and never appears here.
_REAL_PUBLISHER_PROVIDERS = ("youtube", "tiktok", "instagram")


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


def _require_real_publisher_provider(provider: str) -> None:
    if provider not in _REAL_PUBLISHER_PROVIDERS:
        raise HTTPException(status_code=404, detail=f"unknown publisher provider: {provider}")


def _publisher_credentials_configured(provider: str, settings) -> tuple[bool, str | None]:
    """Whether PROVIDER's OAuth client itself is configured -- the gate
    /connect checks before generating any PKCE/state at all. Small,
    deliberate duplication of publication_view_models._oauth_client_configured
    (a display-only helper for the accounts/publish-setup pages): this one
    is the actual precondition check for a mutating action, kept local to
    the route the same way _real_provider_readiness lives here rather than
    in view_models.py."""
    from reel_harness.config import (
        ProviderConfigurationError,
        validate_instagram_credentials_configured,
        validate_tiktok_credentials_configured,
        validate_youtube_credentials_configured,
    )

    validators = {
        "youtube": validate_youtube_credentials_configured,
        "tiktok": validate_tiktok_credentials_configured,
        "instagram": validate_instagram_credentials_configured,
    }
    try:
        validators[provider](settings)
    except ProviderConfigurationError as exc:
        return False, str(exc)
    return True, None


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
    publications = [
        build_publication_summary_view(p, job.topic) for p in ctx.publications.list_publications(job_id=job_id)
    ]
    return _render(request, "job_detail.html", {"job": view, "publications": publications})


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


# --- Publish setup and creation -----------------------------------------------


def _render_publish_setup_with_errors(
    request: Request, ctx: AppContext, job, submitted_provider: str, errors: dict, values: dict,
    status_code: int,
) -> HTMLResponse:
    setup = build_publish_setup_view(job, ctx.settings, ctx.credential_backend())
    return _render(request, "publish_setup.html", {
        "job": job, "setup": setup, "errors": errors, "submitted_provider": submitted_provider,
        "values": values,
    }, status_code=status_code)


@router.get("/jobs/{job_id}/publish", response_class=HTMLResponse, response_model=None)
def job_publish_setup(
    request: Request, job_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse | RedirectResponse:
    """Only reachable when the job is actually COMPLETED and currently
    eligible -- a direct hit otherwise redirects back to the job detail
    page, whose existing eligibility-reasons banner already explains why
    (e.g. Demo/Fake output is permanently ineligible), rather than
    rendering a blank or broken setup form."""
    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    if job.status != JobStatus.COMPLETED.value:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)
    eligibility = ctx.publications.check_eligibility(job_id)
    if not eligibility.eligible:
        return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)

    setup = build_publish_setup_view(job, ctx.settings, ctx.credential_backend())
    return _render(request, "publish_setup.html", {
        "job": job, "setup": setup, "errors": {}, "submitted_provider": None, "values": {},
    })


@router.post("/jobs/{job_id}/publications", dependencies=[Depends(require_csrf)], response_model=None)
def create_publication_web(
    request: Request, job_id: str,
    provider: str = Form(""), account_reference: str = Form(""), privacy_status: str = Form(""),
    confirm_public_upload: bool = Form(False), confirm_platform_options: bool = Form(False),
    ctx: AppContext = Depends(get_context),
) -> HTMLResponse | RedirectResponse:
    """Server-side validation (validate_create_publication_form) is a
    friendliness pass only -- PublicationService.create_publication remains
    the actual authority, and its exceptions (e.g. a race where the
    account got disconnected between page load and submit) are caught here
    and re-rendered the same way, never a raw 404/409 JSON body."""
    try:
        job = ctx.jobs.get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc

    submitted_values = {
        "account_reference": account_reference, "privacy_status": privacy_status,
        "confirm_public_upload": confirm_public_upload, "confirm_platform_options": confirm_platform_options,
    }

    result = validate_create_publication_form(
        provider, account_reference, privacy_status, confirm_public_upload, confirm_platform_options,
        settings=ctx.settings, credential_backend=ctx.credential_backend(),
    )
    if not result.ok or result.value is None:
        return _render_publish_setup_with_errors(
            request, ctx, job, provider, result.errors, submitted_values, status_code=422,
        )

    value = result.value
    from reel_harness.providers.registry import publisher_snapshot

    snapshot = publisher_snapshot(ctx.settings, value.provider, value.account_reference)
    try:
        pub, _eligibility = ctx.publications.create_publication(
            job_id, provider=value.provider, account_reference=value.account_reference,
            publisher_snapshot=snapshot, privacy_status=value.privacy_status,
            confirm_public_upload=value.confirm_public_upload,
            public_upload_enabled=ctx.settings.allow_public_upload,
            confirm_platform_options=value.confirm_platform_options,
        )
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}") from exc
    except PublicationNotEligibleError as exc:
        errors = {"_eligibility": "; ".join(exc.reasons)}
        return _render_publish_setup_with_errors(
            request, ctx, job, provider, errors, submitted_values, status_code=409,
        )
    except PublicationInvalidActionError as exc:
        errors = {"_general": str(exc)}
        return _render_publish_setup_with_errors(
            request, ctx, job, provider, errors, submitted_values, status_code=400,
        )

    return RedirectResponse(url=f"/publications/{pub.id}", status_code=303)


# --- Publications (list, detail, status polling) ------------------------------

_PUBLICATION_FILTERABLE_STATUSES = (
    None, "UPLOADING", "PROCESSING", "PUBLISHED", "FAILED", "CANCELLED",
)
_PUBLICATION_PAGE_SIZE = 20


def _job_topic(ctx: AppContext, job_id: str) -> str | None:
    try:
        return ctx.jobs.get_job(job_id).topic
    except JobNotFoundError:
        return None


@router.get("/publications", response_class=HTMLResponse)
def publications_list(
    request: Request, status_filter: str | None = None, provider: str | None = None, page: int = 1,
    ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    if status_filter not in _PUBLICATION_FILTERABLE_STATUSES:
        status_filter = None
    page = max(page, 1)
    offset = (page - 1) * _PUBLICATION_PAGE_SIZE
    pubs = ctx.publications.list_publications(
        status=status_filter, provider=provider or None, limit=_PUBLICATION_PAGE_SIZE, offset=offset,
    )
    total = ctx.publications.count_publications(status=status_filter, provider=provider or None)
    return _render(request, "publications_list.html", {
        "publications": [build_publication_summary_view(p, _job_topic(ctx, p.job_id)) for p in pubs],
        "status_filter": status_filter, "provider_filter": provider, "page": page,
        "has_next": offset + _PUBLICATION_PAGE_SIZE < total, "has_prev": page > 1, "total": total,
    })


def _publication_status_fragment(
    request: Request, publication_id: str, ctx: AppContext, reconcile_result: dict | None = None,
) -> HTMLResponse:
    try:
        pub = ctx.publications.get_publication(publication_id)
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}") from exc
    view = build_publication_detail_view(pub)
    # Reuses _render (not a bare TemplateResponse), same reason as
    # web.router._status_fragment for jobs: this fragment's own hidden
    # csrf_token form fields need a real value on every htmx-swapped
    # re-render, not just the page's first render.
    return _render(request, "fragments/publication_status.html", {
        "publication": view, "reconcile_result": reconcile_result,
    })


@router.get("/publications/{publication_id}", response_class=HTMLResponse)
def publication_detail(
    request: Request, publication_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    try:
        pub = ctx.publications.get_publication(publication_id)
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}") from exc
    view = build_publication_detail_view(pub)
    return _render(request, "publication_detail.html", {
        "publication": view, "job_topic": _job_topic(ctx, view.job_id),
    })


@router.get("/publications/{publication_id}/status", response_class=HTMLResponse)
def publication_status_fragment(
    request: Request, publication_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    return _publication_status_fragment(request, publication_id, ctx)


# --- Publication actions -------------------------------------------------------


@router.post(
    "/publications/{publication_id}/cancel", response_class=HTMLResponse, dependencies=[Depends(require_csrf)],
)
def publication_cancel(
    request: Request, publication_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    try:
        ctx.publications.cancel_publication(publication_id)
    except PublicationNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}") from exc
    except PublicationInvalidActionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _publication_status_fragment(request, publication_id, ctx)


@router.post(
    "/publications/{publication_id}/retry", response_class=HTMLResponse, dependencies=[Depends(require_csrf)],
)
def publication_retry(
    request: Request, publication_id: str, from_stage: str = Form(""), ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    """Mirrors api.app.retry_publication_endpoint's exact call shape (same
    core.publish_retry.retry_publication call, in-process instead of over
    HTTP)."""
    from reel_harness.core.publish_retry import PublicationRetryError, retry_publication
    from reel_harness.db.models import Publication

    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}")
        try:
            retry_publication(session, pub, ctx.storage, from_stage=from_stage or None)
        except PublicationRetryError as exc:
            raise HTTPException(status_code=409, detail={"reasons": exc.reasons}) from exc
    return _publication_status_fragment(request, publication_id, ctx)


@router.post(
    "/publications/{publication_id}/refresh", response_class=HTMLResponse, dependencies=[Depends(require_csrf)],
)
def publication_refresh(
    request: Request, publication_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    """Mirrors api.app.refresh_publication's exact lease/run/release
    sequence (re-polls one PROCESSING publication out of turn), in-process
    instead of over HTTP."""
    from reel_harness.db.models import Job, Publication
    from reel_harness.worker.publish_lease import lease_specific_publication, release_publication_lease
    from reel_harness.worker.publish_runner import run_publication

    with ctx.session_factory() as session:
        pub = session.get(Publication, publication_id)
        if pub is None:
            raise HTTPException(status_code=404, detail=f"publication not found: {publication_id}")
        if not lease_specific_publication(session, publication_id, worker_id="web-refresh"):
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
    return _publication_status_fragment(request, publication_id, ctx)


@router.post(
    "/publications/{publication_id}/reconcile", response_class=HTMLResponse, dependencies=[Depends(require_csrf)],
)
def publication_reconcile(
    request: Request, publication_id: str, ctx: AppContext = Depends(get_context),
) -> HTMLResponse:
    """Mirrors api.app.reconcile_publication_endpoint's exact call shape --
    a read-only confirmation against the provider, never starts a new
    upload (see core.publish_reconciliation). The outcome is threaded into
    the re-rendered status fragment so the user sees what reconcile found,
    not just a silent no-op."""
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
    return _publication_status_fragment(request, publication_id, ctx, reconcile_result=result.to_dict())


# --- Publisher accounts (OAuth connect/disconnect) ---------------------------


@router.get("/publisher-accounts", response_class=HTMLResponse)
def publisher_accounts_page(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    backend = ctx.credential_backend()
    accounts = [build_publisher_account_view(p, ctx.settings, backend) for p in _REAL_PUBLISHER_PROVIDERS]
    return _render(request, "publisher_accounts.html", {
        "accounts": accounts,
        "connected": request.query_params.get("connected"),
        "error": request.query_params.get("error"),
    })


def _build_youtube_authorization_url(request: Request, settings, state: str, pkce) -> str:
    from reel_harness.publisher.oauth_youtube import build_authorization_url

    redirect_uri = str(request.url_for("publisher_oauth_callback", provider="youtube"))
    return build_authorization_url(settings.youtube_client_id, redirect_uri, state, pkce)


def _build_tiktok_authorization_url(settings, state: str, pkce) -> str:
    from reel_harness.publisher.oauth_tiktok import build_authorization_url

    return build_authorization_url(
        settings.tiktok_client_key, settings.tiktok_redirect_uri, state, pkce, settings.tiktok_auth_url,
    )


def _build_instagram_authorization_url(settings, state: str, pkce) -> str:
    from reel_harness.publisher.oauth_instagram import build_authorization_url

    return build_authorization_url(
        settings.instagram_app_id, settings.instagram_redirect_uri, state, pkce, settings.instagram_auth_url,
    )


@router.post("/publisher-accounts/{provider}/connect", dependencies=[Depends(require_csrf)])
def publisher_connect(
    request: Request, provider: str, account_reference: str = Form("default"),
    ctx: AppContext = Depends(get_context),
) -> RedirectResponse:
    """Generates PKCE + a single-use `state`, stores them in the transient
    OAuthFlowStore (never a cookie, never the DB -- see
    publisher.oauth_flow_store), then 303-redirects the browser straight to
    the real provider's authorization page. YouTube's redirect_uri is
    computed per-request via url_for (Google's Desktop-app OAuth client type
    tolerates any port on 127.0.0.1, the same property the CLI's own
    ephemeral-port loopback flow already relies on); TikTok/Instagram use
    their existing configured settings verbatim, since those platforms
    require an exact pre-registered match -- see docs/OPERATIONS.md for what
    to register for the web flow to work."""
    _require_real_publisher_provider(provider)
    account_reference = account_reference.strip() or "default"

    configured, reason = _publisher_credentials_configured(provider, ctx.settings)
    if not configured:
        raise HTTPException(status_code=409, detail=reason)

    from reel_harness.publisher.oauth_common import generate_pkce

    pkce = generate_pkce()
    state = ctx.oauth_flow_store().create(provider, account_reference, pkce.verifier)

    if provider == "youtube":
        auth_url = _build_youtube_authorization_url(request, ctx.settings, state, pkce)
    elif provider == "tiktok":
        auth_url = _build_tiktok_authorization_url(ctx.settings, state, pkce)
    else:  # instagram
        auth_url = _build_instagram_authorization_url(ctx.settings, state, pkce)

    return RedirectResponse(url=auth_url, status_code=303)


def _redirect_to_accounts(*, connected: str | None = None, error: str | None = None) -> RedirectResponse:
    import urllib.parse

    params = {}
    if connected:
        params["connected"] = connected
    if error:
        params["error"] = error
    query = f"?{urllib.parse.urlencode(params)}" if params else ""
    return RedirectResponse(url=f"/publisher-accounts{query}", status_code=303)


def _exchange_youtube_code(request: Request, settings, code: str, verifier: str, account_reference: str):
    from datetime import UTC, datetime, timedelta

    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_youtube import YouTubeOAuthClient

    redirect_uri = str(request.url_for("publisher_oauth_callback", provider="youtube"))
    client = YouTubeOAuthClient(settings.youtube_client_id, settings.youtube_client_secret.get_secret_value())
    try:
        tokens = client.exchange_code(code, verifier, redirect_uri)
        identity = client.fetch_channel_identity(tokens.access_token)
    finally:
        client.close()
    now = datetime.now(UTC)
    return OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token,
        expires_at=now + timedelta(seconds=tokens.expires_in), scope=tokens.scope,
        provider="youtube", account_reference=account_reference,
        channel_id=identity.channel_id, channel_title=identity.title,
        created_at=now, last_refreshed_at=now,
    )


def _exchange_tiktok_code(settings, code: str, verifier: str, account_reference: str):
    from datetime import UTC, datetime, timedelta

    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_tiktok import TikTokOAuthClient

    client = TikTokOAuthClient(
        settings.tiktok_client_key, settings.tiktok_client_secret.get_secret_value(), settings.tiktok_token_url,
        connect_timeout=settings.tiktok_connect_timeout_seconds, read_timeout=settings.tiktok_read_timeout_seconds,
    )
    try:
        tokens = client.exchange_code(code, verifier, settings.tiktok_redirect_uri)
    finally:
        client.close()
    now = datetime.now(UTC)
    return OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token,
        expires_at=now + timedelta(seconds=tokens.expires_in),
        refresh_expires_at=(
            now + timedelta(seconds=tokens.refresh_expires_in) if tokens.refresh_expires_in is not None else None
        ),
        scope=tokens.scope, provider="tiktok", account_reference=account_reference,
        channel_id=tokens.open_id or None, channel_title=None,
        created_at=now, last_refreshed_at=now,
    )


def _exchange_instagram_code(settings, code: str, verifier: str, account_reference: str):
    from datetime import UTC, datetime, timedelta

    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_instagram import SCOPES, InstagramOAuthClient

    client = InstagramOAuthClient(
        settings.instagram_app_id, settings.instagram_app_secret.get_secret_value(),
        settings.instagram_token_url, settings.instagram_graph_url,
        connect_timeout=settings.instagram_connect_timeout_seconds,
        read_timeout=settings.instagram_read_timeout_seconds,
    )
    try:
        short_lived = client.exchange_code(code, verifier, settings.instagram_redirect_uri)
        long_lived = client.exchange_long_lived_token(short_lived.access_token)
        identity = client.fetch_account_identity(long_lived.access_token)
    finally:
        client.close()
    now = datetime.now(UTC)
    return OAuthCredential(
        access_token=long_lived.access_token, refresh_token=None,  # see InstagramOAuthClient's docstring
        expires_at=now + timedelta(seconds=long_lived.expires_in), scope=",".join(SCOPES),
        provider="instagram", account_reference=account_reference,
        channel_id=identity.account_id, channel_title=identity.username,
        created_at=now, last_refreshed_at=now,
    )


@router.get("/publisher-accounts/{provider}/callback", name="publisher_oauth_callback")
def publisher_oauth_callback(
    request: Request, provider: str, code: str | None = None, state: str | None = None,
    error: str | None = None, ctx: AppContext = Depends(get_context),
) -> RedirectResponse:
    """Deliberately has NO require_csrf dependency -- this route is reached
    by a top-level browser navigation FROM the OAuth provider's own domain,
    a cross-site navigation our SameSite=Strict rh_csrf cookie would never
    even be sent on regardless of what this route required. The OAuth
    `state` parameter (server-generated, single-use via OAuthFlowStore.pop,
    short-TTL, provider-bound) is the standard, correct CSRF-equivalent
    defense for an OAuth callback -- see docs/OPERATIONS.md's web UI
    security section."""
    _require_real_publisher_provider(provider)

    from reel_harness.observability import redact

    if error:
        return _redirect_to_accounts(error=f"{provider}:{redact(error) or error}")
    if not state:
        return _redirect_to_accounts(error=f"{provider}:missing_state")

    pending = ctx.oauth_flow_store().pop(state)
    if pending is None or pending.get("provider") != provider:
        return _redirect_to_accounts(error=f"{provider}:expired_or_invalid_connect_request")
    if not code:
        return _redirect_to_accounts(error=f"{provider}:missing_code")

    account_reference = pending["account_reference"]
    verifier = pending["verifier"]

    from reel_harness.core.errors import ProviderAuthError, TransientProviderError

    try:
        if provider == "youtube":
            credential = _exchange_youtube_code(request, ctx.settings, code, verifier, account_reference)
        elif provider == "tiktok":
            credential = _exchange_tiktok_code(ctx.settings, code, verifier, account_reference)
        else:  # instagram
            credential = _exchange_instagram_code(ctx.settings, code, verifier, account_reference)
    except ProviderAuthError as exc:
        return _redirect_to_accounts(error=f"{provider}:auth_failed:{redact(str(exc)) or 'unknown'}")
    except TransientProviderError as exc:
        return _redirect_to_accounts(error=f"{provider}:transient_error:{redact(str(exc)) or 'unknown'}")

    ctx.credential_backend().save_credential(credential)
    return _redirect_to_accounts(connected=provider)


@router.post("/publisher-accounts/{provider}/disconnect", dependencies=[Depends(require_csrf)])
def publisher_disconnect(
    provider: str, account_reference: str = Form(...), ctx: AppContext = Depends(get_context),
) -> RedirectResponse:
    """Local-only: deletes the saved credential from this machine's file
    store. Never revokes remote authorization at the platform -- matches
    the CLI's publisher-account-remove behavior exactly, stated explicitly
    in the accounts page's UI copy, not just here."""
    _require_real_publisher_provider(provider)
    ctx.credential_backend().revoke_credential(provider, account_reference)
    return _redirect_to_accounts()
