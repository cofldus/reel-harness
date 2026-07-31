# Reel Harness — Operations (Phase 4A)

Runtime operations for the single-machine deployment: the worker daemon, real
LLM/TTS/asset provider configuration, YouTube publishing (including
production-reliability features -- diagnostics, crash recovery, retry), smoke
checks, and troubleshooting. Design rationale lives in `docs/ARCHITECTURE.md`;
publisher-specific API research lives in `docs/PUBLISHING.md`; current
completion state in `docs/STATUS.md`.

## CI and packaging

`.github/workflows/ci.yml` runs on every push/PR: a Windows + Ubuntu ×
Python 3.11/3.12 matrix (lockfile check, import check, mypy, ruff, the full
pytest suite — which includes the schema-upgrade E2E, the backup/restore
E2E, the supervisor subprocess E2E, and the Linux-only real-symlink
security test — a secret/token grep excluding `tests/`, a tracked-artifact
check), a dedicated Ubuntu `production-smoke` job (real ffmpeg, 1080x1920),
a `package-smoke` job that builds the wheel/sdist, installs the wheel into
a brand-new venv to confirm the CLI entry point/imports/`--version`/a real
fake-provider job all work from the **installed package** (not the source
tree), and validates a real release manifest against the built wheel/sdist,
and a `release-check` job (`--skip-slow`, since the test matrix above
already covers the full pytest/mypy/ruff gate across every OS/Python
combination). No real provider credentials are configured anywhere in CI;
the fake-provider E2E and the httpx.MockTransport-based publisher contract
E2Es cover what runs there, and live provider smoke/live-verify upload
tests are never invoked. Build locally with `uv build`.

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

## Choosing the stock-media provider

Default is the fake provider (no network; produces stand-in PNG images
stamped `FAKE_TEST_LICENSE`, never publish-eligible). To point at the real
Pexels Video API, set:

```
REEL_HARNESS_ASSET_PROVIDER=pexels
REEL_HARNESS_ASSET_BASE_URL=https://api.pexels.com/videos   # default; rarely needs changing
REEL_HARNESS_ASSET_API_KEY=...       # env/.env only; never persisted, always redacted
REEL_HARNESS_ASSET_CONNECT_TIMEOUT / READ_TIMEOUT / MAX_RETRIES / RETRY_BACKOFF
REEL_HARNESS_ASSET_PER_PAGE=15
REEL_HARNESS_ASSET_ORIENTATION=portrait   # portrait | landscape | square
REEL_HARNESS_ASSET_MIN_WIDTH=480
REEL_HARNESS_ASSET_MIN_HEIGHT=480
REEL_HARNESS_ASSET_MIN_DURATION=1.0
REEL_HARNESS_ASSET_MAX_DURATION=60.0
REEL_HARNESS_ASSET_SAFE_SEARCH=true
```

**Why Pexels**: unlike the LLM/TTS adapters (which talk to any
OpenAI-compatible endpoint via a protocol shape, not a specific vendor),
stock-video search has no equivalent cross-vendor standard, so a concrete
vendor had to be picked. Pexels was chosen because (1) its Video Search API
(`GET /videos/search`) is stable and fully documented
(https://www.pexels.com/api/documentation/#videos-search), (2) it serves
real portrait *video* files, not just images, and (3) its license
(https://www.pexels.com/license/ — free for commercial and non-commercial
use, modification allowed, attribution appreciated but not legally
required) maps cleanly onto the manifest's `commercial_use_allowed` /
`modification_allowed` / `attribution_text` fields, letting
`is_publish_eligible()` evaluate real terms instead of a placeholder.
Every Pexels result is tagged `license_type=PEXELS_LICENSE`; attribution
text (`"Video by {creator} on Pexels"`) is always recorded even though
Pexels doesn't strictly require it, so the manifest carries full
provenance regardless.

Selecting the real provider with an unsupported orientation, non-positive
timeouts/min-dimensions/min-duration, a negative retry count, or a missing
API key/base URL fails at startup with `provider configuration error: ...`
(no traceback, no network attempted).

**Search and selection** (`pipeline.asset_query` / `pipeline.asset_selection`):
each scene's own `visual_query` (never the narration/voiceover) is
sanitized (control characters and disallowed punctuation stripped,
length-bounded) into the search query. Candidates are filtered by hard
license/technical requirements (license present, commercial use +
modification allowed, minimum resolution, duration range) and scored by
aspect-ratio fit to the target orientation, resolution, duration fit, and
the provider's own result ranking; ties are broken by provider asset id so
selection is fully deterministic. An asset already selected for an earlier
scene in the same job is excluded from later scenes. If nothing eligible
survives, the query text is deterministically relaxed to fewer leading
words and re-searched — orientation, minimum resolution, duration bounds,
and safe-search are never relaxed — and exhausting that ladder raises
`ASSET_NOT_FOUND` (`REVIEW_REQUIRED`) rather than ever loosening a license
condition to force a success.

**Download, validation, and normalization**: streamed with a byte cap, a
redirect limit, an https-only redirect-scheme policy, and HTML/JSON
error-page rejection; the API key is sent only to the search API, never to
the (separately hosted) file download host. A 2xx status is not treated as
success: the downloaded bytes are validated with real `ffprobe`
(resolution, duration) and normalized with real `ffmpeg` to canonical
H.264/yuv420p, muted (the final render's only audio is the TTS track —
original stock-clip audio is always discarded), stable frame rate.
Scaling/cropping to the render's target resolution still happens once, at
RENDER time, exactly as it always has for image assets, avoiding a second
lossy scale pass. At RENDER time, a video asset shorter than its scene's
narration loops from its start; one longer than the narration is trimmed
from its start — both in one ffmpeg invocation (`-stream_loop -1` +
`-shortest`).

**Provider pinning**: the asset provider id, safe base-URL host, adapter
version, and search/selection policy (orientation, per-page, min width/
height, duration bounds, safe-search) join the same per-job provider
snapshot as the LLM/TTS blocks (`Job.provider_config`, never the key).
Retries and rejects always re-search/re-download with the pinned
provider and policy; if the environment no longer satisfies the snapshot,
the job fails explicitly with `PROVIDER_NOT_CONFIGURED` instead of
silently switching providers.

**Atomic publish**: search/select/download happens into a worker-private
temp root first and each scene's file is `os.replace()`'d onto the job's
official `assets/scene_i/` path only after the fenced commit succeeds — a
worker that lost its lease can never overwrite the current lease owner's
asset, and no temp files leak on that path. This closes the one stage
(ASSET) that was still unfenced after Phase 2A/2B fenced TTS and RENDER.

**Asset provenance history**: the `Asset` table is append-only (schema v4).
A reject/retry of the ASSET stage inserts a new attempt and marks the
prior attempt's rows `is_current=False` rather than deleting them —
rendering and resume only ever read the current attempt, but every earlier
attempt stays on record for audit. Safe per-scene metadata (provider,
creator, license, dimensions, checksum prefix — never a local filesystem
path or the CDN download link) is available via `job-show --json` and
`GET /v1/jobs/{id}/assets`.

**Cost note**: a stage-level retry of ASSET re-runs the full search and
download for every scene, which re-counts against the provider's rate
limit — same caveat as TTS retries documented above.

## Real-provider smoke checks

```
uv run reel-harness provider-smoke llm
uv run reel-harness provider-smoke tts
uv run reel-harness provider-smoke asset
```

Opt-in, single request, retries disabled, secrets redacted, scratch files
cleaned up on exit. `provider-smoke tts` synthesizes one short fixed
sentence and validates the audio for real; `provider-smoke asset` searches
one fixed safe query ("ocean waves"), selects and downloads one real
candidate, and validates it for real. Printed summaries never include the
key, an auth header, the full request/response body, or (for `asset`) a
download URL. Exit codes: 0 success; 2 not configured / fake provider
selected; 3 auth error; 4 transient (timeout/rate limit/5xx); 5 media-
toolchain/validation failure; `asset` additionally uses 6 for no eligible
candidates surviving selection. Without an API key configured, every one
of these refuses before any network I/O. The default pytest suite and
production-smoke never call a real provider.

## Publishing (YouTube)

Design/API research lives in `docs/PUBLISHING.md`; this section is the
operational how-to. A `Publication` is a separate object from a `Job` — one
completed, approved job can have multiple publications (different accounts,
or a retried one after a failure) — with its own status machine
(`reel_harness/core/state_machine.py`'s `PublicationStatus`), separate from
`Job.status`.

### 1. Configure the OAuth client

```
REEL_HARNESS_YOUTUBE_CLIENT_ID=...
REEL_HARNESS_YOUTUBE_CLIENT_SECRET=...      # env/.env only; never persisted, always redacted
REEL_HARNESS_CREDENTIAL_DIR=...             # a directory OUTSIDE this repository checkout
REEL_HARNESS_YOUTUBE_UPLOAD_CHUNK_SIZE=2097152   # must be a positive multiple of 262144 (256 KiB)
REEL_HARNESS_YOUTUBE_CATEGORY_ID=22         # default: 22 ("People & Blogs")
REEL_HARNESS_YOUTUBE_MADE_FOR_KIDS=false    # sent explicitly on every upload, never omitted
REEL_HARNESS_PUBLISHER_PROCESSING_POLL_INTERVAL=30      # seconds between processing-status polls
REEL_HARNESS_PUBLISHER_PROCESSING_MAX_DURATION=3600     # local timeout; never a provider-reported failure
```

`REEL_HARNESS_CREDENTIAL_DIR` must resolve outside the repository — a
repo-internal path (including the cwd during tests) is rejected at startup
by `publisher.secret_store.resolve_secret_dir`, since credentials must never
land somewhere `git add -A` could pick them up. Refresh tokens, access
tokens, and the resumable upload session URI all live there (JSON files,
one per account/session), never in the jobs SQLite DB.

### 2. Connect an account

```
uv run reel-harness publisher-auth youtube [--account ALIAS]
```

Opens a system browser to Google's consent screen (PKCE + `state`, loopback
`http://127.0.0.1:{port}` redirect, single-use, times out if left idle).
Requests `youtube.upload` + `youtube.readonly` only (never the broader
`youtube`/`youtubepartner` scopes). On success, prints the connected
channel's id/title and whether a refresh token was issued — never the token
itself. `--account` lets more than one channel be connected
(`default` if omitted); every publish/smoke/refresh command below accepts
the same flag.

### 2.5. Check readiness in one command

```
uv run reel-harness publisher-doctor youtube [--account ALIAS] [--check-remote] [--json]
```

A single local-first report covering DB/schema access, storage, the
publisher registry, upload chunk size, whether an OAuth client is
configured, the credential backend (repo-internal paths are rejected the
same way `REEL_HARNESS_CREDENTIAL_DIR` is), the named account's saved
credential (present? refresh token present? expired? marked invalid after
a failed refresh?), ffmpeg/ffprobe, and the publication worker config. Each
check reports `PASS`/`WARN`/`FAIL`/`NOT_CONFIGURED`; the overall verdict is
the worst of them. **No network by default.** `--check-remote` additionally
attempts a real token refresh and a read-only channel-identity fetch; both
print `NOT RUN — credentials not configured` and make no request if the
OAuth client or account credential isn't set up. Exit codes mirror
`provider-smoke`: 0 (`PASS`/`WARN`), 1 (`FAIL`), 2 (`NOT_CONFIGURED`). Never
prints a secret or token.

### 2.6. Manage connected accounts

```
uv run reel-harness publisher-account-list [--provider youtube]
uv run reel-harness publisher-account-show <alias> [--provider youtube]
uv run reel-harness publisher-account-remove <alias> --confirm [--provider youtube]
```

`-list`/`-show` report only safe metadata (channel id/title, whether a
refresh token is present, `created_at`/`last_refreshed_at`, whether the
credential is marked `invalid` and why) — never a token.
`-account-remove --confirm` deletes only the **local** saved credential;
it does **not** revoke authorization at Google (that would invalidate
every token issued to this OAuth client, for every account — a much larger
action than removing one local alias, and is not implemented by this
command). To fully disconnect, also revoke access at
https://myaccount.google.com/permissions.

### 3. Check readiness without uploading anything

```
uv run reel-harness publish-job <job_id> --provider youtube --dry-run [--account ALIAS] [--privacy private|unlisted|public]
```

Re-checks eligibility fresh (job COMPLETED, approved, manifest valid, final
video checksum matches, technical validation passed, every current asset's
license/checksum still holds — see `core.publish_eligibility`), previews the
deterministic title/description/tags/category/madeForKids metadata that
would be sent, reports whether an OAuth credential is saved for the account,
the video file size, and the configured chunk size. **Makes no external
request.** Exit 0 only when every check passes; prints one JSON document
either way (`{"eligible": ..., "eligibility_reasons": [...], ...}`).

### 4. Create the publication (upload happens asynchronously)

```
uv run reel-harness publish-job <job_id> --provider youtube [--account ALIAS] [--privacy private]
```

Re-checks eligibility, then creates a `Publication` row (status
`READY_TO_UPLOAD`) and returns immediately — this command never uploads
anything itself. Idempotent: calling it again for the same
(provider, account, job, final-video-checksum) returns the existing
publication instead of creating a duplicate upload target. `--privacy` is
`private` by default; see the public-upload safeguard below.

### 5. Run the publisher worker

```
uv run reel-harness publisher-run-once [--worker-id ID] [--lease-timeout SEC]
    [--process-upload] [--process-status]
uv run reel-harness publisher-run [--worker-id ID] [--poll-interval SEC]
    [--lease-timeout SEC] [--max-publications N] [--idle-exit-after SEC] [--stop-on-error]
    [--process-upload] [--process-status]
```

A **separate** daemon from `worker-run` (render pipeline) — run both if you
want jobs to render and publish automatically. Same lease-fencing discipline
as the render worker, on its own `locked_by`/`heartbeat_at`/`lease_token`
columns (`Publication`, not `Job`), so the two workers never contend over
the same lease.

**Two lanes, one daemon by default.** `--process-upload` handles session
creation through chunked resumable upload (resuming from the provider's own
confirmed offset after any interruption, never guessing).
`--process-status` handles the processing-status poller (see below).
Omitting both flags means both (the historical, still-default behavior);
passing exactly one restricts this process to that lane, so uploads and
processing polls can run on separate daemon processes if you want to scale
or restart them independently. When one process runs both lanes, it
alternates which lane it tries first each poll cycle so a deep backlog in
one can never starve the other. A transient upstream error backs off to
`RETRY_WAIT`; an auth failure lands in `AUTH_REQUIRED` and a quota error in
`QUOTA_BLOCKED` (all three are reachable from PROCESSING too, not just
UPLOADING — a hiccup polling processing status gets the same soft-retry
treatment as one uploading a chunk).

### 6. Processing completion is polled automatically

Uploading a video is not the same as it being published — YouTube processes
it afterward. The processing lane (`--process-status`, on by default) polls
each `PROCESSING` publication on its own pace: `next_poll_at` spaces
consecutive polls out (default 30s, `REEL_HARNESS_PUBLISHER_PROCESSING_POLL_INTERVAL`)
so it never hammers the provider, and a local max-duration timeout (default
3600s, `REEL_HARNESS_PUBLISHER_PROCESSING_MAX_DURATION`) fails the
publication with `PROCESSING_TIMEOUT` **without ever calling the provider**
once exceeded — the video may still finish on YouTube's side;
`publication-reconcile` (below) can confirm that later. Both are pinned onto
each publication at creation, like the chunk size, so they never change
mid-flight from an operator editing config.

To poke one publication out of turn instead of waiting for the poller:

```
uv run reel-harness publication-status <publication_id>      # read-only, no external request
uv run reel-harness publication-refresh <publication_id>     # re-polls PROCESSING publications only
```

`publication-refresh` only succeeds while the publication is `PROCESSING`;
it advances to `PUBLISHED` (with the real `publication_url`) once YouTube
reports `processingStatus=succeeded`, or to `FAILED` on `failed`/
`terminated`. The equivalent API endpoints are `POST /v1/jobs/{job_id}/publications`,
`GET /v1/publications/{id}`, `POST /v1/publications/{id}/refresh`, and
`POST /v1/publications/{id}/cancel`.

### 6.5. List and filter publications

```
uv run reel-harness publication-list [--provider X] [--account X] [--status X]
    [--job-id X] [--created-after ISO] [--created-before ISO]
    [--failed-only] [--processing-only]
```

Read-only, never contacts the provider. `--failed-only` matches
`FAILED`/`AUTH_REQUIRED`/`QUOTA_BLOCKED` together (`--status` overrides it
if both are given). Safe fields only (id, job id, provider, account,
status, privacy, provider video id, publication URL, byte counts,
failure code/summary, timestamps) — never a token, the upload session
reference, a local credential path, or a raw provider response.

### 6.6. Recover a publication after a crash

A real upload can succeed at the provider in the same instant a worker
process dies, before the DB transaction recording that fact ever commits.
A durable, fsync'd journal (`publisher.journal`, written the instant a
chunk-upload response reports completion — before any DB write) is what
makes recovery possible without ever risking a duplicate upload:

