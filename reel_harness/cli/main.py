from __future__ import annotations

import argparse
import json
import signal
import sys
import uuid
from urllib.parse import urlsplit

from reel_harness.bootstrap import AppContext
from reel_harness.core.service import InvalidActionError, JobNotFoundError, asset_safe_metadata
from reel_harness.db.models import Channel
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.daemon import DaemonConfig, WorkerDaemon, default_worker_id
from reel_harness.worker.heartbeat import LeaseHeartbeat
from reel_harness.worker.lease import lease_next_job, recover_stale_jobs, release_lease
from reel_harness.worker.runner import run_job


def _print_job(job) -> None:
    print(json.dumps({
        "job_id": job.id,
        "status": job.status,
        "current_stage": job.current_stage,
        "retry_count": job.retry_count,
        "failure_code": job.failure_code,
        "failure_summary": job.failure_summary,
        "reason_code": job.reason_code,
    }, indent=2))


def cmd_doctor(args: argparse.Namespace, ctx: AppContext) -> int:
    deps = check_ffmpeg_available()
    print(json.dumps({
        "ffmpeg": {
            "available": deps.ffmpeg_available,
            "path": str(deps.ffmpeg.path) if deps.ffmpeg.path else None,
            "version": deps.ffmpeg.version,
            "source": deps.ffmpeg.source,
        },
        "ffprobe": {
            "available": deps.ffprobe_available,
            "path": str(deps.ffprobe.path) if deps.ffprobe.path else None,
            "version": deps.ffprobe.version,
            "source": deps.ffprobe.source,
        },
        "database_url": ctx.settings.database_url,
        "jobs_dir": str(ctx.settings.jobs_dir),
    }, indent=2))
    if not deps.all_available:
        print(
            "BLOCKED_DEPENDENCY: ffmpeg/ffprobe not found (checked "
            "REEL_HARNESS_FFMPEG_PATH/REEL_HARNESS_FFPROBE_PATH, "
            "./.tools/ffmpeg/bin, then PATH). RENDERING/VALIDATING stages "
            "will fail until one of those is satisfied (not done "
            "automatically -- see docs/STATUS.md).",
            file=sys.stderr,
        )
    return 0


def cmd_channel_create(args: argparse.Namespace, ctx: AppContext) -> int:
    channel = ctx.jobs.create_channel(name=args.name, niche=args.niche, language=args.language)
    print(json.dumps({"channel_id": channel.id, "name": channel.name}, indent=2))
    return 0


def cmd_job_create(args: argparse.Namespace, ctx: AppContext) -> int:
    idempotency_key = args.idempotency_key or str(uuid.uuid4())
    job, replay = ctx.jobs.create_job(
        channel_id=args.channel_id, idempotency_key=idempotency_key, topic=args.topic,
    )
    print(json.dumps({"job_id": job.id, "status": job.status, "idempotent_replay": replay}, indent=2))
    return 0


def cmd_job_show(args: argparse.Namespace, ctx: AppContext) -> int:
    try:
        job = ctx.jobs.get_job(args.job_id)
    except JobNotFoundError:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 1
    if args.json:
        # Machine-readable contract: stdout carries exactly one valid JSON
        # document, nothing else. Helper paths become fields, not extra lines.
        assets = [asset_safe_metadata(a) for a in ctx.jobs.get_current_assets(job.id)]
        payload = {
            "job_id": job.id,
            "status": job.status,
            "current_stage": job.current_stage,
            "retry_count": job.retry_count,
            "failure_code": job.failure_code,
            "failure_summary": job.failure_summary,
            "reason_code": job.reason_code,
            "preview_path": None,
            "manifest_path": None,
            "assets": assets,
        }
        if job.status == "REVIEW_REQUIRED":
            payload["preview_path"] = str(ctx.storage.job_dir(job.id) / "final" / "final.mp4")
            payload["manifest_path"] = str(ctx.storage.job_dir(job.id) / "manifest.json")
        print(json.dumps(payload, indent=2))
        return 0
    _print_job(job)
    if job.status == "REVIEW_REQUIRED":
        # Human-readable hints; kept off stdout-JSON via --json above.
        print(f"preview: {ctx.storage.job_dir(job.id) / 'final' / 'final.mp4'}", file=sys.stderr)
        print(f"manifest: {ctx.storage.job_dir(job.id) / 'manifest.json'}", file=sys.stderr)
    return 0


