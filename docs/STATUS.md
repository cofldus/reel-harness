# Status

Last updated: 2026-07-23 (Phase 1 completion-gate session, following the
initial Phase 0/1 implementation session the same day).

## Phase 0 — COMPLETE

Minimal project foundation exists and imports cleanly: `pyproject.toml`,
`reel_harness` package (config loader, SQLAlchemy models + `init_db()`, state
machine types/transition rules, provider Protocols, `StorageBackend`
Protocol, CLI entry point, FastAPI entry point), test config, `.gitignore`,
`.env.example`.

- `reel_harness`, `reel_harness.api.app`, `reel_harness.cli.main` import successfully.
- `reel-harness doctor` runs and reports resolved ffmpeg/ffprobe path, version, and source.
- `ruff check reel_harness tests` — all checks passed.
- `mypy reel_harness` — no issues found in 40 source files.
- `pytest` — 87 passed, 0 skipped, 0 failed.

## Phase 1 — IN_PROGRESS (corrected from an earlier, premature "DONE")

An earlier report in this session marked Phase 1 complete. That was wrong:
real FFmpeg rendering, ffprobe validation, and the approve/reject flow
against an actually-rendered video had never been executed. This section
supersedes that claim.

**Verified for real, against actual binaries/files, no mocking of success:**

- Channel creation, idempotent job creation (`job_id` returned immediately),
  worker lease, crash recovery.
- `FakeLLMProvider` script generation + Pydantic schema validation.
- Deterministic policy check.
- `FakeStockMediaProvider` asset fetch — writes real, checksummed, valid PNGs
  under `jobs/{job_id}/assets/scene_*/`.
- `FakeTTSProvider` synthesis — writes real WAV files under
  `jobs/{job_id}/tts/scene_*/`, duration scaled to voiceover length.
- ffmpeg/ffprobe path resolution (env var -> `.tools/ffmpeg/bin` ->
  `PATH`), exposed via `reel-harness doctor` with absolute path/version/source.
- Structured JSON stage logs (`job_id`/`stage`/`attempt`/`event`/`duration_ms`/
  `error_code`) with secret redaction, observed live on stderr during a real
  CLI run.
- `JobService.reject()` -> `RETRY_WAIT` -> resume -> re-executes the targeted
  stage for real (not skipped due to missing ffmpeg — see Test results).
- `JobService.approve()` -> `READY` -> `COMPLETED`, and stamps
  `manifest.json`'s `approval.decision`/`approval.decided_at` for real.
- Manifest schema extended with `render`/`validation`/
  `final_video_checksum_sha256` fields (populated only when a real
  RENDER/VALIDATE pass supplies them) and an `is_publish_eligible()` helper
  that rejects `FAKE_TEST_LICENSE` assets and unapproved jobs.

**Genuinely blocked, not attempted, not faked — requires an operator decision:**

- **A real end-to-end run reaching `COMPLETED` with an actual `final.mp4`.**
  This machine has no ffmpeg/ffprobe at any resolution tier (env var,
  `.tools/ffmpeg/bin`, PATH). `RENDERING` correctly raises
  `DependencyError(code=BLOCKED_DEPENDENCY)` (non-retryable) and the job lands
  in `FAILED` / `current_stage=RENDER`. Confirmed live via CLI in this session
  (see Manual verification below), not mocked.
- **production-smoke** (1080x1920, H.264/AAC, faststart, real ffprobe JSON) —
  cannot be produced without a real ffmpeg/ffprobe binary. Not attempted.
- **ffmpeg version / final video checksum in a real manifest** — the schema
  fields exist and are unit-tested with synthetic data, but no real value has
  ever been written into them on this machine.

This session did **not** install ffmpeg, did **not** download a binary, and
did **not** mock `shutil.which`/the resolver to fake success in the E2E path
— per instruction. `reel-harness doctor` and `check_ffmpeg_available()`
report the real, current state of this machine every time they're called.

**To unblock**: install ffmpeg (bundles ffprobe) so both resolve via any of
the three tiers, then run a fresh job (or `reel-harness job-retry <id>
--stage RENDER` on an existing `FAILED` one). No code changes are needed —
every stage after RENDER already has real code and passing unit-level tests;
they have never executed against a real binary on this machine.

## worker/marker naming — investigated, no issue found

