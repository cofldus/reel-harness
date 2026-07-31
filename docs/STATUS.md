# Status

Last updated: 2026-07-31 (Fable F3 in progress, on branch
`phase6/fable-cinematic-engine`). Phase 2A through 5B plus deployment
sub-phase 6A-1 (dual SQLite/PostgreSQL backend) are merged into `main`.
The deployment track (6A-2 auth .. 6A-5 production mode) is parked; the
Fable cinematic engine is the active track and owns `v0.5.0rc1`.

---

# HANDOFF — read this first if you are picking up mid-flight

Branch: **`phase6/fable-cinematic-engine`** (always push here; `main`
holds everything through 6A-1). Working tree should be clean and in sync
with origin — if it is not, inspect before doing anything else.

## Where the work stands

Fable is a five-sub-phase build: **F1 done, F2 done, F3 in progress,
F4 and F5 not started.**

- **F1 (done)** — cinematic domain (6 tables, schema v8), project/shot
  state machines, `CinematicVideoProvider` Protocol + fake tier, project
  service + separate storage root + CLI, third worker/lease lane,
  offline vertical slice e2e.
- **F2 (done)** — real Narrative Director: strict schema, whole-document
  validation, bounded repair loop, fake + openai-compatible adapters,
  canonical shot-prompt compiler. **Live-verified against gpt-4o.**
- **F3 (in progress)** — reference images, cost/budget, demo + google
  image adapters, multiple candidate takes. Commits 1 (reference provider
  contract) and 2 (cost/budget, schema v10) are done and pushed.
- **F4 (not started)** — web UI + `/v1/fable/*` API.
- **F5 (not started)** — real Veo adapter, film editor, audio, release
  `v0.5.0rc1`.

## F3 remaining commits (the immediate work)

Commits 1 and 2 of 6 are done and pushed. Remaining, in order:

3. **`feat: add reference generation workflow and casting gate`** —
   `approve_story` currently walks straight through `CASTING`; split it
   so `CASTING` is a real stop. New `generate_references(project_id)`
   generates the face portrait FIRST, then feeds it back as a character
   reference for the three-quarter, full-body and wardrobe views (chained,
   never independent — independent generation yields four different
   actors). Per-character `approve_reference` / `reject_reference`;
   `approve_characters` must additionally require every character's
   `reference_approved`. New CLI commands. Same crash-safety discipline as
   F2's adaptation (transition committed before any network call, results
   written atomically, fingerprint-based replay).
4. **`feat: add demo and google reference image adapters`** — demo tier
   (local sample images, `DEMO_TEST_LICENSE`, no network, never presented
   as real AI output) and the real adapter using `google-genai` with
   `gemini-3.1-flash-image` (new optional extra `google`; never a hard
   dependency). Safety refusals must surface as `ContentPolicyRefusedError`
   → `REVIEW_REQUIRED`. Record the SynthID watermark on every result.
   Contract tests only — no live calls in the suite.
5. **`feat: add multiple candidate takes per shot`** — `Settings.
   fable_takes_per_shot` (1 / 2 / 4) plus per-project override; worker
   generates N takes with distinct seeds; each take's cost counts against
   the budget; rejected takes are retained, never deleted on selection.
6. **`test: add fable reference and budget e2e`** — offline e2e through
   the fake tier, plus a `fable-reference-smoke` CLI command and docs.

## Provider decisions already researched (do not re-litigate)

- **Reference images: `gemini-3.1-flash-image` (Nano Banana 2)**, with
  `gemini-3-pro-image` as a configurable escalation. Chosen because it is
  the ONLY option sharing one SDK (`google-genai`) and one credential with
  Veo, has typed character references (4), is GA, and costs ~$0.067/image
  at 1K. **1K is sufficient** — Veo caps reference-driven runs at 720p, so
  paying for 2K/4K buys nothing. **Do not build an Imagen adapter**:
  Imagen shuts down 2026-08-17.
- **Video (F5): Vertex AI `veo-3.1-fast-generate-001`**, region
  `us-central1` only. GA (the Gemini-API Veo endpoints are preview and the
  $300 GCP trial credit does not apply to them). Reference images force
  8s / 720p, max 3 reference images of type `asset`, `personGeneration`
  must be `allow_adult`. **Generated videos are deleted after 2 days** —
  the adapter must download immediately.
- **Open risk, unresolved by any documentation**: whether Veo accepts
  SynthID-watermarked images as character-reference input. Google
  watermarks all generated images with no removal option, so if Veo
  rejects them the whole consistency strategy needs rethinking. F3's
  `fable-reference-smoke` command exists to answer this BEFORE F5 builds
  on it. Run it as soon as GCP credentials exist (~$0.32 for the
  cheapest GA smoke).

## Working agreements this project follows

- Read `CLAUDE.md` and `.claude/rules/architecture.md` first; they are
  binding (artifacts only under the job/project storage roots, state
  changes only via the state machines, vendor names only in
  `providers/registry.py`, subprocess always `list[str]` + `shell=False`,
  ffmpeg absence must fail as `BLOCKED_DEPENDENCY`, fake assets always
  `FAKE_TEST_LICENSE`, `uv` for everything).
- Each sub-phase gets an approval-gated plan before implementation.
- Each commit: targeted tests → `mypy` on BOTH platforms
  (`python -m mypy` and `--platform linux`) → ruff → full suite → push.
  Never batch commits; push each one as it lands.
- **Never claim more than was verified.** Contract tests are not live
  verification. If credentials are absent, report `NOT RUN` explicitly.
  Where an automated check has limits (e.g. the adaptation fidelity
  heuristic), say so in the docs rather than implying completeness.

## Environment quirks on the original dev machine

- `ruff.exe` and `pytest.exe` are blocked by a Windows Application
  Control policy. Use `uv run --no-sync python -m pytest` and
  `uvx ruff@0.14.14 check reel_harness tests`. On a different machine
  plain `uv run ruff` may just work — try it first.
- Local mypy defaults to the win32 platform; CI checks Linux. Always run
  both, or Linux-only failures reach CI (this actually happened).
- Tests must never read a developer's real `.env` — a conftest fixture
  enforces this. Do not remove it.

---

## Fable F3 (in progress) — references, cost/budget, demo+google adapters

Two of six commits have landed on `phase6/fable-cinematic-engine`.

- **Commit 1 — character reference provider contract**: the
  `CharacterReferenceProvider` Protocol (synchronous by contract: every
  image API surveyed returns inline bytes rather than a polled job,
  unlike video) plus `ImageCapabilities` (`watermarked` recorded as the
  provenance fact it is, not hidden) and the fake tier.
- **Commit 2 — cost estimation and project budget limits**: schema **v10**
  (`fable_projects.budget_limit_amount` / `budget_currency` /
  `budget_spent_amount`, plus `fable_takes.cost_amount` / `cost_currency`
  so the running total can be audited against its own line items rather
  than trusted). New `core/cost_service.py` holds four rules: an estimate
  never moves spend (only a provider-REPORTED cost for a completed
  generation does, written in the same fenced commit as its take);
  unknown stays unknown (one unpriceable shot makes the whole project
  estimate `known=False`, reported with the priced subtotal as a stated
  lower bound, never rounded to zero); currencies are never converted;
  and every refusal is a review rather than a failure. The **double
  gate** — `Settings.allow_paid_generation` AND a per-project budget
  limit, neither implying the other — mirrors `allow_public_upload`
  exactly, and asks "does this provider charge?" by *excluding* the free
  tiers (`FREE_PROVIDER_IDS` in `providers/registry.py`), so the real
  adapters landing in commits 4 and 5 count as paid the moment they
  exist. `approve_shots` prices the whole plan before any shot becomes
  claimable (failing there costs nothing); the worker re-checks per shot,
  because config and budget can both change after approval. A blocked
  shot lands `READY -> REVIEW_REQUIRED` (a new state-machine edge)
  carrying `BUDGET_EXCEEDED` / `PAID_GENERATION_NOT_ALLOWED` with no take
  row — nothing was submitted, so nothing was charged — and re-queues
  through the same `REVIEW_REQUIRED -> READY` edge a rejected take uses.
  New `fable-budget` / `fable-estimate` CLI commands, budget block in
  `fable-status`, a `paid_generation_feature_flag` preflight check, and
  `allow_paid_generation` in the config fingerprint.
- **A real pre-existing defect found and fixed while building this**:
  `READY -> FAILED` was missing from `ALLOWED_SHOT_TRANSITIONS`, so any
  failure BEFORE submission (an unconfigured provider refusing to quote
  or validate) made `fable_runner`'s own failure handler raise
  `InvalidFableTransitionError` out of `run_shot` and straight past
  `FableDaemon`'s error isolation — a routine "provider not configured"
  would have killed the worker lane rather than failing one shot. Found
  by the new cost gate's regression test, not by inspection; predates F3.
- **F3 honesty notes so far**: no reference images are generated yet
  (commit 3), the only registered cinematic/image providers are still the
  free fake tiers, so the paid gate has been exercised only against a
  provider id stubbed as paid — never against a real billed call. The
  budget arithmetic rounds at a fixed scale (6 dp) specifically so float
  accumulation cannot refuse a generation the operator paid for; that is
  a deliberate tradeoff, not exact decimal money handling.

## Fable F2 — Narrative Director

Replaces F1's stub adaptation with a real, provider-neutral story ->
shot-plan pipeline. The states, review gates, worker lane, and CLI are
unchanged; what changed is that the adaptation is now genuinely
generated and genuinely validated.

- **Contract** (`providers/base.py`): `NarrativeDirector` Protocol
  (`adapt_story` + `repair_adaptation`) with `AdaptationRequest` /
  `AdaptationResult`. Separate from `LLMProvider` because the short-form
  script call's shapes are semantically wrong for film adaptation.
  Adapters return raw text; validation is downstream, matching the
  existing script-generation discipline.
- **Strict schema** (`pipeline/adaptation_schema.py`) enforces the hard
  rules at the earliest possible point: adult-only characters via
  `Literal[True]` plus an age-bracket whitelist (a minor-looking
  character fails parsing before anything is persisted), shot grammar
  validated against the F1 enums with exactly one camera movement per
  shot by construction, one filmable action per shot (bounded length +
  a documented chained-action heuristic), and structural limits (1-2
  characters, 1-3 locations, 1-6 scenes, 4-15 shots).
- **Whole-document validation** (`pipeline/adaptation_parser.py`):
  one bounded lenient-JSON-extraction pass, then subject/location
  reference integrity, dialogue-line ownership, shot/reverse-shot
  alternation in multi-speaker scenes, contiguous ordering, and a
  source-fidelity heuristic that rejects fabricated citations by matching
  each scene's `source_beat` back into the real source text. Every
  failure collects the FULL error list -- that list is the repair loop's
  input contract.
- **Bounded repair loop** (`core/adaptation_service.py`): max 2 repairs
  (3 director calls total), each carrying the exact validation errors
  back to the director. A refusal or empty response is not repaired at
  all -- re-asking with the same source only burns quota -- so it fails
  immediately instead of consuming the budget. Exhaustion raises
  `SCHEMA_INVALID`, so the existing stage-retry classification applies
  unchanged.
