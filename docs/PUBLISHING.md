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
