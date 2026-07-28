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