def cmd_job_list(args: argparse.Namespace, ctx: AppContext) -> int:
    jobs = ctx.jobs.list_jobs(status=args.status)
    rows = [{"job_id": j.id, "status": j.status, "current_stage": j.current_stage} for j in jobs]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_job_approve(args: argparse.Namespace, ctx: AppContext) -> int:
    try:
        job = ctx.jobs.approve(args.job_id)
    except (JobNotFoundError, InvalidActionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_job(job)
    return 0


def cmd_job_reject(args: argparse.Namespace, ctx: AppContext) -> int:
    try:
        job = ctx.jobs.reject(args.job_id, reason=args.reason, regenerate_from_stage=args.from_stage)
    except (JobNotFoundError, InvalidActionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_job(job)
    return 0


def cmd_job_cancel(args: argparse.Namespace, ctx: AppContext) -> int:
    try:
        job = ctx.jobs.request_cancel(args.job_id)
    except (JobNotFoundError, InvalidActionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_job(job)
    return 0


def cmd_job_retry(args: argparse.Namespace, ctx: AppContext) -> int:
    try:
        job = ctx.jobs.retry_from_stage(args.job_id, stage=args.stage)
    except (JobNotFoundError, InvalidActionError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_job(job)
    return 0


def cmd_worker_run_once(args: argparse.Namespace, ctx: AppContext) -> int:
    lease_timeout = args.lease_timeout or ctx.settings.lease_timeout_seconds
    with ctx.session_factory() as session:
        recover_stale_jobs(session, lease_timeout_seconds=lease_timeout)
        job = lease_next_job(session, worker_id=args.worker_id)
        if job is None:
            print("no job ready to lease")
            return 0
        channel = session.get(Channel, job.channel_id)
        lease_token = job.lease_token
        assert lease_token is not None  # minted by lease_next_job on every successful claim
        # The heartbeat runs on its own thread with its own sessions so a long
        # ffmpeg render or provider call cannot make a healthy job look stale.
        heartbeat = LeaseHeartbeat(
            ctx.session_factory, job.id, lease_token, ctx.settings.lease_heartbeat_seconds,
        )
        heartbeat.start()
        try:
            run_job(session, job, channel, ctx.providers_for_job(job), ctx.storage, lease_token=lease_token)
        finally:
            heartbeat.stop()
            release_lease(session, job, lease_token=lease_token)
    _print_job(job)
    return 0


def _print_publication(publication) -> None:
    print(json.dumps({
        "publication_id": publication.id,
        "job_id": publication.job_id,
        "provider": publication.provider,
        "status": publication.status,
        "privacy_status": publication.privacy_status,
        "provider_video_id": publication.provider_video_id,
        "publication_url": publication.publication_url,
        "bytes_uploaded": publication.bytes_uploaded,
        "total_bytes": publication.total_bytes,
        "failure_code": publication.failure_code,
        "failure_summary": publication.failure_summary,
    }, indent=2))


def cmd_publish_job(args: argparse.Namespace, ctx: AppContext) -> int:
    """Creates a Publication (or, with --dry-run, only reports eligibility
    and a metadata/config preview) for a COMPLETED job. Never uploads
    anything itself -- the actual upload is performed asynchronously by a
    publisher worker (publisher-run / publisher-run-once). --dry-run never
    makes an external request."""
    if args.dry_run:
        return _publish_job_dry_run(ctx, args)

    from reel_harness.core.publish_service import (
        PublicationInvalidActionError,
        PublicationNotEligibleError,
        PublicationNotFoundError,
    )
    from reel_harness.providers.registry import publisher_snapshot

    try:
        snapshot = publisher_snapshot(ctx.settings, args.provider, args.account)
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    try:
        publication, _ = ctx.publications.create_publication(
            args.job_id, provider=args.provider, account_reference=args.account,
            publisher_snapshot=snapshot, privacy_status=args.privacy,
            confirm_public_upload=args.confirm_public_upload,
            public_upload_enabled=ctx.settings.allow_public_upload,
        )
    except PublicationNotFoundError:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 1
    except PublicationNotEligibleError as exc:
        print(json.dumps({"eligible": False, "reasons": exc.reasons}, indent=2))
        return 1
    except PublicationInvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    _print_publication(publication)
    return 0


def _publish_job_dry_run(ctx: AppContext, args: argparse.Namespace) -> int:
    from reel_harness.core.publish_service import PublicationNotFoundError
    from reel_harness.pipeline.publish_metadata import build_publication_metadata

    try:
        eligibility = ctx.publications.check_eligibility(args.job_id)
    except PublicationNotFoundError:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 1

    credential_configured = True
    if args.provider == "youtube":
        client_ok = bool(
            ctx.settings.youtube_client_id and ctx.settings.youtube_client_secret.get_secret_value(),
        )
        credential_configured = client_ok and ctx.credential_backend().has_credential("youtube", args.account)

    metadata_preview = None
    if eligibility.manifest is not None:
        metadata = build_publication_metadata(
            eligibility.manifest, privacy_status=args.privacy,
            category_id=ctx.settings.youtube_category_id, made_for_kids=ctx.settings.youtube_made_for_kids,
        )
        tags_total_length = sum(len(t) for t in metadata.tags) + max(len(metadata.tags) - 1, 0)
        metadata_preview = {
            "title": metadata.title, "title_length": len(metadata.title),
            "description_length_bytes": len(metadata.description.encode("utf-8")),
            "tags": metadata.tags, "tags_total_length": tags_total_length,
            "category_id": metadata.category_id, "made_for_kids": metadata.made_for_kids,
        }

    video_file_size_bytes = None
    final_path = ctx.storage.job_dir(args.job_id) / "final" / "final.mp4"
    if final_path.is_file():
        video_file_size_bytes = final_path.stat().st_size

    public_requested = args.privacy == "public"
    public_upload_allowed = (
        not public_requested
        or (args.confirm_public_upload and ctx.settings.allow_public_upload)
    )

    payload = {
        "job_id": args.job_id, "provider": args.provider, "account_reference": args.account,
        "dry_run": True,
        "eligible": eligibility.eligible, "eligibility_reasons": eligibility.reasons,
        "requested_privacy_status": args.privacy,
        "public_upload_allowed": public_upload_allowed,
        "credential_configured": credential_configured,
        "metadata_preview": metadata_preview,
        "video_file_size_bytes": video_file_size_bytes,
        "upload_chunk_size_bytes": ctx.settings.youtube_upload_chunk_size,
    }
    print(json.dumps(payload, indent=2))
    ready = eligibility.eligible and credential_configured and public_upload_allowed
    return 0 if ready else 1


def cmd_publisher_run_once(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.db.models import Job
    from reel_harness.worker.publish_lease import (
        lease_next_publication,
        recover_stale_publications,
        release_publication_lease,
    )
    from reel_harness.worker.publish_runner import run_publication

    lease_timeout = args.lease_timeout or ctx.settings.lease_timeout_seconds
    with ctx.session_factory() as session:
        recover_stale_publications(session, lease_timeout_seconds=lease_timeout)
        publication = lease_next_publication(session, worker_id=args.worker_id)
        if publication is None:
            print("no publication ready to lease")
            return 0
        lease_token = publication.lease_token
        assert lease_token is not None  # minted by lease_next_publication on every successful claim
        job = session.get(Job, publication.job_id)
        channel_niche = ctx.channel_niche_for_job(job)
        bundle = ctx.bundle_for_publication(publication)
        try:
            run_publication(
                session, publication, ctx.storage, bundle, channel_niche=channel_niche, lease_token=lease_token,
            )
        finally:
            release_publication_lease(session, publication, lease_token=lease_token)
            close = getattr(bundle.publisher, "close", None)
            if callable(close):
                close()
    _print_publication(publication)
    return 0


def cmd_publication_status(args: argparse.Namespace, ctx: AppContext) -> int:
    """Read-only: never contacts the provider, just reports the current DB
    row. Use publication-refresh to actually re-poll."""
    from reel_harness.core.publish_service import PublicationNotFoundError

    try:
        publication = ctx.publications.get_publication(args.publication_id)
    except PublicationNotFoundError:
        print(f"publication not found: {args.publication_id}", file=sys.stderr)
        return 1
    _print_publication(publication)
    return 0


def cmd_publication_refresh(args: argparse.Namespace, ctx: AppContext) -> int:
    """Re-polls a single PROCESSING publication's status out of turn, without
    waiting for a publisher-run daemon's next cycle. Briefly leases just this
    publication (refuses if a worker already holds it or it isn't
    PROCESSING) so a concurrent daemon cycle can never race this command."""
    from reel_harness.db.models import Job, Publication
    from reel_harness.worker.publish_lease import lease_specific_publication, release_publication_lease
    from reel_harness.worker.publish_runner import run_publication

    with ctx.session_factory() as session:
        publication = session.get(Publication, args.publication_id)
        if publication is None:
            print(f"publication not found: {args.publication_id}", file=sys.stderr)
            return 1
        if not lease_specific_publication(session, args.publication_id, worker_id="cli-refresh"):
            print(
                f"publication {args.publication_id} is not currently PROCESSING and unlocked "
                f"(status={publication.status}) -- nothing to refresh",
                file=sys.stderr,
            )
            return 1
        lease_token = publication.lease_token
        assert lease_token is not None
        job = session.get(Job, publication.job_id)
        channel_niche = ctx.channel_niche_for_job(job)
        bundle = ctx.bundle_for_publication(publication)
        try:
            run_publication(
                session, publication, ctx.storage, bundle, channel_niche=channel_niche, lease_token=lease_token,
            )
        finally:
            release_publication_lease(session, publication, lease_token=lease_token)
            close = getattr(bundle.publisher, "close", None)
            if callable(close):
                close()
    _print_publication(publication)
    return 0


def _smoke_llm(ctx: AppContext) -> int:
    from reel_harness.config import normalize_provider_name
    from reel_harness.core.errors import (
        ProviderAuthError,
        SchemaValidationError,
        TransientProviderError,
    )
    from reel_harness.observability import redact
    from reel_harness.pipeline.script_schema import parse_script
    from reel_harness.providers.base import ChannelContext
    from reel_harness.providers.registry import resolve_llm_provider

    name = normalize_provider_name(ctx.settings.llm_provider)
    if name == "fake":
        print(
            "llm provider is 'fake' -- nothing to smoke. Set "
            "REEL_HARNESS_LLM_PROVIDER=openai_compatible (plus base URL, model, "
            "API key) to check a real provider.",
            file=sys.stderr,
        )
        return 2

    # Startup validation already guaranteed base URL/model/key are present.
    smoke_settings = ctx.settings.model_copy(update={"llm_max_retries": 0})
    provider = resolve_llm_provider(name, smoke_settings)
    try:
        result = provider.generate_script(
            "a 30 second kitchen tip",  # minimal, fixed topic: one request, no retries
            ChannelContext(channel_id="provider-smoke", niche="general", language="en", style_preset={}),
        )
        script = parse_script(result.raw_text)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient provider error (timeout/rate limit/5xx): {redact(str(exc))}", file=sys.stderr)
        return 4
    except SchemaValidationError as exc:
        print(f"response schema error (malformed/empty/refusal): {redact(str(exc))}", file=sys.stderr)
        return 5
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()

    print(json.dumps({
        "provider": result.provider_id,
        "model": result.model_id,
        "prompt_version": result.prompt_version,
        "request_id": result.request_id,
        "token_usage": result.usage,
        "script_title": script.title,
        "scene_count": len(script.scenes),
        "schema_valid": True,
    }, indent=2))
    return 0


def _smoke_tts(ctx: AppContext) -> int:
    import shutil
    import tempfile
    import time
    from pathlib import Path

    from reel_harness.config import normalize_provider_name
    from reel_harness.core.errors import (
        DependencyError,
        ProviderAuthError,
        TransientProviderError,
    )
    from reel_harness.media.tts_audio import wav_info
    from reel_harness.observability import redact
    from reel_harness.providers.registry import resolve_tts_provider

    name = normalize_provider_name(ctx.settings.tts_provider)
    if name == "fake":
        print(
            "tts provider is 'fake' -- nothing to smoke. Set "
            "REEL_HARNESS_TTS_PROVIDER=openai_compatible (plus base URL, model, "
            "voice, API key) to check a real provider.",
            file=sys.stderr,
        )
        return 2

    smoke_settings = ctx.settings.model_copy(update={"tts_max_retries": 0})
    provider = resolve_tts_provider(name, smoke_settings)
    scratch = Path(tempfile.mkdtemp(prefix="reel-harness-tts-smoke-"))
    try:
        started = time.monotonic()
        result = provider.synthesize(
            "This is a short configuration check.",  # fixed, safe, one request
            voice_id=ctx.settings.tts_voice, lang="en", dest_dir=scratch,
        )
        latency_ms = (time.monotonic() - started) * 1000
        info = wav_info(result.audio_path)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except DependencyError as exc:
        print(f"audio toolchain unavailable: {redact(str(exc))}", file=sys.stderr)
        return 5
    except TransientProviderError as exc:
        print(
            f"transient provider or audio-validation error: {redact(str(exc))}", file=sys.stderr,
        )
        return 4
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        shutil.rmtree(scratch, ignore_errors=True)  # never leave smoke audio behind

    print(json.dumps({
        "provider": result.provider_id,
        "model": getattr(provider, "model_id", None),
        "voice": result.voice_id,
        "format": getattr(provider, "audio_format", None),
        "request_id_present": result.request_id is not None,
        "duration_sec": round(result.duration_sec, 3),
        "sample_rate": info.sample_rate,
        "channels": info.channels,
        "codec": "pcm_s16le",
        "checksum_prefix": (result.checksum_sha256 or "")[:12],
        "latency_ms": round(latency_ms, 1),
        "audio_valid": True,
    }, indent=2))
    return 0


def _smoke_asset(ctx: AppContext) -> int:
    import shutil
    import tempfile
    import time
    from pathlib import Path

    from reel_harness.config import normalize_provider_name
    from reel_harness.core.errors import DependencyError, ProviderAuthError, TransientProviderError
    from reel_harness.observability import redact
    from reel_harness.pipeline.asset_selection import SelectionPolicy, select_asset
    from reel_harness.providers.registry import resolve_stock_media_provider

    name = normalize_provider_name(ctx.settings.asset_provider)
    if name == "fake":
        print(
            "asset provider is 'fake' -- nothing to smoke. Set "
            "REEL_HARNESS_ASSET_PROVIDER=pexels (plus base URL, API key) to "
            "check a real provider.",
            file=sys.stderr,
        )
        return 2

    smoke_settings = ctx.settings.model_copy(update={"asset_max_retries": 0})
    provider = resolve_stock_media_provider(name, smoke_settings)
    scratch = Path(tempfile.mkdtemp(prefix="reel-harness-asset-smoke-"))
    query = "ocean waves"  # fixed, safe, one search
    try:
        started = time.monotonic()
        candidates = provider.search(
            query, orientation=ctx.settings.asset_orientation,
            min_duration=ctx.settings.asset_min_duration_seconds,
            max_duration=ctx.settings.asset_max_duration_seconds, min_width=ctx.settings.asset_min_width,
            min_height=ctx.settings.asset_min_height, per_page=ctx.settings.asset_per_page,
            safe_search=ctx.settings.asset_safe_search,
        )
        policy = SelectionPolicy(
            min_width=ctx.settings.asset_min_width, min_height=ctx.settings.asset_min_height,
            min_duration_sec=ctx.settings.asset_min_duration_seconds,
            max_duration_sec=ctx.settings.asset_max_duration_seconds,
            target_orientation=ctx.settings.asset_orientation,
        )
        chosen = select_asset(candidates, policy)
        if chosen is None:
            print(
                f"no eligible candidates for {query!r} among {len(candidates)} search result(s)",
                file=sys.stderr,
            )
            return 6
        result = provider.download(chosen, scratch)
        latency_ms = (time.monotonic() - started) * 1000
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except DependencyError as exc:
        print(f"media toolchain unavailable: {redact(str(exc))}", file=sys.stderr)
        return 5
    except TransientProviderError as exc:
        print(f"transient provider or asset-validation error: {redact(str(exc))}", file=sys.stderr)
        return 4
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()
        shutil.rmtree(scratch, ignore_errors=True)  # never leave smoke asset files behind

    print(json.dumps({
        "provider": result.provider_id,
        "query": query,
        "result_count": len(candidates),
        "selected_asset_id": result.provider_asset_id,
        "width": result.width,
        "height": result.height,
        "duration_sec": round(result.duration_sec, 3) if result.duration_sec else None,
        "codec": "h264",
        "container": "mp4",
        "license_type": result.license_type,
        "creator": result.author,
        "source_page_host": urlsplit(result.source_page_url).netloc if result.source_page_url else None,
        "checksum_prefix": (result.checksum_sha256 or "")[:12],
        "request_id_present": result.request_id is not None,
        "latency_ms": round(latency_ms, 1),
        "asset_valid": True,
    }, indent=2))
    return 0


def cmd_publisher_auth(args: argparse.Namespace, ctx: AppContext) -> int:
    """Opt-in OAuth connect flow for a publisher account. Never runs without
    a configured OAuth client (REEL_HARNESS_YOUTUBE_CLIENT_ID/_SECRET); never
    prints an access token, refresh token, client secret, authorization
    code, or PKCE verifier -- only the resulting account/channel identity."""
    from datetime import UTC, datetime, timedelta

    from reel_harness.config import ProviderConfigurationError, validate_youtube_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_youtube import (
        LoopbackCallbackServer,
        OAuthCallbackError,
        YouTubeOAuthClient,
        build_authorization_url,
        generate_pkce,
        generate_state,
    )

    if args.provider != "youtube":  # pragma: no cover - argparse already restricts choices
        print(f"unsupported publisher provider: {args.provider}", file=sys.stderr)
        return 2
    try:
        validate_youtube_credentials_configured(ctx.settings)
    except ProviderConfigurationError as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2

    account = args.account or "default"
    pkce = generate_pkce()
    state = generate_state()
    server = LoopbackCallbackServer(expected_state=state, timeout_seconds=args.timeout)
    auth_url = build_authorization_url(ctx.settings.youtube_client_id, server.redirect_uri, state, pkce)

    print("Open this URL in a browser to authorize Reel Harness:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    try:
        import webbrowser

        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - best-effort only; the printed URL above is the real fallback
        pass

    try:
        code = server.wait_for_code()
    except OAuthCallbackError as exc:
        print(f"oauth callback failed: {exc}", file=sys.stderr)
        return 3

    client = YouTubeOAuthClient(
        ctx.settings.youtube_client_id, ctx.settings.youtube_client_secret.get_secret_value(),
    )
    try:
        tokens = client.exchange_code(code, pkce.verifier, server.redirect_uri)
        identity = client.fetch_channel_identity(tokens.access_token)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4
    finally:
        client.close()

    ctx.credential_backend().save_credential(OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token,
        expires_at=datetime.now(UTC) + timedelta(seconds=tokens.expires_in),
        scope=tokens.scope, provider="youtube", account_reference=account,
        channel_id=identity.channel_id, channel_title=identity.title,
    ))

    print(json.dumps({
        "provider": "youtube",
        "account_reference": account,
        "channel_id": identity.channel_id,
        "channel_title": identity.title,
        "has_refresh_token": tokens.refresh_token is not None,
    }, indent=2))
    return 0


def _smoke_publisher_youtube(
    ctx: AppContext, account: str, upload_private_test: bool, confirm_test_upload: bool,
) -> int:
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.observability import redact

    if not ctx.settings.youtube_client_id or not ctx.settings.youtube_client_secret.get_secret_value():
        print(
            "youtube publisher OAuth client not configured -- set REEL_HARNESS_YOUTUBE_CLIENT_ID "
            "and REEL_HARNESS_YOUTUBE_CLIENT_SECRET.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    backend = ctx.credential_backend()
    if not backend.has_credential("youtube", account):
        print(
            f"no saved youtube credential for account {account!r} -- run "
            f"`reel-harness publisher-auth youtube --account {account}` first.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    from reel_harness.providers.registry import _resolve_fresh_youtube_access_token
    from reel_harness.publisher.oauth_youtube import YouTubeOAuthClient

    try:
        access_token = _resolve_fresh_youtube_access_token(ctx.settings, backend, account)
        client = YouTubeOAuthClient(
            ctx.settings.youtube_client_id, ctx.settings.youtube_client_secret.get_secret_value(),
        )
        try:
            identity = client.fetch_channel_identity(access_token)
        finally:
            client.close()
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4

    summary: dict = {
        "provider": "youtube", "account_reference": account,
        "channel_id": identity.channel_id, "channel_title": identity.title,
        "upload_permission_checked": False, "test_upload": None,
    }

    if upload_private_test and confirm_test_upload:
        summary["upload_permission_checked"] = True
        summary["test_upload"] = _run_publisher_test_upload(ctx, account, access_token)
    elif upload_private_test or confirm_test_upload:
        print(
            "--upload-private-test and --confirm-test-upload must both be given to run the "
            "opt-in test upload -- read-only channel identity check only.",
            file=sys.stderr,
        )

    print(json.dumps(summary, indent=2))
    return 0


def _run_publisher_test_upload(ctx: AppContext, account: str, access_token: str) -> dict:
    import hashlib
    import shutil
    import tempfile
    import uuid
    from pathlib import Path

    from reel_harness.media.deps import check_ffmpeg_available
    from reel_harness.media.runner import run
    from reel_harness.providers.base import PublicationMetadata
    from reel_harness.providers.youtube_publisher import YouTubePublisher

    deps = check_ffmpeg_available()
    if not deps.all_available:
        return {"ran": False, "reason": "ffmpeg/ffprobe not available"}

    scratch = Path(tempfile.mkdtemp(prefix="reel-harness-publisher-smoke-"))
    try:
        video_path = scratch / "smoke.mp4"
        argv = [
            str(deps.ffmpeg.path), "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart",
            str(video_path),
        ]
        result = run(argv, timeout=30)
        if result.returncode != 0:
            return {"ran": False, "reason": "failed to build the local test clip"}

        video_bytes = video_path.read_bytes()
        publisher = YouTubePublisher(
            access_token_provider=lambda: access_token, chunk_size=ctx.settings.youtube_upload_chunk_size,
            connect_timeout=10.0, read_timeout=60.0, max_retries=0,
        )
        try:
            metadata = PublicationMetadata(
                title="[reel-harness provider-smoke test upload]",
                description="Automated connectivity test upload from reel-harness provider-smoke. "
                            "Safe to delete.",
                tags=[], category_id=ctx.settings.youtube_category_id, privacy_status="private",
                made_for_kids=ctx.settings.youtube_made_for_kids,
            )
            session = publisher.create_upload_session(
                metadata, len(video_bytes), "video/mp4", str(uuid.uuid4()),
            )
            chunk_result = publisher.upload_chunk(session, video_bytes, 0, len(video_bytes))
        finally:
            publisher.close()

        return {
            "ran": True,
            "provider_video_id": chunk_result.provider_video_id,
            "privacy_status": "private",
            "checksum_prefix": hashlib.sha256(video_bytes).hexdigest()[:12],
            "note": "remote deletion is never automatic -- see docs/OPERATIONS.md",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def cmd_provider_smoke(args: argparse.Namespace, ctx: AppContext) -> int:
    """Opt-in operator check of a configured real provider: one request with
    retries disabled, real validation, secrets redacted, scratch files cleaned.
    The default test suites never run this."""
    if args.target == "llm":
        return _smoke_llm(ctx)
    if args.target == "asset":
        return _smoke_asset(ctx)
    if args.target == "publisher":
        if args.publisher_provider != "youtube":
            print("usage: provider-smoke publisher youtube [--account ALIAS] [...]", file=sys.stderr)
            return 2
        return _smoke_publisher_youtube(
            ctx, account=args.account or "default",
            upload_private_test=args.upload_private_test, confirm_test_upload=args.confirm_test_upload,
        )
    return _smoke_tts(ctx)


def cmd_worker_run(args: argparse.Namespace, ctx: AppContext) -> int:
    settings = ctx.settings
    config = DaemonConfig(
        worker_id=args.worker_id or default_worker_id(),
        poll_interval_seconds=(
            args.poll_interval if args.poll_interval is not None else settings.worker_poll_interval_seconds
        ),
        lease_timeout_seconds=args.lease_timeout or settings.lease_timeout_seconds,
        heartbeat_interval_seconds=(
            args.heartbeat_interval if args.heartbeat_interval is not None
            else settings.lease_heartbeat_seconds
        ),
        max_jobs=args.max_jobs if args.max_jobs is not None else settings.worker_max_jobs,
        idle_exit_after_seconds=(
            args.idle_exit_after if args.idle_exit_after is not None
            else settings.worker_idle_exit_after_seconds
        ),
        stop_on_error=args.stop_on_error or settings.worker_stop_on_error,
    )
    daemon = WorkerDaemon(
        ctx.session_factory, ctx.storage, ctx.providers_for_job, config,
    )

    def _signal_handler(signum, frame) -> None:  # pragma: no cover - exercised via CLI, not pytest
        daemon.request_stop(f"signal_{signum}")

    # Ctrl+C / SIGINT everywhere; SIGTERM where the platform delivers it;
    # SIGBREAK for Windows console Ctrl+Break. A hard console close on Windows
    # cannot be intercepted -- stale-lease recovery covers that case.
    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    return daemon.run()


def cmd_publisher_run(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.worker.publish_daemon import (
        PublisherDaemon,
        PublisherDaemonConfig,
        default_publisher_worker_id,
    )

    settings = ctx.settings
    config = PublisherDaemonConfig(
        worker_id=args.worker_id or default_publisher_worker_id(),
        poll_interval_seconds=(
            args.poll_interval if args.poll_interval is not None else settings.worker_poll_interval_seconds
        ),
        lease_timeout_seconds=args.lease_timeout or settings.lease_timeout_seconds,
        max_publications=args.max_publications if args.max_publications is not None else None,
        idle_exit_after_seconds=args.idle_exit_after,
        stop_on_error=args.stop_on_error,
    )
    daemon = PublisherDaemon(
        ctx.session_factory, ctx.storage, ctx.bundle_for_publication, ctx.channel_niche_for_job, config,
    )

    def _signal_handler(signum, frame) -> None:  # pragma: no cover - exercised via CLI, not pytest
        daemon.request_stop(f"signal_{signum}")

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    return daemon.run()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reel-harness")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    channel_create = sub.add_parser("channel-create")
    channel_create.add_argument("--name", required=True)
    channel_create.add_argument("--niche", required=True)
    channel_create.add_argument("--language", required=True)
    channel_create.set_defaults(func=cmd_channel_create)

    job_create = sub.add_parser("job-create")
    job_create.add_argument("--channel-id", required=True)
    job_create.add_argument("--idempotency-key", default=None)
    job_create.add_argument("--topic", default=None)
    job_create.set_defaults(func=cmd_job_create)

    job_show = sub.add_parser("job-show")
    job_show.add_argument("job_id")
    job_show.add_argument("--json", action="store_true",
                          help="Emit exactly one JSON document on stdout (for automation)")
    job_show.set_defaults(func=cmd_job_show)

    job_list = sub.add_parser("job-list")
    job_list.add_argument("--status", default=None)
    job_list.set_defaults(func=cmd_job_list)

    job_approve = sub.add_parser("job-approve")
    job_approve.add_argument("job_id")
    job_approve.set_defaults(func=cmd_job_approve)

    job_reject = sub.add_parser("job-reject")
    job_reject.add_argument("job_id")
    job_reject.add_argument("--reason", required=True)
    job_reject.add_argument("--from-stage", required=True, help="Stage enum value, e.g. SCRIPT or ASSET")
    job_reject.set_defaults(func=cmd_job_reject)

    job_cancel = sub.add_parser("job-cancel")
    job_cancel.add_argument("job_id")
    job_cancel.set_defaults(func=cmd_job_cancel)

    job_retry = sub.add_parser("job-retry")
    job_retry.add_argument("job_id")
    job_retry.add_argument("--stage", required=True, help="Stage enum value to resume from")
    job_retry.set_defaults(func=cmd_job_retry)

    worker_run_once = sub.add_parser("worker-run-once")
    worker_run_once.add_argument("--worker-id", default="cli-worker")
    worker_run_once.add_argument(
        "--lease-timeout", type=int, default=None,
        help="Seconds before a locked job with no heartbeat is considered stale "
             "(default: settings.lease_timeout_seconds)",
    )
    worker_run_once.set_defaults(func=cmd_worker_run_once)

    worker_run = sub.add_parser("worker-run", help="Continuous polling worker daemon")
    worker_run.add_argument("--worker-id", default=None, help="Default: generated unique id")
    worker_run.add_argument("--poll-interval", type=float, default=None,
                            help="Seconds to wait when no job is leasable")
    worker_run.add_argument("--lease-timeout", type=int, default=None)
    worker_run.add_argument("--heartbeat-interval", type=float, default=None)
    worker_run.add_argument("--max-jobs", type=int, default=None,
                            help="Exit normally after processing this many jobs")
    worker_run.add_argument("--idle-exit-after", type=float, default=None,
                            help="Exit normally after this many idle seconds")
    worker_run.add_argument("--stop-on-error", action="store_true",
                            help="Exit (code 1) after the first job that ends FAILED")
    worker_run.set_defaults(func=cmd_worker_run)

    publish_job = sub.add_parser(
        "publish-job", help="Create a Publication for a COMPLETED job (upload happens asynchronously)",
    )
    publish_job.add_argument("job_id")
    publish_job.add_argument("--provider", default="youtube", choices=["youtube", "fake"])
    publish_job.add_argument("--account", default="default", help="Account alias (default: 'default')")
    publish_job.add_argument("--privacy", default="private", choices=["private", "unlisted", "public"])
    publish_job.add_argument(
        "--confirm-public-upload", action="store_true",
        help="Required alongside --privacy public (and the allow-public-upload feature flag)",
    )
    publish_job.add_argument(
        "--dry-run", action="store_true",
        help="Report eligibility, metadata preview, and config readiness only -- no publication is created "
             "and no external request is made",
    )
    publish_job.set_defaults(func=cmd_publish_job)

    publisher_run_once = sub.add_parser(
        "publisher-run-once", help="Lease and run one publication, then exit",
    )
    publisher_run_once.add_argument("--worker-id", default="cli-publisher-worker")
    publisher_run_once.add_argument(
        "--lease-timeout", type=int, default=None,
        help="Seconds before a locked publication with no heartbeat is considered stale "
             "(default: settings.lease_timeout_seconds)",
    )
    publisher_run_once.set_defaults(func=cmd_publisher_run_once)

    publisher_run = sub.add_parser("publisher-run", help="Continuous polling publisher worker daemon")
    publisher_run.add_argument("--worker-id", default=None, help="Default: generated unique id")
    publisher_run.add_argument("--poll-interval", type=float, default=None,
                               help="Seconds to wait when no publication is leasable")
    publisher_run.add_argument("--lease-timeout", type=int, default=None)
    publisher_run.add_argument("--max-publications", type=int, default=None,
                               help="Exit normally after processing this many publications")
    publisher_run.add_argument("--idle-exit-after", type=float, default=None,
                               help="Exit normally after this many idle seconds")
    publisher_run.add_argument("--stop-on-error", action="store_true",
                               help="Exit (code 1) after the first publication that ends FAILED")
    publisher_run.set_defaults(func=cmd_publisher_run)

    publication_status = sub.add_parser(
        "publication-status", help="Read-only: show a publication's current DB state",
    )
    publication_status.add_argument("publication_id")
    publication_status.set_defaults(func=cmd_publication_status)

    publication_refresh = sub.add_parser(
        "publication-refresh", help="Re-poll one PROCESSING publication's status out of turn",
    )
    publication_refresh.add_argument("publication_id")
    publication_refresh.set_defaults(func=cmd_publication_refresh)

    provider_smoke = sub.add_parser(
        "provider-smoke", help="One real request against the configured provider (opt-in)",
    )
    provider_smoke.add_argument("target", choices=["llm", "tts", "asset", "publisher"])
    provider_smoke.add_argument(
        "publisher_provider", nargs="?", default=None, choices=["youtube", None],
        help="Required when target=publisher, e.g. 'provider-smoke publisher youtube'",
    )
    provider_smoke.add_argument("--account", default=None, help="Account alias (default: 'default')")
    provider_smoke.add_argument(
        "--upload-private-test", action="store_true",
        help="Also run a real, private, clearly-labeled test upload (requires --confirm-test-upload too)",
    )
    provider_smoke.add_argument(
        "--confirm-test-upload", action="store_true",
        help="Required alongside --upload-private-test to actually run the test upload",
    )
    provider_smoke.set_defaults(func=cmd_provider_smoke)

    publisher_auth = sub.add_parser(
        "publisher-auth", help="Connect a publisher account via OAuth (opt-in, requires a browser)",
    )
    publisher_auth.add_argument("provider", choices=["youtube"])
    publisher_auth.add_argument("--account", default=None, help="Account alias (default: 'default')")
    publisher_auth.add_argument(
        "--timeout", type=float, default=300.0, help="Seconds to wait for the OAuth callback",
    )
    publisher_auth.set_defaults(func=cmd_publisher_auth)

    return parser


def main(argv: list[str] | None = None) -> int:
    from reel_harness.config import ProviderConfigurationError

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        ctx = AppContext()
    except ProviderConfigurationError as exc:
        # Clear operator-facing failure, no traceback, no network attempted.
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2
    return args.func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
