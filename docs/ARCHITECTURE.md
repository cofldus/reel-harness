# Reel Harness — Architecture (as implemented, Phase 0 + Phase 1)

This document describes what actually exists in code today, not the full target
system. See `docs/adr/` for the decisions behind these choices and this file's
"Extension points" section for what is deliberately not built yet.

## Overview

Reel Harness is a local-first, single-user service that turns a topic into a
short vertical video through an explicit, resumable job pipeline. Phase 0
built the project skeleton (config, DB, state machine, provider/storage
interfaces, CLI, API). Phase 1 wired those into a working vertical slice that
runs entirely against **Fake** providers — no real LLM/TTS/stock-media/publish
API is called anywhere in this codebase yet.

```
Client (CLI or HTTP) --> JobService --> SQLite (status source of truth)
                                            |
                                     worker.run_job()
                                            |
        TOPIC -> SCRIPT -> POLICY -> ASSET -> TTS -> RENDER -> VALIDATE
                                            |
                                  manifest.json + REVIEW_REQUIRED
                                            |
                                  operator approve/reject (CLI/API)
```

## Directory structure

```
reel_harness/
  config.py              Settings (pydantic-settings, reads .env)
  bootstrap.py            AppContext: wires config -> engine -> storage -> JobService
  core/
    state_machine.py      JobStatus, Stage, ReasonCode, ALLOWED_TRANSITIONS, apply_transition()
    errors.py              PipelineError hierarchy + ReviewRequiredSignal
    service.py              JobService: create/get/list/cancel/approve/reject/retry_from_stage
  db/
    models.py               SQLAlchemy models: Channel, Job, StageRun, Asset, ApprovalDecision
    schema.py                engine/session-factory creation, init_db()
  providers/
    base.py                  LLMProvider / TTSProvider / StockMediaProvider / Publisher Protocols
    fake_llm.py, fake_tts.py, fake_stock_media.py   Fake implementations (pipeline testing, zero network)
    demo_llm.py, demo_tts.py, demo_stock_media.py   Demo Mode: watchable/audible output, still zero network
                                                     (real speech via local pyttsx3, no API key -- see docs/STATUS.md)
    openai_compatible_llm.py, openai_compatible_tts.py, pexels_stock_media.py   Real providers (opt-in, need credentials)
    registry.py               name -> provider class lookup; the ONLY place vendor names may appear
  storage/
    base.py, local.py         StorageBackend Protocol + LocalFilesystemStorage (path-traversal safe)
  media/
    deps.py                    check_ffmpeg_available() -> DependencyStatus (env var / .tools / PATH resolution)
    runner.py                   ProcessRunner (list-argv subprocess, cross-platform cancel)
    ffmpeg_render.py, ffprobe_validate.py   argv builders (take a resolved binary Path) + ffprobe JSON parsing
  pipeline/
    script_schema.py             Pydantic Script/Scene schema + parse_script()
    policy.py                     deterministic banned-term check
    stages.py                     run_topic_generating .. run_validating (pure-ish stage functions)
  manifest/
    schema.py, writer.py          Manifest pydantic model + write_manifest() + is_publish_eligible()
  observability.py                structured JSON stage logs + secret redaction (see below)
  worker/
    policy.py                     STAGE_RETRY_POLICY, STAGE_ENTRY_STATUS, STAGE_ORDER
    lease.py                      lease_next_job(), recover_stale_jobs()
    runner.py                     run_job() — the actual per-job orchestration loop
  cli/main.py                     argparse CLI (reel-harness ...)
  api/app.py                      FastAPI app (healthz, /v1/jobs, cancel/approve)
```

## State machine

`JobStatus` (16 values) and the allowed-transition table live in
`reel_harness/core/state_machine.py`. Two fields are deliberately separate and
never conflated:

- `status` — the job's overall state (what the state machine governs)
- `current_stage` — which pipeline stage is/was executing; set by the worker,
  never by `apply_transition()`

`RETRY_WAIT` always carries `retry_target_stage`, `next_retry_at`,
`failure_code`, `failure_summary`. `REVIEW_REQUIRED` always carries
`reason_code` (`CONTENT_POLICY_REVIEW`, `ASSET_NOT_FOUND`,
`TECHNICAL_VALIDATION_FAILED`, `USER_APPROVAL_REQUIRED`,
`LICENSE_INFORMATION_MISSING`). `apply_transition()` enforces both the
transition table and these required-field invariants and raises rather than
silently allowing an inconsistent row.