```
uv run reel-harness publication-reconcile <publication_id>
uv run reel-harness publication-reconcile --all   # every non-terminal publication
```

Read-only except for the one case it can positively confirm and repair.
Possible outcomes: `already_consistent`, `recovered_remote_video` (the
journal had a provider_video_id, confirmed via a real read-only
processing-status call, and the DB row is repaired), `upload_incomplete`,
`upload_session_expired`, `remote_video_missing` (a *previously confirmed*
video id no longer resolves), `credentials_unavailable`,
`manual_review_required`, or `ambiguous_remote_state` (the provider reports
the upload as complete but nothing local can explain it — this is the one
case reconciliation deliberately refuses to guess; a human should check the
channel's own uploads before retrying). It never starts a new upload
itself. API equivalent: `POST /v1/publications/{id}/reconcile`.

### 6.7. Manually retry a stuck publication

```
uv run reel-harness publication-retry <publication_id> [--from-stage SESSION|UPLOAD|PROCESSING]
```

Valid only from `FAILED`/`AUTH_REQUIRED`/`QUOTA_BLOCKED`/`RETRY_WAIT` — an
active-looking status (still `UPLOADING`/`PROCESSING`/etc.) is refused with
a pointer to `publication-reconcile` first, never blindly retried.
Re-verifies job eligibility and (once a metadata fingerprint is on record)
that it still matches before allowing the retry; either mismatch refuses
and suggests creating a new publication instead. Without `--from-stage`,
resumes at the least-wasteful safe point automatically (processing-only if
a video id is already known, upload-resume if a session was created, else
from scratch). `AUTH_REQUIRED`/`QUOTA_BLOCKED` retry immediately on the
operator's say-so — retrying cannot itself verify the credential or quota
is actually fixed, so if it isn't, the very next attempt just lands back in
the same status rather than ever risking a duplicate upload. API
equivalent: `POST /v1/publications/{id}/retry` (409 with structured
`reasons` on refusal).

### 7. Public-upload safeguard

`private` is always available with no extra confirmation. `public` requires
**all four** of: `--privacy public`, `--confirm-public-upload`, the job
already being approved, and the `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true`
feature flag — missing any one of these refuses the publish with no upload
attempted. CI, the default pytest suite, and `provider-smoke` never perform
a real public upload.

### 8. Smoke-check the account

```
uv run reel-harness provider-smoke publisher youtube [--account ALIAS]
uv run reel-harness provider-smoke publisher youtube --upload-private-test --confirm-test-upload [--account ALIAS]
```

Default: read-only — refreshes the token if needed and fetches the
connected channel's identity, no quota-significant call. With both
`--upload-private-test` and `--confirm-test-upload`: additionally uploads
one small, real, always-`private`, clearly-titled
(`[reel-harness provider-smoke test upload]`) test clip built from a local
scratch file (never a real job's video) and cleans up the local scratch
directory. **Never auto-deletes the remote video** — YouTube publisher-smoke
does not implement remote delete (see below). Without a configured OAuth
client or a saved credential, prints `NOT RUN — credentials not configured`
and makes no request.

### Cancellation policy

`reel-harness publication-status`/API `cancel` behavior depends on where the
publication is: `CREATED`/`READY_TO_UPLOAD`/`RETRY_WAIT`/`AUTH_REQUIRED`/
`QUOTA_BLOCKED`/`UPLOAD_COMPLETED`/`PROCESSING` cancel immediately (no
worker has anything in flight, and `UPLOAD_COMPLETED`/`PROCESSING` cancel is
purely local bookkeeping — it never deletes anything already on YouTube).
`UPLOADING`/`UPLOAD_PAUSED` only set a flag, honored by the worker at its
next chunk boundary (an in-flight chunk write is never yanked mid-request).
`PUBLISHED`/`CANCELLED` are terminal and refuse.

### Remote video delete — still not implemented in Phase 3B

There is no `publication-delete`/`--delete-remote` command. Cancelling a
publication at any status never deletes an already-uploaded or already-
published YouTube video; removing a real video is left to YouTube Studio
directly. This remains a deliberate scope boundary, not an oversight — see
`docs/PUBLISHING.md`.

### Out of scope for Phase 3B

Facebook Reels publishers, automatic public publishing, scheduled-publish
automation, automatic remote delete, thumbnail/subtitle upload, analytics
collection, auto-commenting, a cloud secret manager, and a cloud queue —
none of these exist yet. TikTok and Instagram publishing are covered below
(Phase 3C/3D). (An OAuth account-management UI and a web dashboard were
out of scope as of Phase 3B; both now exist — see "Web UI — Publishing
(Phase 5B)" above. PostgreSQL was also out of scope then; it exists as of
Phase 6A-1 — see "Database backends" below.)

## Publishing (TikTok)

Design/API research lives in `docs/PUBLISHING.md` (official docs checked
2026-07-29). Everything generic about publishing above — the `Publication`
state machine, the two-lane worker, the processing poller,
`publication-list`/`-reconcile`/`-retry`, cancellation policy — applies to
TikTok exactly as written for YouTube; this section covers only what's
different. A publisher's actual capabilities (allowed privacy values,
whether comments/duet/stitch are configurable, whether a confirmation step
is required before every publish) are never hardcoded per vendor in the
CLI/API/service layers — they come from
`providers.registry.provider_capabilities("tiktok")`
(`providers.base.PublisherCapabilities`), the same mechanism YouTube uses.

**The single biggest real-world constraint**: TikTok forces every post from
an app that hasn't passed its review/audit process to `SELF_ONLY`
visibility, regardless of what privacy level was requested. This project
surfaces that explicitly wherever it's relevant (`publisher-doctor tiktok
--check-remote`'s `app_review_status` check,
`core.publish_reconciliation`'s `app_review_required` outcome, the
`APP_REVIEW_REQUIRED` error) — never as a confusing generic rejection.

### 1. Configure the OAuth client

```
REEL_HARNESS_TIKTOK_CLIENT_KEY=...
REEL_HARNESS_TIKTOK_CLIENT_SECRET=...        # env/.env only; never persisted, always redacted
REEL_HARNESS_TIKTOK_REDIRECT_URI=...         # must be registered with the TikTok app; see step 2
REEL_HARNESS_CREDENTIAL_DIR=...              # same repository-external directory YouTube uses
REEL_HARNESS_TIKTOK_UPLOAD_CHUNK_SIZE=10485760   # default 10 MiB; the official docs do not specify a
                                                  # min/max chunk size (see docs/PUBLISHING.md) -- fully
                                                  # operator-configurable, not a hardcoded protocol limit
REEL_HARNESS_TIKTOK_DEFAULT_PRIVACY=SELF_ONLY    # always the most restrictive option; rarely needs changing
# REEL_HARNESS_TIKTOK_BASE_URL / _AUTH_URL / _TOKEN_URL default to the real TikTok endpoints --
# only override for a contract-test fake server.
```

### 2. Connect an account

```
uv run reel-harness publisher-auth tiktok [--account ALIAS]
```

Opens a system browser to TikTok's consent screen (PKCE + `state`,
requesting only the `video.publish` scope). Unlike YouTube, TikTok's docs
require an HTTPS redirect_uri with no documented "any loopback port"
exception the way Google's installed-app flow has — so this command
supports two flows depending on what `REEL_HARNESS_TIKTOK_REDIRECT_URI` is:

- **A loopback address you registered yourself**
  (`http://127.0.0.1:PORT`/`http://localhost:PORT`, at your own risk): the
  callback is captured automatically, bound to that exact registered port
  (TikTok redirects to exactly what was registered — unlike Google, there's
  no "any ephemeral port accepted" behavior to rely on).
- **Anything else** (the documented case — an `https://` URL you control):
  the command prints the authorization URL, and after you authorize in the
  browser, you paste back the full URL your browser landed on; `state` is
  validated against it exactly like the automated flow, and the code is
  used exactly once.

On success, prints the connected account's TikTok `open_id` and whether a
refresh token was issued — never the token itself. TikTok's refresh token
is itself valid ~365 days (tracked as `refresh_expires_at`, surfaced by
`publisher-doctor tiktok`) and a refresh call may return a *new* refresh
token, which always replaces the stored one — a real behavioral difference
from YouTube's refresh token, which never rotates.

### 2.5. Check readiness in one command

```
uv run reel-harness publisher-doctor tiktok [--account ALIAS] [--check-remote] [--json]
```

Same shape as YouTube's doctor, plus TikTok-specific checks: granted
scope, refresh-token expiry. `--check-remote` additionally attempts a real
token refresh and a read-only `creator_info` query, which reveals the
account's actual allowed privacy levels, comment/duet/stitch
configurability, and max post duration — and, most importantly, whether
this app has passed TikTok's review (`app_review_status`:
`PASS`/`APP_REVIEW_REQUIRED`/`FAIL`/`NOT_CONFIGURED`). Without credentials,
every remote check prints `NOT RUN — credentials not configured` and makes
no request.

