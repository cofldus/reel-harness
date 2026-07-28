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


def cmd_provider_smoke(args: argparse.Namespace, ctx: AppContext) -> int:
    """Opt-in operator check of a configured real provider: one request with
    retries disabled, real validation, secrets redacted, scratch files cleaned.
    The default test suites never run this."""
    if args.target == "llm":
        return _smoke_llm(ctx)
    if args.target == "asset":
        return _smoke_asset(ctx)
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

    provider_smoke = sub.add_parser(
        "provider-smoke", help="One real request against the configured provider (opt-in)",
    )
    provider_smoke.add_argument("target", choices=["llm", "tts", "asset"])
    provider_smoke.set_defaults(func=cmd_provider_smoke)

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