A later instruction referred to `reel_harness/marker/{policy,lease,runner}.py`
and asked whether that was a typo. It is not present anywhere in this
codebase (confirmed via `grep`/`glob` across the repo) — the module has always
been `reel_harness/worker/{policy,lease,runner}.py`. No rename was needed;
the "marker" reference did not originate from this code or from this
project's own documentation.

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
["."]` in `pyproject.toml`. For ad-hoc `python -m ...` invocations outside
pytest, set `PYTHONPATH=.` explicitly (see Manual verification below).

This is a workaround, not a fix — `reel_harness` is never `pip install`-ed
into the venv at all right now. Changing the Windows system locale to UTF-8
would fix it at the root cause, but that's a system-wide, reboot-requiring
change this session did not make.

## Manual CLI verification (this session, scratch dirs deleted afterward)

```
reel-harness doctor
  -> ffmpeg: {"available": false, "path": null, "version": null, "source": "not_found"}
  -> ffprobe: same
reel-harness channel-create --name cooking-shorts --niche "quick recipes" --language en
reel-harness job-create --channel-id <id> --topic "5 minute fried rice"   # -> QUEUED
reel-harness worker-run-once
  -> real stderr log lines: stage_started/stage_succeeded for SCRIPT, POLICY, ASSET, TTS
  -> stage_failed for RENDER, error_code=BLOCKED_DEPENDENCY
  -> final status: FAILED, current_stage=RENDER, failure_code=BLOCKED_DEPENDENCY
```

Confirmed on disk before cleanup: real checksummed PNGs under
`assets/scene_*/`, real WAV files under `tts/scene_*/`, all isolated under
that one job's directory. `final/final.mp4` and `manifest.json` do **not**
exist for this job, consistent with RENDER never succeeding.

## Test results (`pytest -v`, this session)

```
87 passed, 0 skipped, 0 failed
```

Zero skips in the core suite — the one skip present in the prior session's
run (`test_reject_routes_back_to_the_requested_stage_via_retry_wait`, which
used to require a real REVIEW_REQUIRED reached via rendering) was eliminated
by redesigning that test to synthesize the REVIEW_REQUIRED state the same way
a real render would leave it, so the reject/approve *mechanics* are verified
independently of whether ffmpeg is installed. This is documented in the
test's own docstring so it is not mistaken for a claim that rendering
succeeded.

Newly covered in this gate-completion pass: ffmpeg/ffprobe resolution
priority (env var / project-local `.tools` / PATH) and version parsing,
structured-log redaction (Authorization/Bearer patterns and a registered
secret value, verified via `caplog`), manifest `render`/`validation`/checksum
field population and JSON roundtrip, `is_publish_eligible()` against
FAKE_TEST_LICENSE/missing-license/no-assets/unapproved cases, approve's real
manifest-stamping, reject's real resume-and-re-execute behavior.

Previously covered (still passing): allowed/forbidden state transitions incl.
required-field validation, idempotent job creation incl. a 16-thread race
test, three concurrent jobs' directory isolation, worker lease race (two
sessions), RETRY_WAIT backoff timing, crash recovery via stale heartbeat,
cancel-before-next-stage, manual retry-from-FAILED, malformed/too-few-scenes
script rejection, Fake provider failure-injection modes, ffmpeg/ffprobe argv
construction (incl. the Windows-backslash concat-list bug fixed via
`Path.as_posix()`), process-tree cancellation, FastAPI auth + job-creation
smoke test, real-network-blocked sanity check, no-temp-leak check.

## Phase 1 completion criteria — outstanding

Phase 1 becomes COMPLETE only once, on a machine with ffmpeg/ffprobe
resolvable by `reel-harness doctor`:

- [ ] A real job reaches `COMPLETED` with a real `jobs/{id}/final/final.mp4`
- [ ] `manifest.json` contains a real ffmpeg version, real render dimensions,
      real ffprobe validation results, and a real final-video checksum
- [ ] A production-smoke run (1080x1920, H.264/AAC, faststart) passes real
      ffprobe validation
- [ ] The approve flow is re-verified against that real manifest (already
      verified against a synthesized one — see Test results)

None of these are satisfied on this machine as of this update.

## Phase 2 entry conditions

Phase 2 (a real LLM provider) is **not started**, per instruction. Before
starting it: pick a real vendor (open decision below) and resolve the Phase 1
completion criteria above.

## Open decisions (not made this session)

- Which real LLM/TTS/stock-media/publish vendors to integrate in Phase 2/3.
- Whether/when to install ffmpeg on this machine (or point
  `REEL_HARNESS_FFMPEG_PATH`/`REEL_HARNESS_FFPROBE_PATH` at an existing
  install, or place one under `.tools/ffmpeg/bin`).
- Whether to move off SQLite/local-only before real usage data exists.
