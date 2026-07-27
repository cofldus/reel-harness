from __future__ import annotations

import argparse
import json
import signal
import sys
import uuid

from reel_harness.bootstrap import AppContext
from reel_harness.core.service import InvalidActionError, JobNotFoundError
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
    _print_job(job)
    if job.status == "REVIEW_REQUIRED":
        print(f"preview: {ctx.storage.job_dir(job.id) / 'final' / 'final.mp4'}")
        print(f"manifest: {ctx.storage.job_dir(job.id) / 'manifest.json'}")
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


def cmd_provider_smoke(args: argparse.Namespace, ctx: AppContext) -> int:
    """Opt-in check of the configured real LLM provider: one minimal script
    generation with retries disabled, schema-validated, secrets redacted. The
    default test suites never run this -- it is an operator command."""
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
    provider_smoke.add_argument("target", choices=["llm"])
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
