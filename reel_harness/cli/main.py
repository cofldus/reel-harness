from __future__ import annotations

import argparse
import json
import signal
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from reel_harness.bootstrap import AppContext
from reel_harness.core.service import InvalidActionError, JobNotFoundError, asset_safe_metadata
from reel_harness.db.models import Channel
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.worker.daemon import DaemonConfig, WorkerDaemon, default_worker_id
from reel_harness.worker.heartbeat import LeaseHeartbeat
from reel_harness.worker.lease import lease_next_job, recover_stale_jobs, release_lease
from reel_harness.worker.runner import run_job


def _parse_iso_datetime(value: str):
    from datetime import datetime

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO 8601 datetime: {value!r}") from exc
    return parsed


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


def cmd_preflight(args: argparse.Namespace, ctx: AppContext) -> int:
    """Single operator-facing readiness check before running this process
    for real -- see docs/OPERATIONS.md. `--profile production` escalates a
    fixed set of operationally-risky findings (placeholder secrets, an
    unwritable storage root, an unsafe heartbeat/lease ratio, ...) from WARN
    to FAIL; `--profile fake` (default) is the permissive local-dev bar.
    Local checks only unless `--check-remote` is also passed."""
    from reel_harness.ops.preflight import PreflightCheck, run_preflight, run_remote_checks

    report = run_preflight(ctx.settings, ctx.session_factory, profile=args.profile)
    remote_checks: list[PreflightCheck] = []
    if args.check_remote:
        requested_publishers = tuple(args.publisher) if args.publisher else ("youtube", "tiktok", "instagram")
        remote_checks = run_remote_checks(ctx.settings, ctx.credential_backend(), publishers=requested_publishers)
        for name in args.provider or ():
            remote_checks.append(PreflightCheck(
                f"remote_{name}", "NOT_CONFIGURED",
                f"live check not performed by preflight -- use `reel-harness provider-smoke {name}`",
            ))
    else:
        for name in (args.publisher or ()) :
            remote_checks.append(PreflightCheck(f"remote_{name}", "PASS", "not requested (pass --check-remote)"))
        for name in (args.provider or ()):
            remote_checks.append(PreflightCheck(f"remote_{name}", "PASS", "not requested (pass --check-remote)"))

    payload = report.to_dict()
    payload["checks"].extend(c.to_dict() for c in remote_checks)
    overall_rank = {"PASS": 0, "NOT_CONFIGURED": 1, "WARN": 2, "FAIL": 3}
    payload["overall"] = max(payload["checks"], key=lambda c: overall_rank[c["status"]])["status"] \
        if payload["checks"] else "PASS"

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"Preflight ({payload['profile']} profile) -- overall: {payload['overall']}")
        for check in payload["checks"]:
            detail = f" -- {check['detail']}" if check.get("detail") else ""
            print(f"  [{check['status']:^13}] {check['name']}{detail}")

    if payload["overall"] == "FAIL":
        return 1
    if payload["overall"] == "NOT_CONFIGURED":
        return 2
    return 0


