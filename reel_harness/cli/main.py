from __future__ import annotations

import argparse
import json
import sys
import uuid

from reel_harness.bootstrap import AppContext
from reel_harness.core.service import InvalidActionError, JobNotFoundError
from reel_harness.db.models import Channel
from reel_harness.media.deps import check_ffmpeg_available
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
            run_job(session, job, channel, ctx.default_providers(), ctx.storage, lease_token=lease_token)
        finally:
            heartbeat.stop()
            release_lease(session, job, lease_token=lease_token)
    _print_job(job)
    return 0


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

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    ctx = AppContext()
    return args.func(args, ctx)


if __name__ == "__main__":
    sys.exit(main())