- **Persistence and idempotency**: `ADAPTING` commits before any network
  call and the adaptation's own writes land in ONE transaction, so a
  partial adaptation is never observable and a crash is resumable by
  re-running the same command. `adaptation_fingerprint` (source +
  parameters + prompt version; schema v9 additive column -- the first use
  of the now dialect-portable migration path) makes an unchanged re-run a
  replay rather than a second paid call, while changed input on an
  already-adapted project is refused so drift cannot silently discard an
  approved adaptation. Re-adaptation replaces children and refuses
  outright if any shot already has takes.
- **Adapters**: `FakeNarrativeDirector` is deterministic and produces a
  COMPLETE document that passes the real parser and validators -- its
  scene beats are genuine quotes from the caller's own source text, so
  the fidelity check is exercised meaningfully rather than vacuously; its
  modes drive the repair, exhaustion, adult-rejection, and transient
  paths. `OpenAICompatibleNarrativeDirector` subclasses the existing
  OpenAI-compatible LLM provider to inherit its transport contract
  verbatim (bounded retries, Retry-After, auth never retried and never
  echoing the credential, bodies never logged), differing only in prompt
  and output budget (`REEL_HARNESS_NARRATIVE_MAX_OUTPUT_TOKENS`,
  `REEL_HARNESS_NARRATIVE_READ_TIMEOUT`). It reuses the
  `REEL_HARNESS_LLM_*` endpoint block -- adaptation is a
  chat-completions call against the same kind of endpoint.
- **Canonical prompt compiler** (`pipeline/shot_prompt.py`): fifteen
  slots in fixed order, provider-neutral (vendor dialects are an adapter
  concern in F5). The character's fixed identity is injected into EVERY
  shot -- the only mechanism keeping one virtual actor recognizable
  across separately-generated clips. `prompt_fingerprint` is versioned,
  and it is what makes paid take generation idempotent via
  `FableTake`'s unique constraint.
- **F2 honesty notes**: the fidelity check only rejects obvious drift
  (fabricated citations, a dropped ending); real semantic faithfulness
  stays the STORY_REVIEW gate's human decision and nothing claims more.
  The MockTransport adapter tests prove protocol conformance only, never
  live success.
- **Live verification: RUN and PASSED** (2026-07-31, real credentials,
  `openai-compatible` against `gpt-4o`). A real Korean short story was
  adapted in ~26s: 1 adult character with fixed identity, 1 location,
  2 scenes, 4 shots (18s total), every scene beat a genuine source
  quote, every shot a single filmable action with one camera movement,
  all validators passed on the first attempt with no repair needed.
  That run surfaced one real defect -- wardrobe was emitted twice per
  prompt when fixed_identity already carried it -- now fixed with
  regression coverage.

## Fable F1 — Cinematic domain + fake vertical slice

## Fable F1 — Cinematic domain + fake vertical slice

First of five Fable sub-phases (F1 domain/fake slice, F2 narrative
director, F3 references/demo/budget, F4 web UI, F5 real Veo 3.1 adapter +
film editor + release). F1 ships the complete offline vertical slice
(its stub adaptation was replaced by the real pipeline in F2):
story text -> stub adaptation -> explicit review gates -> shot generation
through a real worker lane -> take selection -> hard-cut final render ->
COMPLETED, all against the fake provider with zero network.

- **Domain** (`db/cinematic_models.py`, schema v8 — new tables only):
  `fable_projects` (story_bible as one JSON doc), `fable_characters`
  (virtual adult actors only; `adult_confirmed`/`reference_approved`
  default False), `fable_locations`, `fable_scenes`, `fable_shots` (the
  leasable unit, same lease-fencing columns as Job/Publication),
  `fable_takes` (append-only; `(shot_id, prompt_fingerprint,
  attempt_number)` unique constraint is the duplicate-generation guard).
- **State machines** (`core/cinematic_state.py`, the third pair by the
  documented non-genericity precedent): project statuses with five
  explicit `*_REVIEW` gates — `SHOT_REVIEW -> GENERATING` is the only
  entry into paid generation; shot statuses with shot-level retry that
  never fails the whole project. Shot grammar enums (one camera movement
  per shot by construction).
- **Provider layer**: `CinematicVideoProvider` Protocol (submit/poll/
  download — the shape every surveyed real API has) + frozen
  capabilities; a distinct `"moderated"` poll state routes to human
  review, never a blind retry; cost estimates return unknown rather than
  invented numbers. Fake provider is deterministic, renders REAL mp4s via
  local ffmpeg (never bypasses BLOCKED_DEPENDENCY), stamps
  FAKE_TEST_LICENSE. Registry/snapshot/fail-loud ladder identical to the
  other provider families; `REEL_HARNESS_CINEMATIC_PROVIDER=fake` only.
- **Worker lane**: third lease module + daemon
  (`worker/fable_lease.py`/`fable_runner.py`/`fable_daemon.py`), fenced
  commits on every status change, crash recovery through the state
  machine with a bounded re-queue budget, project auto-advance
  `GENERATING -> TAKE_REVIEW`. `serve --fable-workers N` (default 0) and
  `fable-worker-run`.
- **Service + CLI**: `FableService` (idempotent creation, every gate an
  explicit approval, adult-confirmation enforced at the character gate,
  single-selected-take with append-only retention, ffprobe-validated
  final concat under the separate `fable_projects/` storage root); CLI
  `fable-create/adapt/approve/status/list/select-take/render/cancel`.
- **F1 honesty notes**: adaptation is a deterministic stub (real
  NarrativeDirector is F2); no reference images yet (F3); no
  transitions/audio mix/color (F5); provider polling is inline (a
  dedicated poll lane comes with the real adapter). Provider research
  (July 2026 official docs) selected Google Veo 3.1 for F5 — the only
  surveyed API with first-class multi-image character reference — with
  Runway as runner-up.

## Phase 6A-1 — Dual database backend (SQLite + PostgreSQL)

## Phase 6A-1 — Dual database backend (SQLite + PostgreSQL) [merged to main]

First sub-phase of the (now parked) deployment track. SQLite
stays the zero-config default; PostgreSQL is now a fully-supported second
backend. Deliberately the lowest-risk 6A sub-phase: purely additive, no
security-critical surface, zero behavioral change for existing SQLite
users (the full pre-existing suite runs unchanged).

- **Engine layer** (`db.schema.create_engine_from_url`): real dialect
  detection via `make_url()` (replacing a `startswith("sqlite")` string
  check), PostgreSQL connection pooling with `pool_pre_ping`, optional
  server-side statement timeout, bare-`postgresql://`-to-`+psycopg`
  normalization. New `Settings` fields `db_pool_size` /
  `db_pool_max_overflow` / `db_statement_timeout_seconds` (SQLite-inert),
  and `DATABASE_URL` scheme validation at startup. Driver: `psycopg` v3
  via a new optional `postgres` dependency group — never a hard
  dependency.
- **Migration mechanism made dialect-portable**: `_ensure_column` now uses
  `sqlalchemy.inspect()` instead of `PRAGMA table_info`, and
  `_ADDITIVE_COLUMNS` holds real SQLAlchemy types rendered per-dialect via
  `CreateColumn` (the old hand-written `"BOOLEAN NOT NULL DEFAULT 1"`
  strings would fail outright on PostgreSQL). Migration lock dispatches by
  backend: SQLite keeps the PID lockfile, PostgreSQL uses
  `pg_try_advisory_lock` (auto-released by the server on crash).
- **Backup/restore**: PostgreSQL path via `pg_dump --format=custom` /
  `pg_restore --clean --if-exists --no-owner`, invoked through
  `ProcessRunner`'s `list[str]` + `shell=False` discipline, producing the
  same checksum + manifest-sidecar contract as the SQLite path so
  `db_restore`'s safety checks apply uniformly.
- **Verification**: new dual-backend parity suite
  (`tests/integration/test_postgres_backend_parity.py`) parametrized via
  `REEL_HARNESS_TEST_POSTGRES_URL` (skips cleanly when unset, matching the
  `FFMPEG_PRESENT` convention), including a real two-thread concurrent
  lease-claim race — the direct verification of the guarded-UPDATE claim
  pattern under real PostgreSQL row-level locking that had previously only
  been reasoned about. CI runs it against a real `postgres:16` service
  container on every push.
- **Deliberately deferred to 6A-2**: SQLite→PostgreSQL data transfer
  (`db-transfer`) — it depends on the multi-user ownership model (who owns
  migrated rows must be an explicit decision). PostgreSQL is for fresh
  databases until then.
- Remaining 6A sub-phases (each gets its own approval-gated planning pass
  before implementation): 6A-2 multi-user auth + ownership, 6A-3 object
  storage abstraction, 6A-4 worker process separation + Docker, 6A-5
  production mode + reference deployment + `v0.5.0rc1`.

## Phase 5B — Publishing Web UI

Goal: extend the Phase 5A web UI to cover real-platform publishing --
connecting a YouTube/TikTok/Instagram account, creating a publication from
a completed job, and watching it through upload/processing to a published
video, entirely by clicking. No change to the underlying publish backend
(`PublicationService`, `core.publish_retry`/`publish_reconciliation`, the
publisher worker) -- every new web route calls the exact same in-process
functions the CLI/`/v1/publications/*` API already used.

- **Repo audit confirmed the backend was already complete**: `Publisher`
  Protocol + 3 real adapters + `fake`, `PublicationService`, a
  `Publication`/`PublicationStatus` state machine independent of
  `Job`/`JobStatus`, eligibility checking that permanently blocks Demo/Fake
  output, a durable crash-recovery journal, reconciliation, manual retry,
  and full `/v1/publications/*` routes. The actual gap was narrow: no HTTP
  OAuth callback existed anywhere (the CLI's `publisher-auth` holds a
  loopback listener open synchronously in the terminal, which a browser
  can't do), no page rendered `JobDetailView.can_publish` (already
  computed in Phase 5A, but dead -- no template used it), no
  "which publishers are connected" query existed, and the CLI's
  credential-safe-metadata allowlist was inlined twice instead of factored
  out once.
- **New `publisher.oauth_flow_store.OAuthFlowStore`**: transient, single-
  use state (PKCE verifier + pending account_reference) bridging a
  browser's `POST /connect` and its `GET /callback` -- a browser redirect
  round-trip can't hold a CLI process's in-memory PKCE state the way
  `publisher-auth` does. File-backed (same repository-external secret
  directory as credentials, its own namespace), keyed by `state` only
  (never `(provider, account_reference)`, so concurrent/abandoned-then-
  retried flows never collide), TTL-checked lazily on `pop()` -- no
  background sweep thread, consistent with the rest of this codebase.
