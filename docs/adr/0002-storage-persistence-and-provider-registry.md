# ADR-0002: Local filesystem storage, SQLite persistence, provider registry

## Context

Three related axes needed a decision for Phase 0/1: where job artifacts live,
where job state persists, and how LLM/TTS/stock-media/publish vendors are
referenced from pipeline code.

## Decision

- **Storage**: `StorageBackend` Protocol (`reel_harness/storage/base.py`) with
  one implementation, `LocalFilesystemStorage`, which confines every read/write
  under `jobs/{job_id}/` — `job_id` must match a UUID regex and every
  `rel_path` is resolved and checked to still live under the job directory
  before any I/O happens.
- **Persistence**: SQLAlchemy models against SQLite
  (`reel_harness/db/models.py`, `schema.py`). Schema is created wholesale via
  `Base.metadata.create_all()` plus a `schema_migrations` bookkeeping table —
  not Alembic, since there is no existing data to migrate from yet.
- **Providers**: `LLMProvider` / `TTSProvider` / `StockMediaProvider` /
  `Publisher` Protocols (`reel_harness/providers/base.py`). Concrete classes
  are looked up by string key only through
  `reel_harness/providers/registry.py`. As of Phase 0/1 only `"fake"` is
  registered for each category.

## Consequences

- `LocalFilesystemStorage` rejects path traversal and non-UUID job IDs by
  construction (`tests/unit/test_storage_local.py`), not by convention.
- Swapping SQLite for Postgres later is a connection-string change plus
  introducing Alembic for the first real migration — no model rewrite, since
  nothing in `models.py` uses SQLite-only features.
- Swapping a Fake provider for a real one (Groq-like LLM, TTS API,
  Pexels-like stock media, or a publish API) means adding one class and one
  registry entry. `reel_harness/pipeline/*` and `reel_harness/worker/*` import
  only the Protocol types and never a vendor name, so they need zero changes.
- An `S3CompatibleStorage` implementation can be added the same way once
  needed — nothing currently depends on `LocalFilesystemStorage` beyond the
  `StorageBackend` Protocol.
