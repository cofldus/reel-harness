# Status

Last updated: 2026-07-28 (Phase 2C real-tts session, on branch
`phase2/real-tts`). Phase 2A and Phase 2B are merged into `main`.

## Phase 2C — real TTS execution path + runtime dependency closure (this branch)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage):

- **Runtime dependency gate closed**: `uv` now runs unrestricted on this
  machine (uv 0.11.27), closing the `BLOCKED_ENVIRONMENT` item carried from
  Phase 2A/2B. `httpx` moved from the dev extra to `[project.dependencies]`
  and `uv.lock` was regenerated with `uv lock` (verified via `uv lock
  --check`); both the fake-only and real-provider import paths work in the
  installed environment.
- **`OpenAICompatibleTTSProvider`**: an OpenAI-compatible `/audio/speech`
  adapter isolated behind the `TTSProvider` Protocol — pipeline and worker
  code depend only on the Protocol, never on a vendor name. The Fake TTS
  provider is unchanged and remains the default.
- **TTS configuration**: `REEL_HARNESS_TTS_PROVIDER` (`fake` |
  `openai_compatible`), `_BASE_URL`, `_MODEL`, `_API_KEY` (`SecretStr`),
  `_VOICE`, `_FORMAT` (closed set: `wav`, `mp3`), `_SPEED`,
  `_CONNECT_TIMEOUT`, `_READ_TIMEOUT`, `_MAX_RETRIES`, `_RETRY_BACKOFF`.
  Selecting the real provider with any field missing, an unsupported
  format, an out-of-range speed, a non-positive timeout, or a negative
  retry count fails at startup with a clear `ProviderConfigurationError` —
  no traceback, no network call.
- **TTS provider snapshot pinning**: `provider_snapshot()` now emits a
  combined LLM+TTS block persisted on `Job.provider_config` (the same
  additive JSON column added in Phase 2B — no new migration needed).
  Retries/rejects/resumes resolve the TTS provider from the job's snapshot
  via `resolve_tts_for_snapshot()`; a snapshot pinned to a provider that's
  since been deregistered, had its host changed, or lost its credentials
  fails explicitly (`PROVIDER_NOT_CONFIGURED`-style `_UnconfiguredTTSProvider`)
  instead of silently falling back to a different provider.
- **Real audio validation + normalization** (`reel_harness/media/
  tts_audio.py`): provider audio is never trusted on HTTP status alone —
  it's parsed (WAV via the stdlib, everything else via real `ffprobe`),
  checked for a non-zero audio stream and duration, then normalized through
  real `ffmpeg` to canonical PCM WAV (`44100` Hz, mono, `pcm_s16le`)
  regardless of the source format. Both the raw and normalized checksums
  are tracked.
- **Lease-fenced atomic publish**: synthesized/normalized audio is written
  to a worker-private temp path first and only `os.replace()`'d onto the
  job's official path after the fenced commit succeeds — a worker that has
  lost its lease (or a late response racing a retake) can never overwrite
  the current lease owner's audio or manifest.
- **`provider-smoke tts`**: opt-in, single fixed-sentence synthesis,
  retries disabled, real audio validation, scratch-only temp storage,
  cleaned up on exit. Redacted summary (provider, model, voice, format,
  duration, codec, sample rate, channels, checksum prefix, latency) — never
  the key, header, or full request/response body. Distinct exit codes:
  0 success, 2 not configured, 3 auth error, 4 transient, 5 audio
  validation failure.
- **Hybrid E2E** (`tests/integration/test_hybrid_real_tts_pipeline.py`):
  real OpenAI-compatible LLM contract transport → policy → fake asset →
  real OpenAI-compatible TTS contract transport → real audio
  validation/normalization → real ffmpeg → real ffprobe → `REVIEW_REQUIRED`,
  with LLM+TTS metadata on the manifest, `publish_eligible=false` on the
  fake asset license, no key anywhere, and provider/voice pinning verified
  across reject-from-TTS (re-synthesizes) vs. reject-from-RENDER (does not).
  This is contract-transport wiring coverage, NOT a live provider call.
