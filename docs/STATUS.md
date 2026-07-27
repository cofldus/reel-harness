# Status

Last updated: 2026-07-27 (Phase 1 critical remediation session, following an
independent audit).

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