- **`/callback` has no CSRF dependency, by design**: it's reached by a
  genuine cross-site top-level navigation from the OAuth provider's own
  domain; `rh_csrf` is `SameSite=Strict`, so that cookie would never be
  sent there regardless. The single-use, short-TTL, provider-bound
  `state` parameter is the correct CSRF-equivalent defense for an OAuth
  callback -- documented explicitly in `docs/OPERATIONS.md` so a future
  reviewer doesn't "fix" it into a bug.
- **Redirect_uri asymmetry, deliberate**: YouTube's redirect_uri is
  computed per-request via `request.url_for(...)` (Google's Desktop-app
  client type tolerates any loopback port, same property the CLI's
  ephemeral-port listener already relies on -- no new setting). TikTok/
  Instagram reuse their existing configured `_redirect_uri` settings
  verbatim, since those platforms need an exact pre-registered match.
- **Platform-specific options are read-only, not editable**: a real gap
  found while designing the publish-setup form --
  `PublicationService.create_publication` has no parameter to accept a
  custom per-publication `platform_options` override at all; the worker
  always applies `providers.registry.default_platform_options(provider)`
  (the same thing `publish-job`/the CLI already do). The form shows these
  defaults read-only with a confirmation checkbox, rather than pretending
  to let the user customize something the backend can't yet persist --
  changing that would mean touching `PublicationService`/the worker's
  metadata-snapshot resume path, real domain-logic surface this phase
  deliberately left alone.
- **A real pre-existing bug found and fixed**:
  `PublicationService.cancel_publication` assumed every non-terminal
  status could transition straight to `CANCELLED` via its
  `_IMMEDIATE_CANCEL_STATUSES` set, but the state machine deliberately
  only allows `FAILED` -> `RETRY_WAIT` (see
  `test_failed_allows_only_manual_retry_wait`, a pre-existing test this
  session didn't write) -- calling cancel on a `FAILED` publication
  crashed with a raw `InvalidTransitionError` instead of a clean 409.
  Found by the new web UI's `can_cancel`-mirrors-the-real-precondition
  test (the same discipline Phase 5A established for jobs' `can_cancel`).
  `FAILED` is now refused explicitly, alongside `PUBLISHED`/`CANCELLED`.
  Predates Phase 5B; only ever reachable via
  `/v1/publications/{id}/cancel`, but nothing had exercised that specific
  path before.
- **Two more template bugs a template-only "hide when unavailable" pattern
  produced, both fixed**: (1) `publisher_accounts.html`/`publish_setup.html`
  originally hid a platform's entire form (including its CSRF hidden
  field) whenever that platform wasn't selectable -- a page where nothing
  is configured/connected then has zero CSRF-carrying elements at all.
  Fixed by always rendering the form with disabled inputs instead, the
  same fix Phase 5A's own CSRF-fragment bug already established the
  precedent for. (2) Job Detail's new "게시" empty-state message was
  gated on `not job.can_publish`, so an eligible job with zero
  publications yet showed neither the publish button's list nor the
  empty state -- a genuinely eligible job's page looked broken. Both
  caught by dedicated tests, not manual inspection.
- **Routes**: `/publisher-accounts` (+ `connect`/`callback`/`disconnect`),
  `/jobs/{id}/publish` (setup) + `POST /jobs/{id}/publications` (create),
  `/publications` (list), `/publications/{id}` (detail) +
  `/publications/{id}/status` (HTMX fragment) +
  `/{cancel,retry,refresh,reconcile}`. None overlap `/v1/*` -- same
  unprefixed-web-route-alongside-`/v1/*`-API pattern Phase 5A established
  for jobs. One additive `GET /v1/publications` list route was added for
  API-client parity, mirroring Phase 5A's own `GET /v1/jobs` precedent.
- **No new SQL table, no Alembic, `SCHEMA_VERSION` unchanged** -- confirmed
  during design review, not assumed: nothing downstream expects OAuth
  pending-flow state to survive a restart, and the one existing audit
  table (`PublicationAuditEvent`) is FK'd to `publication_id`, which
  doesn't exist yet at OAuth-connect time.
- **Verification**: unit tests for every new view model's `can_*`
  mirroring the real service precondition (not a transition-table guess),
  form validation (allow-lists sourced from `provider_capabilities()`,
  never hardcoded), and label coverage; route tests (`TestClient`) for
  CSRF gating, every action's success + precondition-violation case, and
  the full OAuth connect/callback/disconnect flow (state single-use,
  expired/missing/mismatched state, provider error handling) using an
  injected fake `*OAuthClient` -- no real network call anywhere, per
  CLAUDE.md; an integration test driving a full publish lifecycle purely
  through the web routes with the `fake` publisher provider (needs no
  OAuth account) across two real worker-drive cycles
  (`READY_TO_UPLOAD` -> `PROCESSING`, then `PROCESSING` -> `PUBLISHED`,
  mirroring `run_publication`'s own two-call contract) to actually reach
  `PUBLISHED`; a real-Chromium Playwright scenario confirming the
  publish-eligible job's "게시하기" link, the publish-setup page's real
  (unconfigured) readiness rendering, and its navigation to
  `/publisher-accounts`. Full suite green, mypy/ruff clean.
- **Manual verification performed against a real running `serve`
  process** (not just `TestClient`): the `/connect` redirect resolves to
  Google's real authorization URL with a correct
  `state`/`code_challenge`/`redirect_uri` (matching the actual bound
  port), and the not-configured/missing-CSRF/invalid-state paths all
  return the right status code.

## Phase 5A — Local Web UI MVP

Goal: expose the existing CLI/API backend through a browser -- no new
domain logic, no new pipeline behavior. A user runs `reel-harness serve`
and can create a Demo job, watch it progress, review the result, and
download it, entirely by clicking. Real-platform publishing UX (OAuth
connect, account selection, per-platform options) is explicitly deferred
to Phase 5B.

- **Tech**: FastAPI + Jinja2 (server-rendered) + HTMX + a small amount of
  vanilla JS + FastAPI `StaticFiles` -- no React/Vue/Next.js, no Node.js
  build step, no client state store. `reel_harness/web/` (`router.py`,
  `dependencies.py`, `view_models.py`, `labels.py`, `forms.py`,
  `formatting.py`, `templates/`, `static/`) mounted onto the single
  existing `api/app.py` `FastAPI()` singleton via `app.include_router(...)`
  at import time -- `ops.supervisor.Supervisor` needed zero changes since
  it already just uses `api.app.app` as-is.
- **6 screens**: Dashboard (`/`), Job List (`/jobs`, paginated/filterable),
  New Job (`/jobs/new`), Job Detail (`/jobs/{id}`), System Status
  (`/system`), Settings Guide (`/settings`, read-only -- config stays
  env/.env-driven, never a secret-input form).
- **3 real backend gaps closed first** (own commit, useful to API clients
  independent of the UI): `JobService.list_jobs` gained `limit`/`offset` +
  a new `count_jobs`; new `JobService.get_stage_runs` reads the
  previously-unqueried `StageRun` table for real per-stage timing; new
  `GET /v1/jobs` (paginated list), `POST /v1/jobs/{id}/reject`,
  `POST /v1/jobs/{id}/retry` (both `JobService` methods already existed,
  CLI-only, mirroring how `approve`/`cancel` already made the HTTP jump).
- **Per-job provider profile**: `JobService.create_job` gained an optional
  `provider_snapshot` override parameter -- the New Job form's Demo/Real/
  Fake choice is a genuine per-job override, not just whatever
  `REEL_HARNESS_LLM_PROVIDER`/etc. the process happened to start with.
  Real is gated on an independent readiness check against the specific
  real provider names; Fake is hidden unless
  `REEL_HARNESS_UI_SHOW_FAKE_PROFILE=true` (env flag, not a UI toggle).
- **Progress polling**: HTMX self-terminating poll (`hx-trigger` only
  re-included in the swapped-in fragment while the job is still active) --
  stops on `TERMINAL_STATUSES` (`COMPLETED`/`CANCELLED`) **or**
  `NEEDS_ACTION_STATUSES` (`FAILED`/`REVIEW_REQUIRED`/`READY`), since the
  narrower `TERMINAL_STATUSES` alone under-covers "no further automatic
  progress without a human." No fake percentage -- stage label + `StageRun`
  timeline + elapsed time only.
- **Video streaming**: `GET /jobs/{id}/video` uses Starlette's
  `FileResponse` (confirmed built-in Range/206 support, no hand-rolled
  byte-range code), path resolved exclusively via
  `LocalFilesystemStorage.path_for()`.
- **CSRF, not a login system**: double-submit cookie
  (`rh_csrf`/`X-CSRF-Token`), independent of `/v1/*`'s bearer-token
  `require_api_key`. New `Settings.api_host` + `preflight`'s
  `public_bind_security` check (WARN any profile, FAIL under
  `--profile production`) so binding beyond loopback is never a silent
  default. Security-header middleware (CSP with no inline script/style,
  `X-Frame-Options`, etc.) applies to every response, `/v1/*` included.
- **Packaging**: `reel_harness/web/templates/`+`static/` needed an
  explicit new `[tool.hatch.build.targets.wheel] artifacts` entry --
  zero non-`.py` files existed under `reel_harness/` before this, so
  hatchling's `packages` selection alone would not have swept them in.
  Verified by an actual `uv build` + wheel-listing inspection + clean-venv
  install + browser check, not assumed. New hard dependencies: `jinja2`,
  `python-multipart`. HTMX is vendored (`static/htmx.min.js`, Zero-Clause
  BSD, no CDN reference anywhere).
- **A real FastAPI bug found and fixed mid-session**: the create-job
  route's return type (`HTMLResponse | RedirectResponse`) made FastAPI try
  to build a Pydantic response model from a `Union` of two Starlette
  response classes, crashing the app at import time
  (`FastAPIError: Invalid args for response field!`) -- fixed with
  `response_model=None` on that one route. Also found: FastAPI's
  `Form(...)` (required) rejects a genuinely-empty string field before the
  app's own validation ever runs, producing a raw JSON 422 instead of the
  intended friendly re-rendered form -- fixed by giving every string form
  field a `Form("")` default and letting `web.forms.validate_new_job_form`
  be the actual source of truth for what's acceptable.
- **Verification**: real end-to-end manual smoke test via `curl` against a
  live `serve` process (create → poll → REVIEW_REQUIRED → video Range
  request returns real 206 + correct `Content-Range` → approve →
  COMPLETED), confirmed real non-silent Demo audio in the streamed video
  (ffprobe/volumedetect). A real Playwright/Chromium browser E2E
  (`tests/e2e/test_web_ui_playwright.py`) drives the identical flow
  through an actual browser and passed. Unit (view models, labels, forms),
  route (`TestClient`, including CSRF/security-header/Range assertions),
  and integration (full lifecycle through the web routes with a real
  worker run) tests all pass; full suite green, mypy/ruff clean.

## Phase 4C — Demo Mode

Not a new content/publishing provider in the Phase 2/3 sense -- a new
`provider_id = "demo"` tier (LLM/TTS/stock-media, registered in
`providers/registry.py` parallel to `fake`/the real providers) that
produces genuinely watchable/audible output with zero external API calls
or credentials, addressing a real gap found while manually demoing the
service to the user: a `fake`-provider job renders a silent, flat-color
video that proves the pipeline mechanics work but is useless for judging
actual UX.

- **`reel_harness/providers/demo_llm.py`**: deterministic, no-network
  script generation (same non-LLM approach as `FakeLLMProvider`), but
  writes subtitle text that embeds the real topic (`"{i+1}/{n}: {topic}"`)
  so burned-in captions are meaningful.
- **`reel_harness/providers/demo_stock_media.py`**: synthetic per-scene
  images from a fixed 16-color high-contrast palette (not
  `FakeStockMediaProvider`'s raw hash-derived RGB, which frequently landed
  on visually-similar dark colors) -- reuses `fake_stock_media.py`'s
  from-scratch PNG builder. Every asset is stamped `DEMO_TEST_LICENSE`.
- **`reel_harness/providers/demo_tts.py`**: real, audible speech via
  `pyttsx3` (SAPI5 on Windows, espeak/espeak-ng on Linux) -- fully offline,
  no API key. New optional dependency (`uv sync --extra demo`), never
  required by the default/fake/real pipelines. Best-effort voice-by-language
  matching (`voice.languages`, never `voice.id` -- a Windows SAPI5 voice
  id's fixed `...\Voices\Tokens\...` registry-path segment spuriously
  contains "en", found by actually testing against real installed voices,
  not assumed). Missing engine raises `DependencyError`
  (`BLOCKED_DEPENDENCY`), mirroring ffmpeg-missing handling exactly.
- **`Settings.render_burn_subtitles`** (default `False`): a generic,
  provider-agnostic pipeline flag -- burns `Scene.subtitle` onto the video
  via ffmpeg `drawtext`. Deliberately NOT a `demo`-provider-name branch in
  `pipeline/stages.py` (`registry.py`'s own rule: vendor names never
  checked outside the registry); works with any provider combination.
  `media/ffmpeg_render.py`'s `_drawtext_filter` references the caption
  text file and font file by **bare filename only, with `cwd` set to their
  directory** -- a Windows absolute path's drive-letter colon could not be
  made to survive ffmpeg's filtergraph option-value escaping in practice
  (every backslash-escaping variant tried against a real ffmpeg build still
  terminated the option early at the colon); the relative-path/cwd approach
  was verified working by direct ffmpeg invocation before being written
  into the pipeline. `media/deps.py`'s new `resolve_font_path()` mirrors
  `check_ffmpeg_available()`'s resolution order (env var -> project-local
  `.tools/fonts/` -> platform default -> `font=sans-serif` fontconfig
  fallback, never a hard failure).
- **License gate**: `DEMO_TEST_LICENSE` added to
  `manifest.schema.NON_PUBLISHABLE_LICENSES` alongside `FAKE_TEST_LICENSE`
  -- Demo Mode assets can never pass real publish eligibility, same
  invariant as Fake provider assets (see `CLAUDE.md`).
  `reel-harness demo-run` (new CLI command): channel-create (if needed) +
  job-create + drive-to-terminal-status in one command, collapsing the
  manual create/lease/inspect loop used to first demonstrate this to the
  user.
- **CI**: `espeak-ng` installed on the Ubuntu runner (mirrors the existing
  ffmpeg apt-get step) and `uv sync --extra demo` added to the main test
  job so demo-TTS tests actually run in CI rather than always skipping;
  Windows CI already ships SAPI5.
- **A real environment bug found and fixed mid-session**: an earlier
  `uv sync ... --no-install-project` call left the editable install's
  `_editable_impl_reel_harness.pth` as an un-renamed `.pth.tmp`, breaking
  `import reel_harness` for any subprocess NOT launched through `uv run`
  (e.g. `tests/e2e/test_supervisor_subprocess_e2e.py`'s real `sys.executable`
  subprocess). Fixed by a full `uv sync --extra dev --extra demo` (no
  `--no-install-project`) followed by the documented CP949 re-encode of the
  `.pth` file (see the `venv-cp949-pth-workaround` memory / this file's
  Windows+non-ASCII-path note below) -- unrelated to Demo Mode's own code,
  but found and fixed in the course of this session's own regression run.
- **Verification**: real `demo-run` executions inspected directly --
  extracted video frames read back as images (distinct palette colors per
  scene, readable burned-in Korean captions), `ffprobe`/`volumedetect`
  confirmed genuinely non-silent audio (~-21dB mean, not the ~-91dB silence
  floor). Full suite 956 passed/1 skipped, mypy/ruff clean.

## Phase 4B — Live platform verification and v0.1.0 release

Goal was NOT new features -- verify Phase 4A's release candidate against
real platforms where possible, fix any real defects found, and cut the
final `0.1.0` release. See `CHANGELOG.md`'s `[0.1.0]`/`[0.1.0rc2]`
entries for the user-facing summary.

- **Credential check**: `~/.reel-harness/credentials/oauth_credentials/`
  is empty on this machine; `publisher-doctor --check-remote` and
  `provider-smoke publisher <platform>` for YouTube/TikTok/Instagram all
  report `NOT_CONFIGURED` / "credentials not configured". No live
  platform upload was possible or attempted.
- **Operational soak test**: 5 concurrent fake jobs through a real
  `reel-harness serve` subprocess (2 render workers, 1 publisher worker,
  60s run, graceful shutdown via `CTRL_BREAK_EVENT`/`SIGINT`, then a
  simulated restart against the same DB file) surfaced two real defects,
  both fixed and covered by regression tests:
  1. `storage-verify` flagged healthy jobs still in `CREATED`/`QUEUED`/
     `TOPIC_GENERATING`/`SCRIPT_GENERATING`/`POLICY_CHECKING` as
     `missing_directory` -- those stages legitimately have not written a
     file to disk yet (only `ASSET_FETCHING` onward does). Fixed in
     `reel_harness/ops/storage_tools.py`.
  2. A subprocess-stdout-decoding mismatch (parent decoding a
     UTF-8-forced child's output with the platform default locale
     encoding, cp949 on this machine) could raise `UnicodeDecodeError`
     under high non-ASCII log volume. Fixed in
     `tests/e2e/test_supervisor_subprocess_e2e.py`'s `subprocess.Popen`
     call (`encoding="utf-8", errors="replace"`).
- **RC2 process**: because real product code changed after `v0.1.0rc1`
  was tagged, `v0.1.0rc1`'s own verification could not be reused as-is.
  Version bumped to `0.1.0rc2`, full `release-check` re-run (PASS,
  930 passed/1 skipped, mypy/ruff clean), package rebuilt and clean-
  installed into a fresh venv (verified `--version`, `preflight
  --profile fake`, `channel-create`), release manifest regenerated,
  live-verify re-run (still `NOT_CONFIGURED` all three platforms as
  expected), tagged `v0.1.0rc2` and pushed. `v0.1.0rc1` was never moved,
  deleted, or force-updated.
- **Merge to `main`**: `phase4/release-candidate` (carrying both Phase 4A
  and the rc2 fixes) merged into `main` with `--no-ff`. Both `v0.1.0rc1`
  and `v0.1.0rc2` are reachable ancestors of the merge commit; neither
  tag moved. Full gate re-run clean on `main` post-merge.
- **Final `0.1.0` release — Path B (local-first, limitations documented)**:
  since no live platform credentials exist on this machine, YouTube/
  TikTok/Instagram live publishing remains unverified for this release.
  This is recorded explicitly (not silently) in `README.md`,
  `CHANGELOG.md`'s `[0.1.0]` entry, and the release manifest's
  `live_verification` field -- the phrase "production live publishing
  verified" is not used anywhere in this release's documentation.
  Publisher features are described as preview/credential-required.
  Version bumped to final `0.1.0`, full verification re-run, tagged
  `v0.1.0` and pushed once green on `main`.

## Phase 4A — Production release candidate (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage,
`CHANGELOG.md` for the user-facing summary). This phase does not add a new
content/publishing provider — it makes the existing pipeline genuinely
operable: diagnostics, backup/restore, a unified runtime supervisor,
metrics/incident tooling, live cross-platform verification, and a real
release process.