**Reject-and-regenerate** does not fork a new job: `JobService.reject()` keeps
the same `job_id`, increments `attempt_number`, and routes back through
`RETRY_WAIT` targeting the requested stage — the exact same resume mechanism
crash recovery and automatic retries use. `parent_job_id` exists on `Job` but
is intentionally unused in Phase 0/1; it is reserved for explicit A/B-variant
jobs, which are a Phase 8 concern.

**Manual retry from FAILED** is an operator-only escape hatch
(`JobService.retry_from_stage`) distinct from the automatic RETRY_WAIT path —
`FAILED -> RETRY_WAIT` is allowed in the transition table only for this.

## Worker execution model

`worker.runner.run_job(session, job, channel, providers, storage)` resumes a
leased job from `job.current_stage` (or `TOPIC`/`SCRIPT` for a fresh job) and
runs forward through as many stages as succeed **in one call**, stopping at
the first `RETRY_WAIT` / `FAILED` / `REVIEW_REQUIRED` / `CANCELLED` outcome.
Each stage commits its `StageRun` row and updated `Job` fields before moving
to the next stage, so a process crash mid-run is safe: `worker.lease.
recover_stale_jobs()` finds jobs whose `heartbeat_at` is older than the lease
timeout and routes them to `RETRY_WAIT` (or `FAILED` if retries are exhausted)
targeting whatever `current_stage` was last committed — not stage zero.

Leasing uses a guarded SQL `UPDATE ... WHERE locked_by IS NULL` with a
rowcount check, which is safe under SQLite's transaction semantics for the
concurrency levels this is designed for (`tests/integration/test_worker_lease.py`
exercises the race directly with two sessions).

### Renewable leases, heartbeats, and fencing (Phase 2A)

Every successful lease acquisition mints a `lease_token` (UUID) on the job
row. From that point:

- A `LeaseHeartbeat` thread (own short-lived sessions, never the worker's main
  session) refreshes `heartbeat_at` every `lease_heartbeat_seconds` (default
  60s, vs the 300s `lease_timeout_seconds`) so a healthy worker inside a long
  ffmpeg render or provider call is never reclaimed as stale. Heartbeat DB
  errors are counted, logged, and exposed — never silently swallowed.
- Every job-state commit (stage entry, stage result, status transition,
  cancel, manifest write, release) is **fenced**: a guarded
  `UPDATE ... WHERE lease_token = :token` runs inside the same transaction
  immediately before the commit. Because that UPDATE takes SQLite's write
  lock, a takeover either already committed (the fence matches 0 rows and the
  worker rolls back and abandons with an internal `LEASE_LOST` outcome) or
  must wait until this commit finishes — there is no check-then-commit gap.
- `recover_stale_jobs()` rotates the token (clears it) when reclaiming, so
  the old worker can never heartbeat, commit, or release again. Its
  in-flight `StageRun` is closed as `lease_lost` by the old worker itself;
  attempt numbers come from StageRun history (`max(attempt)+1`), so the new
  owner's re-run never duplicates an attempt number.
- The RENDER stage renders to a worker-private temp file
  (`final/final-inprogress-<uuid>.mp4`) and promotes it to the official
  `final/final.mp4` via `os.replace` only under a held fence, deleting the
  now-stale `manifest.json` in the same step. A fenced-out worker cleans up
  only its own temp file and never touches the official output.

Schema note: `lease_token` was added via the additive-only migration path in
`db.schema` (`_ADDITIVE_COLUMNS`, schema v2) — `init_db()` applies
`ALTER TABLE ADD COLUMN` to pre-existing dev databases; anything beyond
nullable column additions is the trigger to adopt Alembic.

### Manifest atomicity

All manifest and render-metadata writes go through
`LocalFilesystemStorage.write_bytes_atomic` (unique temp file in the same
directory → flush → fsync → `os.replace`), so `manifest.json` on disk is
always a complete JSON document; a failed write preserves the previous file
and leaves no temp files. Write order: the manifest file is written inside
the fenced section immediately before the `REVIEW_REQUIRED` (or approval)
commit.

## Dependency gating and resolution (ffmpeg/ffprobe)

`RENDERING` and `VALIDATING` call `media.deps.check_ffmpeg_available()` fresh
every time — never cached — and raise `DependencyError` (`code=
BLOCKED_DEPENDENCY`, non-retryable) if the binary is missing. This is a
**terminal, non-retryable** failure distinct from a transient provider error:
retrying without the binary present would fail identically forever.

