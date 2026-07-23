# Status

Last updated: 2026-07-23 (Phase 0 + Phase 1 implementation session).

## Phase 0 — DONE

Minimal project foundation exists and imports cleanly: `pyproject.toml`,
`reel_harness` package (config loader, SQLAlchemy models + `init_db()`, state
machine types/transition rules, provider Protocols, `StorageBackend`
Protocol, CLI entry point, FastAPI entry point), test config, `.gitignore`,
`.env.example`.

Verified (commands below, all run from the repo root):

- `reel_harness`, `reel_harness.api.app`, `reel_harness.cli.main` import
  successfully.
- `reel-harness doctor` runs and reports dependency status.
- `ruff check reel_harness tests` — all checks passed.
- `mypy reel_harness` — no issues found in 39 source files.
- `pytest` — 63 passed, 1 skipped (see below), 0 failed.

## Phase 1 — DONE (against Fake providers only)

Implemented and exercised for real, not just written: channel creation, job
creation with idempotency, `job_id` returned immediately, worker lease,
`FakeLLMProvider` script generation, Pydantic schema validation, policy check,
`FakeStockMediaProvider` asset fetch (writes real, checksummed PNGs under
`jobs/{job_id}/assets/`), `FakeTTSProvider` audio synthesis (writes real WAV
files under `jobs/{job_id}/tts/`), manifest build, CLI preview/approve/reject.

**Blocked and reported honestly, not bypassed**: this development machine has
no `ffmpeg`/`ffprobe` on PATH. `RENDERING` correctly raises
`DependencyError(code=BLOCKED_DEPENDENCY)`, which is non-retryable by design,
and the job lands in `FAILED` with `failure_code=BLOCKED_DEPENDENCY`,
`current_stage=RENDER`. This was verified against the real environment, not
mocked — see the manual CLI walkthrough below. No system-wide ffmpeg install
was performed (per instructions); this is an operator decision, not something
this session decided on its own.

**To unblock rendering/validation**: install ffmpeg (which bundles ffprobe)
so both binaries are on PATH, then re-run `reel-harness job-retry <id> --stage
RENDER` (or a fresh job) — no code changes needed. Every stage after RENDER
already has passing tests; they simply couldn't execute against a real binary
on this machine in this session.

## Known environment issue (Windows + non-ASCII path) — WORKED AROUND

This repo's absolute path (`C:\Users\이채연\umma`) contains Korean characters.
`uv sync`'s default project install writes an editable-install `.pth` file
containing that literal path; Python's `site.py` opens `.pth` files using the
system **locale** encoding (cp949 here) regardless of UTF-8 mode, and Python
3.11.0 crashes with `Fatal Python error: init_import_site` /
`UnicodeDecodeError: 'cp949' codec can't decode byte 0xec ...` merely by
starting the interpreter in the venv.

**Workaround in place** (already reflected in how every command in this repo
must be run):

```
uv sync --extra dev --no-install-project      # installs deps only, once
uv run --no-sync <command>                     # --no-sync stops uv from
                                                # re-installing the project
                                                # (which recreates the bad .pth)
```

`pytest` picks up the package via `[tool.pytest.ini_options] pythonpath =
["."]` in `pyproject.toml`. For ad-hoc `python -m ...` / `python -c ...`
invocations outside pytest, set `PYTHONPATH=.` explicitly (see the CLI
walkthrough below for the exact pattern used).

This is a workaround, not a fix — `reel_harness` is never `pip install`-ed
into the venv at all right now. That's fine for a single local-first repo; it
would need revisiting before this project is ever installed as a dependency
of something else. Changing the Windows system locale to UTF-8 would also fix
it at the root cause, but that's a system-wide, reboot-requiring change this
session did not make.

## Manual CLI verification (this session, then cleaned up)

Ran in a scratch DB/jobs dir (deleted afterward, not part of the repo):

```
reel-harness doctor                                          # ffmpeg/ffprobe: false/false
reel-harness channel-create --name cooking-shorts --niche "quick recipes" --language en
reel-harness job-create --channel-id <id> --topic "3 minute garlic noodles"  # -> QUEUED
reel-harness worker-run-once                                 # -> FAILED / RENDER / BLOCKED_DEPENDENCY
reel-harness job-show <job_id>                                # confirmed script+assets+tts were persisted
reel-harness job-retry <job_id> --stage RENDER                # -> RETRY_WAIT (operator override works)
```

Confirmed on disk: `assets/scene_0..2/*.png` (real, valid, checksummed PNGs)
and `tts/scene_0..2/tts.wav` (real WAV files with duration scaled to
voiceover length) — all isolated under that one job's directory, nothing
written elsewhere.

## Test results (`pytest -q`, this session)

```
63 passed, 1 skipped in ~5s
```

The 1 skip is `tests/integration/test_cancel_and_review.py::
test_reject_routes_back_to_the_requested_stage_via_retry_wait`: it needs a job
to actually reach `REVIEW_REQUIRED` before exercising reject, which requires a
real RENDER/VALIDATE pass — not possible on this ffmpeg-less machine, so it
skips with an explicit reason instead of failing or faking success. Re-run
after installing ffmpeg to cover it (and the equivalent branch in
`tests/e2e/test_vertical_slice_fake.py`, which already asserts the
full-success outcome — `REVIEW_REQUIRED`, `manifest.json`/`final.mp4` exist —
whenever `shutil.which` finds both binaries).

Covered: allowed/forbidden state transitions incl. required-field validation,
idempotent job creation incl. a 16-thread race test, three concurrent jobs'
directory isolation, worker lease race (two sessions), RETRY_WAIT backoff
timing, crash recovery via stale heartbeat, cancel-before-next-stage, reject
-> RETRY_WAIT -> resume, manual retry-from-FAILED, malformed/too-few-scenes
script rejection, Fake provider failure-injection modes (empty/timeout/
corrupted), ffmpeg/ffprobe argv construction (incl. the Windows-backslash
concat-list bug the reference pipeline hit — fixed via `Path.as_posix()`),
process-tree cancellation (real subprocess spawn+kill), FastAPI auth +
job-creation smoke test, real-network-blocked sanity check, no-temp-leak
check.

**Not implemented, so not tested**: structured logging / secret redaction —
there is no logging subsystem yet, only `print()`-based CLI output and
FastAPI's default access logs. This is listed as an extension point in
`docs/ARCHITECTURE.md`, not silently skipped.

## Phase 2 entry conditions

Per the confirmed scope, Phase 2 (a real LLM provider) is **not started**.
Before starting it: pick a real vendor (open decision — see below), install
ffmpeg/ffprobe (operator decision, not automatic) so RENDER/VALIDATE can be
exercised for real, and confirm the full vertical slice reaches
`REVIEW_REQUIRED` with a real `final.mp4` on this machine.

## Open decisions (not made this session)

- Which real LLM/TTS/stock-media/publish vendors to integrate in Phase 2/3.
- Whether/when to install ffmpeg on this machine.
- Whether to move off SQLite/local-only before real usage data exists.