- **`job-show --json` is machine-readable**: the human-readable
  `REVIEW_REQUIRED` hint that used to print alongside `--json` output (and
  broke JSON parsing) now goes to stderr / a JSON field only; stdout is
  exactly one JSON document.
- **`REVIEW_REQUIRED` (and other unleased idle states) cancel immediately**:
  `request_cancel` used to only set `cancel_requested` even when no worker
  was ever going to observe it (`REVIEW_REQUIRED`/`RETRY_WAIT`/`QUEUED`/
  `CREATED` jobs with no active lease aren't leasable), leaving the job
  stuck. It now transitions those states straight to `CANCELLED`; a
  leased/running job still only gets the flag and the worker honors it at
  its next stage boundary. API and CLI share the same service method.
  Artifacts are preserved; approve/reject/retry/re-cancel are refused
  afterward.
- **Live smoke**: `NOT RUN — credentials not configured` for both LLM and
  TTS (no `REEL_HARNESS_LLM_API_KEY` / `REEL_HARNESS_TTS_API_KEY` set on
  this machine). `provider-smoke llm` / `provider-smoke tts` are the
  documented paths to run them once credentials exist.

Suite after Phase 2C: **203 passed, 0 failed, 0 skipped** (171 → 203,
4 of those from the `REVIEW_REQUIRED` cancel fix). mypy clean (45 files).
ruff clean.

## Phase 2B — production worker + real LLM execution path (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage):

- **Worker daemon** (`reel-harness worker-run`): continuous polling with
  stale recovery, fenced execution, background heartbeats, `--max-jobs` /
  `--idle-exit-after` / `--stop-on-error`, graceful shutdown on
  Ctrl+C/SIGINT/SIGTERM/SIGBREAK, per-job error isolation, structured
  worker events. Verified by 10 in-process lifecycle tests (including a
  real two-daemon race on one DB) plus 2 real-subprocess CLI E2Es.
- **Provider configuration completed**: canonical `REEL_HARNESS_LLM_*` env
  vars (legacy `LLM_*` accepted), SecretStr API key (hidden in repr),
  strict startup validation with a clear no-traceback failure, and
  `provider-smoke llm` — an opt-in, retries-disabled, redacted single-shot
  check of the real provider with distinct exit codes per failure class.
- **Provider snapshot pinning**: every job persists provider id/model/
  endpoint-host/prompt-version/sampling params at creation (schema v3,
  additive migration; never the key). Retries and resumes resolve from the
  snapshot; unsatisfiable snapshots fail explicitly with
  `PROVIDER_NOT_CONFIGURED` — no silent provider switches.
- **Hybrid pipeline coverage**: the real OpenAI-compatible adapter over a
  contract MockTransport driving the real pipeline with fake asset/TTS and
  real ffmpeg/ffprobe — structured script + provider metadata on job and
  manifest, no key anywhere, `publish_eligible=false` on fake licenses.
  This is wiring coverage, NOT a live provider call; no live LLM smoke has
  been run (no API key configured on this machine).
- **Health/readiness**: `GET /readyz` (DB, schema version, storage,
  provider config validity — checked locally, media toolchain) returning
  503 with named checks; `/healthz` stays shallow.

Environment limitation (unchanged): every `uv` execution path is blocked by
the OS application-control policy, so `httpx` could not be promoted to a
runtime dependency (lockfile untouched — recorded as BLOCKED_ENVIRONMENT);
ruff remains NOT VERIFIED on this machine for the same reason.

Suite after Phase 2B: **171 passed, 0 failed, 0 skipped** (142 -> 171).
mypy clean (43 files).

## Phase 2A — reliability foundation + real LLM plumbing (merged to `main`)

Implemented and tested this session (each landed as its own commit; all
verification real, no mocked E2E claims):

- **Persisted-error redaction**: one `observability.redact()` rule set backs
  the logging filter AND every persisted error field (`failure_summary`,
  `StageRun.error_detail`, the reject reason, the failure fields now echoed
  by the job API). Patterns cover bearer/basic auth, authorization headers,
  api-key style headers/params/JSON fields, credential-named URL query
  values, and `sk-` style keys; registered secrets are replaced
  longest-first, values under 8 chars are never registered.
- **Renewable leases + fencing**: every lease mints a `lease_token`;
  heartbeats, stage commits, status transitions, manifest writes, and
  release are token-guarded at the DB level (the fence UPDATE holds SQLite's
  write lock through the commit — no check-then-commit gap). A
  `LeaseHeartbeat` thread refreshes the heartbeat during long stages
  (defaults: 60s interval vs 300s timeout, both settings). RENDER goes to a
  worker-private temp file and is promoted under a held fence; recovery
  rotates the token. Multi-worker takeover is covered by a real two-thread
  test on a file DB: the late worker's commit is refused, its StageRun is
  closed as `lease_lost`, attempt numbers never duplicate, and only the new
  owner's manifest/output are official.
- **Atomic manifests + strict validation**: manifest/render-metadata writes
  are temp-file + fsync + `os.replace`; a new render deletes the stale
  manifest at promote time. Pipeline VALIDATE now enforces H.264/AAC, audio
  stream presence, exact resolution, duration bounds, and faststart on every
  render (fake 360x640 included), reporting all violated conditions together.
- **Real LLM plumbing (no live calls)**: a vendor-neutral OpenAI-compatible
  adapter behind the registry, fully configured via settings/.env
  (`llm_provider=openai-compatible`, base URL, model, API key, timeouts,
  retries). Error mapping: 401/403 non-retryable `UPSTREAM_AUTH`; 429
  (Retry-After honored) / 5xx / timeouts retried within bounds; empty or
  refused responses are `SCHEMA_INVALID`. The API key is env-only, redacted
  everywhere, and never persisted. Contract tests use `httpx.MockTransport`
  (no sockets), so the whole suite still runs with the network-block fixture
  and the fake provider remains the default. `httpx` is currently a dev
  dependency; promote it to a runtime dependency (pyproject + uv.lock) before
  actually enabling a real provider — `uv` could not be run on this machine
  (blocked by the OS application-control policy), so the lockfile was left
  untouched.

Not in scope / not yet: real TTS or stock-media vendors, publishing, OAuth,
multi-node queues, live API calls. Multi-worker operation is now protected by
fencing but the polling loop is still `worker-run-once` per invocation.

Suite after Phase 2A: **142 passed, 0 failed, 0 skipped** (99 -> 142; new
lease/heartbeat, multi-worker, redaction, atomic-manifest/validation, and
adapter contract tests). mypy clean (42 files). ruff remains blocked by the
OS application-control policy on this machine (NOT VERIFIED — run it once in
an unrestricted environment).

## Phase 0 — COMPLETE

Minimal project foundation exists and imports cleanly: `pyproject.toml`,
`reel_harness` package (config loader, SQLAlchemy models + `init_db()`, state
machine types/transition rules, provider Protocols, `StorageBackend`
Protocol, CLI entry point, FastAPI entry point), test config, `.gitignore`,
`.env.example`.

- `reel_harness`, `reel_harness.api.app`, `reel_harness.cli.main` import successfully.
- `reel-harness doctor` reports resolved ffmpeg/ffprobe path, version, and source.
- `ruff check reel_harness tests` — all checks passed.
- `mypy reel_harness` — no issues found in 40 source files.

## Phase 1 — COMPLETE

Two earlier reports in this session marked Phase 1 complete, then walked that
back to IN_PROGRESS once it became clear real ffmpeg rendering had never
actually executed. Both were superseded by placing real ffmpeg/ffprobe
binaries at `.tools/ffmpeg/bin/{ffmpeg,ffprobe}.exe` (project-local, gitignored)
and re-running every completion criterion for real. This time all of them
passed:

- **ffmpeg/ffprobe resolved from the project-local tier**: `reel-harness
  doctor` reports `path=.tools/ffmpeg/bin/ffmpeg.exe`,
  `version=8.1.2-essentials_build-www.gyan.dev`, `source=project_local` (and
  the same for ffprobe).
- **Full vertical slice, real binaries, no mocking**: a real CLI-driven job
  went `CREATED -> QUEUED -> SCRIPT_GENERATING -> POLICY_CHECKING ->
  ASSET_FETCHING -> TTS_GENERATING -> RENDERING -> VALIDATING ->
  REVIEW_REQUIRED -> (approve) -> READY -> COMPLETED`. Every stage transition
  was logged live via the structured JSON logger.
- **Real artifacts confirmed on disk**: `jobs/{id}/final/final.mp4` (14.5KB,
  a real playable H.264/AAC mp4), `jobs/{id}/manifest.json` with real,
  non-null values -- see the manifest excerpt below.
- **production-smoke, 1080x1920, real ffmpeg**:
  `tests/e2e/test_production_smoke.py::test_production_smoke_1080x1920`
  renders a 3-scene video at 1080x1920 with the real toolchain and asserts:
  resolution exactly 1080x1920, `video_codec=="h264"`, `audio_codec=="aac"`,
  `has_audio_stream is True`, duration within `[4.5s, 60s]`, and `moov`
  precedes `mdat` in the file (the real, file-level signature of
  `-movflags +faststart` actually taking effect). Passed for real.
- **Approve flow, real manifest**: `job-approve` on a real REVIEW_REQUIRED job
  transitioned it to `COMPLETED` and stamped the *real* `manifest.json`'s
  `approval.decision="approve"` / `approval.decided_at=<real timestamp>` --
  read back from disk after the CLI call, not asserted from memory.
- **Reject/regenerate flow, real re-execution**: `job-reject --from-stage
  SCRIPT` on a second real REVIEW_REQUIRED job moved it to `RETRY_WAIT`
  (`retry_target_stage=SCRIPT`, `failure_code=USER_REJECTED`), and the next
  `worker-run-once` call re-ran the *entire* remaining pipeline for real
  (SCRIPT through VALIDATE, each stage logged) and reached `REVIEW_REQUIRED`
  again with a freshly rendered `final.mp4`.
- **Manifest completeness, real values** (see excerpt below): `job_id`,
  `schema_version`, `topic`, structured `script` (via `job.script`, not
  shown in the manifest file itself but present on the `Job` row), provider
  identifiers, per-asset checksums, `FAKE_TEST_LICENSE` on every asset,
  real `ffmpeg_version`, real `final_video_checksum_sha256`, real
  `validation` block, real `approval` block after approve.
- **Secret redaction**: unchanged from the prior pass, still passing;
  structured logs observed live during this session's CLI runs never
  contained the app API key or an Authorization/Bearer value.
- **Test gate**: `ruff check` all clean, `mypy` 40 files no issues, `pytest`
  **88 passed, 0 failed, 0 skipped** -- zero skips anywhere, core or
  otherwise. (Superseded by the remediation section below: the suite is now
  99 tests, and two of the claims in this section were later disproven and
  fixed.)

## Phase 1 critical remediation — 2026-07-27

An independent read-only audit disproved two Phase 1 completion claims with
live reproductions. Both were fixed this session, plus the underlying
worker-safety gap. Nothing below is synthesized: every "reproduced" and
"verified" line was an actual CLI/pytest run against real ffmpeg/ffprobe.

### Defects found (all reproduced before fixing)

1. **BLOCKER: resume context was never restored.** `run_job()` passed
   inter-stage data only through an in-memory dict, so any resume targeting
   TTS/RENDER/VALIDATE (reject, automatic RENDER retry, manual retry) crashed
   the worker with `KeyError: 'assets'`, and the CLI's unconditional lease
   release then stranded the job as `RENDERING` + `locked_by=NULL` -- a state
   no lease or recovery path could ever pick up again.
2. **BLOCKER: stale-lease recovery crashed cross-process.** `heartbeat_at`
   read back from SQLite is naive while the recovery threshold was aware UTC:
   `recover_stale_jobs()` raised `TypeError` in any fresh process, and since
   recovery runs before leasing, one crashed worker bricked every subsequent
   `worker-run-once`. The old test compared same-session aware objects and
   could not catch this.
3. **HIGH: unexpected exceptions stranded jobs.** Any non-`PipelineError`
   escaping a stage left the job ACTIVE while the lease was released.

### Fixes

- **Persistent resume context** (`worker/runner.py`): ASSET success now
  persists `Asset` rows (replace-per-attempt); RENDER success persists
  `render/render_meta.json`; `_restore_context()` rebuilds assets (checksum-
  verified against the files on disk), TTS results (duration re-read from the
  actual WAV headers), and render output purely from DB + job storage. A
  missing/corrupt prerequisite fails explicitly with `MISSING_PREREQUISITE`
  naming the gap; an unsupported persisted resume target fails with
  `UNSUPPORTED_RESUME_STAGE`. Stale-output policy: `run_rendering()` deletes
  any pre-existing `final.mp4` before rendering, and the manifest checksum is
  always recomputed from the file on disk after success.
- **Datetime policy** (`db/models.py`): a `UTCDateTime` TypeDecorator on every
  datetime column stores naive UTC and returns aware UTC, so values compare
  correctly regardless of which session loaded them. Storage format is
  unchanged; existing rows stay readable.
- **Unexpected-exception boundary** (`worker/runner.py`): `run_job()` no
  longer propagates non-`PipelineError` exceptions; it rolls back, closes any
  running StageRun as failed, and moves the job to FAILED with
  `UNEXPECTED_PIPELINE_ERROR` (short summary, no traceback).
  `KeyboardInterrupt`/`SystemExit` still propagate.
- **Lease invariant** (`worker/lease.py`): `release_lease()` refuses to unlock
  a job still in an ACTIVE stage status (recovery reclaims it instead), and
  `find_orphaned_active_jobs()` detects the forbidden ACTIVE+unlocked state.
- **Retry-target validation** (`core/service.py` + `core/state_machine.py`):
  reject/manual-retry targets are validated against `RESUMABLE_STAGES`
  (SCRIPT/POLICY/ASSET/TTS/RENDER/VALIDATE); PUBLISH/TOPIC/unknown values are
  clean user errors that change no job state.
- **StageRun attempts** (`worker/runner.py`): attempt numbers now come from
  the StageRun history (`max(attempt)+1`), not `retry_count+1`, so a reject
  re-run records attempt 2 instead of a second attempt 1. History is never
  deleted or overwritten.

### Verification (this session, all real)

- Targeted regression tests: 11 new tests in
  `tests/integration/test_resume_from_stage.py` (reject->RENDER/TTS/VALIDATE
  resume with StageRun/checksum assertions, automatic RENDER retry with a real
  nonzero-exit "ffmpeg" and a planted stale final.mp4, manual retry of a
  FAILED job, invalid-target rejection at both the service boundary and the
  worker, missing-prerequisite failure) and
  `tests/integration/test_worker_crash_recovery.py` (stale recovery after a
  real engine-dispose/reopen DB roundtrip, unexpected-exception containment,
  release-lease guard). All passed.
- Full suite: **99 passed, 0 failed, 0 skipped** (88 pre-existing + 11 new).
- production-smoke (real ffmpeg 8.1.2, 1080x1920, h264/aac/faststart/duration
  checks): passed.
- Manual CLI E2E on a scratch DB: `job-reject --from-stage RENDER` then
  `worker-run-once` resumed with RENDER/VALIDATE attempt 2 only (earlier
  stages untouched), reached REVIEW_REQUIRED, manifest checksum matched the
  current `final.mp4`, `approval` unset.
- Manual cross-process stale recovery on the audit session's stuck DB: the
  previously bricked `worker-run-once` now recovers the dead-worker job to
  RETRY_WAIT (`WORKER_CRASHED`); the legacy pre-fix job (no persisted Asset
  rows) then failed explicitly with `MISSING_PREREQUISITE` and was fully
  revived via `job-retry --stage ASSET` through to REVIEW_REQUIRED.
- Forbidden-state sweep (`ACTIVE` + `locked_by IS NULL`): 0 rows in every DB
  touched by the fixed code.
- `mypy`: no issues in 40 source files. `ruff check`: **not verified this
  session** -- the ruff native executable (and `uv.exe`) is blocked by this
  machine's OS Application Control policy; no equivalent execution path
  exists in the venv. The 2026-07-23 "ruff all clean" claim predates this
  session's changes.

### Known limitation

Jobs that were already stranded as ACTIVE+unlocked by the *old* code are not
retroactively repaired (the fixed code can no longer create that state, and
`release_lease` now prevents new occurrences). Any such legacy row needs a
one-time manual DB edit.

### Real manifest excerpt (from the approved job, `approval` block added after approve)

```json
{
  "job_id": "24dc85b5-1f7d-4eb5-8ae5-226b0b21f8fe",
  "topic": "5 minute fried rice",
  "assets": [{"license_type": "FAKE_TEST_LICENSE", "checksum_sha256": "ed301f72..."}, "... x3"],
  "render": {"ffmpeg_version": "8.1.2-essentials_build-www.gyan.dev", "width": 360, "height": 640},
  "validation": {"duration_sec": 8.074, "video_codec": "h264", "audio_codec": "aac", "has_audio_stream": true},
  "final_video_checksum_sha256": "4fa62f31872b4eb541328d2d8cb8cdd22fa3a763f9596b1945688686e2161c72",
  "approval": {"decision": "approve", "decided_at": "2026-07-23T08:23:58.515499Z"}
}
```

Standard per-job renders (via `worker.run_job`) still use the fast-test
resolution (360x640) by design -- `pipeline.stages.run_rendering`/
`run_validating` now take optional `width`/`height` parameters (default
360x640) precisely so the separate production-smoke check could exercise
1080x1920 through the same code path without changing what every ordinary
job renders at. `FAKE_TEST_LICENSE` is confirmed to still make
`manifest.is_publish_eligible()` return `False`.

## worker/marker naming — investigated, no issue found

`reel_harness/marker` does not exist anywhere in this codebase; the module
has always been `reel_harness/worker/{policy,lease,runner}.py`.

## Known environment issue (Windows + non-ASCII path) — WORKED AROUND

This repo's absolute path (`C:\Users\이채연\umma`) contains Korean characters,
which breaks `uv`'s default editable project install (`site.py` decodes
`.pth` files using the cp949 locale codec regardless of UTF-8 mode). Fix in
use, unchanged from before:

