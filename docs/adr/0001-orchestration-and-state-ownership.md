# ADR-0001: No n8n in MVP — application DB owns job state

## Context

The reference pipeline this project supersedes used n8n as the scheduler and
orchestrator, calling a Flask endpoint synchronously (2-5 minute blocking HTTP
request per video). That coupled "what stage is this job in" to n8n's
execution log, with no queue, no retry/backoff model, no idempotency, and no
way to test the pipeline without the n8n container running.

## Decision

n8n is excluded from Phase 0/1 entirely. The application's own SQLite
database is the single source of truth for job state, via an explicit state
machine (`reel_harness/core/state_machine.py`). A single in-process polling
worker (`reel_harness/worker/`) advances jobs; the API only ever enqueues and
returns `job_id` immediately.

If external scheduling/notification is wanted later, it is added as a client
of the application's own idempotent job-creation API (an n8n workflow calling
`POST /v1/jobs`), never as the owner of job state.

## Consequences

- No n8n container, admin UI, or webhook surface to secure in Phase 0/1.
- All state-machine logic is unit-testable without any external service
  running (see `tests/unit/test_state_machine.py`,
  `tests/integration/test_worker_lease.py`).
- Scheduling/cron and human notification (Slack/email) are not implemented —
  deferred to whenever they're actually needed, at which point they can be
  bolted on as a thin client without touching job-state code.