- **Phase 3D merged to `main`** (pre-merge gate: 794 collected/793 passed/1
  skipped, mypy+ruff clean), then `phase4/release-candidate` branched from
  the freshly-updated `main`.
- **`reel-harness preflight`** (`reel_harness/ops/preflight.py`): a single
  local-first readiness report (config, DB, storage, credential/journal
  safety, ffmpeg, runtime dependencies, the provider/publisher registry,
  worker lease/heartbeat sanity, upload chunk settings, the public-upload
  flag, API-key strength, placeholder-secret detection) with a stricter
  `--profile production` bar and an opt-in `--check-remote` that reuses the
  same real token-refresh/identity calls `publisher-doctor` already makes
  — never a second, possibly-divergent implementation.
- **`ops.fingerprint.config_fingerprint()`**: a safe, deterministic,
  non-secret configuration snapshot, logged once at every process startup
  and reused by `/status`, incident bundles, the release manifest, and
  live-verification records.
- **Database operations** (`db-status`/`db-migrate`/`db-backup`/
  `db-restore`/`db-verify`): wraps the existing idempotent additive-column
  `init_db()` with a migration lock, default safety backups, dry-run,
  SQLite-online-backup-API backups with checksummed manifests, and a
  destructive `db-restore` that refuses on a running worker, a checksum
  mismatch, or a too-new schema, always taking its own pre-restore backup
  first.
- **Storage verification and backup bundles** (`storage-verify
  [--repair-safe]`, `backup-create`/`-inspect`/`-restore`): cross-checks
  job storage against the DB (checksums, manifests, orphan directories,
  leaked temp files), and a single portable, checksummed,
  traversal/archive-bomb-hardened `tar.gz` of the DB + jobs storage +
  publish journal — deliberately never OAuth credentials.
- **Unified runtime supervisor** (`reel-harness serve`): the API and
  render/publisher workers as threads sharing one `AppContext`, with a
  documented per-component failure policy (API death is fatal to the
  whole process; a render- or publisher-worker thread dying is tracked
  without tearing down the other) and graceful, bounded-timeout shutdown.
- **`GET /status`**: version, config fingerprint, schema, uptime,
  job/publication status breakdowns, stale-lease counts, and (inside
  `serve`) live component/fatal-error state. No API key required.
- **`GET /metrics`**: dependency-free Prometheus text exposition, every
  value derived fresh from DB state at scrape time rather than an
  in-memory counter that would silently reset on restart.
- **`reel-harness incident-bundle`**: a self-secret-scanned diagnostics
  zip (preflight report, DB status, status breakdowns, recent failure
  codes, publish-journal integrity, dependency/platform versions) —
  refuses to write rather than ship anything secret-shaped.
- **`reel-harness live-verify`**: a single read-only sweep across
  YouTube/TikTok/Instagram, with a real upload test gated behind an
  explicit per-platform confirmation flag (Instagram's is deliberately the
  strongest, since it has no private-post option). An append-only
  live-verification log records every run, distinct from `Publication`.