```
uv sync --extra dev --no-install-project
uv run --no-sync <command>          # with PYTHONPATH=. set for ad-hoc invocations
```

## ffmpeg/ffprobe resolution

Resolution order (`reel_harness/media/deps.py`): `REEL_HARNESS_FFMPEG_PATH` /
`REEL_HARNESS_FFPROBE_PATH` env var -> `<project_root>/.tools/ffmpeg/bin/` ->
system `PATH`. On this machine both binaries are now present at
`.tools/ffmpeg/bin/{ffmpeg,ffprobe}.exe` (gitignored via `.tools/` in
`.gitignore` -- not intended to be committed). `reel-harness doctor` reports
the resolved absolute path, version, and which tier resolved it.

## Test results (`pytest -v`, this session)

```
99 passed, 0 skipped, 0 failed   (88 collected before this session's 11 new regression tests)
```

All previously-skipped tests were fixed to synthesize their precondition
state when the real environment doesn't naturally produce it (rather than
skip), so there are zero conditional skips left in the suite regardless of
whether ffmpeg is present on a given machine.

## Phase 2 entry conditions

Phase 1's completion criteria are now satisfied. Phase 2 (a real LLM
provider) has **not** been started, per instruction -- picking a vendor
remains an open decision for the user to make first.

## Open decisions (not made this session)

- Which real LLM/TTS/stock-media/publish vendors to integrate in Phase 2/3.
- Whether to move off SQLite/local-only before real usage data exists.
- Whether/how `.tools/ffmpeg/bin` binaries should be provisioned on other
  machines (this session did not commit or distribute them; they are
  gitignored and were placed locally by the user).
