# Reel Harness — Publishing (Phase 3A: YouTube)

Design/implementation reference for the Publisher subsystem. Operational
usage (CLI commands, config, troubleshooting) lives in `docs/OPERATIONS.md`;
this file exists to record the official API research this subsystem is built
against, per the project rule that provider adapters must be built from
current official documentation, not blog posts or memory.

## Official documentation consulted (checked 2026-07-28)

All of the following were fetched directly from `developers.google.com` on
2026-07-28 for this implementation. If YouTube's API changes after this
date, re-check these pages before modifying `providers/youtube_publisher.py`.

- Resumable upload guide:
  https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol
- `videos.insert` reference: https://developers.google.com/youtube/v3/docs/videos/insert
- Video resource schema: https://developers.google.com/youtube/v3/docs/videos
- Error response format: https://developers.google.com/youtube/v3/docs/errors
- OAuth 2.0 for installed/desktop apps: https://developers.google.com/identity/protocols/oauth2/native-app

### Resumable upload protocol

1. **Session creation**: `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status`
   with `X-Upload-Content-Length` (total bytes) and `X-Upload-Content-Type`
   (`video/*` or `application/octet-stream`) headers, and the video resource
   (snippet/status JSON) as the body. On success (`200`), the session URI is
   returned in the `Location` response header.
2. **Chunk upload**: `PUT` to the session URI with `Content-Range: bytes
   START-END/TOTAL` (0-based, inclusive). Chunk sizes **must be a multiple of
   262144 bytes (256 KiB)** except the final chunk, which may be any
   remaining size.
3. **Resuming after an interruption**: `PUT` to the session URI with
   `Content-Range: bytes */TOTAL` and `Content-Length: 0` (empty body). A
   `308 Resume Incomplete` response's `Range` header (`bytes=0-N`) reports
   the last byte the server has durably received; resume the next chunk at
   `N+1`.
4. **Completion**: `200`/`201` with the created video resource (including
   `id`) in the body.
