# Reel Harness — Operations (Phase 2C)

Runtime operations for the single-machine deployment: the worker daemon, real
LLM provider configuration, smoke checks, and troubleshooting. Design
rationale lives in `docs/ARCHITECTURE.md`; current completion state in
`docs/STATUS.md`.

## Worker daemon

```
uv run reel-harness worker-run [--worker-id ID] [--poll-interval SEC]
    [--lease-timeout SEC] [--heartbeat-interval SEC]
    [--max-jobs N] [--idle-exit-after SEC] [--stop-on-error]
```

Loop: recover stale leases → lease one ready job → run it (fenced commits,
background heartbeat) → release → repeat; when nothing is leasable the daemon
sleeps `--poll-interval` on a stop-event wait (interruptible, no busy loop).
Defaults come from config (`WORKER_POLL_INTERVAL_SECONDS`,
`WORKER_IDLE_EXIT_AFTER_SECONDS`, `WORKER_MAX_JOBS`, `WORKER_STOP_ON_ERROR`,
`LEASE_TIMEOUT_SECONDS`, `LEASE_HEARTBEAT_SECONDS`); CLI flags override.
`worker-run-once` remains for one-shot/debug runs.

- **Exit codes**: 0 = graceful (`--max-jobs` reached, `--idle-exit-after`
  elapsed, shutdown signal honored); 1 = infrastructure failure (DB/storage
  unusable) or `--stop-on-error`; 130 = hard interrupt mid-job.
- **Shutdown**: Ctrl+C / SIGINT / SIGTERM / SIGBREAK stop new leasing; the
  in-flight stage finishes to its next safe boundary; the heartbeat thread is
  joined and the lease released. A hard kill (console close,
  TerminateProcess) cannot be intercepted on Windows — that is exactly the
  crash case stale-lease recovery handles: the job is reclaimed after
  `LEASE_TIMEOUT_SECONDS` and retried.
- **Error isolation**: one failed job is recorded on that job only; the
  daemon moves on. Multiple daemons may run concurrently — lease fencing
  guarantees a job is never processed twice (`tests/integration/
  test_multi_worker.py`, `test_worker_daemon.py`).
- **Events** (structured JSON on the redacting logger): `worker_started`,
  `worker_idle`, `job_leased`, `job_completed`, `job_failed`, `lease_lost`,
  `stale_jobs_recovered`, `worker_shutdown_requested`, `worker_stopped`.
  Only identifiers, short lease prefixes, durations, and outcome fields.

## Choosing the LLM provider

Default is the fake provider (no network). To point at a real
OpenAI-compatible endpoint, set (see `.env.example`; legacy `LLM_*` names are
also accepted):

```
REEL_HARNESS_LLM_PROVIDER=openai_compatible
REEL_HARNESS_LLM_BASE_URL=...      # any /chat/completions-style endpoint
REEL_HARNESS_LLM_MODEL=...
REEL_HARNESS_LLM_API_KEY=...       # env/.env only; never persisted, always redacted
REEL_HARNESS_LLM_CONNECT_TIMEOUT / READ_TIMEOUT / MAX_RETRIES /
REEL_HARNESS_LLM_RETRY_BACKOFF / TEMPERATURE / MAX_OUTPUT_TOKENS
```

Selecting the real provider with incomplete configuration fails at startup
with `provider configuration error: ... missing REEL_HARNESS_LLM_*` (exit 2,
no traceback, no network attempted).

**Provider pinning**: every job stores a provider snapshot at creation
(provider id, model, endpoint host, prompt version, sampling params — never
the key). Retries, rejects, and resumes always use the snapshot; if the
current environment no longer satisfies it (credentials removed, endpoint
host changed, provider unregistered), the job fails explicitly with
`PROVIDER_NOT_CONFIGURED` instead of silently switching providers. Fix the
configuration, then `job-retry` the job.

## Real-provider smoke check

```
uv run reel-harness provider-smoke llm
```

Opt-in, single request, retries disabled. Prints a redacted summary
(provider, model, prompt version, request id, token usage, scene count) on
success. Exit codes: 0 success; 2 not configured / fake provider selected;
3 auth error; 4 transient (timeout/rate limit/5xx); 5 malformed/empty/refused
response or schema mismatch. Without an API key configured it refuses before
any network I/O. The default pytest suite and production-smoke never call a
real provider.

## Choosing the TTS provider

Default is the fake provider (no network). To point at a real
OpenAI-compatible `/audio/speech` endpoint, set:

```
REEL_HARNESS_TTS_PROVIDER=openai_compatible
REEL_HARNESS_TTS_BASE_URL=...      # e.g. https://.../v1  (POST {base_url}/audio/speech)
REEL_HARNESS_TTS_MODEL=...
REEL_HARNESS_TTS_API_KEY=...       # env/.env only; never persisted, always redacted
REEL_HARNESS_TTS_VOICE=...
REEL_HARNESS_TTS_FORMAT=wav        # wav | mp3 only -- not a free-form string
REEL_HARNESS_TTS_SPEED=1.0
REEL_HARNESS_TTS_CONNECT_TIMEOUT / READ_TIMEOUT / MAX_RETRIES / RETRY_BACKOFF
```

Selecting the real provider with any field missing, an unsupported format,
an out-of-range speed, a non-positive timeout, or a negative retry count
fails at startup with `provider configuration error: ...` (no traceback, no
network attempted).