Resolution order for each of `ffmpeg`/`ffprobe` (`media/deps.py::_resolve_binary`):

1. `REEL_HARNESS_FFMPEG_PATH` / `REEL_HARNESS_FFPROBE_PATH` env var, if it
   points at a real file
2. `<project_root>/.tools/ffmpeg/bin/{ffmpeg,ffprobe}[.exe]`
3. System `PATH` (`shutil.which`)

Whichever tier resolves, the exact absolute path (never the bare string
`"ffmpeg"`) is what actually gets passed as `argv[0]` to `media.runner.run()`
in `pipeline.stages.run_rendering`/`run_validating` — `media/ffmpeg_render.py`
and `media/ffprobe_validate.py`'s argv builders take that path as a required
first argument. `reel-harness doctor` reports the resolved absolute path,
version string (parsed from `<binary> -version`), and which tier it came from
(`env` / `project_local` / `path` / `not_found`) for both binaries. None of
this installs or downloads anything — a miss at every tier is reported as
`not_found`/`BLOCKED_DEPENDENCY`, not silently worked around. This machine
currently resolves both binaries via the project-local
`.tools/ffmpeg/bin/` tier (gitignored, provisioned locally, not committed) —
see `docs/STATUS.md` for the resolved version and how that was verified.

Subtitle overlay burn-in and BGM mixing are **not implemented** — `RENDERING`
currently produces a still-image-plus-TTS-audio clip per scene and concatenates
them into `jobs/{job_id}/final/final.mp4`. This proves the ffmpeg integration
end-to-end without pretending the visual output matches the target product
yet (see Extension points).

## Observability

`reel_harness/observability.py` provides `log_stage_event(job_id, stage,
attempt, event, duration_ms, error_code)`, called by
`worker.runner._run_single_stage` at `stage_started` /
`stage_succeeded` / `stage_failed` / `stage_review_required`. Every log line
is a single JSON object with exactly those fields — script/voiceover text is
never passed in. A logger-level `_RedactingFilter` scrubs
`Authorization:`/`Bearer ...`-shaped text unconditionally, plus any value
explicitly registered via `register_secret()` (called once for
`settings.app_api_key` in `AppContext.__init__`) — so the app's own API key
can never leak into a log line even in an unanticipated code path.

## Data model

- `Channel` — name, niche, language, style_preset (JSON), auto_approve
- `Job` — see `core/state_machine.py` fields above, plus `idempotency_key`
  (unique with `channel_id`), `topic`, `script` (JSON), `attempt_number`,
  `retry_count`, `locked_by`/`heartbeat_at` (lease bookkeeping)
- `StageRun` — one row per stage attempt: stage, attempt, status, error_detail,
  started_at/finished_at — the audit trail behind `job-show`/`job-list`
- `Asset` — per-scene downloaded asset: source_url, author, license_type,
  checksum_sha256, local_path
- `ApprovalDecision` — approve/reject audit row, reason, regenerate_from_stage

Schema is created via `db.schema.init_db()` (`Base.metadata.create_all` plus a
`schema_migrations` bookkeeping table), not Alembic — see Extension points.

## API and CLI

FastAPI (`api/app.py`) exposes `GET /healthz` (no auth) and `POST /v1/jobs`,
`GET /v1/jobs/{id}`, `POST /v1/jobs/{id}/cancel`, `POST /v1/jobs/{id}/approve`
(all behind a static bearer-token check against `settings.app_api_key`). The
CLI (`cli/main.py`, `reel-harness` console script) is the primary interface
for Phase 0/1 per the CLI-first decision: `doctor`, `channel-create`,
`job-create`, `job-show`, `job-list`, `job-approve`, `job-reject`,
`job-cancel`, `job-retry`, `worker-run-once`. Both layers call the exact same
`JobService`/`run_job` — there is no duplicated business logic between them.

## Known environment quirk: Windows + non-ASCII path

This repository's path (`C:\Users\이채연\umma`) contains Korean characters.
`uv sync`'s default editable install writes a `.pth` file containing that raw
path; Python's `site.py` opens `.pth` files with the **locale** encoding
(cp949 on this machine) regardless of UTF-8 mode, and crashes with
`UnicodeDecodeError` on non-ASCII bytes. Fix in use: install dependencies only
(`uv sync --extra dev --no-install-project`), never let `reel_harness` itself
get installed/editable-installed, and run everything via
`uv run --no-sync ...` with `PYTHONPATH=.` set (pytest already does this via
`[tool.pytest.ini_options] pythonpath = ["."]`). Do **not** run a bare
`uv sync` or `uv run` without `--no-sync`/`--no-install-project` in this repo —
it will recreate the broken `.pth` file. See `docs/STATUS.md` for the exact
commands that are known to work.