def cmd_db_status(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.db_tools import db_status

    status = db_status(ctx.engine, ctx.settings.database_url)
    print(json.dumps(status.to_dict(), indent=2))
    return 0 if not status.pending_migrations and status.integrity_status == "ok" else 1


def cmd_db_migrate(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.db_tools import MigrationLockedError, db_migrate

    backup_dir = Path(args.backup_dir) if args.backup_dir else None
    if not args.dry_run and not args.no_backup and backup_dir is None:
        print(
            "db-migrate requires --backup-dir (or --no-backup to explicitly skip the safety backup)",
            file=sys.stderr,
        )
        return 2
    try:
        result = db_migrate(
            ctx.engine, ctx.settings.database_url, dry_run=args.dry_run,
            backup_dir=None if args.no_backup else backup_dir,
        )
    except MigrationLockedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_db_backup(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.db_tools import DbToolsError, db_backup

    try:
        result = db_backup(ctx.settings.database_url, Path(args.dest_dir))
    except DbToolsError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_db_restore(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.db_tools import DbToolsError, RestoreRefusedError, db_restore

    try:
        result = db_restore(
            ctx.settings.database_url, Path(args.backup_path), confirm_restore=args.confirm_restore,
            session_factory=ctx.session_factory, lease_timeout_seconds=ctx.settings.lease_timeout_seconds,
            pre_restore_backup_dir=Path(args.pre_restore_backup_dir), engine=ctx.engine,
        )
    except (RestoreRefusedError, DbToolsError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_db_verify(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.db_tools import db_verify

    result = db_verify(ctx.engine, ctx.session_factory)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_storage_verify(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.storage_tools import storage_verify

    result = storage_verify(ctx.storage, ctx.session_factory, repair_safe=args.repair_safe)
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


def cmd_backup_create(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.backup_bundle import backup_create

    result = backup_create(
        ctx.settings.database_url, ctx.storage.root_dir, ctx.publish_journal().root_dir,
        ctx.config_fingerprint(), Path(args.dest_path),
    )
    print(json.dumps(result, indent=2))
    return 0


def cmd_backup_inspect(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.backup_bundle import BackupBundleError, backup_inspect

    try:
        result = backup_inspect(Path(args.bundle_path))
    except BackupBundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_backup_restore(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.backup_bundle import BackupBundleError, backup_restore

    try:
        result = backup_restore(
            Path(args.bundle_path), ctx.storage.root_dir, ctx.settings.database_url,
            ctx.publish_journal().root_dir, confirm_restore=args.confirm_restore,
        )
    except BackupBundleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_incident_bundle(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.incident import IncidentBundleSecretDetectedError, build_incident_bundle

    try:
        result = build_incident_bundle(ctx, Path(args.dest_path))
    except IncidentBundleSecretDetectedError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


def cmd_live_verify(args: argparse.Namespace, ctx: AppContext) -> int:
    """Single-command read-only sweep (default) across YouTube/TikTok/
    Instagram live account state, optionally followed by a real,
    per-platform-confirmed upload test. A provider with no saved
    credential is reported NOT_CONFIGURED and the sweep continues to the
    next platform -- never aborts the whole run. Every result (read-only
    and upload-test) is appended to the append-only live-verification
    log, distinct from Publication."""
    from reel_harness.ops.live_verify import LiveVerificationLog, LiveVerificationRecord, run_read_only_live_verify

    requested = [p for p, enabled in (
        ("youtube", args.youtube), ("tiktok", args.tiktok), ("instagram", args.instagram),
    ) if enabled] or ["youtube", "tiktok", "instagram"]
    account = args.account or "default"
    log = LiveVerificationLog(ctx.publish_journal().root_dir.parent / "live_verification")

    records = run_read_only_live_verify(ctx, providers=tuple(requested), account=account)
    for record in records:
        log.append(record)

    if args.upload_tests:
        confirm_map = {
            "youtube": args.confirm_youtube_private, "tiktok": args.confirm_tiktok_restricted,
            "instagram": args.confirm_instagram_public,
        }
        upload_fn_map = {
            "youtube": lambda: _smoke_publisher_youtube(
                ctx, account, upload_private_test=True, confirm_test_upload=True,
            ),
            "tiktok": lambda: _smoke_publisher_tiktok(
                ctx, account, upload_private_test=True, confirm_test_upload=True, confirm_platform_options=True,
            ),
            "instagram": lambda: _smoke_publisher_instagram(
                ctx, account, upload_public_test=True, confirm_test_upload=True,
                confirm_public_upload=True, confirm_platform_options=True,
            ),
        }
        from datetime import UTC, datetime

        from reel_harness._version import __version__
        from reel_harness.ops.fingerprint import fingerprint_hash

        for provider in requested:
            if not confirm_map[provider]:
                continue  # never runs an upload test without this platform's explicit confirmation flag
            started_at = datetime.now(UTC).isoformat()
            exit_code = upload_fn_map[provider]()
            outcome = "PASS" if exit_code == 0 else "FAIL"
            record = LiveVerificationRecord(
                provider=provider, account_alias=account, verification_type="upload_test",
                started_at=started_at, completed_at=datetime.now(UTC).isoformat(), outcome=outcome,
                application_version=__version__,
                config_fingerprint_hash=fingerprint_hash(ctx.config_fingerprint()),
                detail=f"exit_code={exit_code}",
            )
            records.append(record)
            log.append(record)

    payload = {"providers": requested, "account": account, "records": [r.to_dict() for r in records]}
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for record in records:
            print(
                f"[{record.outcome:^20}] {record.provider} ({record.verification_type}) -- {record.detail}",
            )
    return 0 if all(r.outcome in ("PASS", "NOT_CONFIGURED") for r in records) else 1


def cmd_release_manifest(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.ops.release import build_release_manifest, write_release_manifest

    test_summary = None
    if args.test_summary_json:
        test_summary = json.loads(Path(args.test_summary_json).read_text(encoding="utf-8"))
    manifest = build_release_manifest(
        repo_root=Path.cwd(),
        wheel_path=Path(args.wheel_path) if args.wheel_path else None,
        sdist_path=Path(args.sdist_path) if args.sdist_path else None,
        lock_path=Path(args.lock_path) if args.lock_path else None,
        test_summary=test_summary,
        live_verification_status=args.live_verification_status,
    )
    dest = write_release_manifest(manifest, Path(args.dest_path))
    print(json.dumps(manifest, indent=2))
    print(f"written to {dest}", file=sys.stderr)
    return 0


def cmd_release_check(args: argparse.Namespace, ctx: AppContext) -> int:
    """Everything that must pass before an RC tag is created. Never
    creates a commit or tag itself -- see docs/OPERATIONS.md for the tag
    step, which is always a separate, explicit, manual action."""
    from reel_harness.ops.release_check import run_release_check

    report = run_release_check(Path.cwd(), skip_slow=args.skip_slow, pytest_timeout=args.pytest_timeout)
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print(f"release-check -- overall: {report.overall} -- ready_to_tag: {report.ready_to_tag}")
        for item in report.items:
            detail = f" -- {item.detail}" if item.detail else ""
            print(f"  [{item.status:^8}] {item.name}{detail}")
    return 0 if report.ready_to_tag else 1


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


# --- Fable cinematic projects (Phase F1) -----------------------------------


def cmd_fable_create(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.service import InvalidActionError

    if args.story_file:
        source_text = Path(args.story_file).read_text(encoding="utf-8")
    else:
        source_text = args.story or ""
    idempotency_key = args.idempotency_key or str(uuid.uuid4())
    try:
        project, replay = ctx.fable.create_project(
            title=args.title, source_text=source_text, idempotency_key=idempotency_key,
            language=args.language, genre=args.genre, tone=args.tone,
            target_duration_sec=args.duration, aspect_ratio=args.aspect_ratio,
            takes_per_shot=args.takes_per_shot,
        )
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "project_id": project.id, "status": project.status, "idempotent_replay": replay,
    }, indent=2))
    return 0


def cmd_fable_adapt(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        project = ctx.fable.adapt_project(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    print(json.dumps({"project_id": project.id, "status": project.status}, indent=2))
    return 0


def cmd_fable_approve(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    actions = {
        "story": ctx.fable.approve_story,
        "characters": ctx.fable.approve_characters,
        "shots": ctx.fable.approve_shots,
        "final": ctx.fable.approve_final,
    }
    try:
        project = actions[args.step](args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"project_id": project.id, "status": project.status}, indent=2))
    return 0


def cmd_fable_status(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        project = ctx.fable.get_project(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    shots = ctx.fable.project_shots(args.project_id)
    budget = ctx.fable.budget_status(args.project_id)
    payload = {
        "project_id": project.id,
        "title": project.title,
        "status": project.status,
        "aspect_ratio": project.aspect_ratio,
        "failure_code": project.failure_code,
        "failure_summary": project.failure_summary,
        "budget": {
            "limit_amount": budget.limit_amount,
            "currency": budget.currency,
            "spent_amount": budget.spent_amount,
            "remaining_amount": budget.remaining_amount,
            # Completed takes the provider published no price for. A
            # non-zero count means `spent_amount` is a lower bound.
            "unpriced_take_count": budget.unpriced_take_count,
        },
        "characters": [_character_payload(c) for c in ctx.fable.project_characters(args.project_id)],
        "shots": [
            {
                "shot_id": shot.id, "status": shot.status, "order": shot.shot_order,
                "action": shot.action, "duration_sec": shot.duration_sec,
                # Carries BUDGET_EXCEEDED / PAID_GENERATION_NOT_ALLOWED for a
                # shot the worker stopped before spending anything.
                "failure_code": shot.failure_code,
                "takes": [
                    {
                        "take_id": take.id, "status": take.status, "selected": take.selected,
                        "attempt_number": take.attempt_number,
                        "cost_amount": take.cost_amount, "cost_currency": take.cost_currency,
                    }
                    for take in ctx.fable.shot_takes(shot.id)
                ],
            }
            for shot in shots
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_fable_generate_references(args: argparse.Namespace, ctx: AppContext) -> int:
    """CASTING -> CHARACTER_REVIEW, generating each character's four-view
    reference sheet (face first, the rest chained off it)."""
    from reel_harness.core.errors import PipelineError
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    try:
        project = ctx.fable.generate_references(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except PipelineError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({
        "project_id": project.id,
        "status": project.status,
        "characters": [_character_payload(c) for c in ctx.fable.project_characters(project.id)],
    }, indent=2))
    return 0


def _character_payload(character) -> dict:
    return {
        "character_id": character.id,
        "name": character.name,
        "adult_confirmed": character.adult_confirmed,
        "reference_approved": character.reference_approved,
        "reference_images": character.reference_images or {},
        # Present when a safety filter refused a view -- the sheet stays
        # incomplete and unapprovable until the bible is edited.
        "reference_failure_code": character.reference_failure_code,
        "reference_failure_summary": character.reference_failure_summary,
    }


def cmd_fable_reference(args: argparse.Namespace, ctx: AppContext) -> int:
    """Approve or reject one character's reference sheet."""
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    action = ctx.fable.reject_reference if args.reject else ctx.fable.approve_reference
    try:
        character = action(args.character_id)
    except FableProjectNotFoundError:
        print(f"fable character not found: {args.character_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(_character_payload(character), indent=2))
    return 0


def cmd_fable_reference_smoke(args: argparse.Namespace, ctx: AppContext) -> int:
    """One REAL reference-image chain against the configured provider.

    Exists to answer, with actual bytes rather than documentation, two
    questions the test suite structurally cannot: does the real adapter
    work end to end, and does the model accept its own generated image
    back as a character reference (the chaining the whole reference sheet
    depends on)?

    It spends real money on a real provider, so it is opt-in twice over
    and it says up front what it will cost. Against a free tier it is
    simply a cheap wiring check."""
    import tempfile

    from reel_harness.core.errors import PipelineError
    from reel_harness.pipeline.reference_prompt import (
        DEFAULT_REFERENCE_RESOLUTION,
        REFERENCE_ASPECT_RATIO,
    )
    from reel_harness.providers.base import ReferenceImageRequest
    from reel_harness.providers.registry import (
        provider_charges_money,
        resolve_reference_image_provider,
    )

    provider = resolve_reference_image_provider(
        ctx.settings.reference_image_provider, ctx.settings,
    )
    paid = provider_charges_money(provider.provider_id)
    estimate = provider.estimate_cost(ReferenceImageRequest(
        prompt="", aspect_ratio=REFERENCE_ASPECT_RATIO, resolution=DEFAULT_REFERENCE_RESOLUTION,
    ))
    # Two images: the face, then one view chained off it. That is the
    # smallest run that actually tests the chaining rather than just the
    # transport.
    projected = estimate.amount * 2 if estimate.known and estimate.amount is not None else None

    if paid and not args.confirm_paid_generation:
        print(json.dumps({
            "status": "NOT RUN",
            "reason": "would spend real money -- re-run with --confirm-paid-generation",
            "provider": provider.provider_id,
            "model": getattr(provider, "model_id", None),
            "images": 2,
            "projected_cost": projected,
            "projected_cost_currency": estimate.currency,
            "projected_cost_known": estimate.known,
        }, indent=2))
        return 4

    prompt = (
        "a single fictional adult actor, 30s adult, plain neutral background, "
        "head-and-shoulders portrait, facing camera directly, neutral expression, "
        "even soft lighting, photorealistic reference photograph, "
        "not a real or recognizable person"
    )
    with tempfile.TemporaryDirectory(prefix="reel-harness-reference-smoke-") as scratch:
        dest = Path(scratch)
        try:
            face = provider.generate_reference(ReferenceImageRequest(
                prompt=prompt, aspect_ratio=REFERENCE_ASPECT_RATIO,
                resolution=DEFAULT_REFERENCE_RESOLUTION,
                correlation_id="reference-smoke:face",
            ), dest)
            chained = provider.generate_reference(ReferenceImageRequest(
                prompt=prompt.replace(
                    "head-and-shoulders portrait, facing camera directly",
                    "three-quarter view from the waist up, head turned 45 degrees from camera",
                ),
                aspect_ratio=REFERENCE_ASPECT_RATIO,
                resolution=DEFAULT_REFERENCE_RESOLUTION,
                character_reference_paths=[face.image_path],
                correlation_id="reference-smoke:three_quarter",
            ), dest)
        except PipelineError as exc:
            print(json.dumps({
                "status": "FAIL", "provider": provider.provider_id,
                "failure_code": exc.code, "failure_summary": str(exc)[:500],
            }, indent=2))
            return 3

        payload = {
            "status": "PASS",
            "provider": provider.provider_id,
            "model": face.model_id,
            "face_bytes": face.image_path.stat().st_size,
            "chained_bytes": chained.image_path.stat().st_size,
            "face_checksum_sha256": face.checksum_sha256,
            "chained_checksum_sha256": chained.checksum_sha256,
            # The point of the second call: the model accepted its own
            # generated image back as a character reference.
            "chained_reference_accepted": True,
            "watermark": chained.watermark,
            "license": chained.license,
            "cost_amount": (
                (face.cost_amount or 0.0) + (chained.cost_amount or 0.0)
                if face.cost_amount is not None else None
            ),
            "cost_currency": face.cost_currency,
            # Stated in the output itself so a copied-and-pasted result can
            # never be read as more than it is.
            "proves": [
                "the adapter reaches the provider and returns real image bytes",
                "the provider accepts a previously generated image as a character reference",
            ],
            "does_not_prove": [
                "that the two images depict a recognizably identical person "
                "(no automated check judges that; look at them)",
                "that Veo accepts a watermarked image as character-reference input "
                "(open question -- F5's video adapter is what will answer it)",
            ],
        }
        if args.keep_output:
            kept = Path(args.keep_output)
            kept.mkdir(parents=True, exist_ok=True)
            for result, name in ((face, "face"), (chained, "three_quarter")):
                target = kept / f"{name}{result.image_path.suffix}"
                target.write_bytes(result.image_path.read_bytes())
            payload["output_dir"] = str(kept)
        print(json.dumps(payload, indent=2))
    return 0


def cmd_fable_budget(args: argparse.Namespace, ctx: AppContext) -> int:
    """Sets or reports a project's spending ceiling. With no --limit/--clear
    it is purely a report, so checking a budget can never change one."""
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    try:
        if args.clear:
            ctx.fable.set_budget(args.project_id, None)
        elif args.limit is not None:
            ctx.fable.set_budget(args.project_id, args.limit, args.currency)
        status = ctx.fable.budget_status(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "project_id": args.project_id,
        "limit_amount": status.limit_amount,
        "currency": status.currency,
        "spent_amount": status.spent_amount,
        "remaining_amount": status.remaining_amount,
        "unpriced_take_count": status.unpriced_take_count,
        "paid_generation_enabled": ctx.settings.allow_paid_generation,
    }, indent=2))
    return 0


def cmd_fable_estimate(args: argparse.Namespace, ctx: AppContext) -> int:
    """Prices the project's shots with its own pinned provider. Read-only:
    approves nothing and spends nothing."""
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    try:
        estimate = ctx.fable.estimate_cost(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({
        "project_id": args.project_id,
        # False means the number below is NOT a total -- see `detail`.
        "known": estimate.known,
        "amount": estimate.amount,
        "currency": estimate.currency,
        "shot_count": estimate.shot_count,
        "unpriced_shot_count": estimate.unpriced_shot_count,
        "detail": estimate.detail,
    }, indent=2))
    return 0


def cmd_fable_list(args: argparse.Namespace, ctx: AppContext) -> int:
    rows = [
        {"project_id": p.id, "title": p.title, "status": p.status}
        for p in ctx.fable.list_projects()
    ]
    print(json.dumps(rows, indent=2))
    return 0


def cmd_fable_select_take(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    try:
        shot = ctx.fable.select_take(args.take_id)
    except FableProjectNotFoundError:
        print(f"fable take not found: {args.take_id}", file=sys.stderr)
        return 1
    except InvalidActionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"shot_id": shot.id, "status": shot.status}, indent=2))
    return 0


def cmd_fable_render(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.errors import PipelineError
    from reel_harness.core.fable_service import FableProjectNotFoundError
    from reel_harness.core.service import InvalidActionError

    try:
        final_path = ctx.fable.render_final(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    except (InvalidActionError, PipelineError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps({"project_id": args.project_id, "final_path": str(final_path)}, indent=2))
    return 0


def cmd_fable_cancel(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.core.fable_service import FableProjectNotFoundError

    try:
        project = ctx.fable.cancel_project(args.project_id)
    except FableProjectNotFoundError:
        print(f"fable project not found: {args.project_id}", file=sys.stderr)
        return 1
    print(json.dumps({"project_id": project.id, "status": project.status}, indent=2))
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


_DEMO_RUN_TERMINAL_STATUSES = frozenset({
    "REVIEW_REQUIRED", "COMPLETED", "FAILED",
})


def cmd_demo_run(args: argparse.Namespace, ctx: AppContext) -> int:
    """Collapses channel-create (if needed) + job-create + the same lease/
    heartbeat/run_job/release-lease sequence as worker-run-once, looped until
    THIS job reaches a terminal-ish status, into one command -- for quickly
    seeing a job's actual rendered output (most useful with
    REEL_HARNESS_LLM_PROVIDER=demo / TTS_PROVIDER=demo / ASSET_PROVIDER=demo,
    see README.md's Demo Mode section, but not itself provider-specific).
    Not a substitute for `serve` -- other queued jobs may also get processed
    along the way, exactly like any other worker-run-once call would."""
    if args.channel_id:
        channel_id = args.channel_id
    else:
        channel = ctx.jobs.create_channel(
            name=args.channel_name or "demo", niche=args.niche, language=args.language,
        )
        channel_id = channel.id
        print(json.dumps({"channel_id": channel_id, "name": channel.name}, indent=2), file=sys.stderr)

    idempotency_key = args.idempotency_key or str(uuid.uuid4())
    job, replay = ctx.jobs.create_job(channel_id=channel_id, idempotency_key=idempotency_key, topic=args.topic)
    print(
        json.dumps({"job_id": job.id, "status": job.status, "idempotent_replay": replay}, indent=2),
        file=sys.stderr,
    )

    lease_timeout = ctx.settings.lease_timeout_seconds
    worker_id = f"demo-run-{uuid.uuid4().hex[:12]}"
    for _ in range(args.max_attempts):
        with ctx.session_factory() as session:
            db_job = ctx.jobs.get_job(job.id)
            if db_job.status in _DEMO_RUN_TERMINAL_STATUSES:
                break
            recover_stale_jobs(session, lease_timeout_seconds=lease_timeout)
            leased = lease_next_job(session, worker_id=worker_id)
            if leased is None:
                break
            leased_channel = session.get(Channel, leased.channel_id)
            lease_token = leased.lease_token
            assert lease_token is not None
            heartbeat = LeaseHeartbeat(
                ctx.session_factory, leased.id, lease_token, ctx.settings.lease_heartbeat_seconds,
            )
            heartbeat.start()
            try:
                run_job(
                    session, leased, leased_channel, ctx.providers_for_job(leased), ctx.storage,
                    lease_token=lease_token,
                )
            finally:
                heartbeat.stop()
                release_lease(session, leased, lease_token=lease_token)

    final_job = ctx.jobs.get_job(job.id)
    payload = {
        "job_id": final_job.id,
        "status": final_job.status,
        "current_stage": final_job.current_stage,
        "failure_code": final_job.failure_code,
        "failure_summary": final_job.failure_summary,
        "reason_code": final_job.reason_code,
        "preview_path": None,
    }
    if final_job.status == "REVIEW_REQUIRED":
        payload["preview_path"] = str(ctx.storage.job_dir(final_job.id) / "final" / "final.mp4")
    print(json.dumps(payload, indent=2))
    return 0 if final_job.status in ("REVIEW_REQUIRED", "COMPLETED") else 1


def _publication_fields(publication) -> dict:
    """Safe fields only -- never a token, the upload session reference/URL,
    a local credential path, an Authorization header, or a raw provider
    response body. Shared by _print_publication and publication-list."""
    return {
        "publication_id": publication.id,
        "job_id": publication.job_id,
        "provider": publication.provider,
        "account_reference": publication.account_reference,
        "status": publication.status,
        "privacy_status": publication.privacy_status,
        "provider_video_id": publication.provider_video_id,
        "publication_url": publication.publication_url,
        "bytes_uploaded": publication.bytes_uploaded,
        "total_bytes": publication.total_bytes,
        "processing_poll_count": publication.processing_poll_count,
        "failure_code": publication.failure_code,
        "failure_summary": publication.failure_summary,
        "created_at": publication.created_at.isoformat() if publication.created_at else None,
        "updated_at": publication.updated_at.isoformat() if publication.updated_at else None,
    }


def _print_publication(publication) -> None:
    print(json.dumps(_publication_fields(publication), indent=2))


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
            confirm_platform_options=args.confirm_platform_options,
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
    from reel_harness.providers.registry import provider_capabilities

    try:
        eligibility = ctx.publications.check_eligibility(args.job_id)
    except PublicationNotFoundError:
        print(f"job not found: {args.job_id}", file=sys.stderr)
        return 1

    try:
        caps = provider_capabilities(args.provider)
    except NotImplementedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    privacy_status = args.privacy if args.privacy is not None else caps.default_privacy

    privacy_valid = privacy_status in caps.privacy_values

    credential_configured = True
    if args.provider == "youtube":
        client_ok = bool(
            ctx.settings.youtube_client_id and ctx.settings.youtube_client_secret.get_secret_value(),
        )
        credential_configured = client_ok and ctx.credential_backend().has_credential("youtube", args.account)
    elif args.provider == "tiktok":
        client_ok = bool(
            ctx.settings.tiktok_client_key and ctx.settings.tiktok_client_secret.get_secret_value()
            and ctx.settings.tiktok_redirect_uri,
        )
        credential_configured = client_ok and ctx.credential_backend().has_credential("tiktok", args.account)
    elif args.provider == "instagram":
        client_ok = bool(
            ctx.settings.instagram_app_id and ctx.settings.instagram_app_secret.get_secret_value()
            and ctx.settings.instagram_redirect_uri,
        )
        credential_configured = client_ok and ctx.credential_backend().has_credential("instagram", args.account)

    # YouTube-shaped preview ("fake" stands in for YouTube's shape
    # throughout this project's tests); TikTok/Instagram get their own
    # previews below, since their metadata models (post text/caption +
    # platform_options) don't match YouTube's title/description/tags/
    # category shape.
    metadata_preview = None
    tiktok_preview = None
    instagram_preview = None
    upload_chunk_size_bytes = ctx.settings.youtube_upload_chunk_size
    if args.provider in ("youtube", "fake") and eligibility.manifest is not None:
        metadata = build_publication_metadata(
            eligibility.manifest, privacy_status=privacy_status,
            category_id=ctx.settings.youtube_category_id, made_for_kids=ctx.settings.youtube_made_for_kids,
        )
        tags_total_length = sum(len(t) for t in metadata.tags) + max(len(metadata.tags) - 1, 0)
        metadata_preview = {
            "title": metadata.title, "title_length": len(metadata.title),
            "description_length_bytes": len(metadata.description.encode("utf-8")),
            "tags": metadata.tags, "tags_total_length": tags_total_length,
            "category_id": metadata.category_id, "made_for_kids": metadata.made_for_kids,
        }
    elif args.provider == "tiktok" and eligibility.manifest is not None:
        tiktok_preview = _tiktok_dry_run_preview(ctx, eligibility.manifest, privacy_status, credential_configured)
        upload_chunk_size_bytes = ctx.settings.tiktok_upload_chunk_size
    elif args.provider == "instagram" and eligibility.manifest is not None:
        instagram_preview = _instagram_dry_run_preview(ctx, eligibility.manifest, credential_configured)
        upload_chunk_size_bytes = instagram_preview.get("video_file_size_bytes") or 0

    video_file_size_bytes = None
    final_path = ctx.storage.job_dir(args.job_id) / "final" / "final.mp4"
    if final_path.is_file():
        video_file_size_bytes = final_path.stat().st_size

    public_requested = privacy_status in caps.public_privacy_values
    public_upload_allowed = (
        not public_requested
        or (args.confirm_public_upload and ctx.settings.allow_public_upload)
    )
    platform_options_confirmed = (not caps.requires_user_confirmation) or args.confirm_platform_options
    post_text_valid = tiktok_preview is None or tiktok_preview.get("post_text_error") is None
    caption_valid = instagram_preview is None or instagram_preview.get("caption_error") is None

    payload = {
        "job_id": args.job_id, "provider": args.provider, "account_reference": args.account,
        "dry_run": True,
        "eligible": eligibility.eligible, "eligibility_reasons": eligibility.reasons,
        "requested_privacy_status": privacy_status,
        "privacy_status_valid": privacy_valid,
        "public_upload_allowed": public_upload_allowed,
        "requires_user_confirmation": caps.requires_user_confirmation,
        "platform_options_confirmed": platform_options_confirmed,
        "credential_configured": credential_configured,
        "metadata_preview": metadata_preview,
        "tiktok_preview": tiktok_preview,
        "instagram_preview": instagram_preview,
        "video_file_size_bytes": video_file_size_bytes,
        "upload_chunk_size_bytes": upload_chunk_size_bytes,
    }
    print(json.dumps(payload, indent=2))
    ready = (
        eligibility.eligible and privacy_valid and credential_configured
        and public_upload_allowed and platform_options_confirmed and post_text_valid and caption_valid
    )
    return 0 if ready else 1


def _tiktok_dry_run_preview(ctx: AppContext, manifest, privacy_status: str, credential_configured: bool) -> dict:
    """Entirely local/network-free, mirroring every other dry-run check in
    this project (`publish-job --dry-run` never even calls TikTok's
    creator_info query, let alone publish/init -- see `publisher-doctor
    tiktok --check-remote` for a live check). Reports what CAN be
    determined without a network call: the post text that would be sent
    (validated against TikTok's own length/forbidden-marker rules), the
    default platform_options, the FILE_UPLOAD chunk plan, and an explicit
    note that creator-info/app-review status require a live check."""
    from reel_harness.pipeline.publish_metadata import build_title
    from reel_harness.providers.registry import default_platform_options
    from reel_harness.providers.tiktok_publisher import build_post_text

    title = build_title(manifest.topic, manifest.script_title)
    post_text_error = None
    try:
        build_post_text(title)
    except Exception as exc:  # noqa: BLE001 - reported as a field, never raised through dry-run
        post_text_error = str(exc)

    video_file_size_bytes = None
    final_path = ctx.storage.job_dir(manifest.job_id) / "final" / "final.mp4"
    if final_path.is_file():
        video_file_size_bytes = final_path.stat().st_size
    chunk_size = ctx.settings.tiktok_upload_chunk_size
    total_chunk_count = None
    if video_file_size_bytes is not None and chunk_size > 0:
        total_chunk_count = max(1, -(-video_file_size_bytes // chunk_size))  # ceil division

    return {
        "post_text": title, "post_text_length_utf16_units": len(title.encode("utf-16-le")) // 2,
        "post_text_error": post_text_error,
        "platform_options": default_platform_options("tiktok"),
        "expected_api_mode": "FILE_UPLOAD",
        "chunk_size_bytes": chunk_size, "total_chunk_count": total_chunk_count,
        "creator_info": (
            "not fetched -- dry-run never contacts the network; run "
            "`publisher-doctor tiktok --check-remote` for a live creator_info/app-review check"
        ),
        "app_review_status": (
            "unknown -- not checked (see creator_info note above)" if credential_configured
            else "unknown -- no credential configured"
        ),
    }


def _instagram_dry_run_preview(ctx: AppContext, manifest, credential_configured: bool) -> dict:
    """Entirely local/network-free, mirroring _tiktok_dry_run_preview.
    Reports what CAN be determined without a network call: the caption
    that would be sent (validated against Instagram's own length/hashtag/
    mention/forbidden-marker rules), whether the video's duration/file
    size fall within Instagram's documented Reels limits (a check TikTok's
    own preview can't make, since TikTok's limits were never confirmed --
    see docs/PUBLISHING.md), the default platform_options, the expected
    API mode, and an explicit note that account-info/publishing-limit
    status require a live check."""
    from reel_harness.core.errors import VideoTooLargeError, VideoTooLongError
    from reel_harness.pipeline.publish_metadata import build_title
    from reel_harness.providers.instagram_media import validate_video_for_reels
    from reel_harness.providers.instagram_publisher import build_caption
    from reel_harness.providers.registry import default_platform_options

    title = build_title(manifest.topic, manifest.script_title)
    caption_error = None
    try:
        build_caption(title)
    except Exception as exc:  # noqa: BLE001 - reported as a field, never raised through dry-run
        caption_error = str(exc)

    video_file_size_bytes = None
    final_path = ctx.storage.job_dir(manifest.job_id) / "final" / "final.mp4"
    if final_path.is_file():
        video_file_size_bytes = final_path.stat().st_size

    duration_sec = manifest.validation.duration_sec if manifest.validation else None
    video_limits_error = None
    if video_file_size_bytes is not None:
        try:
            validate_video_for_reels(duration_sec, video_file_size_bytes)
        except (VideoTooLongError, VideoTooLargeError) as exc:
            video_limits_error = str(exc)
        if caption_error is None and video_limits_error is not None:
            caption_error = video_limits_error  # either failure blocks "ready" the same way

    return {
        "caption": title, "caption_length": len(title), "caption_error": caption_error,
        "video_limits_error": video_limits_error,
        "video_file_size_bytes": video_file_size_bytes,
        "platform_options": default_platform_options("instagram"),
        "expected_api_mode": "FILE_UPLOAD_RESUMABLE",
        "account_info": (
            "not fetched -- dry-run never contacts the network; run "
            "`publisher-doctor instagram --check-remote` for a live account-info/publishing-limit check"
        ),
        "account_eligibility_status": (
            "unknown -- not checked (see account_info note above)" if credential_configured
            else "unknown -- no credential configured"
        ),
    }


def _resolve_publisher_lanes(args: argparse.Namespace) -> tuple[bool, bool]:
    """--process-upload/--process-status split the two roles a publisher
    worker plays; omitting both means "do both" (the historical default
    behavior), not "do neither"."""
    if not args.process_upload and not args.process_status:
        return True, True
    return args.process_upload, args.process_status


def cmd_publisher_run_once(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.db.models import Job
    from reel_harness.worker.publish_lease import (
        lease_next_via_lanes,
        recover_stale_publications,
        release_publication_lease,
    )
    from reel_harness.worker.publish_runner import run_publication

    process_upload, process_status = _resolve_publisher_lanes(args)
    lease_timeout = args.lease_timeout or ctx.settings.lease_timeout_seconds
    with ctx.session_factory() as session:
        recover_stale_publications(session, lease_timeout_seconds=lease_timeout)
        publication = lease_next_via_lanes(
            session, worker_id=args.worker_id, process_upload=process_upload, process_status=process_status,
        )
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


_FAILED_LIKE_STATUSES = ["FAILED", "AUTH_REQUIRED", "QUOTA_BLOCKED"]


def cmd_publication_list(args: argparse.Namespace, ctx: AppContext) -> int:
    """Read-only operator listing across publications, with filters. Never
    contacts the provider. Safe fields only -- see _publication_fields."""
    statuses = None
    status = args.status
    if status is None:
        if args.failed_only:
            statuses = _FAILED_LIKE_STATUSES
        elif args.processing_only:
            status = "PROCESSING"

    rows = ctx.publications.list_publications(
        job_id=args.job_id, status=status, provider=args.provider, account_reference=args.account,
        statuses=statuses, created_after=args.created_after, created_before=args.created_before,
    )
    print(json.dumps([_publication_fields(row) for row in rows], indent=2))
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


def _reconcile_one(ctx: AppContext, publication_id: str) -> dict:
    from reel_harness.core.publish_reconciliation import reconcile_publication
    from reel_harness.db.models import Publication

    with ctx.session_factory() as session:
        publication = session.get(Publication, publication_id)
        if publication is None:
            return {"publication_id": publication_id, "error": "not found"}
        bundle = ctx.bundle_for_publication(publication)
        try:
            result = reconcile_publication(session, publication, bundle)
        finally:
            close = getattr(bundle.publisher, "close", None)
            if callable(close):
                close()
        return result.to_dict()


def cmd_publication_reconcile(args: argparse.Namespace, ctx: AppContext) -> int:
    """Determines whether a publication's local state actually matches
    reality at the provider (recovering a provider_video_id the DB never
    committed, distinguishing a genuinely expired upload session from an
    incomplete one, etc.) -- never starts a new upload itself. See
    core.publish_reconciliation for the full outcome list; anything
    uncertain reports manual_review_required or ambiguous_remote_state
    rather than guessing."""
    from reel_harness.core.state_machine import PublicationStatus
    from reel_harness.db.models import Publication

    if args.all:
        with ctx.session_factory() as session:
            from sqlalchemy import select

            terminal = {PublicationStatus.PUBLISHED.value, PublicationStatus.CANCELLED.value}
            ids = list(session.execute(
                select(Publication.id).where(Publication.status.not_in(terminal)),
            ).scalars())
        results = [_reconcile_one(ctx, pub_id) for pub_id in ids]
        print(json.dumps(results, indent=2))
        return 0 if all("error" not in r for r in results) else 1

    if not args.publication_id:
        print("usage: publication-reconcile <publication_id> | --all", file=sys.stderr)
        return 2
    result = _reconcile_one(ctx, args.publication_id)
    print(json.dumps(result, indent=2))
    return 1 if "error" in result else 0


def cmd_publication_retry(args: argparse.Namespace, ctx: AppContext) -> int:
    """Manually retries a stuck publication (FAILED / AUTH_REQUIRED /
    QUOTA_BLOCKED / RETRY_WAIT). Never uploads anything itself -- it
    repositions the publication for the next publisher-run/-run-once cycle
    to actually resume it. See core.publish_retry for the full policy
    (eligibility and metadata-fingerprint are always re-verified first)."""
    from reel_harness.core.publish_retry import PublicationRetryError, retry_publication
    from reel_harness.db.models import Publication

    with ctx.session_factory() as session:
        publication = session.get(Publication, args.publication_id)
        if publication is None:
            print(f"publication not found: {args.publication_id}", file=sys.stderr)
            return 1
        try:
            result = retry_publication(session, publication, ctx.storage, from_stage=args.from_stage)
        except PublicationRetryError as exc:
            print(json.dumps({"retried": False, "reasons": exc.reasons}, indent=2))
            return 1
    print(json.dumps({"retried": True, **result.to_dict()}, indent=2))
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
    """Opt-in OAuth connect flow for a publisher account. Dispatches to the
    provider-specific flow below; never prints an access token, refresh
    token, client secret, authorization code, or PKCE verifier -- only the
    resulting account identity."""
    if args.provider == "youtube":
        return _cmd_publisher_auth_youtube(args, ctx)
    if args.provider == "tiktok":
        return _cmd_publisher_auth_tiktok(args, ctx)
    if args.provider == "instagram":
        return _cmd_publisher_auth_instagram(args, ctx)
    print(f"unsupported publisher provider: {args.provider}", file=sys.stderr)  # pragma: no cover
    return 2


def _cmd_publisher_auth_youtube(args: argparse.Namespace, ctx: AppContext) -> int:
    """Never runs without a configured OAuth client
    (REEL_HARNESS_YOUTUBE_CLIENT_ID/_SECRET); never prints an access token,
    refresh token, client secret, authorization code, or PKCE verifier --
    only the resulting account/channel identity."""
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
        created_at=datetime.now(UTC), last_refreshed_at=datetime.now(UTC),
    ))

    print(json.dumps({
        "provider": "youtube",
        "account_reference": account,
        "channel_id": identity.channel_id,
        "channel_title": identity.title,
        "has_refresh_token": tokens.refresh_token is not None,
    }, indent=2))
    return 0


def _cmd_publisher_auth_tiktok(args: argparse.Namespace, ctx: AppContext) -> int:
    """Never runs without a configured OAuth client and a registered
    redirect_uri (REEL_HARNESS_TIKTOK_CLIENT_KEY/_SECRET/_REDIRECT_URI);
    never prints an access token, refresh token, client secret,
    authorization code, or PKCE verifier -- only the resulting account
    identity (open_id).

    TikTok's docs require an HTTPS redirect_uri with no documented
    Google-style "any loopback port" exception, so two flows are
    supported depending on what the operator registered:

    - `tiktok_redirect_uri` is `http://127.0.0.1:PORT`/`http://localhost:PORT`
      (the operator's own choice, at their own risk): the callback is
      captured automatically, same mechanism as YouTube's loopback flow,
      but bound to that exact registered port (not an OS-assigned one --
      TikTok redirects to exactly what was registered).
    - Anything else (the documented case, an https:// URL the operator
      controls): the CLI cannot safely stand up a listener there, so the
      operator pastes back the full URL their browser was redirected to
      after authorizing; `state` is validated against it exactly like the
      automated flow, and the code is discarded after exactly one use."""
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlsplit

    from reel_harness.config import ProviderConfigurationError, validate_tiktok_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_common import LoopbackCallbackServer, OAuthCallbackError
    from reel_harness.publisher.oauth_tiktok import (
        TikTokOAuthClient,
        build_authorization_url,
        generate_pkce,
        generate_state,
    )

    try:
        validate_tiktok_credentials_configured(ctx.settings)
    except ProviderConfigurationError as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2

    account = args.account or "default"
    pkce = generate_pkce()
    state = generate_state()
    redirect_uri = ctx.settings.tiktok_redirect_uri
    auth_url = build_authorization_url(
        ctx.settings.tiktok_client_key, redirect_uri, state, pkce, ctx.settings.tiktok_auth_url,
    )

    parsed_redirect = urlsplit(redirect_uri)
    use_loopback = parsed_redirect.scheme == "http" and parsed_redirect.hostname in ("127.0.0.1", "localhost")

    print("Open this URL in a browser to authorize Reel Harness:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    try:
        import webbrowser

        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - best-effort only; the printed URL above is the real fallback
        pass

    if use_loopback:
        server = LoopbackCallbackServer(
            expected_state=state, timeout_seconds=args.timeout, port=parsed_redirect.port or 80,
        )
        try:
            code = server.wait_for_code()
        except OAuthCallbackError as exc:
            print(f"oauth callback failed: {exc}", file=sys.stderr)
            return 3
    else:
        print(
            "After authorizing, paste the full URL your browser was redirected to below "
            "(never just the bare code -- the full URL lets this command verify `state`):",
            file=sys.stderr,
        )
        pasted = input("Redirect URL: ").strip()
        pasted_params = parse_qs(urlsplit(pasted).query)
        if pasted_params.get("error"):
            print(f"oauth callback failed: {pasted_params['error'][0]}", file=sys.stderr)
            return 3
        if pasted_params.get("state", [None])[0] != state:
            print("oauth callback failed: state_mismatch", file=sys.stderr)
            return 3
        code_values = pasted_params.get("code")
        if not code_values:
            print("oauth callback failed: missing_code", file=sys.stderr)
            return 3
        code = code_values[0]

    client = TikTokOAuthClient(
        ctx.settings.tiktok_client_key, ctx.settings.tiktok_client_secret.get_secret_value(),
        ctx.settings.tiktok_token_url,
        connect_timeout=ctx.settings.tiktok_connect_timeout_seconds,
        read_timeout=ctx.settings.tiktok_read_timeout_seconds,
    )
    try:
        tokens = client.exchange_code(code, pkce.verifier, redirect_uri)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4
    finally:
        client.close()

    now = datetime.now(UTC)
    ctx.credential_backend().save_credential(OAuthCredential(
        access_token=tokens.access_token, refresh_token=tokens.refresh_token,
        expires_at=now + timedelta(seconds=tokens.expires_in),
        refresh_expires_at=(
            now + timedelta(seconds=tokens.refresh_expires_in) if tokens.refresh_expires_in is not None else None
        ),
        scope=tokens.scope, provider="tiktok", account_reference=account,
        channel_id=tokens.open_id or None, channel_title=None,
        created_at=now, last_refreshed_at=now,
    ))

    print(json.dumps({
        "provider": "tiktok",
        "account_reference": account,
        "open_id": tokens.open_id or None,
        "has_refresh_token": tokens.refresh_token is not None,
    }, indent=2))
    return 0


def _cmd_publisher_auth_instagram(args: argparse.Namespace, ctx: AppContext) -> int:
    """Never runs without a configured OAuth client and a registered
    redirect_uri (REEL_HARNESS_INSTAGRAM_APP_ID/_SECRET/_REDIRECT_URI);
    never prints an access token, client secret, authorization code, or
    PKCE verifier -- only the resulting account identity.

    Meta's docs don't document a loopback-port exception either (same
    situation as TikTok's), so this reuses the exact same dual
    loopback-or-manual-paste flow. After the authorization-code exchange,
    this additionally exchanges the short-lived token for a long-lived
    one (~60 days) -- Instagram Login for Business's own two-step token
    model, distinct from YouTube's/TikTok's single-exchange flow -- and
    fetches the connected account's identity (never a Facebook Page
    lookup, since this project uses Instagram Login for Business only;
    see docs/PUBLISHING.md)."""
    from datetime import UTC, datetime, timedelta
    from urllib.parse import parse_qs, urlsplit

    from reel_harness.config import ProviderConfigurationError, validate_instagram_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.observability import redact
    from reel_harness.publisher.credentials import OAuthCredential
    from reel_harness.publisher.oauth_common import LoopbackCallbackServer, OAuthCallbackError
    from reel_harness.publisher.oauth_instagram import (
        SCOPES,
        InstagramOAuthClient,
        build_authorization_url,
        generate_pkce,
        generate_state,
    )

    try:
        validate_instagram_credentials_configured(ctx.settings)
    except ProviderConfigurationError as exc:
        print(f"provider configuration error: {exc}", file=sys.stderr)
        return 2

    account = args.account or "default"
    pkce = generate_pkce()
    state = generate_state()
    redirect_uri = ctx.settings.instagram_redirect_uri
    auth_url = build_authorization_url(
        ctx.settings.instagram_app_id, redirect_uri, state, pkce, ctx.settings.instagram_auth_url,
    )

    parsed_redirect = urlsplit(redirect_uri)
    use_loopback = parsed_redirect.scheme == "http" and parsed_redirect.hostname in ("127.0.0.1", "localhost")

    print("Open this URL in a browser to authorize Reel Harness:", file=sys.stderr)
    print(auth_url, file=sys.stderr)
    try:
        import webbrowser

        webbrowser.open(auth_url)
    except Exception:  # noqa: BLE001 - best-effort only; the printed URL above is the real fallback
        pass

    if use_loopback:
        server = LoopbackCallbackServer(
            expected_state=state, timeout_seconds=args.timeout, port=parsed_redirect.port or 80,
        )
        try:
            code = server.wait_for_code()
        except OAuthCallbackError as exc:
            print(f"oauth callback failed: {exc}", file=sys.stderr)
            return 3
    else:
        print(
            "After authorizing, paste the full URL your browser was redirected to below "
            "(never just the bare code -- the full URL lets this command verify `state`):",
            file=sys.stderr,
        )
        pasted = input("Redirect URL: ").strip()
        pasted_params = parse_qs(urlsplit(pasted).query)
        if pasted_params.get("error"):
            print(f"oauth callback failed: {pasted_params['error'][0]}", file=sys.stderr)
            return 3
        if pasted_params.get("state", [None])[0] != state:
            print("oauth callback failed: state_mismatch", file=sys.stderr)
            return 3
        code_values = pasted_params.get("code")
        if not code_values:
            print("oauth callback failed: missing_code", file=sys.stderr)
            return 3
        code = code_values[0]

    client = InstagramOAuthClient(
        ctx.settings.instagram_app_id, ctx.settings.instagram_app_secret.get_secret_value(),
        ctx.settings.instagram_token_url, ctx.settings.instagram_graph_url,
        connect_timeout=ctx.settings.instagram_connect_timeout_seconds,
        read_timeout=ctx.settings.instagram_read_timeout_seconds,
    )
    try:
        short_lived = client.exchange_code(code, pkce.verifier, redirect_uri)
        long_lived = client.exchange_long_lived_token(short_lived.access_token)
        identity = client.fetch_account_identity(long_lived.access_token)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4
    finally:
        client.close()

    now = datetime.now(UTC)
    ctx.credential_backend().save_credential(OAuthCredential(
        access_token=long_lived.access_token, refresh_token=None,  # see InstagramOAuthClient's docstring
        expires_at=now + timedelta(seconds=long_lived.expires_in),
        scope=",".join(SCOPES),
        provider="instagram", account_reference=account,
        channel_id=identity.account_id, channel_title=identity.username,
        created_at=now, last_refreshed_at=now,
    ))

    print(json.dumps({
        "provider": "instagram",
        "account_reference": account,
        "account_id": identity.account_id,
        "username": identity.username,
        "token_expires_in_days": round(long_lived.expires_in / 86400, 1),
    }, indent=2))
    return 0


_DOCTOR_STATUS_RANK = {"PASS": 0, "WARN": 1, "NOT_CONFIGURED": 2, "APP_REVIEW_REQUIRED": 2, "FAIL": 3}
_DOCTOR_EXIT_CODE = {"PASS": 0, "WARN": 0, "NOT_CONFIGURED": 2, "APP_REVIEW_REQUIRED": 2, "FAIL": 1}


def cmd_publisher_doctor(args: argparse.Namespace, ctx: AppContext) -> int:
    """Local-first readiness report for publisher accounts. Dispatches to
    the provider-specific check list below."""
    if args.provider == "youtube":
        return _cmd_publisher_doctor_youtube(args, ctx)
    if args.provider == "tiktok":
        return _cmd_publisher_doctor_tiktok(args, ctx)
    if args.provider == "instagram":
        return _cmd_publisher_doctor_instagram(args, ctx)
    print(f"unsupported publisher provider: {args.provider}", file=sys.stderr)  # pragma: no cover
    return 2


def _cmd_publisher_doctor_youtube(args: argparse.Namespace, ctx: AppContext) -> int:
    """DB/storage/credential-store/ffmpeg reachability and one account's
    token state, all without any network call by default. --check-remote
    additionally attempts a real token refresh and a read-only
    channel-identity fetch. Never prints a secret or token -- only
    booleans, timestamps, and redacted error summaries."""
    import os as os_module
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as sa_text

    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.db.schema import SCHEMA_VERSION
    from reel_harness.observability import redact
    from reel_harness.publisher.secret_store import SecretStoreError

    account = args.account or "default"
    checks: list[dict] = []

    def add(name: str, status: str, detail: str | None = None) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        with ctx.session_factory() as session:
            session.execute(sa_text("SELECT 1"))
            version = session.execute(sa_text("SELECT version FROM schema_migrations")).scalar_one()
        add("database", "PASS")
        add(
            "schema_version", "PASS" if version == SCHEMA_VERSION else "FAIL",
            f"v{version} (expected v{SCHEMA_VERSION})",
        )
    except Exception as exc:  # noqa: BLE001 - doctor must report, never raise
        detail = (redact(str(exc)) or "")[:200]
        add("database", "FAIL", f"{type(exc).__name__}: {detail}")
        add("schema_version", "FAIL", "unknown -- database unreachable")

    root = ctx.storage.root_dir
    if root.is_dir() and os_module.access(root, os_module.W_OK):
        add("storage", "PASS")
    else:
        add("storage", "FAIL", "storage root missing or not writable")
    try:
        probe = root / ".publisher_doctor_probe"
        probe.mkdir(parents=True, exist_ok=True)
        probe.rmdir()
        add("final_video_path_access", "PASS", "per-job final/ directories can be created under the storage root")
    except OSError as exc:
        add("final_video_path_access", "FAIL", (redact(str(exc)) or "")[:200])

    try:
        from reel_harness.providers.youtube_publisher import YouTubePublisher  # noqa: F401

        add("publisher_registry", "PASS", "youtube adapter importable")
    except Exception as exc:  # noqa: BLE001
        add("publisher_registry", "FAIL", type(exc).__name__)

    chunk_size = ctx.settings.youtube_upload_chunk_size
    if chunk_size > 0 and chunk_size % 262144 == 0:
        add("upload_chunk_size", "PASS", str(chunk_size))
    else:
        add("upload_chunk_size", "FAIL", f"{chunk_size} is not a positive multiple of 262144")

    if ctx.settings.youtube_client_id and ctx.settings.youtube_client_secret.get_secret_value():
        add("oauth_client_config", "PASS")
    else:
        add(
            "oauth_client_config", "NOT_CONFIGURED",
            "REEL_HARNESS_YOUTUBE_CLIENT_ID / REEL_HARNESS_YOUTUBE_CLIENT_SECRET not set",
        )

    add("public_upload_feature_flag", "PASS", f"allow_public_upload={ctx.settings.allow_public_upload}")

    backend = None
    try:
        backend = ctx.credential_backend()
        add(
            "credential_backend", "PASS",
            f"{backend.__class__.__name__} rooted outside the repository (repo-internal paths are rejected "
            "at construction)",
        )
    except SecretStoreError as exc:
        add("credential_backend", "FAIL", str(exc))

    cred = backend.get_credential("youtube", account) if backend is not None else None
    if backend is None:
        pass
    elif cred is None:
        add(
            "account_credential", "NOT_CONFIGURED",
            f"no saved credential for account {account!r} -- run publisher-auth youtube",
        )
    else:
        add("account_credential", "PASS", f"account={account!r}")
        add(
            "refresh_token_present", "PASS" if cred.refresh_token else "WARN",
            "present" if cred.refresh_token else "missing -- re-auth required once the access token expires",
        )
        if cred.invalid:
            add(
                "credential_valid", "FAIL",
                f"marked invalid after a failed refresh: {cred.last_refresh_error or 'unknown reason'}",
            )
        elif cred.expires_at is None:
            add("token_expiry", "WARN", "no expiry recorded")
        else:
            now = datetime.now(UTC)
            if cred.expires_at > now + timedelta(minutes=2):
                add("token_expiry", "PASS", f"valid until {cred.expires_at.isoformat()}")
            elif cred.refresh_token:
                add("token_expiry", "WARN", "access token expired/near-expiry but refreshable")
            else:
                add("token_expiry", "FAIL", "access token expired and no refresh token")

    deps = check_ffmpeg_available()
    add("ffmpeg", "PASS" if deps.ffmpeg_available else "FAIL")
    add("ffprobe", "PASS" if deps.ffprobe_available else "FAIL")

    add(
        "publication_worker_config", "PASS",
        f"lease_timeout={ctx.settings.lease_timeout_seconds}s "
        f"poll_interval={ctx.settings.worker_poll_interval_seconds}s",
    )

    client_configured = bool(
        ctx.settings.youtube_client_id and ctx.settings.youtube_client_secret.get_secret_value()
    )
    if not args.check_remote:
        # Not checking remote by default is expected, not a defect -- PASS,
        # never a status that could drag "overall" down on its own.
        add("remote_token_refresh", "PASS", "not requested (pass --check-remote)")
        add("remote_channel_identity", "PASS", "not requested (pass --check-remote)")
    elif not client_configured or cred is None:
        add("remote_token_refresh", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
        add("remote_channel_identity", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
    else:
        from reel_harness.providers.registry import _resolve_fresh_youtube_access_token
        from reel_harness.publisher.oauth_youtube import YouTubeOAuthClient

        token: str | None = None
        try:
            token = _resolve_fresh_youtube_access_token(ctx.settings, backend, account)
            add("remote_token_refresh", "PASS")
        except (ProviderAuthError, TransientProviderError) as exc:
            add("remote_token_refresh", "FAIL", (redact(str(exc)) or "")[:200])

        if token is None:
            add("remote_channel_identity", "FAIL", "skipped -- token refresh failed above")
        else:
            client = YouTubeOAuthClient(
                ctx.settings.youtube_client_id, ctx.settings.youtube_client_secret.get_secret_value(),
            )
            try:
                identity = client.fetch_channel_identity(token)
                add("remote_channel_identity", "PASS", f"channel_id={identity.channel_id}")
            except (ProviderAuthError, TransientProviderError) as exc:
                add("remote_channel_identity", "FAIL", (redact(str(exc)) or "")[:200])
            finally:
                client.close()

    overall = max((c["status"] for c in checks), key=lambda s: _DOCTOR_STATUS_RANK[s])

    if args.json:
        print(json.dumps({"provider": "youtube", "account_reference": account, "overall": overall,
                           "checks": checks}, indent=2))
    else:
        print(f"YouTube publisher doctor -- account={account!r} -- overall: {overall}")
        for c in checks:
            detail = f" -- {c['detail']}" if c.get("detail") else ""
            print(f"  [{c['status']:^13}] {c['name']}{detail}")

    return _DOCTOR_EXIT_CODE[overall]


def _cmd_publisher_doctor_tiktok(args: argparse.Namespace, ctx: AppContext) -> int:
    """Local-first readiness report for TikTok publishing -- mirrors the
    YouTube doctor's shape. --check-remote additionally attempts a real
    token refresh and a read-only creator_info query, which also reveals
    whether the app has passed TikTok's review (surfaced as the distinct
    `app_review_status` check, never hidden inside a generic failure --
    see docs/PUBLISHING.md). Never prints a secret or token."""
    import os as os_module
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as sa_text

    from reel_harness.config import ProviderConfigurationError, validate_tiktok_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.db.schema import SCHEMA_VERSION
    from reel_harness.observability import redact
    from reel_harness.publisher.secret_store import SecretStoreError

    account = args.account or "default"
    checks: list[dict] = []

    def add(name: str, status: str, detail: str | None = None) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        with ctx.session_factory() as session:
            session.execute(sa_text("SELECT 1"))
            version = session.execute(sa_text("SELECT version FROM schema_migrations")).scalar_one()
        add("database", "PASS")
        add(
            "schema_version", "PASS" if version == SCHEMA_VERSION else "FAIL",
            f"v{version} (expected v{SCHEMA_VERSION})",
        )
    except Exception as exc:  # noqa: BLE001 - doctor must report, never raise
        detail = (redact(str(exc)) or "")[:200]
        add("database", "FAIL", f"{type(exc).__name__}: {detail}")
        add("schema_version", "FAIL", "unknown -- database unreachable")

    root = ctx.storage.root_dir
    if root.is_dir() and os_module.access(root, os_module.W_OK):
        add("storage", "PASS")
    else:
        add("storage", "FAIL", "storage root missing or not writable")

    try:
        from reel_harness.providers.tiktok_publisher import TikTokPublisher  # noqa: F401

        add("publisher_registry", "PASS", "tiktok adapter importable")
    except Exception as exc:  # noqa: BLE001
        add("publisher_registry", "FAIL", type(exc).__name__)

    chunk_size = ctx.settings.tiktok_upload_chunk_size
    add("upload_chunk_size", "PASS" if chunk_size > 0 else "FAIL", str(chunk_size))

    try:
        validate_tiktok_credentials_configured(ctx.settings)
        add("oauth_client_config", "PASS")
    except ProviderConfigurationError as exc:
        add("oauth_client_config", "NOT_CONFIGURED", str(exc))

    backend = None
    try:
        backend = ctx.credential_backend()
        add(
            "credential_backend", "PASS",
            f"{backend.__class__.__name__} rooted outside the repository (repo-internal paths are rejected "
            "at construction)",
        )
    except SecretStoreError as exc:
        add("credential_backend", "FAIL", str(exc))

    cred = backend.get_credential("tiktok", account) if backend is not None else None
    if backend is None:
        pass
    elif cred is None:
        add(
            "account_credential", "NOT_CONFIGURED",
            f"no saved credential for account {account!r} -- run publisher-auth tiktok",
        )
    else:
        add("account_credential", "PASS", f"account={account!r}")
        add(
            "refresh_token_present", "PASS" if cred.refresh_token else "WARN",
            "present" if cred.refresh_token else "missing -- re-auth required once the access token expires",
        )
        add(
            "required_scope_granted", "PASS" if "video.publish" in (cred.scope or "") else "WARN",
            cred.scope or "no scope recorded",
        )
        if cred.invalid:
            add(
                "credential_valid", "FAIL",
                f"marked invalid after a failed refresh: {cred.last_refresh_error or 'unknown reason'}",
            )
        elif cred.expires_at is None:
            add("token_expiry", "WARN", "no expiry recorded")
        else:
            now = datetime.now(UTC)
            if cred.expires_at > now + timedelta(minutes=2):
                add("token_expiry", "PASS", f"valid until {cred.expires_at.isoformat()}")
            elif cred.refresh_token:
                add("token_expiry", "WARN", "access token expired/near-expiry but refreshable")
            else:
                add("token_expiry", "FAIL", "access token expired and no refresh token")
        if cred.refresh_expires_at is not None:
            if cred.refresh_expires_at > datetime.now(UTC):
                add("refresh_token_expiry", "PASS", f"valid until {cred.refresh_expires_at.isoformat()}")
            else:
                add("refresh_token_expiry", "FAIL", "refresh token expired -- re-run publisher-auth tiktok")

    deps = check_ffmpeg_available()
    add("ffmpeg", "PASS" if deps.ffmpeg_available else "FAIL")
    add("ffprobe", "PASS" if deps.ffprobe_available else "FAIL")

    add(
        "publication_worker_config", "PASS",
        f"lease_timeout={ctx.settings.lease_timeout_seconds}s "
        f"poll_interval={ctx.settings.worker_poll_interval_seconds}s",
    )

    client_configured = bool(
        ctx.settings.tiktok_client_key and ctx.settings.tiktok_client_secret.get_secret_value()
        and ctx.settings.tiktok_redirect_uri
    )
    if not args.check_remote:
        add("remote_token_refresh", "PASS", "not requested (pass --check-remote)")
        add("remote_creator_info", "PASS", "not requested (pass --check-remote)")
        add("app_review_status", "PASS", "not requested (pass --check-remote)")
    elif not client_configured or cred is None:
        add("remote_token_refresh", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
        add("remote_creator_info", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
        add("app_review_status", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
    else:
        from reel_harness.providers.registry import _resolve_fresh_tiktok_access_token
        from reel_harness.providers.tiktok_publisher import TikTokPublisher

        token: str | None = None
        try:
            token = _resolve_fresh_tiktok_access_token(ctx.settings, backend, account)
            add("remote_token_refresh", "PASS")
        except (ProviderAuthError, TransientProviderError) as exc:
            add("remote_token_refresh", "FAIL", (redact(str(exc)) or "")[:200])

        if token is None:
            add("remote_creator_info", "FAIL", "skipped -- token refresh failed above")
            add("app_review_status", "FAIL", "skipped -- token refresh failed above")
        else:
            publisher = TikTokPublisher(
                access_token_provider=lambda: token, base_url=ctx.settings.tiktok_base_url,
                connect_timeout=ctx.settings.tiktok_connect_timeout_seconds,
                read_timeout=ctx.settings.tiktok_read_timeout_seconds,
            )
            try:
                creator_info = publisher.get_creator_info()
                if creator_info is None:
                    add("remote_creator_info", "FAIL", "no creator_info returned")
                    add("app_review_status", "FAIL", "cannot determine -- creator_info unavailable")
                else:
                    add(
                        "remote_creator_info", "PASS",
                        f"account={creator_info.account_identifier!r}",
                    )
                    if creator_info.allowed_privacy_values == frozenset({"SELF_ONLY"}):
                        add(
                            "app_review_status", "APP_REVIEW_REQUIRED",
                            "creator_info reports only SELF_ONLY as allowed -- this app has not passed "
                            "TikTok's review (see docs/PUBLISHING.md)",
                        )
                    else:
                        add(
                            "app_review_status", "PASS",
                            f"allowed privacy levels: {sorted(creator_info.allowed_privacy_values)}",
                        )
                    add("account_privacy_options", "PASS", f"{sorted(creator_info.allowed_privacy_values)}")
                    max_duration = creator_info.max_post_duration_sec
                    add(
                        "max_video_length", "PASS" if max_duration else "WARN",
                        f"{max_duration}s" if max_duration else "not reported",
                    )
            except (ProviderAuthError, TransientProviderError) as exc:
                add("remote_creator_info", "FAIL", (redact(str(exc)) or "")[:200])
                add("app_review_status", "FAIL", "skipped -- creator_info query failed above")
            finally:
                publisher.close()

    overall = max((c["status"] for c in checks), key=lambda s: _DOCTOR_STATUS_RANK[s])

    if args.json:
        print(json.dumps({"provider": "tiktok", "account_reference": account, "overall": overall,
                           "checks": checks}, indent=2))
    else:
        print(f"TikTok publisher doctor -- account={account!r} -- overall: {overall}")
        for c in checks:
            detail = f" -- {c['detail']}" if c.get("detail") else ""
            print(f"  [{c['status']:^13}] {c['name']}{detail}")

    return _DOCTOR_EXIT_CODE[overall]


def _cmd_publisher_doctor_instagram(args: argparse.Namespace, ctx: AppContext) -> int:
    """Local-first readiness report for Instagram publishing -- mirrors
    the TikTok/YouTube doctors' shape. --check-remote additionally
    attempts a real token refresh and a read-only account-info +
    publishing-limit query. Never prints a secret or token."""
    import os as os_module
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text as sa_text

    from reel_harness.config import ProviderConfigurationError, validate_instagram_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.db.schema import SCHEMA_VERSION
    from reel_harness.observability import redact
    from reel_harness.publisher.secret_store import SecretStoreError

    account = args.account or "default"
    checks: list[dict] = []

    def add(name: str, status: str, detail: str | None = None) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    try:
        with ctx.session_factory() as session:
            session.execute(sa_text("SELECT 1"))
            version = session.execute(sa_text("SELECT version FROM schema_migrations")).scalar_one()
        add("database", "PASS")
        add(
            "schema_version", "PASS" if version == SCHEMA_VERSION else "FAIL",
            f"v{version} (expected v{SCHEMA_VERSION})",
        )
    except Exception as exc:  # noqa: BLE001 - doctor must report, never raise
        detail = (redact(str(exc)) or "")[:200]
        add("database", "FAIL", f"{type(exc).__name__}: {detail}")
        add("schema_version", "FAIL", "unknown -- database unreachable")

    root = ctx.storage.root_dir
    if root.is_dir() and os_module.access(root, os_module.W_OK):
        add("storage", "PASS")
    else:
        add("storage", "FAIL", "storage root missing or not writable")

    try:
        from reel_harness.providers.instagram_publisher import InstagramPublisher  # noqa: F401

        add("publisher_registry", "PASS", "instagram adapter importable")
    except Exception as exc:  # noqa: BLE001
        add("publisher_registry", "FAIL", type(exc).__name__)

    try:
        validate_instagram_credentials_configured(ctx.settings)
        add("oauth_client_config", "PASS")
    except ProviderConfigurationError as exc:
        add("oauth_client_config", "NOT_CONFIGURED", str(exc))

    backend = None
    try:
        backend = ctx.credential_backend()
        add(
            "credential_backend", "PASS",
            f"{backend.__class__.__name__} rooted outside the repository (repo-internal paths are rejected "
            "at construction)",
        )
    except SecretStoreError as exc:
        add("credential_backend", "FAIL", str(exc))

    cred = backend.get_credential("instagram", account) if backend is not None else None
    if backend is None:
        pass
    elif cred is None:
        add(
            "account_credential", "NOT_CONFIGURED",
            f"no saved credential for account {account!r} -- run publisher-auth instagram",
        )
    else:
        add("account_credential", "PASS", f"account={account!r} (instagram_account_id={cred.channel_id!r})")
        if cred.invalid:
            add(
                "credential_valid", "FAIL",
                f"marked invalid after a failed refresh: {cred.last_refresh_error or 'unknown reason'}",
            )
        elif cred.expires_at is None:
            add("token_expiry", "WARN", "no expiry recorded")
        else:
            now = datetime.now(UTC)
            if cred.expires_at > now + timedelta(minutes=2):
                add("token_expiry", "PASS", f"valid until {cred.expires_at.isoformat()}")
            else:
                # Instagram's long-lived token refreshes itself (no
                # separate refresh_token -- see oauth_instagram) as long
                # as it's at least 24h old and not yet fully expired;
                # this doctor can't know that age/expiry margin without
                # attempting the refresh, so WARN rather than FAIL here.
                add("token_expiry", "WARN", "access token expired/near-expiry -- self-refresh will be attempted")

    deps = check_ffmpeg_available()
    add("ffmpeg", "PASS" if deps.ffmpeg_available else "FAIL")
    add("ffprobe", "PASS" if deps.ffprobe_available else "FAIL")

    add(
        "publication_worker_config", "PASS",
        f"lease_timeout={ctx.settings.lease_timeout_seconds}s "
        f"poll_interval={ctx.settings.worker_poll_interval_seconds}s",
    )

    client_configured = bool(
        ctx.settings.instagram_app_id and ctx.settings.instagram_app_secret.get_secret_value()
        and ctx.settings.instagram_redirect_uri
    )
    if not args.check_remote:
        add("remote_token_refresh", "PASS", "not requested (pass --check-remote)")
        add("remote_account_info", "PASS", "not requested (pass --check-remote)")
        add("account_eligibility_status", "PASS", "not requested (pass --check-remote)")
    elif not client_configured or cred is None:
        add("remote_token_refresh", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
        add("remote_account_info", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
        add("account_eligibility_status", "NOT_CONFIGURED", "NOT RUN — credentials not configured")
    else:
        from reel_harness.providers.instagram_publisher import InstagramPublisher
        from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

        token: str | None = None
        try:
            token = _resolve_fresh_instagram_access_token(ctx.settings, backend, account)
            add("remote_token_refresh", "PASS")
        except (ProviderAuthError, TransientProviderError) as exc:
            add("remote_token_refresh", "FAIL", (redact(str(exc)) or "")[:200])

        if token is None:
            add("remote_account_info", "FAIL", "skipped -- token refresh failed above")
            add("account_eligibility_status", "FAIL", "skipped -- token refresh failed above")
        else:
            publisher = InstagramPublisher(
                access_token_provider=lambda: token, graph_url=ctx.settings.instagram_graph_url,
                api_version=ctx.settings.instagram_graph_api_version, account_id=cred.channel_id,
                connect_timeout=ctx.settings.instagram_connect_timeout_seconds,
                read_timeout=ctx.settings.instagram_read_timeout_seconds,
            )
            try:
                account_info = publisher.get_creator_info()
                if account_info is None:
                    add("remote_account_info", "FAIL", "no account info returned")
                    add("account_eligibility_status", "FAIL", "cannot determine -- account info unavailable")
                else:
                    add(
                        "remote_account_info", "PASS",
                        f"account={account_info.display_name!r} id={account_info.account_identifier!r}",
                    )
                    if "publishing_limit_reached" in account_info.warnings:
                        add(
                            "account_eligibility_status", "WARN",
                            "this account has reached Instagram's 100-posts-per-24-hours publishing limit "
                            "(resets on a rolling basis -- see docs/PUBLISHING.md)",
                        )
                    elif account_info.warnings:
                        add("account_eligibility_status", "FAIL", "; ".join(account_info.warnings))
                    else:
                        add("account_eligibility_status", "PASS", "account is Reels-eligible")
                    add(
                        "max_video_length", "PASS" if account_info.max_post_duration_sec else "WARN",
                        f"{account_info.max_post_duration_sec}s" if account_info.max_post_duration_sec
                        else "not reported",
                    )
            except (ProviderAuthError, TransientProviderError) as exc:
                add("remote_account_info", "FAIL", (redact(str(exc)) or "")[:200])
                add("account_eligibility_status", "FAIL", "skipped -- account info query failed above")
            finally:
                publisher.close()

    overall = max((c["status"] for c in checks), key=lambda s: _DOCTOR_STATUS_RANK[s])

    if args.json:
        print(json.dumps({"provider": "instagram", "account_reference": account, "overall": overall,
                           "checks": checks}, indent=2))
    else:
        print(f"Instagram publisher doctor -- account={account!r} -- overall: {overall}")
        for c in checks:
            detail = f" -- {c['detail']}" if c.get("detail") else ""
            print(f"  [{c['status']:^13}] {c['name']}{detail}")

    return _DOCTOR_EXIT_CODE[overall]


def cmd_publisher_account_list(args: argparse.Namespace, ctx: AppContext) -> int:
    """Lists saved account aliases for a publisher provider -- never a
    token, only safe identity/status fields."""
    from reel_harness.publisher.credentials import publisher_account_safe_metadata

    backend = ctx.credential_backend()
    aliases = backend.list_accounts(args.provider)
    accounts = []
    for alias in aliases:
        cred = backend.get_credential(args.provider, alias)
        if cred is None:
            accounts.append({
                "account_reference": alias, "channel_id": None, "channel_title": None,
                "has_refresh_token": False, "expires_at": None, "invalid": False,
            })
            continue
        safe = publisher_account_safe_metadata(cred)
        accounts.append({
            key: safe[key]
            for key in ("account_reference", "channel_id", "channel_title", "has_refresh_token",
                        "expires_at", "invalid")
        })
    print(json.dumps({"provider": args.provider, "accounts": accounts}, indent=2))
    return 0


def cmd_publisher_account_show(args: argparse.Namespace, ctx: AppContext) -> int:
    """Shows one saved account's safe metadata. Never prints access_token,
    refresh_token, client_secret, an authorization code, a PKCE verifier, or
    the raw stored JSON."""
    from reel_harness.publisher.credentials import publisher_account_safe_metadata

    backend = ctx.credential_backend()
    cred = backend.get_credential(args.provider, args.alias)
    if cred is None:
        print(f"no saved credential for account {args.alias!r}", file=sys.stderr)
        return 2
    print(json.dumps(publisher_account_safe_metadata(cred), indent=2))
    return 0


def cmd_publisher_account_remove(args: argparse.Namespace, ctx: AppContext) -> int:
    """Deletes the LOCAL saved credential only. This never revokes remote
    authorization at Google -- that is a separate, much larger-blast-radius
    action (it invalidates every token issued to this OAuth client, for
    every account) and is deliberately not implemented by this command."""
    if not args.confirm:
        print(
            "refusing to remove without --confirm (this deletes the LOCAL saved credential only -- "
            "it does NOT revoke remote authorization at Google)",
            file=sys.stderr,
        )
        return 2
    backend = ctx.credential_backend()
    if not backend.has_credential(args.provider, args.alias):
        print(f"no saved credential for account {args.alias!r}", file=sys.stderr)
        return 2
    backend.revoke_credential(args.provider, args.alias)
    print(json.dumps({"provider": args.provider, "account_reference": args.alias, "removed": True}, indent=2))
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


def _smoke_publisher_tiktok(
    ctx: AppContext, account: str, upload_private_test: bool, confirm_test_upload: bool,
    confirm_platform_options: bool,
) -> int:
    """Read-only by default: credential, token refresh, creator_info,
    scope, publishability, and this account's actual privacy options --
    never uploads. The opt-in test upload requires all three of
    --upload-private-test/--confirm-test-upload/--confirm-platform-options
    AND real application permission (confirmed via creator_info) -- always
    the most restrictive privacy (SELF_ONLY), a very short scratch clip,
    clear test wording, and comments/duet/stitch all disabled. Never
    public, never auto-deleted."""
    from reel_harness.config import ProviderConfigurationError, validate_tiktok_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, PublisherPermissionDeniedError, TransientProviderError
    from reel_harness.observability import redact

    try:
        validate_tiktok_credentials_configured(ctx.settings)
    except ProviderConfigurationError:
        print(
            "tiktok publisher OAuth client not configured -- set REEL_HARNESS_TIKTOK_CLIENT_KEY, "
            "REEL_HARNESS_TIKTOK_CLIENT_SECRET, and REEL_HARNESS_TIKTOK_REDIRECT_URI.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    backend = ctx.credential_backend()
    if not backend.has_credential("tiktok", account):
        print(
            f"no saved tiktok credential for account {account!r} -- run "
            f"`reel-harness publisher-auth tiktok --account {account}` first.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    from reel_harness.providers.registry import _resolve_fresh_tiktok_access_token
    from reel_harness.providers.tiktok_publisher import TikTokPublisher

    try:
        access_token = _resolve_fresh_tiktok_access_token(ctx.settings, backend, account)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4

    publisher = TikTokPublisher(
        access_token_provider=lambda: access_token, base_url=ctx.settings.tiktok_base_url,
        connect_timeout=ctx.settings.tiktok_connect_timeout_seconds,
        read_timeout=ctx.settings.tiktok_read_timeout_seconds, max_retries=0,
    )
    try:
        creator_info = publisher.get_creator_info()
    except ProviderAuthError as exc:
        publisher.close()
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except PublisherPermissionDeniedError as exc:
        publisher.close()
        print(f"permission error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        publisher.close()
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4

    # Real application permission means creator_info actually reports SOME
    # allowed privacy level -- an empty set (as opposed to the normal
    # unaudited-app case, which is always at least {SELF_ONLY}) is the
    # signature of a deeper permission problem than the audit-status gate.
    app_permission_available = creator_info is not None and bool(creator_info.allowed_privacy_values)
    app_review_status = "unknown"
    if creator_info is not None:
        app_review_status = (
            "app_review_required" if creator_info.allowed_privacy_values == frozenset({"SELF_ONLY"})
            else "passed"
        )
    summary: dict = {
        "provider": "tiktok", "account_reference": account,
        "account_identifier": creator_info.account_identifier if creator_info else None,
        "allowed_privacy_values": sorted(creator_info.allowed_privacy_values) if creator_info else [],
        "app_review_status": app_review_status,
        "comments_configurable": creator_info.comments_configurable if creator_info else None,
        "remix_configurable": creator_info.remix_configurable if creator_info else None,
        "max_post_duration_sec": creator_info.max_post_duration_sec if creator_info else None,
        "upload_permission_checked": False, "test_upload": None,
    }

    if upload_private_test and confirm_test_upload and confirm_platform_options:
        if not app_permission_available:
            print("TikTok private upload smoke: NOT RUN — application permission not available")
            summary["test_upload"] = {"ran": False, "reason": "application permission not available"}
        else:
            summary["upload_permission_checked"] = True
            summary["test_upload"] = _run_tiktok_test_upload(ctx, access_token, creator_info)
    elif upload_private_test or confirm_test_upload or confirm_platform_options:
        print(
            "--upload-private-test, --confirm-test-upload, and --confirm-platform-options must all be given "
            "to run the opt-in test upload -- read-only creator_info check only.",
            file=sys.stderr,
        )

    publisher.close()
    print(json.dumps(summary, indent=2))
    return 0


def _run_tiktok_test_upload(ctx: AppContext, access_token: str, creator_info) -> dict:
    import hashlib
    import shutil
    import tempfile
    import uuid
    from pathlib import Path

    from reel_harness.media.deps import check_ffmpeg_available
    from reel_harness.media.runner import run
    from reel_harness.providers.base import PublicationMetadata
    from reel_harness.providers.tiktok_publisher import (
        TikTokPostOptions,
        TikTokPublisher,
        build_post_text,
        validate_publish_options,
    )

    deps = check_ffmpeg_available()
    if not deps.all_available:
        return {"ran": False, "reason": "ffmpeg/ffprobe not available"}

    # Every default: SELF_ONLY, comments/duet/stitch disabled, no disclosure
    # toggles -- the most restrictive combination this account allows.
    options = TikTokPostOptions()
    try:
        validate_publish_options(creator_info, "SELF_ONLY", options)
    except Exception as exc:  # noqa: BLE001 - reported, never crashes the smoke command
        return {"ran": False, "reason": f"platform options rejected: {exc}"}

    scratch = Path(tempfile.mkdtemp(prefix="reel-harness-tiktok-smoke-"))
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
        post_text = build_post_text("[reel-harness provider-smoke test upload -- safe to ignore]")
        publisher = TikTokPublisher(
            access_token_provider=lambda: access_token, base_url=ctx.settings.tiktok_base_url,
            chunk_size=ctx.settings.tiktok_upload_chunk_size,
            connect_timeout=ctx.settings.tiktok_connect_timeout_seconds,
            read_timeout=ctx.settings.tiktok_read_timeout_seconds, max_retries=0,
        )
        try:
            metadata = PublicationMetadata(
                title=post_text, description="", tags=[], category_id="", privacy_status="SELF_ONLY",
                made_for_kids=False, platform_options=options.as_platform_options(),
            )
            session = publisher.create_upload_session(
                metadata, len(video_bytes), "video/mp4", str(uuid.uuid4()),
            )
            chunk_result = publisher.upload_chunk(session, video_bytes, 0, len(video_bytes))
        finally:
            publisher.close()

        return {
            "ran": True,
            "provider_video_id": chunk_result.provider_video_id or session.provider_reference,
            "privacy_status": "SELF_ONLY",
            "checksum_prefix": hashlib.sha256(video_bytes).hexdigest()[:12],
            "note": "remote deletion is never automatic -- see docs/OPERATIONS.md",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _smoke_publisher_instagram(
    ctx: AppContext, account: str, upload_public_test: bool, confirm_test_upload: bool,
    confirm_public_upload: bool, confirm_platform_options: bool,
) -> int:
    """Read-only by default: credential, token refresh, account info, and
    current publishing-limit usage -- never uploads. The opt-in test
    upload requires ALL FOUR of --upload-public-test/--confirm-test-upload/
    --confirm-public-upload/--confirm-platform-options AND real account
    eligibility (confirmed via account info) -- Instagram has no private-
    visibility option, so this is never offered under a misleading
    '--upload-private-test' name the way YouTube's/TikTok's smoke is: any
    real Instagram test upload IS genuinely public. A very short (3.5s,
    above Instagram's documented 3s minimum) scratch clip, clear test
    wording, comments disabled, Reels-tab only (not also Feed). Never
    auto-deleted."""
    from reel_harness.config import ProviderConfigurationError, validate_instagram_credentials_configured
    from reel_harness.core.errors import ProviderAuthError, PublisherPermissionDeniedError, TransientProviderError
    from reel_harness.observability import redact

    try:
        validate_instagram_credentials_configured(ctx.settings)
    except ProviderConfigurationError:
        print(
            "instagram publisher OAuth client not configured -- set REEL_HARNESS_INSTAGRAM_APP_ID, "
            "REEL_HARNESS_INSTAGRAM_APP_SECRET, and REEL_HARNESS_INSTAGRAM_REDIRECT_URI.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    backend = ctx.credential_backend()
    cred = backend.get_credential("instagram", account)
    if cred is None:
        print(
            f"no saved instagram credential for account {account!r} -- run "
            f"`reel-harness publisher-auth instagram --account {account}` first.",
            file=sys.stderr,
        )
        print("NOT RUN — credentials not configured")
        return 2

    from reel_harness.providers.instagram_publisher import InstagramPublisher
    from reel_harness.providers.registry import _resolve_fresh_instagram_access_token

    try:
        access_token = _resolve_fresh_instagram_access_token(ctx.settings, backend, account)
    except ProviderAuthError as exc:
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4

    publisher = InstagramPublisher(
        access_token_provider=lambda: access_token, graph_url=ctx.settings.instagram_graph_url,
        api_version=ctx.settings.instagram_graph_api_version, account_id=cred.channel_id,
        connect_timeout=ctx.settings.instagram_connect_timeout_seconds,
        read_timeout=ctx.settings.instagram_read_timeout_seconds, max_retries=0,
    )
    try:
        account_info = publisher.get_creator_info()
    except ProviderAuthError as exc:
        publisher.close()
        print(f"auth error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except PublisherPermissionDeniedError as exc:
        publisher.close()
        print(f"permission error: {redact(str(exc))}", file=sys.stderr)
        return 3
    except TransientProviderError as exc:
        publisher.close()
        print(f"transient error: {redact(str(exc))}", file=sys.stderr)
        return 4

    # Real eligibility means account info reports no warnings at all
    # (a non-professional account type, or an exhausted publishing limit,
    # are both concrete blockers -- see providers.instagram_publisher.get_creator_info).
    app_permission_available = account_info is not None and not account_info.warnings
    summary: dict = {
        "provider": "instagram", "account_reference": account,
        "account_identifier": account_info.account_identifier if account_info else None,
        "display_name": account_info.display_name if account_info else None,
        "warnings": account_info.warnings if account_info else [],
        "max_post_duration_sec": account_info.max_post_duration_sec if account_info else None,
        "upload_permission_checked": False, "test_upload": None,
    }

    if upload_public_test and confirm_test_upload and confirm_public_upload and confirm_platform_options:
        if not app_permission_available:
            print("Instagram public upload smoke: NOT RUN — application permission not available")
            summary["test_upload"] = {"ran": False, "reason": "application permission not available"}
        else:
            summary["upload_permission_checked"] = True
            summary["test_upload"] = _run_instagram_test_upload(ctx, access_token, account_info)
    elif upload_public_test or confirm_test_upload or confirm_public_upload or confirm_platform_options:
        print(
            "--upload-public-test, --confirm-test-upload, --confirm-public-upload, and "
            "--confirm-platform-options must all be given to run the opt-in test upload -- read-only "
            "account-info check only.",
            file=sys.stderr,
        )

    publisher.close()
    print(json.dumps(summary, indent=2))
    return 0


def _run_instagram_test_upload(ctx: AppContext, access_token: str, account_info) -> dict:
    import hashlib
    import shutil
    import tempfile
    import time as time_module
    import uuid
    from pathlib import Path

    from reel_harness.media.deps import check_ffmpeg_available
    from reel_harness.media.runner import run
    from reel_harness.providers.base import PublicationMetadata
    from reel_harness.providers.instagram_publisher import (
        InstagramPublisher,
        InstagramReelsOptions,
        build_caption,
        validate_publish_options,
    )

    deps = check_ffmpeg_available()
    if not deps.all_available:
        return {"ran": False, "reason": "ffmpeg/ffprobe not available"}

    # Comments disabled, Reels-tab only (not also Feed), no collaborators/
    # disclosures -- the most restrictive combination this account allows.
    options = InstagramReelsOptions()
    try:
        validate_publish_options(account_info, options)
    except Exception as exc:  # noqa: BLE001 - reported, never crashes the smoke command
        return {"ran": False, "reason": f"platform options rejected: {exc}"}

    scratch = Path(tempfile.mkdtemp(prefix="reel-harness-instagram-smoke-"))
    try:
        video_path = scratch / "smoke.mp4"
        argv = [
            str(deps.ffmpeg.path), "-y",
            # 3.5s -- above Instagram's documented 3s Reels minimum.
            "-f", "lavfi", "-i", "testsrc=duration=3.5:size=320x568:rate=25",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=3.5",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-movflags", "+faststart",
            str(video_path),
        ]
        result = run(argv, timeout=30)
        if result.returncode != 0:
            return {"ran": False, "reason": "failed to build the local test clip"}

        video_bytes = video_path.read_bytes()
        caption = build_caption("[reel-harness provider-smoke test upload -- safe to ignore]")
        publisher = InstagramPublisher(
            access_token_provider=lambda: access_token, graph_url=ctx.settings.instagram_graph_url,
            api_version=ctx.settings.instagram_graph_api_version, account_id=account_info.account_identifier,
            connect_timeout=ctx.settings.instagram_connect_timeout_seconds,
            read_timeout=ctx.settings.instagram_read_timeout_seconds, max_retries=0,
        )
        try:
            metadata = PublicationMetadata(
                title=caption, description="", tags=[], category_id="", privacy_status="PUBLIC",
                made_for_kids=False, platform_options=options.as_platform_options(),
            )
            session = publisher.create_upload_session(
                metadata, len(video_bytes), "video/mp4", str(uuid.uuid4()),
            )
            chunk_result = publisher.upload_chunk(session, video_bytes, 0, len(video_bytes))
            if not chunk_result.completed or not chunk_result.provider_video_id:
                return {"ran": False, "reason": "upload did not complete in one shot"}

            container_id: str = chunk_result.provider_video_id
            status = None
            # Meta's own guidance: poll roughly once a minute, for no more
            # than 5 minutes -- scaled down for this short test clip.
            deadline = time_module.monotonic() + 60.0
            while time_module.monotonic() < deadline:
                status = publisher.get_processing_status(container_id)
                if status.processing_status in ("succeeded", "failed", "terminated"):
                    break
                time_module.sleep(3.0)
        finally:
            publisher.close()

        if status is None or status.processing_status != "succeeded":
            return {
                "ran": True, "published": False, "container_id": container_id,
                "reason": (
                    f"processing did not succeed within the smoke test's wait window "
                    f"(last status: {status.processing_status if status else 'unknown'})"
                ),
                "note": "the container may still complete later -- check publisher-doctor instagram directly; "
                        "remote deletion is never automatic -- see docs/OPERATIONS.md",
            }
        return {
            "ran": True, "published": True, "container_id": container_id,
            "publication_url": status.publication_url,
            "checksum_prefix": hashlib.sha256(video_bytes).hexdigest()[:12],
            "note": "remote deletion is never automatic -- see docs/OPERATIONS.md",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def cmd_fable_adapt_eval(args: argparse.Namespace, ctx: AppContext) -> int:
    """Measure adaptation quality across repeated runs.

    Opt-in and never part of a test suite: each run is a real, paid LLM
    call. The command states the call count up front and refuses to spend
    anything without --yes, because "I did not realise it would charge"
    is not a failure mode worth designing in.

    Pointing it at the fake provider costs nothing and is the right way
    to check the report itself before spending on the real one.
    """
    from reel_harness.ops.adapt_eval import SAMPLE_STORIES, evaluate, format_report

    if args.story:
        path = Path(args.story)
        if not path.is_file():
            print(f"story file not found: {path}", file=sys.stderr)
            return 2
        stories = {path.stem: path.read_text(encoding="utf-8")}
    else:
        stories = SAMPLE_STORIES

    director = ctx.narrative_director_for_project(None)
    provider = getattr(director, "provider_id", "?")
    model = getattr(director, "model_id", "?")
    calls = len(stories) * args.runs

    print(f"provider={provider} model={model}")
    print(f"{calls} adaptation call(s): {len(stories)} story/stories x {args.runs} run(s)")
    if provider != "fake":
        print("each call is billed by the provider, and a repair adds another")
        if not args.yes:
            print("refusing to spend without --yes", file=sys.stderr)
            return 2
    print()

    results = evaluate(
        director, stories, runs=args.runs, target_duration_sec=args.duration,
    )
    print(format_report(results, show_plans=args.show_plans))
    # A sweep where nothing adapted at all is a failure; individual bad
    # plans are findings to read, not a non-zero exit.
    return 0 if any(r.metrics is not None for r in results) else 1


def cmd_provider_smoke(args: argparse.Namespace, ctx: AppContext) -> int:
    """Opt-in operator check of a configured real provider: one request with
    retries disabled, real validation, secrets redacted, scratch files cleaned.
    The default test suites never run this."""
    if args.target == "llm":
        return _smoke_llm(ctx)
    if args.target == "asset":
        return _smoke_asset(ctx)
    if args.target == "publisher":
        if args.publisher_provider == "youtube":
            return _smoke_publisher_youtube(
                ctx, account=args.account or "default",
                upload_private_test=args.upload_private_test, confirm_test_upload=args.confirm_test_upload,
            )
        if args.publisher_provider == "tiktok":
            return _smoke_publisher_tiktok(
                ctx, account=args.account or "default",
                upload_private_test=args.upload_private_test, confirm_test_upload=args.confirm_test_upload,
                confirm_platform_options=args.confirm_platform_options,
            )
        if args.publisher_provider == "instagram":
            return _smoke_publisher_instagram(
                ctx, account=args.account or "default",
                upload_public_test=args.upload_public_test, confirm_test_upload=args.confirm_test_upload,
                confirm_public_upload=args.confirm_public_upload,
                confirm_platform_options=args.confirm_platform_options,
            )
        print(
            "usage: provider-smoke publisher {youtube|tiktok|instagram} [--account ALIAS] [...]", file=sys.stderr,
        )
        return 2
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


def cmd_fable_worker_run(args: argparse.Namespace, ctx: AppContext) -> int:
    from reel_harness.worker.daemon import default_worker_id
    from reel_harness.worker.fable_daemon import FableDaemon, FableDaemonConfig

    settings = ctx.settings
    config = FableDaemonConfig(
        worker_id=args.worker_id or default_worker_id(),
        poll_interval_seconds=(
            args.poll_interval if args.poll_interval is not None else settings.worker_poll_interval_seconds
        ),
        lease_timeout_seconds=args.lease_timeout or settings.lease_timeout_seconds,
        heartbeat_interval_seconds=settings.lease_heartbeat_seconds,
        max_shots=args.max_shots,
        idle_exit_after_seconds=args.idle_exit_after,
        stop_on_error=args.stop_on_error,
        allow_paid_generation=settings.allow_paid_generation,
        takes_per_shot=settings.fable_takes_per_shot,
    )
    daemon = FableDaemon(
        ctx.session_factory, ctx.fable_storage, ctx.cinematic_provider_for_shot, config,
    )

    def _signal_handler(signum, frame) -> None:  # pragma: no cover - exercised via CLI, not pytest
        daemon.request_stop(f"signal_{signum}")

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
    process_upload, process_status = _resolve_publisher_lanes(args)
    config = PublisherDaemonConfig(
        worker_id=args.worker_id or default_publisher_worker_id(),
        poll_interval_seconds=(
            args.poll_interval if args.poll_interval is not None else settings.worker_poll_interval_seconds
        ),
        lease_timeout_seconds=args.lease_timeout or settings.lease_timeout_seconds,
        max_publications=args.max_publications if args.max_publications is not None else None,
        idle_exit_after_seconds=args.idle_exit_after,
        stop_on_error=args.stop_on_error,
        process_upload=process_upload, process_status=process_status,
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


def cmd_serve(args: argparse.Namespace, ctx: AppContext) -> int:
    """Runs the API, render worker, and publisher worker together in one
    supervised process -- see ops.supervisor.Supervisor for the failure
    policy. Each of --api/--render-worker/--publisher-worker can be
    disabled individually (all three run by default)."""
    from reel_harness.ops.supervisor import Supervisor, SupervisorConfig
    from reel_harness.worker.daemon import DaemonConfig, default_worker_id
    from reel_harness.worker.publish_daemon import PublisherDaemonConfig, default_publisher_worker_id

    settings = ctx.settings
    host = args.host if args.host is not None else settings.api_host
    config = SupervisorConfig(
        run_api=args.api, run_render_worker=args.render_worker, run_publisher_worker=args.publisher_worker,
        host=host, port=args.port,
        render_workers=args.render_workers, publisher_workers=args.publisher_workers,
        fable_workers=args.fable_workers,
        shutdown_timeout_seconds=args.shutdown_timeout,
        render_daemon_config=DaemonConfig(
            worker_id=default_worker_id(), poll_interval_seconds=settings.worker_poll_interval_seconds,
            lease_timeout_seconds=settings.lease_timeout_seconds,
            heartbeat_interval_seconds=settings.lease_heartbeat_seconds,
        ),
        publisher_daemon_config=PublisherDaemonConfig(
            worker_id=default_publisher_worker_id(), poll_interval_seconds=settings.worker_poll_interval_seconds,
            lease_timeout_seconds=settings.lease_timeout_seconds,
            process_upload=True, process_status=True,
        ),
    )
    supervisor = Supervisor(ctx, config)

    def _signal_handler(signum, frame) -> None:  # pragma: no cover - exercised via CLI, not pytest
        supervisor.request_stop(f"signal_{signum}")

    signal.signal(signal.SIGINT, _signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _signal_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _signal_handler)

    return supervisor.run()


def build_parser() -> argparse.ArgumentParser:
    from reel_harness._version import __version__

    parser = argparse.ArgumentParser(prog="reel-harness")
    # argparse's "version" action prints and exits inside parse_args() itself,
    # before main() ever constructs an AppContext -- so --version never needs
    # a working DB/storage/provider config.
    parser.add_argument("--version", action="version", version=f"reel-harness {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor").set_defaults(func=cmd_doctor)

    preflight = sub.add_parser("preflight", help="Single operator readiness check before running for real")
    preflight.add_argument("--profile", choices=["fake", "production"], default="fake")
    preflight.add_argument(
        "--provider", action="append", choices=["llm", "tts", "asset"],
        help="Repeatable; scopes --check-remote to specific pipeline providers (llm/tts/asset)",
    )
    preflight.add_argument(
        "--publisher", action="append", choices=["youtube", "tiktok", "instagram"],
        help="Repeatable; scopes --check-remote to specific publishers (default: all three)",
    )
    preflight.add_argument(
        "--check-remote", action="store_true", help="Also attempt real, read-only remote checks",
    )
    preflight.add_argument("--json", action="store_true")
    preflight.set_defaults(func=cmd_preflight)

    sub.add_parser("db-status", help="Schema/migration/integrity summary").set_defaults(func=cmd_db_status)

    db_migrate_p = sub.add_parser("db-migrate", help="Apply pending schema migrations")
    db_migrate_p.add_argument("--dry-run", action="store_true", help="Report the plan; touch nothing")
    db_migrate_p.add_argument(
        "--backup-dir", default=None,
        help="Directory for the pre-migration safety backup (required unless --no-backup)",
    )
    db_migrate_p.add_argument(
        "--no-backup", action="store_true", help="Skip the pre-migration safety backup (not recommended)",
    )
    db_migrate_p.set_defaults(func=cmd_db_migrate)

    db_backup_p = sub.add_parser(
        "db-backup", help="Online backup with checksum manifest (SQLite or PostgreSQL, from DATABASE_URL)",
    )
    db_backup_p.add_argument(
        "--dest-dir", required=True, help="Directory to write the backup into (outside the repository)",
    )
    db_backup_p.set_defaults(func=cmd_db_backup)

    db_restore_p = sub.add_parser("db-restore", help="Restore the database from a db-backup archive")
    db_restore_p.add_argument(
        "backup_path",
        help="Path to a *.sqlite3.bak (SQLite) or *.pgdump (PostgreSQL) file produced by db-backup",
    )
    db_restore_p.add_argument(
        "--confirm-restore", action="store_true", help="Required -- this replaces the live database",
    )
    db_restore_p.add_argument(
        "--pre-restore-backup-dir", required=True,
        help="Directory for the automatic backup of the CURRENT database taken before restoring",
    )
    db_restore_p.set_defaults(func=cmd_db_restore)

    sub.add_parser(
        "db-verify", help="Integrity check, foreign keys, orphan rows, forbidden ACTIVE+unlocked rows",
    ).set_defaults(func=cmd_db_verify)

    storage_verify_p = sub.add_parser(
        "storage-verify", help="Cross-check job storage against the DB (checksums, manifests, orphans)",
    )
    storage_verify_p.add_argument(
        "--repair-safe", action="store_true",
        help="Also delete stale (>1h) leaked temp files -- never touches final.mp4/manifests/Publication status",
    )
    storage_verify_p.set_defaults(func=cmd_storage_verify)

    backup_create_p = sub.add_parser("backup-create", help="Portable archive of DB + jobs storage + journal")
    backup_create_p.add_argument(
        "--dest-path", required=True, help="Output archive path (outside the repository)",
    )
    backup_create_p.set_defaults(func=cmd_backup_create)

    backup_inspect_p = sub.add_parser(
        "backup-inspect", help="Read-only: validate and summarize a backup-create bundle",
    )
    backup_inspect_p.add_argument("bundle_path")
    backup_inspect_p.set_defaults(func=cmd_backup_inspect)

    backup_restore_p = sub.add_parser("backup-restore", help="Restore a backup-create bundle (destructive)")
    backup_restore_p.add_argument("bundle_path")
    backup_restore_p.add_argument(
        "--confirm-restore", action="store_true", help="Required -- this overwrites the destination",
    )
    backup_restore_p.set_defaults(func=cmd_backup_restore)

    incident_bundle_p = sub.add_parser(
        "incident-bundle", help="Diagnostics zip for offline incident analysis (secret-scanned before writing)",
    )
    incident_bundle_p.add_argument(
        "--dest-path", required=True, help="Output archive path (outside the repository)",
    )
    incident_bundle_p.set_defaults(func=cmd_incident_bundle)

    live_verify_p = sub.add_parser(
        "live-verify",
        help="Read-only live account sweep across YouTube/TikTok/Instagram (default), or upload tests",
    )
    live_verify_p.add_argument("--youtube", action="store_true")
    live_verify_p.add_argument("--tiktok", action="store_true")
    live_verify_p.add_argument("--instagram", action="store_true")
    live_verify_p.add_argument("--account", default=None, help="Default: 'default'")
    live_verify_p.add_argument(
        "--read-only", action="store_true",
        help="No-op -- read-only is always the default; --upload-tests is the only opt-out",
    )
    live_verify_p.add_argument(
        "--upload-tests", action="store_true",
        help="Opt-in: allow a real per-platform upload test IF that platform's confirm flag is also given",
    )
    live_verify_p.add_argument(
        "--confirm-youtube-private", action="store_true",
        help="Required (with --upload-tests) to run YouTube's private test upload",
    )
    live_verify_p.add_argument(
        "--confirm-tiktok-restricted", action="store_true",
        help="Required (with --upload-tests) to run TikTok's SELF_ONLY test upload",
    )
    live_verify_p.add_argument(
        "--confirm-instagram-public", action="store_true",
        help="Required (with --upload-tests) to run Instagram's public test Reel -- the strongest confirmation",
    )
    live_verify_p.add_argument("--json", action="store_true")
    live_verify_p.set_defaults(func=cmd_live_verify)

    release_manifest_p = sub.add_parser(
        "release-manifest", help="Build a release manifest (version/commit/schema/checksums/known limitations)",
    )
    release_manifest_p.add_argument("--dest-path", required=True)
    release_manifest_p.add_argument("--wheel-path", default=None)
    release_manifest_p.add_argument("--sdist-path", default=None)
    release_manifest_p.add_argument("--lock-path", default=None)
    release_manifest_p.add_argument(
        "--test-summary-json", default=None, help="Path to a JSON file to embed verbatim as test_summary",
    )
    release_manifest_p.add_argument(
        "--live-verification-status", default="not_run",
        help="Default 'not_run' -- pass a real status only after actually running live-verify",
    )
    release_manifest_p.set_defaults(func=cmd_release_manifest)

    release_check_p = sub.add_parser(
        "release-check", help="Pre-tag gate: git/version/lockfile/tests/mypy/ruff/secret-scan/artifact-scan",
    )
    release_check_p.add_argument(
        "--skip-slow", action="store_true", help="Skip full_pytest/mypy/ruff -- fast iterative check only",
    )
    release_check_p.add_argument("--pytest-timeout", type=float, default=900.0)
    release_check_p.add_argument("--json", action="store_true")
    release_check_p.set_defaults(func=cmd_release_check)

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

    fable_create = sub.add_parser("fable-create", help="Create a Fable cinematic project from a story")
    fable_create.add_argument("--title", required=True)
    story_source = fable_create.add_mutually_exclusive_group(required=True)
    story_source.add_argument("--story", help="Story text inline")
    story_source.add_argument("--story-file", help="Path to a UTF-8 text file containing the story")
    fable_create.add_argument("--language", default="ko")
    fable_create.add_argument("--genre", default=None)
    fable_create.add_argument("--tone", default=None)
    fable_create.add_argument("--duration", type=int, default=60, help="Target duration in seconds")
    fable_create.add_argument("--aspect-ratio", default="9:16", choices=("9:16", "16:9"))
    fable_create.add_argument(
        "--takes-per-shot", type=int, default=None, choices=(1, 2, 4),
        help="Candidate takes generated per shot (default: the operator-wide setting). "
             "Each take is a separate paid generation.",
    )
    fable_create.add_argument("--idempotency-key", default=None)
    fable_create.set_defaults(func=cmd_fable_create)

    fable_adapt = sub.add_parser("fable-adapt", help="Run story adaptation (DRAFT -> STORY_REVIEW)")
    fable_adapt.add_argument("project_id")
    fable_adapt.set_defaults(func=cmd_fable_adapt)

    fable_approve = sub.add_parser("fable-approve", help="Approve the current review gate")
    fable_approve.add_argument("project_id")
    fable_approve.add_argument("--step", required=True, choices=("story", "characters", "shots", "final"))
    fable_approve.set_defaults(func=cmd_fable_approve)

    fable_status = sub.add_parser("fable-status", help="Project status with per-shot/take detail (JSON)")
    fable_status.add_argument("project_id")
    fable_status.set_defaults(func=cmd_fable_status)

    fable_generate_references = sub.add_parser(
        "fable-generate-references",
        help="Generate every character's reference sheet (CASTING -> CHARACTER_REVIEW)",
    )
    fable_generate_references.add_argument("project_id")
    fable_generate_references.set_defaults(func=cmd_fable_generate_references)

    fable_reference = sub.add_parser(
        "fable-reference", help="Approve (default) or --reject one character's reference sheet",
    )
    fable_reference.add_argument("character_id")
    fable_reference.add_argument(
        "--reject", action="store_true",
        help="Un-approve and clear the fingerprint so the next generation run regenerates it",
    )
    fable_reference.set_defaults(func=cmd_fable_reference)

    fable_reference_smoke = sub.add_parser(
        "fable-reference-smoke",
        help="Generate one REAL reference image chain against the configured provider",
    )
    fable_reference_smoke.add_argument(
        "--confirm-paid-generation", action="store_true",
        help="Required when the configured provider charges money (2 images).",
    )
    fable_reference_smoke.add_argument(
        "--keep-output", default=None,
        help="Directory to copy the two images into, for looking at them yourself.",
    )
    fable_reference_smoke.set_defaults(func=cmd_fable_reference_smoke)

    fable_budget = sub.add_parser(
        "fable-budget", help="Set or show a project's spending limit (no flags = show only)",
    )
    fable_budget.add_argument("project_id")
    budget_action = fable_budget.add_mutually_exclusive_group()
    budget_action.add_argument("--limit", type=float, default=None, help="Spending ceiling")
    budget_action.add_argument(
        "--clear", action="store_true",
        help="Remove the limit (re-closes the paid-generation gate; spends nothing back)",
    )
    fable_budget.add_argument(
        "--currency", default=None, help="Currency for --limit (required with --limit)",
    )
    fable_budget.set_defaults(func=cmd_fable_budget)

    fable_estimate = sub.add_parser(
        "fable-estimate", help="Estimated cost of generating this project's shots (read-only)",
    )
    fable_estimate.add_argument("project_id")
    fable_estimate.set_defaults(func=cmd_fable_estimate)

    fable_list = sub.add_parser("fable-list")
    fable_list.set_defaults(func=cmd_fable_list)

    fable_select_take = sub.add_parser("fable-select-take", help="Select one take for its shot")
    fable_select_take.add_argument("take_id")
    fable_select_take.set_defaults(func=cmd_fable_select_take)

    fable_render = sub.add_parser("fable-render", help="Concatenate selected takes into the final film")
    fable_render.add_argument("project_id")
    fable_render.set_defaults(func=cmd_fable_render)

    fable_cancel = sub.add_parser("fable-cancel")
    fable_cancel.add_argument("project_id")
    fable_cancel.set_defaults(func=cmd_fable_cancel)

    fable_worker_run = sub.add_parser(
        "fable-worker-run", help="Continuous polling worker for Fable shot generation",
    )
    fable_worker_run.add_argument("--worker-id", default=None)
    fable_worker_run.add_argument("--poll-interval", type=float, default=None)
    fable_worker_run.add_argument("--lease-timeout", type=int, default=None)
    fable_worker_run.add_argument("--max-shots", type=int, default=None)
    fable_worker_run.add_argument("--idle-exit-after", type=float, default=None)
    fable_worker_run.add_argument("--stop-on-error", action="store_true")
    fable_worker_run.set_defaults(func=cmd_fable_worker_run)

    worker_run_once = sub.add_parser("worker-run-once")
    worker_run_once.add_argument("--worker-id", default="cli-worker")
    worker_run_once.add_argument(
        "--lease-timeout", type=int, default=None,
        help="Seconds before a locked job with no heartbeat is considered stale "
             "(default: settings.lease_timeout_seconds)",
    )
    worker_run_once.set_defaults(func=cmd_worker_run_once)

    demo_run = sub.add_parser(
        "demo-run",
        help="channel-create (if needed) + job-create + drive to a terminal status, in one command -- "
             "most useful with REEL_HARNESS_LLM_PROVIDER=demo/TTS_PROVIDER=demo/ASSET_PROVIDER=demo",
    )
    demo_run.add_argument("--topic", required=True)
    demo_run.add_argument("--channel-id", default=None, help="Reuse an existing channel instead of creating one")
    demo_run.add_argument("--channel-name", default=None, help="Only used when --channel-id is not given")
    demo_run.add_argument("--niche", default="general", help="Only used when --channel-id is not given")
    demo_run.add_argument("--language", default="en", help="Only used when --channel-id is not given")
    demo_run.add_argument("--idempotency-key", default=None)
    demo_run.add_argument(
        "--max-attempts", type=int, default=10,
        help="Max lease/run cycles before giving up (default: 10)",
    )
    demo_run.set_defaults(func=cmd_demo_run)

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
    publish_job.add_argument("--provider", default="youtube", choices=["youtube", "tiktok", "instagram", "fake"])
    publish_job.add_argument("--account", default="default", help="Account alias (default: 'default')")
    publish_job.add_argument(
        "--privacy", default=None,
        help="Provider-specific privacy value; default is that provider's own most-restrictive option "
             "(validated against the provider's actual PublisherCapabilities, not a fixed list)",
    )
    publish_job.add_argument(
        "--confirm-public-upload", action="store_true",
        help="Required alongside a public-visibility --privacy value (and the allow-public-upload feature flag)",
    )
    publish_job.add_argument(
        "--confirm-platform-options", action="store_true",
        help="Required by providers whose PublisherCapabilities.requires_user_confirmation is true "
             "(e.g. TikTok) -- confirms the platform-specific options (comments/remix/disclosure/etc.) "
             "were reviewed",
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
    publisher_run_once.add_argument(
        "--process-upload", action="store_true", help="Only lease upload-lane publications (omit both for both)",
    )
    publisher_run_once.add_argument(
        "--process-status", action="store_true",
        help="Only lease processing-poll-lane publications (omit both for both)",
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
    publisher_run.add_argument(
        "--process-upload", action="store_true", help="Only lease upload-lane publications (omit both for both)",
    )
    publisher_run.add_argument(
        "--process-status", action="store_true",
        help="Only lease processing-poll-lane publications (omit both for both)",
    )
    publisher_run.set_defaults(func=cmd_publisher_run)

    serve = sub.add_parser(
        "serve", help="Run the API, render worker, and publisher worker together in one supervised process",
    )
    serve.add_argument("--api", dest="api", action="store_true", default=True, help="Default: on")
    serve.add_argument("--no-api", dest="api", action="store_false")
    serve.add_argument(
        "--render-worker", dest="render_worker", action="store_true", default=True, help="Default: on",
    )
    serve.add_argument("--no-render-worker", dest="render_worker", action="store_false")
    serve.add_argument(
        "--publisher-worker", dest="publisher_worker", action="store_true", default=True, help="Default: on",
    )
    serve.add_argument("--no-publisher-worker", dest="publisher_worker", action="store_false")
    serve.add_argument(
        "--host", default=None,
        help="Default: settings.api_host (REEL_HARNESS_API_HOST, itself defaulting to 127.0.0.1)",
    )
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument(
        "--render-workers", type=int, default=1, help="Thread count for the render worker (SQLite: keep this low)",
    )
    serve.add_argument(
        "--publisher-workers", type=int, default=1,
        help="Thread count for the publisher worker (SQLite: keep this low)",
    )
    serve.add_argument(
        "--fable-workers", type=int, default=0,
        help="Thread count for the Fable cinematic shot-generation worker (default 0 = off)",
    )
    serve.add_argument(
        "--shutdown-timeout", type=float, default=30.0, help="Seconds to wait for graceful shutdown",
    )
    serve.set_defaults(func=cmd_serve)

    publication_status = sub.add_parser(
        "publication-status", help="Read-only: show a publication's current DB state",
    )
    publication_status.add_argument("publication_id")
    publication_status.set_defaults(func=cmd_publication_status)

    publication_list = sub.add_parser("publication-list", help="Read-only, filtered listing of publications")
    publication_list.add_argument("--provider", default=None)
    publication_list.add_argument("--account", default=None)
    publication_list.add_argument("--status", default=None)
    publication_list.add_argument("--job-id", dest="job_id", default=None)
    publication_list.add_argument(
        "--created-after", dest="created_after", type=_parse_iso_datetime, default=None,
    )
    publication_list.add_argument(
        "--created-before", dest="created_before", type=_parse_iso_datetime, default=None,
    )
    publication_list.add_argument("--failed-only", action="store_true")
    publication_list.add_argument("--processing-only", action="store_true")
    publication_list.set_defaults(func=cmd_publication_list)

    publication_refresh = sub.add_parser(
        "publication-refresh", help="Re-poll one PROCESSING publication's status out of turn",
    )
    publication_refresh.add_argument("publication_id")
    publication_refresh.set_defaults(func=cmd_publication_refresh)

    publication_reconcile = sub.add_parser(
        "publication-reconcile",
        help="Confirm a publication's local state against the provider and repair it if safely confirmable",
    )
    publication_reconcile.add_argument("publication_id", nargs="?", default=None)
    publication_reconcile.add_argument(
        "--all", action="store_true", help="Reconcile every non-terminal publication instead of one",
    )
    publication_reconcile.set_defaults(func=cmd_publication_reconcile)

    publication_retry = sub.add_parser(
        "publication-retry",
        help="Manually retry a stuck publication (FAILED/AUTH_REQUIRED/QUOTA_BLOCKED/RETRY_WAIT)",
    )
    publication_retry.add_argument("publication_id")
    publication_retry.add_argument(
        "--from-stage", choices=["SESSION", "UPLOAD", "PROCESSING"], default=None,
        help="Resume point; default picks the least-wasteful safe point automatically",
    )
    publication_retry.set_defaults(func=cmd_publication_retry)

    fable_adapt_eval = sub.add_parser(
        "fable-adapt-eval",
        help="Measure adaptation quality across repeated runs (real LLM calls)",
    )
    fable_adapt_eval.add_argument("--runs", type=int, default=3, help="runs per story (default 3)")
    fable_adapt_eval.add_argument(
        "--duration", type=int, default=32, help="target runtime in seconds (default 32)",
    )
    fable_adapt_eval.add_argument("--story", help="path to a .txt source instead of the samples")
    fable_adapt_eval.add_argument(
        "--show-plans", action="store_true", dest="show_plans",
        help="print each shot plan, not just its metrics",
    )
    fable_adapt_eval.add_argument(
        "--yes", action="store_true", help="confirm the paid calls (required for a real provider)",
    )
    fable_adapt_eval.set_defaults(func=cmd_fable_adapt_eval)

    provider_smoke = sub.add_parser(
        "provider-smoke", help="One real request against the configured provider (opt-in)",
    )
    provider_smoke.add_argument("target", choices=["llm", "tts", "asset", "publisher"])
    provider_smoke.add_argument(
        "publisher_provider", nargs="?", default=None, choices=["youtube", "tiktok", "instagram", None],
        help="Required when target=publisher, e.g. 'provider-smoke publisher youtube'",
    )
    provider_smoke.add_argument("--account", default=None, help="Account alias (default: 'default')")
    provider_smoke.add_argument(
        "--upload-private-test", action="store_true",
        help="Also run a real, private, clearly-labeled test upload (requires --confirm-test-upload too, "
             "and --confirm-platform-options too for tiktok) -- youtube/tiktok only, instagram has no "
             "private-visibility option (see --upload-public-test)",
    )
    provider_smoke.add_argument(
        "--upload-public-test", action="store_true",
        help="instagram only: run a real, PUBLIC, clearly-labeled test Reel upload (requires "
             "--confirm-test-upload, --confirm-public-upload, AND --confirm-platform-options) -- instagram "
             "has no private-visibility option, so this is never silently offered as 'private'",
    )
    provider_smoke.add_argument(
        "--confirm-test-upload", action="store_true",
        help="Required alongside --upload-private-test/--upload-public-test to actually run the test upload",
    )
    provider_smoke.add_argument(
        "--confirm-public-upload", action="store_true",
        help="Required alongside --upload-public-test for instagram, on top of --confirm-test-upload -- "
             "the same double-confirmation discipline every real public upload in this project requires",
    )
    provider_smoke.add_argument(
        "--confirm-platform-options", action="store_true",
        help="Required alongside --upload-private-test/--upload-public-test/--confirm-test-upload for "
             "tiktok/instagram (see providers.base.PublisherCapabilities.requires_user_confirmation) -- "
             "ignored for youtube",
    )
    provider_smoke.set_defaults(func=cmd_provider_smoke)

    publisher_auth = sub.add_parser(
        "publisher-auth", help="Connect a publisher account via OAuth (opt-in, requires a browser)",
    )
    publisher_auth.add_argument("provider", choices=["youtube", "tiktok", "instagram"])
    publisher_auth.add_argument("--account", default=None, help="Account alias (default: 'default')")
    publisher_auth.add_argument(
        "--timeout", type=float, default=300.0, help="Seconds to wait for the OAuth callback",
    )
    publisher_auth.set_defaults(func=cmd_publisher_auth)

    publisher_doctor = sub.add_parser(
        "publisher-doctor", help="Local-first readiness report for a publisher (no network by default)",
    )
    publisher_doctor.add_argument("provider", choices=["youtube", "tiktok", "instagram"])
    publisher_doctor.add_argument("--account", default=None, help="Account alias (default: 'default')")
    publisher_doctor.add_argument(
        "--check-remote", action="store_true",
        help="Additionally attempt a real token refresh and read-only channel-identity fetch",
    )
    publisher_doctor.add_argument("--json", action="store_true")
    publisher_doctor.set_defaults(func=cmd_publisher_doctor)

    account_list = sub.add_parser("publisher-account-list", help="List saved publisher account aliases")
    account_list.add_argument("--provider", default="youtube", choices=["youtube", "tiktok", "instagram"])
    account_list.set_defaults(func=cmd_publisher_account_list)

    account_show = sub.add_parser("publisher-account-show", help="Show one saved account's safe metadata")
    account_show.add_argument("alias")
    account_show.add_argument("--provider", default="youtube", choices=["youtube", "tiktok", "instagram"])
    account_show.set_defaults(func=cmd_publisher_account_show)

    account_remove = sub.add_parser(
        "publisher-account-remove", help="Delete a LOCAL saved credential (does not revoke remote authorization)",
    )
    account_remove.add_argument("alias")
    account_remove.add_argument("--provider", default="youtube", choices=["youtube", "tiktok", "instagram"])
    account_remove.add_argument("--confirm", action="store_true")
    account_remove.set_defaults(func=cmd_publisher_account_remove)

    return parser


def _make_console_encoding_safe() -> None:
    """Some Windows consoles (this project's own primary dev environment
    included) use a legacy codepage (e.g. cp949) that cannot encode
    ordinary Unicode punctuation this CLI prints (an em dash in "NOT RUN --
    credentials not configured", for one) -- without this, printing that
    exact message crashes with UnicodeEncodeError instead of reporting
    cleanly. Reconfiguring to UTF-8 with replacement on encode errors means
    a print statement never crashes the CLI merely because the console's
    codepage cannot represent one of its characters; on redirected output
    (a file, CI log, `2>&1`) this also gets the correct encoding regardless
    of console codepage. `reconfigure` is a real method on the concrete
    TextIOWrapper streams sys.stdout/stderr normally are; guarded because
    some non-interactive/test harnesses replace them with objects that
    don't have it (e.g. pytest's capsys)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


def main(argv: list[str] | None = None) -> int:
    from reel_harness.config import ProviderConfigurationError

    _make_console_encoding_safe()
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