- **Release process**: single-source PEP 440 versioning (`0.1.0rc1`,
  `reel-harness --version`), `reel-harness release-manifest` (version/
  commit/schema/checksums/known-limitations/`live_verification` status),
  `reel-harness release-check` (the pre-tag gate — git/version/lockfile/
  full-test/mypy/ruff/secret-scan/artifact-scan; never creates a commit or
  tag itself), and `CHANGELOG.md`.
- **CI additions**: a CLI `--version` smoke check and release-manifest
  validation added to `package-smoke`; a new `release-check` job
  (`--skip-slow`, since the existing test matrix already covers the full
  gate across every OS/Python combination). Schema-upgrade, backup/
  restore, and supervisor-subprocess E2Es all run automatically as part
  of the existing full-suite `test` job — no new CI step needed for those.
- **Real bugs found and fixed this session** (each via building/testing
  the feature that exposed it, not a separate audit pass):
  1. `observability.configure_logging()`'s `StreamHandler` pinned whatever
     `sys.stderr` object was live the first time logging was configured in
     a process and never rebound it — once something later replaced
     `sys.stderr` (pytest's `capsys` between tests; a real process
     redirecting/rotating its stream), every subsequent log call failed
     and Python's own logging module printed a `--- Logging error ---`
     traceback onto the *current* stderr, corrupting unrelated output.
     Fixed by re-binding the handler's stream on every `configure_logging()`
     call.
  2. `db_backup()`'s manifest hardcoded the running code's `SCHEMA_VERSION`
     constant instead of the database's own actual version — a backup of
     an old, not-yet-migrated database falsely claimed to already be
     current, which would have made `db_restore`'s "refuse a backup newer
     than supported" check meaningless. Fixed by reading the real version
     from the backup file itself.
  3. The SQLAlchemy engine's connection pool kept a real open file handle
     to the SQLite database during `db-restore`, which `os.replace()`
     silently allows swapping out from under on POSIX but Windows refuses
     outright — fixed by disposing the engine's pool immediately before
     the atomic file swap.
  4. `observability.redact()`'s "authorization" pattern used a bare `\S+`
     value charset (unlike its sibling api-key pattern), which greedily
     consumed a trailing JSON closing quote when redacted text was
     re-scanned a second time — found by `incident-bundle`'s own
     self-secret-scan re-running `redact()` over already-redacted JSON.
     Fixed by giving it the same safe charset the api-key pattern already
     used.
  5. `uv.lock` had been stale since this phase's own version bump
     (`0.1.0` → `0.1.0rc1`) — `uv lock --check` was silently failing;
     regenerated via `uv lock`.
  6. The CI secret/token grep (and `release-check`'s own copy of it)
     flagged a pre-existing, legitimate test fixture
     (`tests/unit/test_publish_journal.py`'s deliberate
     `"Bearer ya29.fake-leaked-token"` value, used to prove the journal
     *rejects* forbidden-substring content) as a possible real leak.
     Fixed by excluding `tests/` from the pattern-based scan on both
     copies.
- **Deliberate scope decisions**: `db-verify`/`storage-verify` split DB-
  internal consistency from DB-vs-disk consistency rather than
  duplicating checks in both; a credential-bundling backup command was
  deliberately not built (see `docs/OPERATIONS.md`'s "Credential backup
  policy") to avoid making it easy to accidentally archive a token
  long-term; `serve` uses threads, not subprocesses (see "Runtime
  supervisor" in `docs/OPERATIONS.md` for the full reasoning);
  `preflight --check-remote` covers publishers fully but intentionally
  does not perform a real LLM/TTS/asset generation call (that remains
  `provider-smoke`'s job, to avoid an unexpected-cost diagnostic).

Explicitly out of scope this phase: Facebook Reels publishing, automatic
public/scheduled publishing, automatic remote delete, an OAuth
account-management UI, PostgreSQL, cloud object storage/CDN, a credential-
bundling backup command, arbitrary tunneling, a web dashboard, analytics,
subtitle/thumbnail upload, and automating any platform's own app-review
process.

Suite after Phase 4A: **928 passed, 0 failed, 1 skipped** (794 → 929
collected — one net skip accounted for by the pre-existing Windows
symlink test). The skip is the same pre-existing, unrelated one from
Phase 3A–3D. mypy clean (84 source files). ruff clean (`reel_harness` +
`tests`).

Live verification: `NOT RUN — credentials not configured` for all three
publishers on this machine. `reel-harness live-verify` (read-only) and
`reel-harness preflight --check-remote` are the documented paths to run
once credentials exist. **Live external-platform verification remains not
run because production credentials and permissions were not configured.**

## Phase 3D — Instagram Reels publisher (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage,
`docs/PUBLISHING.md` for the official-doc research this is built from,
checked 2026-07-29, Graph API `v25.0`):

- **Official Meta for Developers docs researched and recorded**: Instagram
  Content Publishing API (`developers.facebook.com/docs/instagram-platform/
  content-publishing/`), the `ig-user/media` reference, and Instagram Login
  for Business/Business Login docs. `docs/PUBLISHING.md` records what's
  confirmed (account/permission model, container→publish flow, video specs,
  publishing limit) and, honestly, what the docs do **not** clearly specify
  (a complete error-code table, true multi-chunk resumability) — nothing
  guessed at and presented as confirmed.
- **Architectural finding that changed scope**: Instagram's Content
  Publishing API supports `upload_type=resumable` — a direct binary POST to
  `rupload.facebook.com`, no public URL needed — as an alternative to the
  `video_url`-hosted path the original plan assumed was required. This
  project implements **only** the resumable path, explicitly declining to
  stand up a new public HTTPS media-hosting listener for a local-first,
  single-user tool. `REEL_HARNESS_INSTAGRAM_MEDIA_URL_MODE=external_url` is
  a recognized-but-not-implemented config value that fails loudly at
  startup rather than silently falling back.
- **Instagram OAuth** (`publisher/oauth_instagram.py`,
  `publisher-auth instagram`): Instagram Login for Business (no Facebook
  Page/Business Manager dependency for this login method), PKCE + `state`,
  the same dual loopback/manual-paste flow as TikTok's, plus one extra step
  Instagram requires — a short-lived token exchanged for a long-lived
  (~60 day) token. **No separate `refresh_token` grant**: unlike
  YouTube/TikTok, `OAuthCredential.refresh_token` always stays `None` for
  Instagram; the long-lived token refreshes **itself** by presenting its
  own current value to Meta's `refresh_access_token` endpoint — a genuine,
  deliberate divergence from the established refresh pattern, documented
  and tested explicitly (including a test confirming the refresh call
  presents `access_token`, never a separate `refresh_token`, as the query
  param).
- **`InstagramPublisher` adapter** (`providers/instagram_publisher.py`):
  container creation (`media_type=REELS`, `upload_type=resumable`,
  `caption`, `share_to_feed`, cover/thumb-offset options) →
  single-whole-file resumable upload → processing-status poll →
  transparent `media_publish` the first time `FINISHED` is observed
  (never re-published on a later poll that finds `PUBLISHED`) → permalink
  fetch. `query_upload_offset` always raises `UploadSessionExpiredError`
  (no documented offset-query mechanism) but is never actually reached in
  practice: `worker.publish_runner._upload_stage`'s `bytes_uploaded==0`
  shortcut retries the **same** container directly on any interrupted
  attempt, since nothing was ever confirmed received. `build_caption`
  validates length (2200 chars) and rejects internal markers
  (job id/local path/secret/signed-URL fragments), mirroring TikTok's
  `build_post_text`. Local video-limit validation
  (`providers/instagram_media.py`): duration 3s–15min, file size ≤300MB,
  both genuinely confirmed via Meta's `ig-user/media` reference (unlike
  TikTok's unconfirmed limits).
- **Capability model**: `privacy_values={"PUBLIC"}`,
  `default_privacy="PUBLIC"` — every Instagram publish is inherently
  public, so the double-confirmation gate
  (`--confirm-public-upload --confirm-platform-options` +
  `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true`) applies unconditionally, with no
  lower-friction private option to fall back to.
- **`publisher-doctor instagram [--check-remote] [--json]`**: mirrors
  TikTok's doctor shape; since there's no refresh token, the local check
  reports `token_expiry` (warns as the ~60-day long-lived token nears
  expiry) instead; `--check-remote` adds a real self-refresh, account-
  identity fetch, and `account_eligibility_status`
  (`PASS`/`WARN`/`FAIL`/`NOT_CONFIGURED`).
- **`publish-job --provider instagram --dry-run`**: an Instagram-shaped
  preview (caption + validation, local video-limit checks,
  `expected_api_mode="FILE_UPLOAD_RESUMABLE"`) entirely local — never
  calls account-info, let alone container creation; `account_info`/
  `account_eligibility_status` explicitly reported as "not fetched."
  `--confirm-platform-options` is required (`requires_user_confirmation`).
- **`provider-smoke publisher instagram`**: read-only by default
  (credential/token-refresh/account-info/Page-linkage/publishing-limit);
  the opt-in test-Reel upload uses `--upload-public-test` (**deliberately
  not** a misleadingly-named `--upload-private-test` — Instagram has no
  private-post feature) requiring all three of `--upload-public-test
  --confirm-test-upload --confirm-public-upload --confirm-platform-options`.
  Distinct `NOT RUN` wording for missing credentials vs. missing
  application permission/account linkage.
- **Instagram reconciliation and idempotency**, reusing the existing
  provider-generic `core.publish_reconciliation`/durable-journal framework:
  one genuinely new outcome, `publishing_limit_reached`, proactively
  surfaced via `_check_instagram_eligibility_block` (mirroring TikTok's
  `_check_tiktok_app_review_block`) before any upload starts.
- **Registry wiring**: `resolve_publisher`/`provider_capabilities`/
  `publisher_snapshot` all recognize `"instagram"` — the existing
  provider-generic worker, API, and `bundle_for_publication` wiring needed
  zero Instagram-specific code beyond the adapter and registry entry.
- **Instagram contract E2E** (`tests/e2e/test_instagram_publisher_e2e.py`):
  a real ffmpeg-built `final.mp4` driven through the full publish state
  machine against the real `InstagramPublisher` adapter and a stateful
  fake Meta server (account-info/publishing-limit/container/upload/status/
  media_publish/permalink, validating the real resumable-upload wire
  contract — `Authorization: OAuth`/`offset`/`file_size` headers, not just
  returning canned responses). Covers idempotent publication creation, a
  transient upload failure that retries the SAME container (never a
  duplicate), the early `provider_video_id` closure, processing-poll →
  transparent `media_publish` → `PUBLISHED` with the real permalink, the
  full audit trail, the durable journal, and that no secret/token/local
  path ever reaches the DB, the journal, or the caption actually sent.
- **A real cross-cutting bug found and fixed by this E2E test**, in shared
  worker code used by every provider: `worker.publish_runner.
  _resolve_session_handle()` (and its duplicate in
  `core.publish_reconciliation.py`) reconstructed `UploadSessionHandle`
  **without** `provider_reference`, silently blanking
  `Publication.provider_video_id` back to `None` the next time an upload
  completed after a session was reused. Invisible for TikTok (whose
  `query_upload_offset` always raises, forcing a brand-new session on
  every resume, which always carries a fresh `provider_reference`) but
  real for Instagram, whose single-shot whole-file upload leaves
  `bytes_uploaded` at 0 through any failed attempt, reaching the
  same-session-reuse path for the first time. Fixed by carrying forward
  `provider_reference=publication.provider_video_id` on the reconstructed
  handle. Also fixed a related, previously-latent issue in the same
  function: `_chunk_size_for()` fell back to an arbitrary ~2MB default
  when no provider-specific chunk-size config existed — harmless for
  YouTube/TikTok (both always set an explicit chunk-size key) but would
  have corrupted a real Instagram upload over 2MB by splitting a
  single-shot-only upload into wrongly-sized chunk requests. Fixed by
  falling back to the actual total file size instead. Verified safe for
  YouTube/TikTok too, not just beneficial to Instagram: 184 targeted
  YouTube/TikTok tests (144 in dedicated provider test files, 40 in
  cross-provider CLI/worker tests) plus the full suite all pass unchanged.
- **YouTube/TikTok regression explicitly re-verified** after all of the
  above: 184 targeted tests (144 dedicated, 40 cross-provider), full suite
  793 passed / 1 skipped, mypy clean, production-smoke clean.

Explicitly out of scope this phase (see `docs/OPERATIONS.md`): Facebook
Reels Publisher, Facebook Login for Business, the `video_url`
(`external_url`)-hosted upload path and any public media-hosting server it
would require, automating Meta's own app-review process, automatic public
publishing, scheduled-publish automation, automatic remote post delete,
thumbnail-only upload, subtitle upload, analytics collection, comments
management, a web dashboard, PostgreSQL, a cloud queue, a forced
cloud-storage vendor, and arbitrary tunneling software.

Suite after Phase 3D: **793 passed, 0 failed, 1 skipped** (664 → 794
collected, 130 new tests). The one skip is the same pre-existing,
unrelated one from Phase 3A/3B/3C (`test_secret_store.py`'s
symlink-rejection test — this Windows machine doesn't permit symlink
creation without elevated privileges). mypy clean (71 source files). ruff
clean (`reel_harness` + `tests`).

Live smoke: `NOT RUN — credentials not configured` (no
`REEL_HARNESS_INSTAGRAM_APP_ID`/`_APP_SECRET`/`_REDIRECT_URI` set on this
machine). `publisher-doctor instagram --check-remote` and
`provider-smoke publisher instagram [--upload-public-test --confirm-test-upload
--confirm-public-upload --confirm-platform-options]` are the documented
paths to run them once credentials, Business/Creator account linkage, and
Meta app permissions exist. **Live Instagram publishing remains unverified
because credentials, permissions, account linkage, media delivery, or
explicit public-upload confirmation were not configured.**

## Phase 3C — TikTok publisher (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage,
`docs/PUBLISHING.md` for the official-doc research this is built from,
checked 2026-07-29):

- **A platform capability model** (`providers.base.PublisherCapabilities`/
  `CreatorInfo`, `providers.registry.provider_capabilities`/
  `default_platform_options`/`validate_platform_options`): what one
  publisher adapter actually supports (direct publish, upload-only,
  scheduled publish, public/unlisted privacy, comments/remix control,
  processing poll, remote delete, whether creator-info/user confirmation
  is required, the provider's own privacy vocabulary and most-restrictive
  default) — checked by the service/CLI/API layers instead of scattering
  `if provider == "..."` conditionals through domain code. YouTube and the
  fake provider were retrofitted onto the same model with no behavior
  change (verified by the full pre-existing YouTube suite passing
  unchanged).
- **TikTok OAuth**: `publisher-auth tiktok` (PKCE + `state`, `video.publish`
  scope only) supports both an operator-registered loopback redirect (bound
  to its exact registered port, unlike YouTube's any-ephemeral-port
  installed-app flow) and a manual-paste flow for the documented `https://`
  redirect_uri case — TikTok's docs don't describe a loopback exception the
  way Google's do. `OAuthCredential` gained `refresh_expires_at` (TikTok's
  refresh token itself expires, ~365 days); a refresh call rotating the
  refresh token is handled (a real behavioral difference from YouTube's,
  which never rotates). Shared PKCE/state/loopback-server code was
  extracted from `publisher.oauth_youtube` into `publisher.oauth_common`
  with zero behavior change (verified by the existing YouTube OAuth suite).
- **`TikTokPublisher` adapter** (`providers/tiktok_publisher.py`): Direct
  Post / `FILE_UPLOAD` only (`PULL_FROM_URL` needs hosting this project has
  no equivalent of — explicitly out of scope). `get_creator_info` is always
  a fresh network call, never cached; `validate_publish_options` rejects an
  unsupported privacy/interaction combination explicitly (including a
  distinct `APP_REVIEW_REQUIRED` signal for the unaudited-app case, never a
  silent fallback). `build_post_text` validates length (2200 UTF-16 units)
  and rejects disallowed internal markers (job id/local path/secret/
  signed-URL fragments) as defense in depth. TikTok's `publish_id` is known
  immediately at session-creation time (`UploadSessionHandle.
  provider_reference`), persisted onto `Publication.provider_video_id`
  right away — an even earlier crash-recovery closure than YouTube's.
  `query_upload_offset` always raises `UploadSessionExpiredError`: the
  official docs don't document a way to query a session's confirmed
  offset, so every interruption starts a **brand-new** session and
  re-uploads the entire file rather than guessing at an offset (a real
  efficiency cost, documented, not hidden). A real bug surfaced by the
  contract E2E test and fixed this session: `worker.publish_runner.
  _upload_stage` previously queried the upload offset unconditionally,
  even on a session created moments earlier in the same call — harmless
  for YouTube (a fresh-session offset query just returns 0) but actively
  wasteful for TikTok (forced an immediate, silently-discarded second
  session on every single publish attempt). Fixed by skipping the query
  entirely when nothing has been uploaded yet; verified as a genuine
  efficiency improvement for YouTube too, not just a TikTok fix (its own
  full test suite, including the contract E2E, passes unchanged).
- **Creator-info validation wired into the real publish path**, not just
  the CLI smoke command: `worker.publish_runner` re-fetches `creator_info`
  fresh and re-validates the requested privacy/platform-options against it
  immediately before creating (or re-creating) an upload session, for any
  provider whose capabilities require it — never trusting an earlier
  snapshot, never silently substituting a different option if something
  changed since the publication was created.
- **`publisher-doctor tiktok [--check-remote] [--json]`**: mirrors
  YouTube's doctor plus TikTok-specific checks (granted scope,
  refresh-token expiry) and a distinct `app_review_status` check
  (`PASS`/`APP_REVIEW_REQUIRED`/`FAIL`/`NOT_CONFIGURED`) from a live
  `creator_info` query — surfaced explicitly, never hidden inside a
  generic failure.