5. **Retry policy**: `500`/`502`/`503`/`504` are retryable with exponential
   backoff (mirrors the LLM/TTS/asset adapters' existing pattern); any other
   4xx is a permanent failure of that session.

### OAuth 2.0 (installed-app / loopback flow, per the native-app guide)

- Authorization endpoint: `https://accounts.google.com/o/oauth2/v2/auth`
- Token endpoint: `https://oauth2.googleapis.com/token`
- Redirect URI: loopback only — `http://127.0.0.1:{port}` (Google's guide
  also allows `http://[::1]:{port}`; this project uses IPv4 loopback only).
- PKCE (`code_challenge`/`code_challenge_method=S256`) is Google's
  recommended hardening for the installed-app flow — used here even though
  not strictly mandatory, alongside a random `state` value for CSRF
  protection.
- `access_type=offline` is required to receive a `refresh_token` on first
  consent; `prompt=consent` is used so a refresh token is issued even on a
  re-authorization.
- Token exchange/refresh both POST to the token endpoint
  (`grant_type=authorization_code` with `code`+`code_verifier`, or
  `grant_type=refresh_token` with `refresh_token`).

### OAuth scopes used

`https://www.googleapis.com/auth/youtube.upload` (upload permission) plus
`https://www.googleapis.com/auth/youtube.readonly` (channel identity checks
for `provider-smoke publisher youtube` and OAuth account verification) —
the minimal pair for this adapter's actual operations, not the broader
`youtube` or `youtubepartner` scopes `videos.insert` also accepts.

### Video resource limits and fields actually enforced

- `title`: max 100 characters, must not contain `<` or `>`.
- `description`: max 5000 **bytes** (UTF-8), must not contain `<` or `>`.
- `tags`: max 500 characters total across all tags (commas/quotes count).
- `status.privacyStatus`: `private` | `public` | `unlisted` (this adapter
  defaults to `private` — see docs/OPERATIONS.md's public-upload safeguard).
- `status.selfDeclaredMadeForKids`: boolean, required by YouTube's
  COPPA-driven policy on every upload. Reel Harness requires this be set
  explicitly via config (`REEL_HARNESS_YOUTUBE_MADE_FOR_KIDS`, default
  `false`) rather than silently omitted.
- `status.embeddable`, `status.publicStatsViewable`: booleans, optional.
- Max file size: 256 GB; accepted content types `video/*` /
  `application/octet-stream`.

### Processing status (post-upload)

- `status.uploadStatus`: `uploaded` | `processed` | `failed` | `rejected` |
  `deleted`, with `failureReason` (`codec`/`conversion`/`emptyFile`/
  `invalidFile`/`tooSmall`/`uploadAborted`) or `rejectionReason`
  (`claim`/`copyright`/`duplicate`/`inappropriate`/`legal`/`length`/
  `termsOfUse`/`trademark`/`uploaderAccountClosed`/
  `uploaderAccountSuspended`) populated on failure/rejection.
- `processingDetails.processingStatus`: `processing` | `succeeded` |
  `failed` | `terminated`, with `processingFailureReason` (`other`/
  `streamingFailed`/`transcodeFailed`/`uploadFailed`) on failure.

### Error response shape

```json
{"error": {"code": 403, "message": "...", "errors": [
  {"domain": "youtube.api", "reason": "quotaExceeded", "message": "..."}
]}}
```

Reasons this adapter classifies: `quotaExceeded` (403),
`uploadLimitExceeded` (400), `forbidden` (403), `insufficientPermissions`
(403), `rateLimitExceeded` (429), `mediaBodyRequired` (400),
`invalidVideoMetadata` (400). A bare `401` (expired/invalid bearer token,
not covered by the reason table above) is treated as an auth failure
requiring a token refresh, not a permanent error.

### Quota

Per third-party reporting on a late-2025/2026 Google change, `videos.insert`
now costs roughly 100 units and uploads bill against a separate ~100-call/
day bucket rather than the shared 10,000-unit pool — this is **not**
independently confirmed against a primary Google source in this session
(no official quota-cost page was found with a specific per-method table)
and the account's actual current quota should always be checked in Google
Cloud Console rather than assumed from this document. The adapter does not
hardcode a quota assumption; it classifies `quotaExceeded` from the live
error response instead.

## Design decisions this research drove

- **`httpx` only, no Google client SDK**: the resumable upload protocol
  above is a plain REST/HTTP contract with no YouTube-specific transport
  requirements, so it's implemented the same way as the LLM/TTS/asset
  adapters — no new runtime dependency.
- **Chunk size**: the adapter's default chunk size is a multiple of 262144
  bytes (see `REEL_HARNESS_YOUTUBE_CHUNK_SIZE` in docs/OPERATIONS.md),
  enforced at config-validation time exactly like the 256 KiB protocol
  requirement above.
- **`privacyStatus` defaults to `private`** and the CLI requires two
  independent, explicit flags to upload `public` (see docs/OPERATIONS.md) —
  this is a Reel Harness safety policy layered on top of the API, not
  something the API itself requires.
- **`selfDeclaredMadeForKids` is always sent explicitly** (never omitted)
  because YouTube's own policy requires every upload to declare it.

## Phase 3B re-verification (checked 2026-07-28)

Re-fetched directly from `developers.google.com` before extending the
adapter for production reliability (reconciliation, retry, doctor). Findings
that changed or added to the Phase 3A research above:

- **Chunk-upload completion is `201 Created`**, not `200` — this adapter
  already accepted both (`response.status_code in (200, 201)` in
  `upload_chunk`), so no code change was needed, but it's now confirmed
  against the primary source rather than assumed.
- **Session expiry has no documented fixed TTL.** The guide states only
  that "each resumable session URI has a finite lifetime and eventually
  expires," surfaced as `404 Not Found` on the next request. The adapter
  already treats any `404` as `UploadSessionExpiredError` rather than
  assuming a specific lifetime — confirmed as the correct approach; there is
  no TTL value to add.
- **`videos.list` cannot list a channel's own uploads** — it only accepts
  `id`/`chart`/`myRating` filters (1 unit/call). Discovering an *unknown*
  video id for reconciliation therefore cannot use `videos.list` alone.
  Enumerating a channel's own recent uploads without `search.list`'s
  quota cost uses `channels.list(mine=true, part=contentDetails)` →
  `relatedPlaylists.uploads` (the auto-created uploads playlist id) →
  `playlistItems.list(playlistId=...)`. This is the mechanism
  `publication-reconcile`'s ambiguous-state fallback path uses (see below);
  it is a best-effort title/recency match, not an exact-identifier lookup,
  and is documented as such rather than treated as authoritative.
- **`status.publishAt`** (scheduled publishing) only applies when
  `privacyStatus=private` and only before a video has ever been published;
  a past timestamp publishes immediately. Confirmed but **not used** —
  scheduled publishing remains explicitly out of scope (see
  `docs/OPERATIONS.md`).
- **OAuth**: loopback redirect (`http://127.0.0.1:{port}`) is confirmed as
  the current recommended pattern for desktop apps (custom URI schemes are
  now explicitly documented as unsupported "due to the risk of app
  impersonation" — this project never used one). PKCE `S256` and `state`
  remain as implemented. Google's docs now also mention an optional DPoP
  (Demonstration of Proof-of-Possession) token-binding enhancement; this is
  optional, adds meaningful complexity (a bound signing key, nonce
  handling), and is **not implemented this phase** — recorded here as a
  possible future hardening, not a regression.
- **Programmatic token revocation** exists at
  `https://oauth2.googleapis.com/revoke` (`POST`, `token` param) and revokes
  every token issued under the project for that user, not just one
  account's. This is why `publisher-account-remove` only ever deletes the
  *local* saved credential — a project-wide remote revoke is a much larger
  blast radius than removing one local account alias, so it is exposed (if
  at all) as a separate, explicitly-confirmed action, never bundled into
  ordinary account removal.
- **Error reasons**: `videos.insert`'s documented 400 reasons expand the
  Phase 3A table with `invalidPublishAt`, `invalidPublishScheduleForVideo`,
  `invalidVideoGameRating`, `invalidRecordingDetails`,
  `defaultLanguageNotSet`; 403 adds `forbiddenLicenseSetting`,
  `forbiddenPrivacySetting`, `forbiddenEmbedSetting` (update-only). None of
  these are reachable by this adapter's metadata (no localizations,
  recording details, game rating, or license/embed changes are ever sent),
  so no new error classification branch was needed — recorded for
  completeness.
- **Retry-After on `308`**: one summarized source suggested a `Retry-After`
  header can appear on `308 Resume Incomplete` responses. This is **not**
  independently confirmed against the primary resumable-upload guide's own
  text in this session (a `308` is a normal "keep going from here" signal,
  not a rate-limit/backoff signal) — the adapter does not act on it, and
  this gap is recorded rather than guessed at.

Quota-cost specifics (see the Phase 3A quota note above) remain
unconfirmed against a primary per-method table and are still not
hardcoded anywhere in this codebase.

## Phase 3B: production reliability design

Phase 3A shipped a working upload/processing pipeline; Phase 3B closes the
gaps that matter once it runs unattended for real: what happens when a
worker process crashes at each of the riskiest moments, and how an operator
diagnoses and recovers from that without ever risking a duplicate upload.

### The core risk: a crash between provider success and DB commit

A chunk upload can succeed at YouTube (a `provider_video_id` is minted) in
the exact instant a worker process dies, before the DB transaction that
would record that fact ever commits. Naively resuming afterward risks
re-uploading the same video as a duplicate. Two mechanisms close this:

- **`publisher.journal.PublishJournal`**: an append-only, `fsync`'d,
  per-publication log. The `upload_completed` event is written the instant
  `upload_chunk`'s response reports `completed=True` — *before* any DB
  mutation (see `worker.publish_runner._upload_stage`). A crash immediately
  after a successful journal append still leaves the fact durably
  recoverable. Every record carries an integrity checksum (a corrupted or
  tampered line is skipped, never trusted) and never stores a token, the
  real upload session URI (only a one-way hash via
  `safe_session_reference_hash`), an Authorization header, or a full
  provider response body.
- **`core.publish_reconciliation.reconcile_publication`**: reads the
  journal back, and — critically — never trusts it blindly. A
  journal-recovered `provider_video_id` is always confirmed via a real,
  read-only `get_processing_status` call before the DB row is repaired.
  Eight possible outcomes (`already_consistent`, `recovered_remote_video`,
  `upload_incomplete`, `upload_session_expired`, `remote_video_missing`,
  `credentials_unavailable`, `manual_review_required`,
  `ambiguous_remote_state`); anything this function cannot positively
  confirm lands in one of the last two rather than ever guessing or
  auto-starting a new upload. `ambiguous_remote_state` is the genuinely
  irreducible case: the provider reports the session complete, but there is
  neither a journal record nor a known video id (e.g. the process died so
  early that even the journal write never happened, or a completion
  response was lost in transit after the provider had already committed
  server-side). The mechanism to resolve *that* residually via the official
  API — `channels.list(mine=true, part=contentDetails)` →
  `relatedPlaylists.uploads` → `playlistItems.list` — is documented above
  but deliberately not automated: it is a title/recency heuristic, not an
  exact-identifier lookup, and this project's policy is to surface it for a
  human to check rather than trust a heuristic match to repair state
  automatically.

### Metadata fingerprint

`pipeline.publish_metadata.metadata_fingerprint` is a deterministic hash
over (provider, account, job id, final video checksum, the exact metadata
that would be/was sent). It is stored on `Publication.metadata_fingerprint`
(schema v6) and re-checked before any `publication-retry` — a mismatch
refuses the retry and asks for a new publication instead. It is
deliberately **not** embedded in the video's own title/description: an
internal identifier in user-visible text has no upside and a real, if
small, downside (it leaks internal identifiers to anyone who reads the
description).

### Retry policy

`core.publish_retry.retry_publication` only ever repositions a publication
for the next worker cycle to actually resume it — it never uploads
anything itself. `AUTH_REQUIRED`/`QUOTA_BLOCKED` are allowed to retry
immediately on the operator's say-so rather than the function trying to
verify the credential/quota is actually fixed (neither can be confirmed
without a real network call, which retrying itself must not make); if it
isn't actually fixed, the next attempt just lands back in the same status.
An ACTIVE-looking status is refused with a pointer to
`publication-reconcile` first, never blindly retried.

### State-graph fixes found while building this

Two real gaps in the Phase 3A `PublicationStatus` transition graph were
found and fixed while implementing retry/reconciliation, both because a
resume-from-PROCESSING path didn't exist yet: `RETRY_WAIT` could not
resolve to `PROCESSING` (so a processing-only retry would fail validation
the moment a worker tried to apply it), and `PROCESSING` itself could not
transition to `AUTH_REQUIRED`/`QUOTA_BLOCKED`/`RETRY_WAIT` at all — meaning
*any* error while polling processing status, even a dropped connection,
previously landed straight in `FAILED` with no soft retry, unlike every
other stage. Both are fixed in `core.state_machine`.

### Processing poller

`Publication.next_poll_at`/`processing_started_at`/`processing_poll_count`
(schema v7) let the processing lane pace itself
(`REEL_HARNESS_PUBLISHER_PROCESSING_POLL_INTERVAL`) and enforce a local
max-duration timeout (`REEL_HARNESS_PUBLISHER_PROCESSING_MAX_DURATION`)
that fails a publication `PROCESSING_TIMEOUT` **without ever calling the
provider** once exceeded — the video may still finish on YouTube's side.
Both are pinned onto each publication's `publisher_config` at creation, not
read live from settings mid-flight.

### Lease-lane separation

Before Phase 3B, `PROCESSING` publications only ever advanced via an
operator manually calling `publication-refresh` one at a time — there was
no automatic poller. `worker.publish_lease.lease_next_processing_publication`
is now `PROCESSING`'s own lease, entirely separate from
`lease_next_publication` (the upload lane), so `--process-upload` and
`--process-status` workers (or a single daemon doing both, alternating
fairly each cycle) can never contend for the same row.

### A security gap found while reviewing test skip honesty

Auditing whether the Windows symlink-rejection test (`FileSecretStore`)
honestly reflected a platform constraint surfaced a real, previously
undetected gap: NTFS junctions are a *different* Windows reparse-point
mechanism from symlinks, and — unlike symlinks — can be created without
Developer Mode or administrator privileges. `Path.is_symlink()` does not
detect a junction (confirmed empirically while building the fix), so the
symlink-only check could be bypassed by an attacker-planted junction
redirecting a credential namespace directory elsewhere. Fixed by also
checking Windows' `FILE_ATTRIBUTE_REPARSE_POINT`
(`publisher.secret_store._is_reparse_point`) at the namespace, file, and
secret-root level.

# Reel Harness — Publishing (Phase 3C: TikTok)

## Official documentation consulted (checked 2026-07-29)

All fetched directly from `developers.tiktok.com` (TikTok for Developers)
on 2026-07-29. Blog posts, unofficial SDKs, and third-party integration
guides were explicitly NOT used as a basis for anything implemented —
several turned up in search results and were discarded in favor of the
primary docs below. If TikTok's API changes after this date, re-check
these pages before modifying `providers/tiktok_publisher.py`.

- "Guide to Using the Content Posting API" (get-started overview)
- "TikTok Content Posting API Overview" (Direct Post reference —
  `/v2/post/publish/video/init/`)
- "Content Posting API Video Upload Contract" (chunked `PUT` upload)
- "Query Creator Info API Contract" (`/v2/post/publish/creator_info/query/`)
- "Content Posting API Overview and Status Management" (`/v2/post/publish/status/fetch/`)
- "TikTok Login Kit" (OAuth 2.0 authorization/token/revoke endpoints)
- "Scopes Overview" (scope semantics)

### Direct Post vs. Upload (media transfer)

Two distinct modes exist. **Direct Post** (`/v2/post/publish/video/init/`)
posts immediately to the creator's account. **Upload/media transfer**
(`source=PULL_FROM_URL`) requires TikTok to pull the video from a URL on
the caller's own verified domain — this project has no such hosting, so
per the Phase 3C plan, `PULL_FROM_URL` is explicitly **not implemented**;
only `source=FILE_UPLOAD` (direct byte upload from this process) is used.

### The unaudited-app restriction (the single biggest constraint)

> "All content posted by unaudited clients will be restricted to private
> viewing mode."

Confirmed directly from the Content Posting API overview. Until an app
completes TikTok's audit (`developers.tiktok.com/application/content-posting-api`),
**every post is forced to private/self-only visibility regardless of the
`privacy_level` requested** — this is enforced by TikTok itself, not
something this codebase can or should work around. The adapter and
`publisher-doctor`/`provider-smoke` report this state explicitly
(`APP_REVIEW_REQUIRED`) rather than silently succeeding at a narrower
visibility than requested; see the capability model below.

### OAuth 2.0

- Authorization endpoint: `https://www.tiktok.com/v2/auth/authorize/`
  (`client_key`, `response_type=code`, `scope`, `redirect_uri`, `state`;
  redirect URIs must be HTTPS with no query string or fragment).
- Token endpoint: `https://open.tiktokapis.com/v2/oauth/token/`
  (`client_key`, `client_secret`, `code`, `grant_type=authorization_code`,
  `redirect_uri`, `code_verifier`).
- Revoke endpoint: `https://open.tiktokapis.com/v2/oauth/revoke/`.
- **PKCE is required for desktop/mobile app types** (this project's
  loopback-callback CLI flow is exactly that type) — `code_verifier` is
  sent at token-exchange time, mirroring the YouTube adapter's existing
  PKCE implementation almost exactly.
- Access token: valid 24 hours. Refresh token: valid 365 days. A refresh
  call may return a **different** refresh token, which must replace the
  stored one — this is a real behavioral difference from the YouTube
  adapter (which normally keeps the same refresh token) and is handled
  explicitly in `_resolve_fresh_tiktok_access_token`.

### Scopes

- `video.publish` — required for Direct Post (public-capable, subject to
  the unaudited-app restriction above).
- `video.upload` — upload-only; per third-party corroboration (not the
  primary source, recorded as such) lands drafts in the creator's inbox
  rather than posting directly — **not used**, since this project's whole
  point is direct, tracked publication.
- `user.info.basic` — added by default with Login Kit; not required
  separately for posting, not requested by this adapter (no profile data
  beyond `creator_info` is needed).

Only `video.publish` is requested.

### `creator_info` query — `POST /v2/post/publish/creator_info/query/`

Must be re-queried fresh before every publish attempt (an old snapshot is
never trusted — see `docs/OPERATIONS.md`). Response:
`creator_avatar_url` (2-hour TTL, never persisted), `creator_username`,
`creator_nickname`, `privacy_level_options` (list — the actual allowed
set for this specific creator, not a fixed global list),
`comment_disabled`, `duet_disabled`, `stitch_disabled`,
`max_video_post_duration_sec`. Rate limit: 20 requests/minute per access
token.

### Direct Post init — `POST /v2/post/publish/video/init/`

`post_info`: `privacy_level` (must be one of the creator's own
`privacy_level_options` — `PUBLIC_TO_EVERYONE` | `MUTUAL_FOLLOW_FRIENDS` |
`FOLLOWER_OF_CREATOR` | `SELF_ONLY`), `title` (max 2200 UTF-16 code
units), `disable_duet`, `disable_stitch`, `disable_comment`,
`video_cover_timestamp_ms`, `brand_content_toggle` (paid partnership),
`brand_organic_toggle` (creator's own business), `is_aigc`
(AI-generated-content disclosure).
`source_info`: `source="FILE_UPLOAD"`, `video_size`, `chunk_size`,
`total_chunk_count`. Response: `publish_id` (max 64 chars),
`upload_url` (max 256 chars, **valid 1 hour** — the upload must complete
within that window). Rate limit: 6 requests/minute per access token.

### Chunked upload — `PUT {upload_url}`

Headers: `Content-Type` (`video/mp4` | `video/quicktime` | `video/webm`),
`Content-Length` (this chunk's byte size), `Content-Range`
(`bytes {FIRST}-{LAST}/{TOTAL}`). **The official docs do not specify a
minimum or maximum chunk size** — this is **not independently confirmed**
against a primary per-size-limit table in this session (mirroring the
YouTube quota-cost caveat's existing precedent) and third-party sources
suggesting a ~5–64 MB range were deliberately not used as a basis for
anything. `REEL_HARNESS_TIKTOK_UPLOAD_CHUNK_SIZE` is therefore fully
operator-configurable rather than hardcoded to an unconfirmed number,
defaulting to 10 MiB (a conservative, commonly-cited value that this
session could not verify against TikTok's own docs).

### Post status — `POST /v2/post/publish/status/fetch/`

Request: `publish_id`. Response `status` — exactly five documented
values: `PROCESSING_UPLOAD` (FILE_UPLOAD only), `PROCESSING_DOWNLOAD`
(PULL_FROM_URL only, unused here), `SEND_TO_USER_INBOX`,
`PUBLISH_COMPLETE`, `FAILED`. `fail_reason` (on `FAILED`):
`file_format_check_failed`, `duration_check_failed`,
`frame_rate_check_failed`, `picture_size_check_failed`, `internal`,
`video_pull_failed`, `photo_pull_failed`, `publish_cancelled`,
`auth_removed`, `spam_risk_too_many_posts`,
`spam_risk_user_banned_from_posting`, `spam_risk_text`, `spam_risk`.
`publicly_available_post_id` (only populated once TikTok's moderation
approves a **public** post — "moderation usually finishes within one
minute... may take a few hours" per the docs; never assume `PUBLISH_COMPLETE`
means a public post ID already exists). `uploaded_bytes`. Rate limit: 30
requests/minute per access token.

### What the official docs do NOT specify (recorded, not guessed at)

Maximum video file size, minimum/maximum chunk size, supported video
codec details beyond the three `Content-Type` values, and any daily
post-count cap distinct from the per-minute rate limits above. None of
these are hardcoded anywhere in `providers/tiktok_publisher.py`; the
adapter classifies failures from the live error response instead of
assuming a limit.

Also not documented: any endpoint to query a resumable upload session's
already-accepted byte offset, the way YouTube's protocol documents
`PUT` + `Content-Range: bytes */TOTAL`. Rather than guess at an
undocumented convention, `TikTokPublisher.query_upload_offset` always
raises `UploadSessionExpiredError` — `worker.publish_runner` already
treats that as "start a brand-new session from byte 0" for YouTube's own
genuinely-expired-session case, so this reuses an existing, already-safe
code path instead of risking a wrong guessed offset (re-sending bytes
TikTok already has, or skipping bytes it doesn't). The cost is a
full re-upload after any interruption, rather than a true resume; this
is called out here so it's a known limitation, not a silent one.

The status-fetch response also does not echo enough account info (e.g.
the creator's TikTok handle) for this adapter to construct a public
watch URL on its own — `get_processing_status` always returns
`publication_url=None` for TikTok. The durable reference operators
have is `provider_video_id` (TikTok's `publish_id`), already persisted
on `Publication.provider_video_id`.

## Design decisions this research drove

- **Only `FILE_UPLOAD` is implemented** — `PULL_FROM_URL` requires
  hosting this project has no equivalent of, and is explicitly out of
  scope for Phase 3C (see `docs/OPERATIONS.md`).
- **The unaudited-app restriction is surfaced, never hidden.** An
  operator who hasn't completed TikTok's audit will see
  `APP_REVIEW_REQUIRED` from `publisher-doctor`/`provider-smoke`/
  `publish-job --dry-run`, not a confusing privacy mismatch after the
  fact.
- **`creator_info` is fetched fresh before every publish**, never reused
  from an earlier snapshot — a creator can change their own privacy/
  interaction settings at any time, and TikTok's own guidance is to use
  "the latest creator information."
- **The most restrictive default privacy** (`SELF_ONLY`) is used unless
  an operator explicitly requests otherwise and it's within what the
  creator's own `privacy_level_options` actually allows.
- **`upload_url` is never persisted verbatim** (same pattern as YouTube's
  resumable session URI) — only an opaque local reference, via the same
  `publisher.session_store`.
- **`creator_info` is re-validated in the actual worker publish path**,
  not just the CLI's `provider-smoke`/`publish-job --dry-run` checks:
  `worker.publish_runner._verify_platform_options` re-fetches it and
  re-validates the requested privacy/options immediately before creating
  (or re-creating) an upload session, for any provider whose capabilities
  say it requires this (`PublisherCapabilities.requires_creator_info`) —
  a no-op for YouTube.
- **A real efficiency bug found by the contract E2E test, fixed at the
  shared-code level**: `worker.publish_runner._upload_stage` used to call
  `query_upload_offset` unconditionally, even on a session created moments
  earlier in the same `run_publication` call. Harmless for YouTube (a
  fresh session's offset query just returns 0), but actively wasteful for
  TikTok — since `query_upload_offset` always raises
  `UploadSessionExpiredError` by design (see above), this forced an
  immediate, silently-discarded *second* session (a wasted `publish_id`)
  on every single ordinary, uninterrupted publish attempt. Fixed by
  skipping the query entirely when `Publication.bytes_uploaded == 0` (there
  is nothing to resume yet, so byte 0 is always correct regardless of what
  the provider might report) — verified as a genuine, if minor, efficiency
  improvement for YouTube too (its full adapter/E2E/reconciliation/retry
  suite passes unchanged), not a TikTok-only patch.

## Platform capability model (Phase 3C)

`providers.base.PublisherCapabilities` (populated per adapter, looked up
credential-free via `providers.registry.provider_capabilities`) is what the
CLI/API/service layers check instead of branching on a vendor name.
TikTok's shape: `supports_direct_publish=True`, `supports_upload_only=False`
(no inbox-draft mode is ever requested), `supports_scheduled_publish=False`,
`supports_public_privacy=True` (API-level capability — independent of the
unaudited-app runtime restriction), `supports_unlisted_privacy=False` (no
TikTok privacy level maps to "unlisted"), `supports_comments_control=True`,
`supports_remix_control=True` (duet/stitch), `supports_processing_poll=True`,
`supports_remote_delete=False`, `requires_creator_info=True`,
`requires_user_confirmation=True`, `privacy_values={SELF_ONLY,
MUTUAL_FOLLOW_FRIENDS, FOLLOWER_OF_CREATOR, PUBLIC_TO_EVERYONE}`,
`default_privacy=SELF_ONLY`, `public_privacy_values={PUBLIC_TO_EVERYONE}`.

## Error codes introduced this phase

`APP_REVIEW_REQUIRED` (`PublisherAppReviewRequiredError`), `PRIVACY_NOT_ALLOWED`
(`PublisherPrivacyNotAllowedError`), `CREATOR_NOT_ELIGIBLE`
(`PublisherCreatorNotEligibleError`) — all in `reel_harness/core/errors.py`,
all `retryable=False` (none of these are fixed by retrying; the operator or
the account's own state has to change first). The existing generic
`METADATA_INVALID`/`UPSTREAM_AUTH`/`UPSTREAM_PERMISSION_DENIED`/
`UPSTREAM_RATE_LIMITED`/`UPLOAD_SESSION_EXPIRED`/`UPLOAD_REJECTED`
error codes (built in Phase 3A/3B) are reused as-is for TikTok's own
equivalent failures rather than duplicated under TikTok-specific names.

## Reconciliation outcome vocabulary (Phase 3C)

`core.publish_reconciliation.RECONCILE_OUTCOMES` is intentionally shared
across every provider rather than forked per-provider. TikTok-flavored
concepts map onto the existing names: "upload session created but nothing
uploaded yet" / "upload complete, not yet published" / "publish
submitted" → `already_consistent` (the granular remote status, e.g.
`processing_status=PROCESSING_UPLOAD`, is already in the result's
`reasons`); "remote post found" / "remote post missing" →
`recovered_remote_video` / `remote_video_missing`; "session expired" →
`upload_session_expired` (which every TikTok publication with a
resumable-in-progress session reaches, since `query_upload_offset` always
raises it). `app_review_required` is the one genuinely new outcome:
a proactive, read-only `creator_info` check on a publication that's never
even started uploading, surfacing an unaudited-app block before the
operator wastes a retry cycle discovering it the hard way.