### 3. Check readiness without uploading anything

```
uv run reel-harness publish-job <job_id> --provider tiktok --dry-run [--account ALIAS] \
    [--privacy SELF_ONLY|PUBLIC_TO_EVERYONE|MUTUAL_FOLLOW_FRIENDS|FOLLOWER_OF_CREATOR]
```

**Makes no external request** — never even calls TikTok's `creator_info`
query, let alone the publish-init endpoint (`publisher-doctor tiktok
--check-remote` is the live-check path). Reports a `tiktok_preview` block:
the post text that would be sent (validated against TikTok's length/
forbidden-marker rules, `post_text_error` on a violation), the default
platform_options (comments/duet/stitch all disabled, no disclosure toggles
set, until a future CLI flag exposes them individually), the expected API
mode (`FILE_UPLOAD` — `PULL_FROM_URL` is never used, see below), and the
chunk plan. `creator_info`/`app_review_status` are explicitly reported as
"not fetched" here, with a pointer to the live-check command, rather than
silently omitted.

### 4. Create the publication

```
uv run reel-harness publish-job <job_id> --provider tiktok [--account ALIAS] \
    --privacy SELF_ONLY --confirm-platform-options
```

`--confirm-platform-options` is **required** for TikTok (`publish-job`
refuses without it) — TikTok's capabilities set
`requires_user_confirmation=True`, since a direct TikTok post carries more
consequential per-post choices (comments/duet/stitch, commercial/branded-
content disclosure) than YouTube's upload does. `--privacy` defaults to
`SELF_ONLY` (the provider's own most restrictive option) if omitted;
`PUBLIC_TO_EVERYONE` additionally requires `--confirm-public-upload` and
`REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true`, exactly like YouTube's `public` —
even then, an unaudited app is forced back to `SELF_ONLY` by TikTok itself.

### 5. Run the publisher worker

Same `publisher-run`/`publisher-run-once` commands as YouTube — a
publication's own `publisher_config` snapshot (captured at creation, via
`providers.registry.publisher_snapshot`) determines which adapter it
resolves to, so one worker process handles YouTube and TikTok publications
together, fairly, with no separate TikTok-only daemon. Before creating (or
re-creating, after a fresh-session self-heal) an upload session, the
worker always re-fetches `creator_info` and re-validates the requested
privacy/platform-options against it — never trusting an earlier snapshot,
and never silently substituting a different option if something's changed
(an app losing review status, an account-level comment/duet/stitch
setting changing) since the publication was created.

**TikTok upload sessions cannot be resumed** — the official docs don't
document a way to query a session's already-confirmed byte offset (unlike
YouTube's `Content-Range: bytes */TOTAL` convention). Every interruption
(a transient error, a worker crash, a `RETRY_WAIT` cycle) is handled by
starting a **brand-new** session and re-uploading the entire file from
byte 0, rather than guessing at an offset — the previous session's
`publish_id` is simply abandoned (TikTok never auto-expires or auto-
deletes it; nothing in this project does either). This is a real
efficiency cost compared to YouTube's true resume, traded for never
risking a wrong-offset re-upload against an unconfirmed guess.

TikTok's `publish_id` (this project's `provider_video_id`) is known
immediately from the upload-session-creation response, before any bytes
are sent — unlike YouTube's, which is only known once the upload
completes. It's persisted the moment it's known, closing an even earlier
crash-recovery gap than YouTube's for providers structured this way.

### 6. Processing completion is polled automatically

Same processing poller as YouTube. TikTok's post-status values map to the
common `processing`/`succeeded`/`failed` vocabulary; `SEND_TO_USER_INBOX`
(TikTok routed the post to the account's inbox as a draft instead of
actually publishing it — e.g. the account lacks Direct Post permission) is
treated as `failed`, not left polling forever for a `PUBLISH_COMPLETE`
that will never arrive. `publication_url` stays unset for TikTok — the
status-fetch response doesn't carry enough account info (the creator's
handle) to build a public watch URL from this adapter alone; the durable
reference is `provider_video_id` (the `publish_id`).

### 7. Smoke-check the account

```
uv run reel-harness provider-smoke publisher tiktok [--account ALIAS]
uv run reel-harness provider-smoke publisher tiktok \
    --upload-private-test --confirm-test-upload --confirm-platform-options [--account ALIAS]
```