- **`publish-job --provider tiktok --dry-run`**: a TikTok-shaped preview
  (post text + validation, default platform_options, `FILE_UPLOAD` chunk
  plan) entirely local — never even calls `creator_info`, let alone
  publish-init; `creator_info`/`app_review_status` are explicitly reported
  as "not fetched" with a pointer to the live-check command.
  `--confirm-platform-options` is required for TikTok publications
  (`PublisherCapabilities.requires_user_confirmation`).
- **`provider-smoke publisher tiktok`**: read-only by default
  (credential/token refresh/creator_info/scope/privacy-options); the opt-in
  private upload smoke requires all three of `--upload-private-test
  --confirm-test-upload --confirm-platform-options` AND real application
  permission (confirmed via `creator_info`) — always `SELF_ONLY`, a very
  short scratch clip, comments/duet/stitch all disabled. Distinct `NOT
  RUN` wording for missing credentials vs. missing application permission.
- **TikTok reconciliation and idempotency**, reusing the existing
  provider-generic `core.publish_reconciliation`/durable-journal framework
  rather than forking a parallel one: the DB unique constraint
  (`provider`, `account_reference`, `job_id`, `final_video_checksum`) is
  already provider-scoped, so a YouTube and a TikTok publication for the
  same job/account never collide. One genuinely new outcome,
  `app_review_required`, proactively surfaces an unaudited-app block on a
  publication that's never even started uploading, via a read-only
  `creator_info` check — the rest of TikTok's needed vocabulary (session
  can't be resumed, a recovered/missing video id, credentials
  unavailable) maps onto the existing generic outcomes, whose `reasons`
  already carry the TikTok-specific detail.
- **Registry wiring**: `resolve_publisher`/`provider_capabilities`/
  `publisher_snapshot` all recognize `"tiktok"`, so the existing
  provider-generic worker (`publisher-run`), API
  (`POST /v1/jobs/{id}/publications`), and `bundle_for_publication` wiring
  needed **zero** TikTok-specific code — the capability model built in the
  first commit of this phase already generalized them.
- **TikTok contract E2E** (`tests/e2e/test_tiktok_publisher_e2e.py`): a
  real ffmpeg-built `final.mp4` driven through the full publish state
  machine against the real `TikTokPublisher` adapter and a stateful fake
  TikTok server (creator_info/init/upload/status, validating the real wire
  contract — Content-Range/Content-Length, the `{data, error}` envelope —
  not just returning canned responses). Covers idempotent publication
  creation, a transient mid-upload failure, the documented
  can't-resume-only-restart behavior (a second, distinct `publish_id`;
  the first session's partial bytes permanently abandoned), the early
  `provider_video_id` closure, real polled processing completion, the
  full audit trail, and that no secret/token/local path ever reaches the
  DB, the journal, or the post text actually sent.
- **YouTube regression explicitly re-verified** after all of the above
  (adapter tests, contract E2E, OAuth, reconciliation, retry, publisher
  worker, doctor, API, metadata fingerprint, public-upload safety gate,
  registry): 174 YouTube-relevant tests, all passing unchanged.

Explicitly out of scope this phase (see `docs/OPERATIONS.md`): Instagram/
Facebook Reels publishers, `PULL_FROM_URL`/`URL_PULL_FROM_SERVER` upload,
automating TikTok's own app-review process, automatic public publishing,
scheduled-publish automation, automatic remote post delete, thumbnail/
cover-image upload beyond the default timestamp, subtitle upload,
analytics collection, a web dashboard, PostgreSQL, a cloud secret manager,
a cloud queue, and a CLI surface for per-post platform_options overrides
(comments/duet/stitch/disclosures currently always use the safest
combination).

Suite after Phase 3C: **663 passed, 0 failed, 1 skipped** (536 → 664
collected, 128 new tests). The one skip is the same pre-existing,
unrelated one from Phase 3A/3B (`test_secret_store.py`'s symlink-rejection
test — this Windows machine doesn't permit symlink creation without
elevated privileges). mypy clean (68 source files). ruff clean
(`reel_harness` + `tests`).