## Extension points (documented, not built)

- **Alembic migrations** — once a real schema change needs to preserve data
- **Redis/RQ-backed `JobQueue`** — swap-in behind a not-yet-extracted queue
  interface once single-process polling is insufficient
- **`S3CompatibleStorage`** — second `StorageBackend` implementation
- **Real TTS/StockMedia/Publisher providers** — register in
  `providers/registry.py`; `pipeline/*` and `worker/*` need zero changes. A
  real **LLM** path already exists: the vendor-neutral
  `providers/openai_compatible_llm.py` adapter, selected via
  `llm_provider=openai-compatible` plus `llm_base_url`/`llm_model`/
  `llm_api_key` settings (see `.env.example`). No live call has been made —
  contract tests use a mock transport.
- **Subtitle overlay + BGM mixing** in `media/ffmpeg_render.py`
- **Web admin UI** — CLI is the only interface today
- **Facebook Reels publishing** — the `Publisher` Protocol,
  `PublisherCapabilities` capability model, and the provider-generic
  worker/reconciliation/retry framework already support this without
  further core changes; only a new adapter module + registry entry is
  needed (see `providers/youtube_publisher.py`/`providers/tiktok_publisher.py`/
  `providers/instagram_publisher.py` for the pattern). YouTube (Phase
  3A/3B), TikTok (Phase 3C), and Instagram Reels (Phase 3D) are all real,
  tested implementations as of `docs/STATUS.md`'s current entry — Facebook
  Reels (a distinct product/API surface from Instagram, deliberately out of
  scope through Phase 3D) is the only Publisher Protocol target still
  unimplemented.
- **A public media-hosting `MediaDeliveryBackend`** — deliberately not
  built in Phase 3D even though Instagram's Content Publishing API
  supports a `video_url`-hosted upload mode alongside the resumable
  direct-upload mode this project implements. See
  `docs/PUBLISHING.md`/`docs/OPERATIONS.md` for the reasoning: operating a
  new public HTTPS listener is a materially larger security/operational
  surface than this local-first, single-user tool needs when the
  resumable path already covers the same publishing capability.
- **A credential-bundling backup command** — deliberately not built in
  Phase 4A even though `backup-create` bundles the DB, jobs storage, and
  publish journal. See `docs/OPERATIONS.md`'s "Credential backup policy":
  bundling OAuth tokens/client secrets into an archive meant to be moved
  or shared makes it too easy to accidentally ship a live credential
  long-term; the credential directory is left to the operator's own
  OS-level backup tooling instead.

## Production operations (Phase 4A)

`reel_harness/ops/` is a new top-level package for everything that makes
the system operable rather than functional: `preflight.py` (readiness
diagnostics), `fingerprint.py` (safe config snapshotting), `db_tools.py`
(migration/backup/restore/verify), `storage_tools.py` (job-storage
verification), `backup_bundle.py` (portable archive create/inspect/
restore), `supervisor.py` (the `serve` runtime supervisor), `metrics.py`
(Prometheus text exposition), `incident.py` (diagnostics bundles),
`live_verify.py` (cross-platform live-account verification), `release.py`
(release manifest), and `release_check.py` (the pre-tag gate). This
package depends on the rest of the application (`core`, `db`, `providers`,
`publisher`, `worker`, `storage`) — nothing in those packages imports from
`ops/`, keeping the dependency direction one-way. `cli/main.py` and
`api/app.py` are the only two callers of `ops/`.

`reel-harness serve` (`ops/supervisor.py`) runs the FastAPI app and the
render/publisher worker daemons as **threads** inside one process, sharing
a single `AppContext` — not separate processes. The existing lease-fencing
mechanism (`worker/lease.py`, `worker/publish_lease.py`) already makes
concurrent multi-worker access to the same SQLite DB safe, and the actual
CPU-heavy work (ffmpeg encoding) already runs in a subprocess outside the
GIL — so there is no GIL-bound work here for separate OS processes to
usefully parallelize, and threads avoid the real complexity multiprocess
coordination would add (separate `AppContext`s, IPC for shutdown, no
shared memory) for zero benefit on a single machine.