Default: read-only — refreshes the token if needed and fetches
`creator_info` (account identity, allowed privacy levels, app-review
status, comment/duet/stitch configurability, max post duration). With all
three of `--upload-private-test --confirm-test-upload
--confirm-platform-options`: additionally uploads one small, real, always-
`SELF_ONLY`, clearly-titled test clip with comments/duet/stitch all
disabled, built from a local scratch file (never a real job's video), and
cleans up the local scratch directory. If `creator_info` reports no
allowed privacy levels at all (a deeper permission problem than the
ordinary unaudited-app restriction), prints the distinct `TikTok private
upload smoke: NOT RUN — application permission not available` rather than
attempting an upload that would just fail. **Never auto-deletes the remote
post.** Without a configured OAuth client or a saved credential, prints
`NOT RUN — credentials not configured` and makes no request.

### Out of scope for Phase 3C

`PULL_FROM_URL`/`URL_PULL_FROM_SERVER` upload (no cloud hosting available
— `FILE_UPLOAD` only), automating TikTok's own app-review process,
scheduled publish, thumbnail/cover-image upload beyond the default
`video_cover_timestamp_ms`, subtitle upload, analytics, remote post
delete, and a rich CLI surface for every individual platform_options
toggle (comments/duet/stitch/disclosures currently default to the safest
combination; per-post overrides are a natural future extension, not yet
built).

## Publishing (Instagram Reels)

Design/API research lives in `docs/PUBLISHING.md` (official Meta for
Developers docs checked 2026-07-29, Graph API `v25.0`). Everything generic
about publishing above — the `Publication` state machine, the two-lane
worker, the processing poller, `publication-list`/`-reconcile`/`-retry`,
cancellation policy — applies to Instagram exactly as written for YouTube;
this section covers only what's different.
`providers.registry.provider_capabilities("instagram")` reports
`privacy_values={"PUBLIC"}` — **every Instagram Reels publish is
inherently public; there is no private/unlisted option in the official
API**, so the double-confirmation gate (below) applies unconditionally,
never just to an opt-in "public" choice like YouTube/TikTok.

**Design decision — no public media-hosting server was built.** Instagram's
Content Publishing API documents two ways to hand over video bytes:
`video_url` (Meta's servers fetch a publicly reachable HTTPS URL you host)
or `upload_type=resumable` (you `POST` the bytes directly to
`rupload.facebook.com`, no public URL ever needed). This project
implements **only** the resumable direct-upload path — the same
`InstagramPublisher.create_upload_session`/`upload_chunk` shape every other
adapter uses — deliberately declining to stand up a new public HTTPS
listener for a local-first, single-user tool. `REEL_HARNESS_INSTAGRAM_MEDIA_URL_MODE=external_url`
is a recognized config value that fails loudly with
`ProviderConfigurationError` at startup (not implemented), rather than
silently falling back to the resumable path.

**No confirmed multi-chunk resume.** The official docs don't document a way
to query a resumable session's already-confirmed byte offset (unlike
YouTube's `Content-Range: bytes */TOTAL`), and the upload is always one
whole-file request — `query_upload_offset` always raises
`UploadSessionExpiredError`. In practice that path is never actually hit:
`worker.publish_runner._upload_stage`'s `bytes_uploaded==0` shortcut means
any interrupted attempt (nothing confirmed received yet) simply retries the
**same** container's upload URL directly, rather than querying an offset
that doesn't exist. Only a container the provider itself reports as no
longer usable triggers a brand-new container.

### 1. Configure the app and OAuth client

```
REEL_HARNESS_INSTAGRAM_APP_ID=...
REEL_HARNESS_INSTAGRAM_APP_SECRET=...        # env/.env only; never persisted, always redacted
REEL_HARNESS_INSTAGRAM_REDIRECT_URI=...      # must be registered with the Meta app; see step 2
REEL_HARNESS_CREDENTIAL_DIR=...              # same repository-external directory YouTube/TikTok use
REEL_HARNESS_INSTAGRAM_GRAPH_BASE_URL=https://graph.instagram.com   # default; override only for a fake server
REEL_HARNESS_INSTAGRAM_GRAPH_API_VERSION=v25.0                      # default
REEL_HARNESS_INSTAGRAM_MEDIA_URL_MODE=resumable   # the only implemented mode; "external_url" fails at startup
REEL_HARNESS_INSTAGRAM_SHARE_TO_FEED=false        # default: Reels-only, not also shared to the main feed
```

### 2. Connect an account

```
uv run reel-harness publisher-auth instagram [--account ALIAS]
```

Uses **Instagram Login for Business** (not Facebook Login for Business —
no linked Facebook Page or Business Manager dependency for this login
method), requesting only `instagram_business_basic` +
`instagram_business_content_publish`. Same dual loopback/manual-paste flow
as TikTok's `publisher-auth`, plus one extra step Instagram requires: the
short-lived token from the initial exchange is immediately exchanged for a
**long-lived** token (~60 days). On success, prints the connected
account's Instagram user id/username and the token's expiry — never the
token itself.

**Instagram has no separate `refresh_token` grant.** Unlike YouTube/TikTok,
`OAuthCredential.refresh_token` always stays `None` for Instagram; the
long-lived access token refreshes **itself** by presenting its own current
value to Meta's `refresh_access_token` endpoint, tracked via
`OAuthCredential.expires_at`/`last_refreshed_at`/`last_refresh_error` the
same as any other provider.

### 2.5. Check readiness in one command

```
uv run reel-harness publisher-doctor instagram [--account ALIAS] [--check-remote] [--json]
```

Same shape as YouTube's/TikTok's doctor. Since there's no refresh token to
check, the local pass instead reports `token_expiry` (warns as the
long-lived token nears its ~60-day expiry, noting self-refresh will be
attempted on next use). `--check-remote` additionally attempts the
self-refresh, a real account-identity fetch, and an `account_eligibility_status`
check (`PASS`/`WARN` on approaching the publishing-limit quota/`FAIL` if
the account type isn't Reels-eligible/`NOT_CONFIGURED`). Without
credentials, every remote check prints `NOT RUN — credentials not
configured` and makes no request.

### 3. Check readiness without uploading anything

```
uv run reel-harness publish-job <job_id> --provider instagram --dry-run [--account ALIAS]
```

**Makes no external request.** Reports an `instagram_preview` block: the
caption that would be sent (validated against Instagram's 2200-character
limit and internal-marker rules, `caption_error` on a violation), local
video-limit validation (duration 3s–15min, file size ≤300MB —
`video_limits_error` on a violation, both genuinely confirmed via Meta's
`ig-user/media` reference), the default `platform_options`
(`share_to_feed=false`), and `expected_api_mode="FILE_UPLOAD_RESUMABLE"`.
`account_info`/`account_eligibility_status` are explicitly reported as "not
fetched" here, with a pointer to `publisher-doctor instagram
--check-remote`, exactly like TikTok's `creator_info`.

### 4. Create the publication

```
uv run reel-harness publish-job <job_id> --provider instagram [--account ALIAS] \
    --confirm-public-upload --confirm-platform-options
```

Both flags are **required** — Instagram's capabilities set
`requires_user_confirmation=True` and its only privacy value is `PUBLIC`,
so `publish-job` refuses without both, every time, with no lower-friction
option (there is no `--privacy private` equivalent to fall back to).
`REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true` is also required, same as
YouTube's/TikTok's public path.

### 5. Run the publisher worker

Same `publisher-run`/`publisher-run-once` commands as YouTube/TikTok — one
worker process handles all three providers together, fairly, with no
separate Instagram-only daemon. Before creating the (irreversible)
container, the worker fetches account info fresh and validates eligibility
(account type, Page-linked business/creator requirements, publishing-limit
quota) — never trusting an earlier snapshot. This check happens once, at
container-creation time (mirroring TikTok's already-shipped
`creator_info` check), not re-queried on a later resume that reuses the
same still-live container, since no new irreversible action is being taken
at that point.

Instagram's `container_id` (this project's `provider_video_id`) is known
immediately from the container-creation response, before any bytes are
sent — the same early-closure pattern as TikTok's `publish_id`.

### 6. Processing and publish are handled transparently

Instagram's flow has one more explicit step than YouTube/TikTok: after
upload completion, the container must reach provider status `FINISHED`
before an explicit `media_publish` call returns the real media id — upload
completing is **not** the same as being published. This project's
processing poller handles that transparently: the first poll that observes
`FINISHED` immediately calls `media_publish` and fetches the permalink
inside the same `get_processing_status()` call; a later poll observing the
container already `PUBLISHED` is recognized as already-done and never
re-publishes. From the outside, `PROCESSING -> PUBLISHED` looks identical
to YouTube's flow — the extra call is an internal adapter detail, not a
new CLI step.

### 7. Smoke-check the account

```
uv run reel-harness provider-smoke publisher instagram [--account ALIAS]
uv run reel-harness provider-smoke publisher instagram \
    --upload-public-test --confirm-test-upload --confirm-public-upload --confirm-platform-options [--account ALIAS]
```

Default: read-only — refreshes the token if needed and fetches account
info (identity, account type, Page linkage, publishing-limit quota).
**There is deliberately no `--upload-private-test` option for Instagram**
— since the platform itself has no private-post feature, offering a
flag named that way would misleadingly imply a privacy guarantee this
adapter cannot make. The real-upload flag is named `--upload-public-test`
and requires all three of `--confirm-test-upload --confirm-public-upload
--confirm-platform-options` together before it uploads one small, real,
clearly-captioned test Reel (`[reel-harness provider-smoke test upload]`)
built from a local scratch file, then polls it through to completion.
**Never auto-deletes the remote post** (Instagram Reels delete is not
implemented — see below). Without credentials, prints three distinct `NOT
RUN` lines (remote doctor / read-only smoke / public-upload smoke), each
naming exactly what's missing (credentials vs. permission/account
linkage).

### Remote Reels delete — not implemented

There is no `publication-delete`/`--delete-remote` command for Instagram
(or any provider). Removing a published Reel is left to Instagram
directly.

### Out of scope for Phase 3D

Facebook Reels Publisher, Facebook Login for Business, the `video_url`
(`external_url`)-hosted upload path and any public media-hosting server it
would require, automating Meta's own app-review process, automatic public
publishing (public is Instagram's *only* mode, but still requires the
explicit double-confirmation + feature flag above), scheduled-publish
automation, automatic remote post delete, thumbnail-only upload, subtitle
upload, analytics collection, comments management, a web dashboard (now
exists — Phase 5A/5B), PostgreSQL (now exists — Phase 6A-1), a cloud
queue, a forced cloud-storage vendor, and arbitrary tunneling software.

## Fable cinematic projects (F1/F2)

A separate pipeline from the short-form job flow: a story becomes a shot
plan, each shot is generated as a video clip, you pick takes, and the
selected takes are cut into a film. Artifacts live under
`REEL_HARNESS_FABLE_PROJECTS_DIR` (default `./fable_projects`), never
mixed into `jobs/`.

```
uv run reel-harness fable-create --title "비 오는 밤" --story-file story.txt
uv run reel-harness fable-adapt <project_id>
uv run reel-harness fable-approve <project_id> --step story   # -> CASTING
uv run reel-harness fable-generate-references <project_id>    # -> CHARACTER_REVIEW
uv run reel-harness fable-reference <character_id>            # approve the sheet
uv run reel-harness fable-approve <project_id> --step characters
uv run reel-harness fable-estimate <project_id>                  # what it would cost
uv run reel-harness fable-budget <project_id> --limit 5 --currency USD
uv run reel-harness fable-approve <project_id> --step shots      # cost gate
uv run reel-harness fable-worker-run --idle-exit-after 5
uv run reel-harness fable-status <project_id>
uv run reel-harness fable-select-take <take_id>
uv run reel-harness fable-render <project_id>
uv run reel-harness fable-approve <project_id> --step final
```

**Nothing advances without you.** Every `*_REVIEW` state requires an
explicit approval command; `--step shots` is the single entry into
generation, which is the only phase that can cost money. `serve
--fable-workers N` (default 0) runs the generation lane alongside the
other workers.

### Casting: character reference sheets

`fable-generate-references` produces a four-view reference sheet per
character — a face portrait, a three-quarter view, a full-body view, and
a wardrobe detail — and moves the project `CASTING -> CHARACTER_REVIEW`.

**The face is generated first, and the other three are generated from
it.** Each later view is sent with the face image attached as a character
reference. This is not an optimization: generating the four
independently produces four different-looking people, and since every
shot's footage imitates this sheet, that would make the film's lead
change face between shots. The order lives in
`pipeline/reference_prompt.py`, not in the caller.

Generation is not approval. Each sheet arrives unapproved, and
`fable-approve --step characters` refuses until every character's sheet
has been approved with `fable-reference <character_id>`. Approving the
cast means approving the actor you actually looked at.

- `fable-reference <character_id> --reject` un-approves a sheet and
  clears its fingerprint, so the next `fable-generate-references` run
  regenerates it. The images stay on disk — they were paid for, and
  deleting them would destroy the evidence of what was rejected.
- Re-running `fable-generate-references` with an unchanged character
  bible is a **replay**, not four more paid calls. Changing the bible
  changes the sheet's fingerprint, which regenerates it *and* revokes any
  previous approval — you are looking at a different actor now.
- A **safety refusal** does not fail the project. The reason is recorded
  on the character (`reference_failure_code`), whatever views were
  generated are kept, and the project still reaches `CHARACTER_REVIEW` so
  you meet every refusal at once and decide there: edit the character
  bible and regenerate, or drop the character. An incomplete sheet cannot
  be approved.
- Reference images cost money like any other generation: the same double
  gate applies, the whole cast is priced up front (affording half a cast
  is not affording it), and each character's sheet is a line item in the
  spend audit.

### Choosing the cinematic video provider

`REEL_HARNESS_CINEMATIC_PROVIDER`:

- **`fake`** (default) — deterministic, renders real mp4s via local
  ffmpeg, no network. Stamped `FAKE_TEST_LICENSE`.
- **`google`** — Vertex AI Veo (`veo-3.1-fast-generate-001`). Needs the
  optional `google` extra and a credential; shares both with the
  reference-image adapter.

```
REEL_HARNESS_CINEMATIC_PROVIDER=google
REEL_HARNESS_GOOGLE_USE_VERTEX=true
REEL_HARNESS_GOOGLE_PROJECT=my-project
REEL_HARNESS_GOOGLE_LOCATION=us-central1     # the ONLY supported region
```

Three documented constraints are enforced locally, before any request is
sent, because each one costs a generation to learn the hard way:

- **Reference-driven runs are fixed at 8s / 720p.** Not a preference —
  attaching character references makes the API fix both, so asking for
  anything else silently returns something different. Requesting a
  mismatch is refused here instead.
- **At most 3 reference images**, sent as type `asset` (which transfers
  identity; `style` would transfer look).
- **`person_generation=allow_adult`**, stated at the API boundary as well
  as in every prompt.

`us-central1` is the only region the GA endpoint serves, so a project
pinned elsewhere fails at **startup** with the right region named — not
at generation time, after you believed it was configured.

**Generated videos are deleted after two days.** The adapter downloads
bytes immediately; the local file under `fable_projects/` is the
artifact, and no provider URI is ever treated as durable storage.

A safety-filtered result surfaces as `moderated`, which routes the shot
to `REVIEW_REQUIRED` — never a blind retry, since the same prompt would
be filtered again. **Cancellation is local-only**: the SDK exposes no
cancel for a video operation, so `cancel_generation` forgets the handle
and does not claim to have stopped a generation you will still be billed
for.

### Film assembly: transitions, fades and audio

The final render defaults to **hard cuts**, assembled with a lossless
stream copy. Anything that blends pixels needs a full re-encode, so it is
opt-in rather than on by default:

```
REEL_HARNESS_FABLE_TRANSITION=dissolve        # cut | dissolve | fade_black
REEL_HARNESS_FABLE_TRANSITION_SECONDS=0.5
REEL_HARNESS_FABLE_FADE_IN_SECONDS=0.5
REEL_HARNESS_FABLE_FADE_OUT_SECONDS=1.0
REEL_HARNESS_FABLE_MUTE_AUDIO=false
```

Points worth knowing:

- **A transition overlaps the two shots it joins**, so each one makes the
  film *shorter*. Four 8s shots with 0.5s dissolves run 30.5s, not 32s.
- Audio crossfades alongside the video, so sound does not jump ahead of
  picture. Veo generates native audio; muting is an explicit editorial
  choice, never a silent side effect of assembly.
- A plan that cannot work — a transition longer than the shortest clip,
  fades longer than the film — is refused at **startup**, and again
  before ffmpeg runs. Discovering it at the final render would waste an
  entire paid generation run.
- `REEL_HARNESS_FABLE_RENDER_TIMEOUT_SECONDS` (default 1800) exists
  because re-encoding a film takes far longer than copying one.

### Choosing the reference-image provider

`REEL_HARNESS_REFERENCE_IMAGE_PROVIDER`:

- **`fake`** (default) — deterministic colour panels, no network. For
  tests and for exercising the workflow.
- **`demo`** — the same idea with a per-character palette so a sheet is
  eyeball-able offline: one hue per character, one shade per view. Every
  image is stamped `DEMO_TEST_LICENSE` and can never pass a publish gate.
  Deliberately **synthetic rather than bundled photos**: shipping sample
  "people" with a local-first tool would mean shipping either a real
  person's likeness or AI output this tier explicitly does not produce.
- **`google`** — the real thing: `gemini-3.1-flash-image` via the
  `google-genai` SDK. Needs the optional extra
  (`uv sync --extra google`) and a credential. Never a hard dependency —
  the whole Fable pipeline runs offline on the other two tiers.

Two auth paths, sharing one credential with F5's Veo adapter (the reason
this vendor was chosen):

```
REEL_HARNESS_REFERENCE_IMAGE_PROVIDER=google
REEL_HARNESS_GOOGLE_API_KEY=...                  # Gemini Developer API
# ...or Vertex AI with application-default credentials:
REEL_HARNESS_GOOGLE_USE_VERTEX=true
REEL_HARNESS_GOOGLE_PROJECT=my-project
REEL_HARNESS_GOOGLE_LOCATION=us-central1
```

Selecting `google` without a usable credential fails **at startup** with
the exact missing variable names — never at first use, halfway through a
paid casting run.

Two deliberate limits on the real adapter:

- **Only 512 and 1K resolutions are offered.** Veo caps reference-driven
  runs at 720p, so a 2K or 4K reference costs more and buys nothing any
  shot could use. Offering it would be a trap, not a feature.
- **`REEL_HARNESS_REFERENCE_IMAGE_PRICE_USD`** (default `0.067`) is the
  published list price, configurable because a vendor's tariff is not
  this project's to promise. Unset it and estimates report `unknown`,
  which makes a budgeted project refuse to run rather than spend against
  a number nobody stands behind.

**Every generated image carries a SynthID watermark**, recorded on the
result. Google applies it to all generated imagery with no removal
option. Whether Veo accepts SynthID-watermarked images as
character-reference input is an **open question no documentation
answers** — if it does not, the whole consistency strategy needs
rethinking.

### `fable-reference-smoke`

```
uv run reel-harness fable-reference-smoke --keep-output ./smoke
uv run reel-harness fable-reference-smoke --confirm-paid-generation   # real provider
```

One real reference-image chain — a face, then a three-quarter view
generated *from* it — against whichever provider is configured. It
answers with actual bytes what the test suite structurally cannot: that
the adapter reaches the provider, and that the model accepts its own
generated image back as a character reference.

It spends real money on a real tier, so it refuses without
`--confirm-paid-generation` and tells you what it would cost first
(exit code 4). Against `fake`/`demo` it is a free wiring check.

The output states its own limits in a `does_not_prove` field, so a pasted
result can never be read as more than it is. In particular it does **not**
establish that the two images depict a recognizably identical person —
nothing automated judges that, so look at them (`--keep-output`) — and it
does not answer the Veo/SynthID question, which needs F5's video adapter.

Run it as soon as GCP credentials exist. It is the cheapest way to find
out whether the consistency strategy holds before F5 builds on it.

### Multiple candidate takes per shot

`REEL_HARNESS_FABLE_TAKES_PER_SHOT` (1, 2 or 4; default 1), overridable
per project with `fable-create --takes-per-shot N`. Each take is a
**separate paid generation**, so asking for 4 costs four times as much —
the default is 1 and more is an explicit choice to spend N times as much
for something to choose between.

- Each take gets a **distinct seed**, deterministically derived from the
  prompt fingerprint and the attempt number. Distinct because N takes
  from one prompt with one seed are N copies of the same clip;
  deterministic because a re-run after a crash must reproduce the take it
  already paid for rather than buy a different one.
- The budget is checked **per take**, not per shot. A project that can
  afford two takes but not four generates two and stops with the shot
  reviewable — refusing to produce any would waste the ones it could pay
  for.
- A failure on a later take **never discards the earlier ones**. A shot
  with two good takes and a third that timed out lands in
  `REVIEW_REQUIRED` with the failure recorded, not `FAILED`. A shot only
  fails when it produced nothing at all.
- Re-running a complete batch **buys nothing**: the takes are already
  there, so the run replays.
- Selecting one take **retains the others**, media included. Rejected
  candidates are never deleted on selection.
- Only 1, 2 and 4 are accepted. `40` is a typo that would spend forty
  times the estimate a human approved.

### Cost, budgets, and the paid-generation gate

Generation is the only phase that spends money, and two independent
switches must both be on before a cost-incurring provider will run:

1. `REEL_HARNESS_ALLOW_PAID_GENERATION=true` — the operator-wide switch,
   off by default.
2. `fable-budget <project_id> --limit N --currency X` — that project's own
   explicit ceiling.

Neither implies the other, deliberately: the same shape as
`REEL_HARNESS_ALLOW_PUBLIC_UPLOAD` plus `--confirm-public-upload`. A
project with **no** limit set is not "unlimited" — it is "no decision
made", and a paid provider is refused outright. The offline `fake`/`demo`
tiers cost nothing and are never gated by either switch.

`fable-approve --step shots` prices the whole shot plan before making any
shot claimable, and refuses if the total would pass the limit — failing
there costs nothing, whereas failing later means shots were queued that
could never all be paid for. The worker re-checks per shot anyway, since
config and budget can both change after approval.

**Running out of budget is a review, not a failure.** A shot the project
cannot pay for stops at `REVIEW_REQUIRED` with `failure_code` =
`BUDGET_EXCEEDED` (or `PAID_GENERATION_NOT_ALLOWED`) *before* any provider
call, so nothing was charged and no take exists. Raise the limit and the
shot re-queues through the same path a rejected take uses.

What the numbers mean, precisely:

- **Estimates never move spend.** `budget_spent_amount` only ever
  accumulates a cost the provider *reported* for a generation that
  actually completed, recorded in the same transaction as the take it
  belongs to.
- **Unknown stays unknown.** A provider that publishes no price yields
  `known: false` from `fable-estimate` — never a guessed number. Under a
  live budget an unpriceable generation is *refused*, since allowing an
  unbounded charge is precisely what a ceiling forbids.
- **`unpriced_take_count`** in `fable-status`/`fable-budget` counts
  completed takes the provider gave no figure for. Non-zero means the
  reported spend is a lower bound, and it says so rather than quietly
  under-reporting.
- **Currencies are never converted.** A provider quoting or billing in a
  currency the budget is not denominated in is refused
  (`BUDGET_CURRENCY_MISMATCH`), never converted at an invented rate.

Lowering a limit below what a project has already spent is refused;
clearing a limit (`--clear`) is always allowed, re-closes the paid gate,
and un-spends nothing.

### The Fable web UI

`reel-harness serve` exposes the whole lifecycle at `/fable`, alongside
the job and publication screens. Three pages: a project list, a create
form, and a project detail page that carries every gate action, the
casting review, the shot/take table and the budget controls.

Two rules the pages follow, both inherited from Phase 5A/5B:

- **A button that is shown is one the service will accept.** Every
  `can_*` mirrors the real `FableService` precondition rather than a
  guess from the transition table, so the page can never offer an action
  that 409s. When the character gate is blocked, the page says *why*
  ("2 unapproved reference sheets") instead of silently hiding a button.
- **Forms are disabled, never removed.** A page that hides a whole form
  also hides its CSRF field, which was a real bug in Phase 5A.

Every mutating route is CSRF-protected (double-submit cookie) and
answers with Post/Redirect/Get, so a browser refresh can never
re-submit a generation that costs money. A refusal comes back as a
readable message on the page, not a traceback — refusals here are normal
(a gate not reached, a budget exhausted) and the reason is the useful
part.

### The `/v1/fable/*` API

Every CLI action above has an HTTP equivalent, bearer-token authenticated
like the rest of `/v1/*`. The routes add **no domain logic and no new
permission**: each one calls the same `FableService` method the CLI does,
so every gate that refuses the CLI refuses the API identically.

```
POST   /v1/fable/projects                      create
GET    /v1/fable/projects                      list
GET    /v1/fable/projects/{id}                 status
GET    /v1/fable/projects/{id}/shots           shots + their takes
GET    /v1/fable/projects/{id}/characters      cast + reference sheet state
GET    /v1/fable/projects/{id}/budget          spend position
PUT    /v1/fable/projects/{id}/budget          set/clear the limit
GET    /v1/fable/projects/{id}/estimate        price the plan (read-only)
POST   /v1/fable/projects/{id}/adapt           DRAFT -> STORY_REVIEW
POST   /v1/fable/projects/{id}/references      CASTING -> CHARACTER_REVIEW
POST   /v1/fable/characters/{id}/approve       approve one sheet
POST   /v1/fable/characters/{id}/reject        reject one sheet
POST   /v1/fable/projects/{id}/approve         {"step": story|characters|shots|final}
POST   /v1/fable/takes/{id}/select             select one take for its shot
POST   /v1/fable/projects/{id}/render          cut the final film
POST   /v1/fable/projects/{id}/cancel          cancel
```

One uniform error contract: **404** for something that does not exist,
**409** for an action that is not valid on this project right now (a
review gate not reached, an unapproved reference sheet, a budget
exhausted), **422** for a malformed request, **502** for a provider that
failed underneath.

Response models are explicit rather than serialized ORM rows, so a column
added to `StoryProject` can never leak through the API by accident — the
source text and local filesystem paths are never returned.

### Choosing the Narrative Director

`REEL_HARNESS_NARRATIVE_PROVIDER`:

- **`fake`** (default) — deterministic, no network. Produces a complete
  adaptation that passes every real validator; useful for exercising the
  whole flow offline. Never presented as a real adaptation.
- **`openai-compatible`** — a real LLM. Reuses the
  `REEL_HARNESS_LLM_BASE_URL` / `_MODEL` / `_API_KEY` block (adaptation
  is a chat-completions call against the same kind of endpoint), with its
  own output budget and read timeout since a shot plan is much larger
  than a short-form script:
  `REEL_HARNESS_NARRATIVE_MAX_OUTPUT_TOKENS` (default 6000),
  `REEL_HARNESS_NARRATIVE_READ_TIMEOUT` (default 120s).

Selecting `openai-compatible` without those credentials fails at startup
with the exact missing variable names, never at first use.

### What the adaptation is checked against

The model's output is validated before anything is persisted; a failure
returns the exact errors to the model for at most **two** repair attempts
(three calls total) and then fails the stage. A refusal or empty response
is not repaired at all — re-asking with the same source only burns quota.

- Characters must be fictional **adults** (age bracket whitelist plus an
  explicit adult flag). A minor-looking character fails parsing outright.
- One filmable action per shot; exactly one camera movement per shot;
  shot size/angle/movement must be real film-grammar values.
- 1–2 characters, 1–3 locations, 1–6 scenes, 4–15 shots, 2–8s per shot.
- Every scene must quote the **actual source text** it dramatizes;
  fabricated citations are rejected.
- Multi-speaker scenes must alternate subjects (shot/reverse-shot).

**Scope of the automated fidelity check**: it catches obvious drift —
invented source quotes, a dropped ending. It does **not** judge whether
the adaptation is a *good* reading of your story. That is what the
STORY_REVIEW gate is for; approve it yourself before casting.

### Idempotency and recovery

Re-running `fable-adapt` with unchanged input replays the stored
adaptation instead of paying for a second call. Changing the source text
of an already-adapted project is refused — reject the story review to
re-adapt instead. If adaptation crashes mid-flight, the project stays in
`ADAPTING` with no children written; simply re-run `fable-adapt`.
Re-adaptation refuses outright once any shot has takes, so generated
footage can never be orphaned by a plan change.

### Live verification status

The real-provider adapter is covered by contract tests against a mock
transport (protocol conformance, retries, rate-limit handling, auth
failures that never echo the credential). Contract-test success is never
reported as live success.

**Live adaptation: RUN and PASSED** (2026-07-31, `openai-compatible`
against `gpt-4o`). A real Korean short story produced a valid shot plan
in ~26 seconds — 1 adult character, 1 location, 2 scenes, 4 shots (18s
total) — passing every validator on the first attempt with no repair
needed. Adaptation is a single LLM call of a few thousand output tokens,
so cost per adaptation is roughly that of one large chat completion;
budget controls for the far more expensive video-generation phase arrive
in F3.

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

## Web UI (Phase 5A)

`reel-harness serve` mounts a server-rendered web UI (Jinja2 + HTMX +
vanilla JS, zero Node/SPA build step) onto the same FastAPI app as the
JSON API and ops endpoints — there is no separate `web` command. Open
`http://<host>:<port>/` (default `http://127.0.0.1:8000/`) after `serve`
starts.

**Routes**: full pages (`/`, `/jobs`, `/jobs/new`, `/jobs/{id}`, `/system`,
`/settings`) plus HTMX fragment/action endpoints under the same
`/jobs/{id}/...` path space (`status`, `cancel`, `approve`, `reject`,
`retry`, `video`) and static assets under `/static/*`. None of this
overlaps the existing `/v1/*` JSON API or the top-level ops routes
(`/healthz`, `/readyz`, `/status`, `/metrics`) — the web UI is purely
additive.

**In-process, not a second API client**: web routes call `AppContext`
(`ctx.jobs`, `ctx.storage`, `ctx.publications`) directly, the same way the
CLI does — never an HTTP self-call to `/v1/*`.

**Security model — deliberately not a login system**: the web UI has no
accounts, matching this tool's local-first single-user design (same trust
boundary `/healthz`/`/status` already use — unauthenticated in the "who are
you" sense). What it does add is CSRF hardening: a double-submit cookie
(`rh_csrf`, `SameSite=Strict`) plus a matching `X-CSRF-Token` header/hidden
form field on every mutating request, checked by a `require_csrf`
dependency independent of `/v1/*`'s `require_api_key` bearer-token gate.
This defends against a different attacker than `require_api_key` does — a
malicious page open in another browser tab, not a network attacker who can
already reach the port. Every response also carries
`X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy`, and a
`Content-Security-Policy` with no inline script/style (HTMX is vendored at
`reel_harness/web/static/htmx.min.js`, never loaded from a CDN).

**Binding beyond loopback**: `Settings.api_host` (`REEL_HARNESS_API_HOST`,
default `127.0.0.1`) is what `serve --host` defaults from when `--host` is
not explicitly passed, so `preflight` and the running server always agree
on the same source of truth. `preflight`'s `public_bind_security` check
WARNs (any profile) or FAILs (`--profile production`) when bound beyond
loopback — the web UI having no login is a real exposure if the port is
reachable from an untrusted network; put a real authenticating reverse
proxy in front of it before doing that.

**Provider profile per job**: the New Job form's Demo/Real/Fake choice is a
genuine per-job override (`JobService.create_job`'s optional
`provider_snapshot` parameter), independent of whatever `REEL_HARNESS_
LLM_PROVIDER`/etc. the process was started with. Real is only selectable
when `openai-compatible` LLM/TTS + Pexels are actually configured (checked
fresh per page render); Fake is hidden entirely unless
`REEL_HARNESS_UI_SHOW_FAKE_PROFILE=true` is set (an env flag, not a UI
toggle — Fake is a pipeline-test tool, not an end-user mode).

**Video streaming**: `GET /jobs/{id}/video` (`?download=1` for
`Content-Disposition: attachment`) uses Starlette's `FileResponse`, which
already implements HTTP Range/206 support — no hand-rolled byte-range
code. The filesystem path is resolved exclusively through
`LocalFilesystemStorage.path_for()` (UUID-validated, traversal-safe); the
route never accepts a client-supplied path.

**Packaging**: `reel_harness/web/templates/` and `reel_harness/web/static/`
ship inside the wheel via an explicit `[tool.hatch.build.targets.wheel]
artifacts` entry in `pyproject.toml` — there were zero non-`.py` files
under `reel_harness/` before this, so this is not implicit. Verify after
any packaging change: `uv build`, then check the wheel's file listing
actually includes `templates/`/`static/`, then a clean-venv install +
`python -m reel_harness.cli.main serve` + browser check (the same
discipline used for every release in this project). New runtime
dependencies: `jinja2`, `python-multipart` (both in `[project].dependencies`,
always installed). Browser E2E tests
(`tests/e2e/test_web_ui_playwright.py`) need the separate `e2e-browser`
extra plus `playwright install chromium` — never required for normal use
or the rest of the test suite.

**Scope**: Phase 5A covers Demo-Mode generation end to end (create → watch
progress → review → approve/reject/retry/cancel → download). Real-platform
publishing now has its own web UI too — see "Web UI — Publishing (Phase 5B)"
below.

## Web UI — Publishing (Phase 5B)

Extends the Phase 5A web UI with real-platform publishing: connecting a
YouTube/TikTok/Instagram account, creating a publication from a completed
job, and watching it through upload/processing to a published video — all
by clicking, no CLI required after `serve` starts. Nothing here changes
the underlying publish backend (`PublicationService`, `core.publish_retry`/
`publish_reconciliation`, the publisher worker) — every action route calls
the exact same in-process functions the CLI/`/v1/publications/*` API
already used, just reached from a browser instead.

**Routes**: `/publisher-accounts` (connected-accounts status),
`POST /publisher-accounts/{provider}/connect`,
`GET /publisher-accounts/{provider}/callback`,
`POST /publisher-accounts/{provider}/disconnect`,
`GET /jobs/{id}/publish` (publish setup, only reachable when the job is
COMPLETED and currently eligible), `POST /jobs/{id}/publications`
(create), `GET /publications` (list), `GET /publications/{id}` (detail),
`GET /publications/{id}/status` (HTMX polling fragment), and
`POST /publications/{id}/{cancel,retry,refresh,reconcile}`. None of this
overlaps `/v1/*` (the JSON API keeps its own `/v1/jobs/{id}/publications`
and `/v1/publications/{id}/...` routes, unchanged) — the web UI is purely
additive, the same discipline Phase 5A established for jobs.

**OAuth connect/callback — a genuinely new mechanism, not just a new
page**: until now the only working OAuth flow was `publisher-auth`'s
CLI-driven loopback listener, which holds the terminal process open while
it waits for exactly one redirect. A browser can't do that, so `/connect`
generates a PKCE challenge + a single-use `state` token and stores them in
a new `publisher.oauth_flow_store.OAuthFlowStore` — file-backed (same
repository-external secret directory as credentials, a separate
namespace), never a cookie, never the DB — then 303-redirects the browser
straight to the provider's real authorization page. `/callback` reads it
back by `state`, exchanges the code (mirroring each
`_cmd_publisher_auth_{provider}` CLI flow's exact token-exchange sequence:
YouTube/TikTok single-step, Instagram's three-step short-lived →
long-lived → account-identity), saves the credential, and redirects to
`/publisher-accounts`.

**`/callback` deliberately has no CSRF dependency** — stated explicitly so
it never gets "fixed" into a bug later. It's reached by a genuine
cross-site top-level navigation from the OAuth provider's own domain; the
`rh_csrf` cookie is `SameSite=Strict`, so the browser would never send it
there regardless of what the route required. The single-use, short-TTL,
provider-bound `state` parameter is the standard, correct CSRF-equivalent
defense for an OAuth callback. `/connect` and `/disconnect`, by contrast,
use the same `require_csrf` dependency as every other mutating web route.

**Redirect_uri: YouTube vs. TikTok/Instagram, deliberately asymmetric**.
YouTube's redirect_uri is computed per-request via `request.url_for(...)`
— Google's Desktop-app OAuth client type tolerates any port on
`127.0.0.1` (RFC 8252), the same property the CLI's own ephemeral-port
loopback listener already relies on, so no new setting was needed.
TikTok/Instagram reuse their existing `REEL_HARNESS_TIKTOK_REDIRECT_URI`/
`REEL_HARNESS_INSTAGRAM_REDIRECT_URI` settings verbatim, unchanged, since
those platforms require an exact pre-registered match and those settings
already existed for the CLI's flow. **To use the web connect flow for
TikTok or Instagram, register the exact URL
`http://127.0.0.1:8000/publisher-accounts/{tiktok,instagram}/callback`**
(adjust host/port to match wherever `serve` actually runs) in that
platform's developer console — this is the same
`http://127.0.0.1:PORT`-as-redirect_uri path `_cmd_publisher_auth_tiktok`'s
own docstring already documents as supported (at the operator's own risk),
just pointed at the web route's exact path instead of a CLI loopback port.
Registering the web callback URL supersedes whatever was registered for
the CLI's loopback/paste-back flow for that provider, unless the
platform's console supports registering more than one redirect URI (varies
by platform).

**Disconnect is local-only**, exactly like `publisher-account-remove`:
it deletes the saved credential from this machine's file store and never
revokes remote authorization at the platform. Stated in the UI itself, not
just here.

**Publish-setup form**: each configured-and-connected platform gets its
own form (disabled with the exact reason when not selectable — an
unconfigured OAuth client or zero connected accounts — rather than
omitted, so the CSRF token stays present on the page either way). Privacy/
visibility choices and their labels come from `provider_capabilities()`,
never hardcoded per-provider strings. Platform-specific options
(TikTok/Instagram) are shown read-only from `default_platform_options()`
— `PublicationService.create_publication` has no parameter to accept a
custom per-publication override yet, so the form surfaces the actual
most-restrictive defaults the worker will apply rather than pretending to
let the user customize something the backend can't persist. Any
`privacy_status` in `public_privacy_values` is only selectable at all when
`REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true`; Instagram's only privacy value
(`PUBLIC`) means Instagram is entirely unavailable as a target when that
flag is off. TikTok's unaudited-app SELF_ONLY restriction and Instagram's
always-public model are both called out inline as static warnings — the
live per-account restriction is only knowable via a real
`get_creator_info()` call, which this form never makes automatically (see
below); the real enforcement happens server-side in `create_publication`,
and a resulting `PublisherAppReviewRequiredError` is caught and
re-rendered as a friendly inline error, never a raw exception.

**No automatic real network call, anywhere in this UI.** Publish-setup,
publication list/detail, and status polling are all local-only (DB + file-
store reads). The one exception is the OAuth exchange itself (inherent to
connecting an account) — there is no "check account status now" button in
this phase; use `reel-harness publisher-doctor --check-remote` or
`live-verify` from the CLI for an opt-in real readiness probe.

**Status polling and actions**: `fragments/publication_status.html`
copies the job status fragment's exact self-terminating HTMX pattern —
`hx-get`/`hx-trigger`/`hx-swap` are omitted once the publication is
terminal (`PUBLISHED`/`CANCELLED`) or needs a human action
(`FAILED`/`AUTH_REQUIRED`/`QUOTA_BLOCKED`/`RETRY_WAIT`/`REVIEW_REQUIRED`
— derived from `core.publish_retry`'s real retryable-status set, not
guessed). Cancel/retry/refresh each call the identical
`cancel_publication`/`retry_publication`/lease+`run_publication` logic the
`/v1/publications/*` API routes already used; reconcile's outcome is
threaded into the re-rendered fragment so a click shows what was actually
found (e.g. `ambiguous_remote_state`, `app_review_required`), never a
silent no-op.

**A real pre-existing bug found and fixed while building this**:
`PublicationService.cancel_publication` assumed every non-terminal status
could transition straight to `CANCELLED`, but the state machine
deliberately only allows `FAILED` → `RETRY_WAIT` (see
`test_failed_allows_only_manual_retry_wait`) — calling cancel on a
`FAILED` publication crashed with a raw `InvalidTransitionError` instead
of a clean 409. `FAILED` is now refused explicitly, alongside
`PUBLISHED`/`CANCELLED`, with a message pointing at retry as the
alternative. This predates Phase 5B; it was only ever reachable via
`/v1/publications/{id}/cancel`, but no test had exercised a FAILED
publication's cancel path until this session's `can_cancel`-mirroring
test caught it.

**Job Detail** gained a "게시" section: a publish button when the job is
COMPLETED and currently eligible, and a list of the job's existing
publications (there can be more than one — one per platform) either way.

**Verification**: unit tests for every new view model's `can_*` mirroring
the real service precondition (not a transition-table guess), form
validation, and label coverage; route tests (`TestClient`) for CSRF
gating, every action's success + precondition-violation case, and the
OAuth connect/callback/disconnect flow (state single-use, expired/
missing/mismatched state, provider error handling — all with an injected
fake `*OAuthClient`, no real network call); an integration test driving a
full publish lifecycle purely through the web routes with the `fake`
publisher provider (needs no OAuth account) across two real worker-drive
cycles to reach `PUBLISHED`; a real-Chromium Playwright scenario
confirming the actual rendered publish-setup page and its navigation to
`/publisher-accounts`. Full suite green, mypy/ruff clean.

## Health and readiness

- `GET /healthz` — shallow liveness.
- `GET /readyz` — deep local checks: DB reachable, schema version supported,
  storage root writable, provider configuration valid (checked locally, no
  provider network call), ffmpeg/ffprobe resolved. 503 + named checks when
  not ready. No secrets in responses.
- `GET /status` — version, config fingerprint, schema version, process
  uptime, job/publication status-count breakdowns, queue depth, stale-lease
  counts, and (only inside `reel-harness serve`) live per-component status
  and fatal errors. No API key required, same as `/healthz`/`/readyz`.
- `GET /metrics` — dependency-free Prometheus text exposition. See
  "Operational metrics" below.

## Production operations (Phase 4A)

This section covers the release-candidate operations surface: readiness
diagnostics, database/storage backup and verification, the unified runtime
supervisor, metrics/incident tooling, live cross-platform verification, and
the release process itself. Everything here is local-first and requires no
new external services — a Prometheus endpoint is exposed, but nothing
pushes to one.

### Preflight — one command before running for real

```
uv run reel-harness preflight [--profile fake|production] [--check-remote]
    [--provider llm|tts|asset ...] [--publisher youtube|tiktok|instagram ...] [--json]
```

Local-only by default: config, DB connectivity/schema, storage
root/permissions/free space, credential and journal directory safety
(rejects a repo-internal path or a symlink/junction, same guarantee as
`publisher-auth`), ffmpeg/ffprobe, runtime Python dependencies, the
provider/publisher registry, worker lease/heartbeat sanity (heartbeat must
stay under ⅓ of the lease timeout), upload chunk-size validity, the
public-upload feature flag (WARNs if enabled with no publisher configured
to use it), API-key strength, and known-placeholder secret detection.

`--profile production` escalates a fixed set of these from WARN to FAIL —
a placeholder API key/secret, a repo-internal credential path, an
unwritable storage root, an unsupported schema version, an unsafe
heartbeat/lease ratio, a public-upload flag with nothing configured to use
it, or risky credential-file permissions. `--profile fake` (the default) is
the permissive local-dev bar. `--check-remote` adds real, read-only
publisher account checks (reusing the same token-refresh/identity calls
`publisher-doctor` makes) — the only case this command ever touches a
network. Exit codes mirror `provider-smoke`: 0 (PASS/WARN), 1 (FAIL), 2
(NOT_CONFIGURED).

### Config fingerprint

Every process logs a `startup_config_fingerprint` JSON event at boot: a
deterministic, non-secret snapshot (provider ids/models/hosts, worker/
publisher policy settings, publisher registry) — never an API key, OAuth
token, or signed URL. The same fingerprint (and its short hash) is reused
by `/status`, the release manifest, incident bundles, and live-verify
records, so an operator can always tie a diagnostic artifact back to
exactly how the process that produced it was configured. Settings changed
after a job/publication was created never retroactively change that job's
own already-persisted snapshot.

### Database backends (Phase 6A-1)

SQLite is the zero-config default (`DATABASE_URL=sqlite:///./reel_harness.db`)
and nothing about the local/Demo Mode experience requires anything else.
PostgreSQL is a fully-supported second backend:

```
uv sync --extra postgres        # psycopg v3 driver -- never a hard dependency
DATABASE_URL=postgresql://user:pass@host:5432/reel_harness uv run reel-harness serve
```

- A bare `postgresql://...` URL (the shape most managed-Postgres providers
  hand out) is normalized to `postgresql+psycopg://` automatically; a URL
  that already names an explicit driver is left alone. Unsupported schemes
  are rejected at startup with a clear error (`config.validate_provider_settings`).
- PostgreSQL engines get a real bounded connection pool with a pre-ping
  health check. Tunables (all ignored on SQLite, which has no connection
  pool or server-side statement timeout of its own):
  `REEL_HARNESS_DB_POOL_SIZE` (default 5),
  `REEL_HARNESS_DB_POOL_MAX_OVERFLOW` (default 10),
  `REEL_HARNESS_DB_STATEMENT_TIMEOUT_SECONDS` (default unset — no timeout).
- The additive-column migration mechanism, `db-status`, and `db-verify` are
  dialect-portable (SQLAlchemy introspection + per-dialect DDL rendering,
  never hand-written SQLite type strings). PostgreSQL backup/restore
  requires the `pg_dump`/`pg_restore` client tools on `PATH` (installed
  separately — they ship with any PostgreSQL client package).
- CI runs the repository-level dual-backend suite
  (`tests/integration/test_postgres_backend_parity.py`) against a real
  `postgres:16` service container on every push, including a real
  concurrent lease-claim race. To run it locally, point
  `REEL_HARNESS_TEST_POSTGRES_URL` at a disposable database (its tables
  are **dropped and recreated** by the fixtures — never a database you
  care about); unset, those tests skip cleanly.
- **Not included in 6A-1, deliberately**: a SQLite→PostgreSQL data-transfer
  command (`db-transfer`). Moving existing local data into PostgreSQL
  depends on the multi-user ownership model (Phase 6A-2) — who owns the
  migrated rows must be decided explicitly, not defaulted silently — so
  the transfer tool lands there, not here. Until then, PostgreSQL is for
  fresh databases.

### Database operations

```
uv run reel-harness db-status
uv run reel-harness db-migrate [--dry-run] [--backup-dir DIR | --no-backup]
uv run reel-harness db-backup --dest-dir DIR
uv run reel-harness db-restore <backup_path> --confirm-restore --pre-restore-backup-dir DIR
uv run reel-harness db-verify
```

All five commands work against whichever backend `DATABASE_URL` names.

- **`db-status`**: current vs. latest schema version, pending column/version
  migrations, table row counts, an integrity status, a safe db identifier
  (SQLite: filename only, never the full path; PostgreSQL: bare database
  name, never host/user/password).
- **`db-migrate`**: wraps the existing idempotent `init_db()` — a default
  pre-migration safety backup (`--backup-dir` required unless you pass
  `--no-backup` explicitly), an exclusive lock so two invocations can't
  interleave (SQLite: a PID lockfile next to the DB file; PostgreSQL: a
  native `pg_try_advisory_lock`, which the server releases automatically
  if the process crashes mid-migration), `--dry-run` to report the plan
  without touching anything, and a safe no-op on repeated runs.
- **`db-backup`**: SQLite uses its own online backup API (safe to run while
  the DB is in use); PostgreSQL uses `pg_dump --format=custom`. Both are
  written atomically with the same checksummed JSON manifest alongside.
  The manifest's `schema_version` is the database's own **actual** version
  at backup time — never assumed to match the running code's version, so a
  backup of a not-yet-migrated database honestly records that fact.
- **`db-restore`**: destructive, so every check before the final apply can
  refuse and leave the live database untouched — explicit
  `--confirm-restore`, refuses while any lease looks actively held by a
  running worker (heartbeat within the lease timeout), verifies the
  backup's checksum against its own manifest, refuses a backup from a
  schema newer than this build supports, and always takes its own backup
  of the current database first (`--pre-restore-backup-dir`). The apply
  step is the one backend-specific part: SQLite swaps the file atomically;
  PostgreSQL runs `pg_restore --clean --if-exists --no-owner` against the
  live database.
- **`db-verify`**: integrity check, foreign-key check, orphan publications
  (no matching job), and the forbidden ACTIVE+unlocked state — reusing the
  exact same detectors the worker daemons themselves use. The first two
  are SQLite-specific by nature (`PRAGMA integrity_check` /
  `PRAGMA foreign_key_check`): PostgreSQL has no client-reachable
  whole-file integrity scan (server-side WAL + checksums own that
  concern), and it enforces foreign keys synchronously on every write, so
  a committed violation cannot exist — on PostgreSQL those two checks
  report clean by construction and the row-level checks do the real work.
  Cross-checking the DB against what's actually on disk is `storage-verify`'s
  job, not duplicated here.

### Storage verification and backup bundles

```
uv run reel-harness storage-verify [--repair-safe]
uv run reel-harness backup-create --dest-path PATH
uv run reel-harness backup-inspect <bundle_path>
uv run reel-harness backup-restore <bundle_path> --confirm-restore
```

`storage-verify` walks every job directory and cross-checks it against the
DB: asset/final-video checksum verification, `manifest.json` validation,
unsafe symlink/junction detection, orphan directories with no matching Job
row, and leaked temp files from an interrupted write. Read-only by default;
`--repair-safe` deletes **only** stale (>1h) temp files matching
`storage.local`'s own known scratch-file naming convention — it never
touches `final.mp4`, rewrites a manifest, changes a Publication's status,
or retries a remote upload.

`backup-create` produces a single portable `tar.gz` of the SQLite database,
the jobs storage tree, and the durable publish journal — **never** OAuth
tokens, API keys, `.env`, the rest of the credential backend, ffmpeg
binaries, caches, or logs (see "Credential backup policy" below). Every
archived file is content-checksummed. `backup-restore` validates every
archive member **before** extracting anything (absolute paths, `..`
traversal, symlinks/hardlinks, and a per-file/total size cap are all
refused outright), extracts to a private scratch directory first, verifies
checksums, and only then moves data into the real destinations — a
corrupt or malicious bundle never partially overwrites live data.

**Credential backup policy**: OAuth credentials and the client secret are
deliberately never included in a `backup-create` bundle. Back them up (if
at all) by copying `REEL_HARNESS_CREDENTIAL_DIR` directly through your own
OS-level backup tooling, with the same care you'd give any other secret
store — this project does not provide a credential-bundling command, to
avoid ever making it easy to accidentally ship a token inside a file meant
to be shared or archived long-term.

### Runtime supervisor

```
uv run reel-harness serve [--no-api] [--no-render-worker] [--no-publisher-worker]
    [--host HOST] [--port PORT] [--render-workers N] [--publisher-workers N]
    [--shutdown-timeout SEC]
```

Runs the API, render-worker daemon(s), and publisher-worker daemon(s)
together in one process, as threads sharing a single `AppContext` (the API
never constructs a second one). Threads, not subprocesses: the work here is
I/O-bound (DB, HTTP, an ffmpeg subprocess that already runs outside the
GIL), and every worker already assumes concurrent DB access via the
existing lease-fencing mechanism — separate processes would add real
coordination complexity for no benefit on a single machine. `--render-workers`/
`--publisher-workers` > 1 spins up that many daemon threads, each with a
distinct worker id; since SQLite is single-writer, keep this low (a soft
guidance warning is logged past a small threshold, never enforced).

**Failure policy**: the API server dying is fatal to the whole supervisor
(new requests can never be served without it) — other components are
signaled to stop and the process exits non-zero. A render-worker thread
dying leaves the publisher worker running (and vice versa); either dying
is tracked without tearing down everything else, since the daemons already
isolate ordinary per-job/per-publication failures themselves — check
`/status`'s `supervisor.fatal_errors` field. Graceful shutdown on SIGINT/
SIGTERM/SIGBREAK/Ctrl+Break signals every component to stop, then joins
each with a bounded `--shutdown-timeout`, logging (never hanging
indefinitely) if one doesn't finish in time.

### Operational metrics

`GET /metrics` — dependency-free Prometheus text exposition (no
`prometheus_client` package): `jobs_created/completed/failed_total`,
`active_jobs`, `queue_depth`, `retries_total`,
`stage_duration_seconds_count`/`_sum`, `publications_created/published/
failed_total`, `upload_bytes_total`, `worker_lease_lost_total`,
`stale_recoveries_total`, `provider_errors_total`, `publisher_retries_total`.
Every value is **derived fresh from current DB state at scrape time**,
never an in-memory counter — an in-memory counter would silently reset to
zero on every process restart, exactly the kind of gap a metrics system
exists to catch. No job topic/title/script text is ever a metric value or
label. No API key required.

### Incident bundles

```
uv run reel-harness incident-bundle --dest-path PATH
```

A zip archive for offline incident analysis: app/schema version, config
fingerprint, a full local preflight report, DB status, job/publication
status breakdowns, recent failure codes, publish-journal integrity (per
publication: raw line count vs. integrity-verified event count, flagging
any gap), and dependency/platform versions. Never a token, API key,
credential path, full script/prompt text, a signed URL, or media bytes.
The fully assembled report is independently secret-scanned (reusing the
same redaction rules and every registered secret) before being written —
refuses to write rather than ship a bundle containing anything
secret-shaped that slipped through. Atomic.

### Live verification across platforms

```
uv run reel-harness live-verify [--youtube] [--tiktok] [--instagram]
    [--account ALIAS] [--upload-tests]
    [--confirm-youtube-private] [--confirm-tiktok-restricted] [--confirm-instagram-public] [--json]
```

A single command sweeping live account state across all three publishers
(all three by default). Read-only unless `--upload-tests` is given, and
even then a platform only runs its real upload test if its **own**
specific confirm flag is also present — Instagram (no private-post option)
deliberately requires the strongest gate. A provider with no saved
credential is reported `NOT_CONFIGURED` and the sweep continues to the
next platform. Every run (read-only and upload-test) is appended to an
append-only live-verification log, rooted alongside the publish journal
and distinct from `Publication`/`PublicationAuditEvent` — a diagnostic
verification record, not a real publish attempt.

### Release process

```
uv run reel-harness release-manifest --dest-path PATH [--wheel-path P] [--sdist-path P]
    [--lock-path P] [--test-summary-json P] [--live-verification-status STATUS]
uv run reel-harness release-check [--skip-slow] [--json]
```

`release-manifest` records version, git commit, build timestamp, supported
Python versions/platforms/providers, schema version, dependency-lock/
wheel/sdist checksums (`null` for an artifact that wasn't built — never a
guess), a fixed known-limitations list shared with `CHANGELOG.md`, and
`live_verification` — explicitly `"not_run"` by default, never silently
omitted.

`release-check` is the pre-tag gate: git working-tree cleanliness, current
branch, sync status against `origin` (a real `git fetch`), `pyproject.toml`/
`__version__` consistency, `uv.lock` freshness, the full pytest suite,
mypy, ruff, a secret/token grep (excluding `tests/`, where redaction tests
deliberately embed fake secret-shaped fixtures), and a tracked-artifact
check. Never creates a commit or tag itself — only reports a verdict.
`--skip-slow` omits the full pytest/mypy/ruff run for a fast iterative
check; the real pre-tag gate must always run without it.

Versioning is single-sourced from `reel_harness._version.__version__`
(PEP 440, e.g. `0.1.0rc1`), read by `reel-harness --version`,
`pyproject.toml` (kept in sync, verified by `release-check`), `/status`,
and the release manifest.

## Not yet supported (Phase 4A scope ends here)

YouTube, TikTok, and Instagram Reels publishing all exist (see the three
"Publishing (...)" sections above), sharing production-reliability
features: `publisher-doctor`, account management, durable crash-recovery
reconciliation, manual retry, and a processing poller. Production
operations exist (preflight, DB/storage backup and verification, the
`serve` supervisor, metrics, incident bundles, live-verify, and the
release-manifest/release-check/tagging process). A web UI exists too
(Phase 5A generation + Phase 5B publishing, including an OAuth
account-connect UI — see "Web UI — Publishing (Phase 5B)" above); this
list predates both. Facebook Reels publishing does not exist. Also not
yet supported: automatic public publishing (public always requires the
explicit double-confirmation + feature flag above), scheduled-publish
automation, automatic remote video/post delete, thumbnail/subtitle
upload, analytics collection, SQLite→PostgreSQL data transfer (PostgreSQL
itself is supported as of Phase 6A-1 — see "Database backends" above; the
transfer tool is deferred to Phase 6A-2), cloud storage/CDN (beyond
Instagram's own resumable upload), a credential-bundling backup command
(see "Credential backup policy" above), face-recognition smart crop, BGM
mixing, subtitle burn-in, multi-language dubbing.

## Troubleshooting

| Symptom | Likely cause | Action |
|---|---|---|
| `provider configuration error: ...` at startup | real provider selected, config incomplete | set the listed `REEL_HARNESS_LLM_*`/`REEL_HARNESS_TTS_*`/`REEL_HARNESS_ASSET_*` vars or switch back to `fake` |
| job FAILED `PROVIDER_NOT_CONFIGURED` | environment no longer matches the job's pinned snapshot | restore config (same endpoint host, same TTS voice/model, same asset provider) and `job-retry` |
| TTS stage fails audio validation | provider returned empty/corrupt/wrong-codec audio | check `provider-smoke tts` output; not retried automatically if malformed |
| ASSET stage fails `ASSET_NOT_FOUND` | nothing eligible survived search + the full relaxation ladder | broaden `REEL_HARNESS_ASSET_MIN_WIDTH`/`_MIN_HEIGHT`/duration bounds, or check the scene's `visual_query` isn't too narrow |
| ASSET stage fails media validation | provider returned empty/corrupt/audio-only video, or an oversized/redirect-looping download | check `provider-smoke asset` output; not retried automatically if malformed |
| job stuck `REVIEW_REQUIRED`, cancel had no effect (pre-Phase 2C) | old build without the immediate-cancel fix | upgrade; `job-cancel` now transitions unleased idle states to `CANCELLED` directly |
| job FAILED `BLOCKED_DEPENDENCY` | ffmpeg/ffprobe not resolvable | `reel-harness doctor`; provision `.tools/ffmpeg/bin/` |
| job FAILED `MISSING_PREREQUISITE` | resume artifacts missing/corrupt | `job-retry --stage` the stage that owns the artifact (message names it) |
| job stuck `RETRY_WAIT` with old `next_retry_at` | no worker running | start `worker-run` |
| worker exits 1 immediately | DB/storage/schema unusable | check `GET /readyz` / `doctor`, fix, restart |
| `/readyz` 503 `schema: unsupported version` | DB from a newer schema | upgrade the code or use a matching DB |
| `publish-job --dry-run` reports `eligible: false` | job/manifest/asset state doesn't pass `core.publish_eligibility` | check `eligibility_reasons` in the JSON output — each is a specific code (e.g. `APPROVAL_MISSING`, `FASTSTART_MISSING`, `ASSET_LICENSE_NOT_PUBLISHABLE`) |
| `publish-job` / `provider-smoke publisher youtube` print `NOT RUN — credentials not configured` | OAuth client and/or a saved credential missing | set `REEL_HARNESS_YOUTUBE_CLIENT_ID`/`_CLIENT_SECRET`, then `publisher-auth youtube` |
| `SecretStoreError: credential directory ... is inside the repository` | `REEL_HARNESS_CREDENTIAL_DIR` resolves under the repo checkout | point it outside the repository — credentials must never be `git add`-able |
| `SecretStoreError: ... is a symlink or junction/reparse point` | something (accidentally or otherwise) replaced a namespace directory or credential file with a symlink/NTFS junction | investigate before removing it; this is a security check working as intended, not a bug |
| publication stuck `AUTH_REQUIRED` | saved refresh token invalid/revoked (`publisher-doctor`/`publisher-account-show` will show `invalid: true`) | re-run `publisher-auth youtube --account ALIAS`, then `publication-retry <id>` |
| publication stuck `QUOTA_BLOCKED` | provider quota exhausted | wait for the provider's quota reset, then `publication-retry <id>` |
| publication stuck `PROCESSING` | the processing poller (`--process-status`, on by default) is pacing polls via `next_poll_at` | usually resolves on its own; `publication-refresh <id>` re-polls immediately if you don't want to wait |
| publication `FAILED` with `PROCESSING_TIMEOUT` | processing exceeded the local max-duration timeout (`REEL_HARNESS_PUBLISHER_PROCESSING_MAX_DURATION`) without the provider ever reporting done | this is a LOCAL timeout, not a provider failure — the video may still finish on YouTube's side; check `publication-reconcile <id>` before assuming it's actually gone |
| `publication-retry` refuses with "still active" | the publication is in an ACTIVE-looking status (`UPLOADING`/`PROCESSING`/etc.) | run `publication-reconcile <id>` first to confirm its real state, then retry if appropriate |
| `publication-reconcile` reports `ambiguous_remote_state` | the provider says the upload session is complete but nothing local (durable journal, DB) can confirm which video that produced | check the channel's own recent uploads (YouTube Studio) manually before deciding whether to retry — reconciliation deliberately never guesses here |
| `publish-job --privacy public` refused | missing one of the four required public-upload conditions | add `--confirm-public-upload`, ensure the job is approved, and set `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true` |
| `publish-job --provider instagram` refused | Instagram requires `--confirm-public-upload` AND `--confirm-platform-options` every time (no private option exists) | add both flags and set `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true` |
| `publisher-doctor instagram --check-remote` reports `BUSINESS_ACCOUNT_REQUIRED`/`PAGE_CONNECTION_REQUIRED` | the connected Instagram account isn't a Business/Creator account eligible for Reels publishing | convert the account in the Instagram app, then re-run `publisher-auth instagram` |
| `provider-smoke publisher instagram --upload-public-test` prints `NOT RUN — application permission not available` | the app hasn't been granted `instagram_business_content_publish`, or the publishing limit is exhausted | check Meta App Review status / wait for the rolling 24h publishing-limit window to reset |
| `preflight` reports `repo_internal_credential: FAIL` | `REEL_HARNESS_CREDENTIAL_DIR` resolves under the current working directory | point it at a directory genuinely outside the repository |
| `preflight --profile production` fails on `api_authentication`/`secret_placeholder` | the default/placeholder value is still set | set a real, long `APP_API_KEY` and real provider secrets before running in production |
| `db-migrate` refuses with a lock error | another `db-migrate` is running, or a previous run crashed without cleaning up | wait for the other run, or delete the `.migrate.lock` file next to the DB once you've confirmed no migration is actually in progress |
| `db-restore` refuses with "running worker" | a job/publication lease is still within the lease timeout | stop all workers (`serve`/`worker-run`/`publisher-run`) first, then retry |
| `db-restore`/`backup-restore` refuses with a checksum mismatch | the backup/bundle file was truncated or edited after creation | re-create the backup/bundle from source; never hand-edit a backup file |
| `backup-restore` refuses with "path traversal"/"absolute path"/"symlink" | the archive is corrupt or was tampered with | do not extract it manually either — treat it as untrusted and discard it |
| `serve` exits immediately with a fatal API error | the configured `--port` is already in use, or the API failed to bind for another reason | check `--port`, or run with `--no-api` if only the workers are needed |
| `release-check` reports `lockfile: FAIL` | `uv.lock` is stale relative to `pyproject.toml` (e.g. after a version bump) | run `uv lock` and commit the updated lockfile |
| `release-check` reports `secret_scan: FAIL` outside of `tests/` | a real-looking API key/token literal is in tracked source | remove it and use environment variables / the credential backend instead |