**Provider pinning**: TTS provider id/model/voice/format/speed/endpoint host
join the same per-job provider snapshot as the LLM block (`Job.provider_config`,
never the key). Retries, rejects, and resumes always resynthesize with the
pinned provider/voice/format; if the environment no longer satisfies the
snapshot (credentials removed, host changed, provider unregistered), the job
fails explicitly instead of silently switching TTS providers or voices.

**Audio validation and normalization**: provider audio is never trusted on
HTTP status alone. It's parsed (WAV via the stdlib, everything else via real
`ffprobe`) and checked for byte length, a valid container, a non-zero audio
stream, and a non-zero duration, then normalized through real `ffmpeg` to
canonical PCM WAV — `44100` Hz, mono, `pcm_s16le` — regardless of the source
format, before the render stage ever sees it. Both the raw provider checksum
and the normalized checksum are tracked. 401/403 map to a non-retryable
`UPSTREAM_AUTH` error; 429 honors `Retry-After`; 5xx/timeouts retry up to
`REEL_HARNESS_TTS_MAX_RETRIES` with backoff; malformed/empty/oversized/
unsupported-codec responses fail without a retry loop that could multiply
provider cost per scene.

**Atomic publish**: synthesized/normalized audio is written to a
worker-private temp path first and `os.replace()`'d onto the job's official
audio path only after the fenced commit succeeds — a worker that lost its
lease (or a late response racing a retake) can never overwrite the current
lease owner's audio, and no temp files leak on that path.

## Real-provider smoke checks

```
uv run reel-harness provider-smoke llm
uv run reel-harness provider-smoke tts
```

Opt-in, single request, retries disabled. `provider-smoke tts` synthesizes
one short fixed sentence, validates the audio for real, writes it only to a
scratch path, and prints a redacted summary (provider, model, voice, format,
duration, codec, sample rate, channels, checksum prefix, latency) — never the
key, an auth header, or the full request/response body. Exit codes match
`provider-smoke llm`: 0 success; 2 not configured / fake provider selected;
3 auth error; 4 transient (timeout/rate limit/5xx); 5 audio validation
failure or malformed/empty/refused response. Without an API key configured it
refuses before any network I/O. The default pytest suite and production-smoke
never call a real provider.

## Cancelling a job

`reel-harness job-cancel <id>` / `POST /v1/jobs/{id}/cancel` share one
service path (`JobService.request_cancel`). A job with no worker attached —
`CREATED`, `QUEUED`, `REVIEW_REQUIRED`, or an unleased `RETRY_WAIT` —
transitions straight to `CANCELLED`; there's no active lease that will ever
observe a flag, and `REVIEW_REQUIRED` jobs specifically are not leasable at
all. A leased/running job instead gets `cancel_requested=true` and the
worker honors it at its next stage boundary, so an in-flight stage is never
yanked mid-write. Either way, artifacts already produced (audio, video,
manifest) are preserved for post-mortem with `approval.decision` staying
`null` and `publish_eligible=false`; approve/reject/retry/re-cancel are all
refused once a job is `CANCELLED`.

## Health and readiness

- `GET /healthz` — shallow liveness.
- `GET /readyz` — deep local checks: DB reachable, schema version supported,
  storage root writable, provider configuration valid (checked locally, no
  provider network call), ffmpeg/ffprobe resolved. 503 + named checks when
  not ready. No secrets in responses.

## Not yet supported (Phase 2C scope ends here)

Real stock-media vendors, publishing (the license gate keeps
`publish_eligible=false` for all fake-asset jobs), OAuth, PostgreSQL,
distributed queues, cloud deployment, automatic channel scheduling, web UI.
`httpx` is a runtime dependency as of Phase 2C — the `uv`/WDAC blocker from
Phase 2A/2B is resolved on this machine.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `provider configuration error: ...` at startup | real provider selected, config incomplete | set the listed `REEL_HARNESS_LLM_*`/`REEL_HARNESS_TTS_*` vars or switch back to `fake` |
| job FAILED `PROVIDER_NOT_CONFIGURED` | environment no longer matches the job's pinned snapshot | restore config (same endpoint host, same TTS voice/model) and `job-retry` |
| TTS stage fails audio validation | provider returned empty/corrupt/wrong-codec audio | check `provider-smoke tts` output; not retried automatically if malformed |
| job stuck `REVIEW_REQUIRED`, cancel had no effect (pre-Phase 2C) | old build without the immediate-cancel fix | upgrade; `job-cancel` now transitions unleased idle states to `CANCELLED` directly |
| job FAILED `BLOCKED_DEPENDENCY` | ffmpeg/ffprobe not resolvable | `reel-harness doctor`; provision `.tools/ffmpeg/bin/` |
| job FAILED `MISSING_PREREQUISITE` | resume artifacts missing/corrupt | `job-retry --stage` the stage that owns the artifact (message names it) |
| job stuck `RETRY_WAIT` with old `next_retry_at` | no worker running | start `worker-run` |
| worker exits 1 immediately | DB/storage/schema unusable | check `GET /readyz` / `doctor`, fix, restart |
| `/readyz` 503 `schema: unsupported version` | DB from a newer schema | upgrade the code or use a matching DB |
