# Changelog

All notable user-facing changes to Reel Harness are documented here. This
file summarizes features by release, not every commit — see `git log` and
`docs/STATUS.md` for the full phase-by-phase implementation history.

## [0.1.0] — 2026-07-29 — First stable release (local-first)

This release ships as a **local-first release with explicitly documented
limitations** (see below), not as a release with real-platform publishing
verified end to end. No YouTube, TikTok, or Instagram credentials are
configured on the machine this release was built and tested on.

### Live publisher verification status

- **YouTube**: unverified — no saved credential (`NOT_CONFIGURED`).
- **TikTok**: unverified — no saved credential (`NOT_CONFIGURED`).
- **Instagram**: unverified — no saved credential (`NOT_CONFIGURED`).

Publisher features (YouTube/TikTok/Instagram upload) are implemented and
covered by contract E2E tests against each platform's documented API
behavior, but should be treated as **preview / credential-required**
until exercised against a real account with real credentials. The exact
phrase "production live publishing verified" does not apply to this
release. See `reel-harness live-verify` and the release manifest's
`live_verification` field to check this status yourself, and re-run
`reel-harness live-verify --upload-tests ...` once real credentials are
configured to obtain a real verification result.

### Everything else

All content generation, review, and local pipeline functionality
(Phase 0–4A scope) is unchanged from `0.1.0rc2` — see that entry below
for the full feature list. All contract E2E tests, the full unit/
integration/e2e suite, mypy, and ruff pass on this release.

## [0.1.0rc2] — 2026-07-29 — Second release candidate

### Fixed

- `storage-verify` falsely flagged healthy jobs still in `CREATED` /
  `QUEUED` / `TOPIC_GENERATING` / `SCRIPT_GENERATING` / `POLICY_CHECKING`
  as `missing_directory` — those stages legitimately have not written a
  file to disk yet. Found via an operational soak test (concurrent fake
  jobs through a real `serve` subprocess); `storage-verify` could FAIL on
  any normal system with jobs still waiting in queue. Jobs in those
  statuses are no longer flagged; a genuinely missing directory past
  `ASSET_FETCHING` is still correctly reported.
- A subprocess-output-decoding mismatch (parent decoding a UTF-8-forced
  child's stdout with the platform's default locale encoding instead of
  UTF-8 explicitly) could crash under high non-ASCII log volume; fixed in
  the affected E2E test's subprocess capture.

### Note

- `v0.1.0rc1` is unaffected by these fixes (its own tag and history are
  unchanged) — this candidate exists because real product code changed
  after `v0.1.0rc1` was tagged, so `v0.1.0rc1`'s own verification cannot
  be reused as the basis for the final release as-is.

## [0.1.0rc1] — 2026-07-29 — First release candidate

### Added

- **Pipeline**: a full local job pipeline (topic → script → policy check →
  asset fetch → TTS → render → validate → review/approve → publish),
  running end-to-end against Fake providers with zero external calls, or
  against real providers once configured.
- **Real content providers**: an OpenAI-compatible LLM adapter, an
  OpenAI-compatible TTS adapter, and a Pexels stock-media adapter — each
  behind a vendor-neutral Protocol, each opt-in via configuration, and each
  the fake provider by default.
- **Publishers**: YouTube (resumable upload, OAuth, processing poll),
  TikTok (Direct Post `FILE_UPLOAD`, app-review-aware `SELF_ONLY`
  enforcement), and Instagram Reels (resumable direct upload, container →
  processing → transparent `media_publish`) — all on one shared
  `Publisher` Protocol, capability model, worker, and reconciliation
  framework.
- **Reliability**: renewable lease fencing, stale-worker recovery, a
  durable crash-recovery journal for publish uploads, manual retry, and
  publication reconciliation that never guesses at an ambiguous remote
  state.
- **Interfaces**: a `reel-harness` CLI covering the full job/publication
  lifecycle, and a FastAPI HTTP API with the same operations plus
  `/healthz` `/readyz` `/status` `/metrics`.
- **Repository-external credential storage**: OAuth tokens and upload
  session references live outside the repository, never in the jobs DB,
  with symlink/junction rejection and secret redaction applied to every
  log line and persisted error field.
- **Production preflight** (`reel-harness preflight [--profile production]
  [--check-remote]`): a single readiness report covering config, DB,
  storage, credentials, ffmpeg, dependencies, worker/upload settings, and
  the public-upload flag, with a stricter bar for the `production`
  profile.
- **Safe config fingerprinting**: a deterministic, non-secret snapshot of
  how a process is configured, logged at every startup and reusable by
  diagnostics/incident tooling.
- **Database operations** (`db-status` / `db-migrate` / `db-backup` /
  `db-restore` / `db-verify`): SQLite online-backup-API backups with
  checksummed manifests, a migration lock + dry-run + default safety
  backup, and a destructive `db-restore` that refuses on a running
  worker, a checksum mismatch, or a newer-than-supported schema.
- **Storage verification and backup bundles** (`storage-verify
  [--repair-safe]`, `backup-create` / `backup-inspect` / `backup-restore`):
  cross-checks job storage against the DB, and a single portable,
  checksummed, path-traversal/archive-bomb-hardened archive of the
  database, jobs storage, and publish journal (never credentials).
- **Unified runtime supervisor** (`reel-harness serve`): runs the API and
  render/publisher workers together in one supervised process with a
  documented per-component failure policy and graceful shutdown.
- **Operational metrics and incident bundles**: a dependency-free
  `/metrics` endpoint and `reel-harness incident-bundle`, a self-secret-
  scanned diagnostics zip for offline analysis.
- **Multi-platform live verification** (`reel-harness live-verify`): a
  single read-only sweep across all configured publishers, with a real
  upload test gated behind an explicit per-platform confirmation flag.
- **Release tooling**: single-source PEP 440 versioning
  (`reel-harness --version`), a release manifest command, and a
  `release-check` gate that must pass before a release candidate is
  tagged.

### Changed

- N/A — this is the first tracked release.

### Fixed

- A number of real, adversarially-discovered bugs across every phase of
  development (crash-recovery gaps, a Windows-vs-POSIX file-replace race
  during DB restore, a redaction regex that corrupted JSON on a second
  pass, upload-session-handle fields silently dropped on session reuse,
  and others) — see `docs/STATUS.md` for the phase-by-phase detail on
  each.

### Security

- Every OAuth credential, client secret, and upload-session URI is kept
  entirely out of the jobs database, logs, and manifests.
- Structured log and persisted-error redaction covers bearer/basic auth,
  API-key-shaped headers/params/JSON fields, and every explicitly
  registered secret.
- Backup bundles and incident bundles are validated member-by-member
  before extraction (absolute paths, `..` traversal, symlinks/hardlinks,
  and size caps all refused) and are independently secret-scanned before
  being written.
- Credential and journal directories reject symlinks, NTFS junctions, and
  any path that resolves inside the repository checkout.

### Known limitations

- Live provider credentials and permissions are not configured/verified
  on the machine this release candidate was built on — see the release
  manifest's `live_verification` field.
- Instagram Reels publishing has no private/unlisted option — every
  publish is public.
- TikTok forces `SELF_ONLY` visibility on any app that has not passed its
  own review process.
- SQLite/local filesystem storage only — no PostgreSQL or cloud object
  storage.
- One pre-existing test skip on Windows (symlink-rejection, requires
  elevated privileges to create a real symlink).
- No remote video/post delete is implemented for any publisher.
- No cloud deployment target is implemented — single-machine, local-first
  only.
- Facebook Reels publishing is not implemented (Instagram Reels only).