Live smoke: `NOT RUN — credentials not configured` (no
`REEL_HARNESS_TIKTOK_CLIENT_KEY`/`_CLIENT_SECRET`/`_REDIRECT_URI` set on
this machine). `publisher-doctor tiktok --check-remote` and
`provider-smoke publisher tiktok [--upload-private-test --confirm-test-upload
--confirm-platform-options]` are the documented paths to run them once
credentials and TikTok app permissions exist.

## Phase 3B — YouTube production reliability (this branch)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage,
`docs/PUBLISHING.md` for the design rationale and official-doc
re-verification this is built from):

- **`publisher-doctor youtube [--check-remote] [--json]`**: a single
  local-first readiness report (DB/schema, storage, credential-store
  reachability, per-account token/refresh/invalid state, ffmpeg/ffprobe,
  worker config) with `PASS`/`WARN`/`FAIL`/`NOT_CONFIGURED` per check and
  overall. `--check-remote` additionally attempts a real token refresh and
  read-only channel-identity fetch; without credentials both print
  `NOT RUN — credentials not configured`.
- **Account operations**: `publisher-account-list/-show/-remove` (safe
  metadata only; `-remove` deletes only the local credential, never a
  remote Google revoke). `OAuthCredential` gains `created_at`/
  `last_refreshed_at`/`last_refresh_error`/`invalid`, populated by the
  token-refresh path -- a dead refresh token is now visible locally
  without a network call instead of silently failing on the next upload.
- **Durable crash-recovery reconciliation**: `publisher.journal.PublishJournal`
  (append-only, `fsync`'d, integrity-checksummed) records the moment a
  chunk-upload response reports completion -- *before* any DB mutation --
  closing the real risk of a provider-side success whose DB commit is lost
  to a crash. `core.publish_reconciliation.reconcile_publication` confirms
  any journal-recovered video id via a real read-only call before ever
  repairing the DB, and reports one of 8 outcomes; anything it cannot
  positively confirm becomes `manual_review_required`/
  `ambiguous_remote_state` rather than a guess. `publication-reconcile
  <id>|--all` (CLI) and `POST /v1/publications/{id}/reconcile` (API) expose
  it. A deterministic `metadata_fingerprint` (schema v6) confirms a
  recovered/retried publication still matches the originally intended
  upload without ever embedding an internal id in the visible title/
  description.
- **Manual retry**: `core.publish_retry.retry_publication` repositions a
  stuck publication (`FAILED`/`AUTH_REQUIRED`/`QUOTA_BLOCKED`/`RETRY_WAIT`)
  for the next worker cycle at the least-wasteful safe resume point,
  re-verifying eligibility and the metadata fingerprint first; an
  ACTIVE-looking status is refused with a pointer to reconcile first.
  `publication-retry <id> [--from-stage ...]` (CLI) and
  `POST /v1/publications/{id}/retry` (API, 409 + reasons on refusal).
- **Processing poller**: `Publication.next_poll_at`/`processing_started_at`/
  `processing_poll_count` (schema v7) let `_processing_stage` pace polls
  and enforce a local max-duration timeout (`PROCESSING_TIMEOUT`, never a
  provider-reported failure) instead of polling forever or hammering the
  provider. Before this, `PROCESSING` publications only ever advanced via
  a manual `publication-refresh` call -- there was no automatic poller.
- **Upload/processing lease-lane separation**: `lease_next_processing_publication`
  is `PROCESSING`'s own lease, entirely separate from the upload lane, so
  `publisher-run`/`-run-once --process-upload`/`--process-status` (default:
  both, alternating fairly each cycle) can never contend for the same row.
- **Two real state-graph fixes**, both found while building the above:
  `RETRY_WAIT` could not resolve to `PROCESSING` (a processing-only retry
  would have failed validation), and `PROCESSING` could not transition to
  `AUTH_REQUIRED`/`QUOTA_BLOCKED`/`RETRY_WAIT` at all -- any polling error,
  even a dropped connection, previously went straight to `FAILED` with no
  soft retry, unlike every other stage.
- **`publication-list`**: read-only, filtered (`--provider`/`--account`/
  `--status`/`--job-id`/`--created-after`/`--created-before`/
  `--failed-only`/`--processing-only`), safe fields only.
- **Crash-recovery contract E2E** (`tests/e2e/test_youtube_crash_recovery_e2e.py`):
  four scenarios against the REAL `YouTubePublisher` adapter and a real
  ffmpeg-built `final.mp4` -- provider-success-then-lost-DB-commit
  (recovered, no duplicate), session expiry mid-upload (fresh session,
  exactly one video), a lost completion response (correctly reported
  `ambiguous_remote_state`, never guessed), and a processing-worker crash
  (stale-lease recovery + a fresh worker reaching `PUBLISHED`). A fifth,
  token-refresh-failure scenario is covered at the OAuth-contract level in
  `tests/unit/test_publisher_registry.py`.
- **A real security fix found while reviewing test-skip honesty**: NTFS
  junctions (unlike symlinks) need no Developer Mode or admin privileges on
  Windows and were not caught by `FileSecretStore`'s existing symlink-only
  check (`Path.is_symlink()` does not detect a junction -- confirmed
  empirically). Fixed by also checking `FILE_ATTRIBUTE_REPARSE_POINT` at
  the namespace/file/root level; a new test creates a real junction (no
  elevation needed) and passes for real on this Windows machine, unlike the
  symlink test (which only verifies for real on Linux CI).
- **CI** (`.github/workflows/ci.yml`): Windows + Ubuntu × Python 3.11/3.12
  matrix (lockfile check, import check, mypy, ruff, full pytest, secret
  grep, tracked-artifact check), a dedicated `production-smoke` job (real
  ffmpeg), and a `package-smoke` job (build wheel/sdist, install into a
  clean venv, run from the installed package). No real credentials are
  configured anywhere in CI.
- **Packaging verified manually this session** (hatchling already the
  configured backend): `uv build` produces a wheel + sdist; installing the
  wheel into a brand-new venv and running from there (confirmed via
  `site-packages` in `__file__`, not the source tree) resolved all runtime
  dependencies, imported every key module including the YouTube adapter,
  initialized the schema (v7), and completed a full real fake-provider job
  through `RENDER`/`VALIDATE` with real ffmpeg to `REVIEW_REQUIRED`.
- **Live smoke**: `NOT RUN — credentials not configured` (no
  `REEL_HARNESS_YOUTUBE_CLIENT_ID`/`_CLIENT_SECRET` set on this machine).
  `publisher-doctor youtube --check-remote` and
  `provider-smoke publisher youtube [--upload-private-test --confirm-test-upload]`
  are the documented paths to run them once credentials exist.

Explicitly out of scope this phase (see `docs/OPERATIONS.md`): TikTok/
Instagram publishers, automatic public publishing, scheduled-publish
automation, automatic remote video delete, thumbnail/subtitle upload,
analytics collection, an OAuth account-management UI, web dashboard,
PostgreSQL, cloud secret manager, cloud queue.

Suite after Phase 3B: **535 passed, 0 failed, 1 skipped** (439 → 536
collected, 97 new tests). The one skip is the same pre-existing,
unrelated one from Phase 3A (`test_secret_store.py`'s symlink-rejection
test, skipped only because this Windows machine doesn't permit symlink
creation without elevated privileges -- its NTFS-junction counterpart,
added this phase, passes for real here instead). mypy clean (65 source
files). ruff clean (`reel_harness` + `tests`).

A real live-smoke attempt (`publisher-doctor youtube --check-remote`,
`provider-smoke publisher youtube`, and the private-upload variant --
all correctly printed `NOT RUN` with no credentials configured) also
surfaced a genuine pre-existing bug: printing "NOT RUN -- credentials
not configured" (an em dash) crashed with `UnicodeEncodeError` on this
machine's actual Windows console codepage (cp949), present since Phase
3A's `provider-smoke` and never previously hit by a real restrictive
console. Fixed by reconfiguring `sys.stdout`/`stderr` to UTF-8 with
error-tolerant encoding in `cli.main.main()`.

## Phase 3A — publisher foundation and YouTube upload (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage,
`docs/PUBLISHING.md` for the official API research this is built from):

- **Vendor-neutral `Publisher` Protocol** (`providers/base.py`):
  `validate_configuration`/`create_upload_session`/`upload_chunk`/
  `query_upload_offset`/`get_processing_status`, plus `PublicationMetadata`/
  `UploadSessionHandle`/`UploadChunkResult`/`ProcessingStatusResult`
  dataclasses. Only `providers/registry.py` and `providers/youtube_publisher.py`
  know the vendor name "youtube" exists.
- **A separate `Publication`/`PublicationAuditEvent` state machine**
  (`core/state_machine.py`), deliberately independent from `Job.status` —
  15 statuses, an explicit allowed-transition graph (eligibility failure can
  never reach `UPLOADING`; `FAILED` only leaves via a manual retry;
  `PUBLISHED`/`CANCELLED` are terminal; no re-upload of an already-uploading
  attempt), and required-field enforcement per status
  (`RETRY_WAIT` always carries `retry_target_status`/`next_retry_at`/
  `failure_code`/`failure_summary`).
- **`is_publish_eligible()` is a real enforced gate**
  (`core/publish_eligibility.py`): re-derives job status, approval,
  manifest validity, the final video's actual checksum/codec/duration/
  faststart, and every *current* asset's license/commercial-use/
  modification/attribution terms **and** its on-disk checksum, fresh from
  DB/disk every call — never trusting an earlier check or an in-memory
  value. Fails closed with a structured JSON reason list
  (`core/publish_service.PublicationService.check_eligibility`,
  `publish-job --dry-run`).
- **Deterministic, safe metadata generation** (`pipeline/publish_metadata.py`):
  title/description/tags built only from manifest fields (topic, script
  title, asset attribution) — never a local path, API key, signed URL, full
  internal job id, raw provider response, or private prompt. Privacy
  defaults to `private`; `selfDeclaredMadeForKids` is always sent
  explicitly, never omitted.
- **Public-upload safeguard**: `public` requires all four of
  `--privacy public`, `--confirm-public-upload`, job approval, and the
  `REEL_HARNESS_ALLOW_PUBLIC_UPLOAD` feature flag; `private` needs none of
  them. CI/tests/`provider-smoke` never perform a real public upload.
- **Repository-external OAuth credential storage**
  (`publisher/secret_store.py`, `publisher/credentials.py`): a
  `FileSecretStore` that rejects repo-internal directories (via
  `Path.relative_to`), symlinks, and path traversal; chmod owner-only on
  POSIX. Access/refresh tokens, the client secret, and the real resumable
  upload session URI are **never** written to the jobs DB — the DB only
  ever holds an opaque `Publication.upload_session_reference` key.
- **`publisher-auth youtube [--account ALIAS]`**: real OAuth 2.0
  installed-app flow with PKCE + `state`, loopback-only
  (`http://127.0.0.1:{port}`) callback server, single-use, timed out, never
  logs a token/code/verifier.
- **`YouTubePublisher`** (`providers/youtube_publisher.py`): the real
  official YouTube Data API v3 resumable-upload protocol over plain
  `httpx` (no Google SDK) — session creation, 256 KiB-granularity chunked
  upload, offset query + resume after interruption, completion
  verification, processing-status polling, and full upstream error
  classification (`UPSTREAM_AUTH`/`UPSTREAM_PERMISSION_DENIED`/
  `UPSTREAM_QUOTA_EXCEEDED`/`UPSTREAM_RATE_LIMITED`/`UPLOAD_SESSION_EXPIRED`/
  `UPLOAD_INTERRUPTED`/`UPLOAD_REJECTED`/`PROCESSING_FAILED`/
  `METADATA_INVALID`), each with an explicit retryable flag and never
  storing the full provider response body.
- **Idempotent publication creation**: scoped to
  (provider, account_reference, job_id, final_video_checksum) via a real DB
  `UniqueConstraint`, not just an application check — concurrent identical
  requests and the create/`IntegrityError` race both resolve to the same
  row, never a duplicate upload target.
- **A separate publish lease/lock** (`worker/publish_lease.py`) from the
  render job lease — its own `locked_by`/`heartbeat_at`/`lease_token`
  columns on `Publication`, its own stale-recovery/backoff policy
  (`PUBLICATION_RETRY_POLICY`), and `lease_specific_publication` for
  out-of-turn `publication-refresh` polling of one `PROCESSING` row.
- **`worker/publish_runner.run_publication`**: drives one leased
  publication forward (session → chunked upload → completion →
  processing poll), resuming a real interrupted upload from the provider's
  own confirmed offset (never guessing, never re-sending confirmed bytes),
  self-healing an expired session mid-upload, and landing auth/quota
  failures in dedicated `AUTH_REQUIRED`/`QUOTA_BLOCKED` states an operator
  can act on directly instead of a generic `FAILED`.
- **A separate publisher worker daemon** (`worker/publish_daemon.py`,
  `publisher-run`/`publisher-run-once`) — structurally mirrors the render
  `WorkerDaemon` but is a distinct process, never conflated with it.
- **Append-only `PublicationAuditEvent` log**: `eligibility_checked`,
  `publication_created`, `upload_session_created`, `chunk_uploaded`,
  `upload_resumed`, `upload_completed`, `processing_started`,
  `processing_completed`, `publication_failed`, `publication_cancelled` —
  chunk events store only byte ranges, never a URL or payload.
- **`publish-job <job_id> --provider youtube [--dry-run]`** and
  **`provider-smoke publisher youtube [--upload-private-test --confirm-test-upload]`**:
  dry-run reports eligibility + a metadata/config readiness preview with
  zero external requests; the smoke command defaults to a read-only channel
  identity check and only uploads a real, tiny, clearly-labeled private
  test clip with both opt-in flags. `publication-status`/
  `publication-refresh` CLI commands and the matching
  `POST /v1/jobs/{id}/publications` / `GET /v1/publications/{id}` /
  `POST /v1/publications/{id}/refresh` / `POST /v1/publications/{id}/cancel`
  API endpoints complete the surface.
- **Cancellation policy per status**: idle statuses cancel immediately;
  `UPLOADING`/`UPLOAD_PAUSED` only flag for the worker's next chunk
  boundary; `UPLOAD_COMPLETED`/`PROCESSING` cancel is purely local
  bookkeeping and never deletes anything already on YouTube; `PUBLISHED`
  cannot be cancelled. Remote video delete is explicitly **not
  implemented** this phase — documented, not silently missing.
- **Contract test suite**: unit-level `YouTubePublisher` adapter contract
  tests (`tests/unit/test_youtube_publisher.py`, all via
  `httpx.MockTransport`) plus a full resumable-upload "contract E2E"
  (`tests/e2e/test_youtube_publisher_e2e.py`) that drives a real
  ffmpeg-built `final.mp4` through the entire Publication state machine
  against the real adapter and a stateful fake YouTube server —
  real Content-Range/Content-Length wire validation, a byte-for-byte
  checksum of everything the fake server received, a transient mid-upload
  failure that resumes from the provider's own confirmed offset without
  re-sending an already-confirmed chunk, idempotency, the full audit
  trail, and real (polled) processing completion. Still contract-transport
  coverage, **not** a live YouTube upload.
- **Live smoke**: `NOT RUN — credentials not configured` (no
  `REEL_HARNESS_YOUTUBE_CLIENT_ID`/`_CLIENT_SECRET` set on this machine).
  `provider-smoke publisher youtube` is the documented path to run the
  read-only check once credentials exist; add
  `--upload-private-test --confirm-test-upload` for a real private test
  upload. **Live YouTube publishing remains unverified because OAuth
  credentials were not configured on this machine.**

Explicitly out of scope this phase (see `docs/OPERATIONS.md`): TikTok/
Instagram publishers, automatic public publishing, scheduled-publish
automation, automatic remote video delete, an OAuth account-management UI,
web dashboard, PostgreSQL, cloud secret manager, cloud queue,
auto-commenting, analytics collection, thumbnail upload, subtitle upload.

Suite after Phase 3A: **439 passed, 0 failed, 1 skipped** (284 → 439, 155
new tests). The one skip is pre-existing and unrelated to publishing
(`test_secret_store.py`'s symlink-rejection test, skipped because this
Windows machine doesn't permit symlink creation without elevated
privileges). mypy clean (62 source files). ruff clean (`reel_harness` +
`tests`).

## Phase 2D — real stock-media execution path (merged to `main`)

Implemented and tested this session (see `docs/OPERATIONS.md` for usage):

- **`PexelsStockMediaProvider`**: a real adapter for the Pexels Video API,
  chosen because it has a stable documented REST contract, returns portrait
  *video* (not just images -- the pipeline needed video), and its license
  (commercial use + modification allowed, attribution appreciated but not
  required) maps cleanly onto the manifest's license fields. Isolated behind
  the existing `StockMediaProvider` Protocol and the registry -- the vendor
  name appears nowhere else. `FakeStockMediaProvider` is unchanged and
  remains the default.
- **Asset configuration**: `REEL_HARNESS_ASSET_PROVIDER` (`fake` | `pexels`),
  `_BASE_URL`, `_API_KEY` (`SecretStr`), `_CONNECT_TIMEOUT`, `_READ_TIMEOUT`,
  `_MAX_RETRIES`, `_RETRY_BACKOFF`, `_PER_PAGE`, `_ORIENTATION`,
  `_MIN_WIDTH`/`_MIN_HEIGHT`, `_MIN_DURATION`/`_MAX_DURATION`,
  `_SAFE_SEARCH`. Same startup-validation contract as LLM/TTS: incomplete
  real-provider config fails loudly before any network call.
- **Deterministic search + selection** (`pipeline.asset_query` /
  `pipeline.asset_selection`): a sanitized, bounded query built from each
  scene's own `visual_query` (never the narration); a hard-filter +
  scoring selector (license/commercial-use/modification/resolution/duration
  gates, then aspect-ratio/resolution/duration/provider-rank scoring,
  tie-broken by provider asset id); a deterministic text-only relaxation
  ladder on empty results that never loosens a safety/license condition;
  cross-scene dedup via `exclude_provider_asset_ids`. Exhausting relaxation
  with nothing eligible raises `ASSET_NOT_FOUND`.
- **Real download validation + normalization**: streamed with a byte cap, a
  redirect limit, an https-only redirect-scheme policy, and HTML/JSON
  error-page rejection; the API key is sent only to the search API, never to
  the (separately hosted) file download. Downloaded video is validated with
  real ffprobe (resolution, duration) and normalized with real ffmpeg to
  canonical H.264/yuv420p/muted/stable-fps -- scaling to the render
  resolution still happens once, at RENDER time, exactly as it always has
  for image assets. RENDER gained a video-asset path
  (`render_scene_clip_from_video`): `-stream_loop -1` + `-shortest` loops a
  clip shorter than the narration and trims one longer than it, both in one
  ffmpeg invocation.
- **Lease-fenced atomic asset publish**: the ASSET stage was the last one
  still writing straight to its official path with no fencing at all (TTS
  and RENDER were fenced in Phase 2A/2B). It's now fenced the same way:
  search/select/download into a worker-private temp root, promote with
  `os.replace()` only after re-verifying the lease, DB writes in the same
  fenced transaction. A real second-thread lease-takeover test proves it.
- **Asset provenance history** (schema v4, additive): `Asset` is now
  append-only -- a reject/retry inserts a new `attempt_number` and flips the
  prior attempt's rows to `is_current=False` instead of deleting them.
  Rendering/resume only ever read `is_current=True` rows, so behavior is
  unchanged; only the history is now retained and auditable. Pre-v4 rows
  read as a single current attempt with no history gap.
- **Strengthened `is_publish_eligible()`**: now also requires
  `commercial_use_allowed`, `modification_allowed`, non-empty
  `attribution_text`, and passing technical validation on every asset --
  previously a non-fake license alone was enough, which never reflected
  what a real provider's terms actually require. Missing/ambiguous license
  data still fails closed.
- **`provider-smoke asset`**: opt-in, one fixed search query, real
  selection + download + validation, scratch-only storage, redacted
  summary. Exit codes: 0 success; 2 not configured; 3 auth; 4 transient;
  5 media-toolchain/validation failure; 6 no eligible candidates.
- **Safe asset metadata via CLI and API**: `job-show --json` and a new
  `GET /v1/jobs/{id}/assets` both expose provider/creator/license/
  dimensions/checksum-prefix for the job's current-attempt assets, sharing
  one `core.service.asset_safe_metadata()` function -- never a local
  filesystem path or the CDN download link.
- **Full hybrid E2E** (`tests/integration/test_hybrid_real_media_pipeline.py`):
  all three real adapters (LLM + TTS + Pexels) over contract MockTransports
  driving the real pipeline to `REVIEW_REQUIRED`, with a real ffmpeg-built
  portrait MP4 standing in for the downloaded stock clip. Proves
  `publish_eligible=true` once approved for a fully-real-metadata job
  (unlike any fake-asset job), reject-stage semantics, and provenance
  history across a reject. This is contract-transport wiring coverage, NOT
  a live provider call.
- **Live smoke**: `NOT RUN — credentials not configured` (no
  `REEL_HARNESS_ASSET_API_KEY` set on this machine). `provider-smoke asset`
  is the documented path to run it once credentials exist.

Not implemented this session (explicitly out of scope, see
`docs/OPERATIONS.md`): real publishing/OAuth, PostgreSQL, cloud storage/CDN,
web UI, smart crop, BGM, subtitle burn-in, dubbing.

Suite after Phase 2D: **284 passed, 0 failed, 0 skipped** (203 → 284,
81 new tests). mypy clean (all source files). ruff clean.

## Phase 2C — real TTS execution path + runtime dependency closure (merged to `main`)

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
