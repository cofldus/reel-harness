from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime, ForeignKey, TypeDecorator, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class UTCDateTime(TypeDecorator):
    """Single datetime policy for the whole schema: store naive UTC in the DB,
    hand back timezone-aware UTC to Python.

    SQLite has no timezone type -- SQLAlchemy silently drops tzinfo on write, so
    a value read back in a *new* session is naive while freshly-computed
    `datetime.now(UTC)` values are aware, and comparing the two raises
    TypeError. Normalizing at this bind/result boundary means every datetime an
    application sees is aware UTC, regardless of which session loaded it.
    Storage format is unchanged (naive UTC), so existing rows stay readable.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value  # naive values are by convention already UTC

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


def _now() -> datetime:
    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


class Channel(Base):
    __tablename__ = "channels"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    name: Mapped[str]
    niche: Mapped[str]
    language: Mapped[str]
    style_preset: Mapped[dict] = mapped_column(JSON, default=dict)
    auto_approve: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    jobs: Mapped[list[Job]] = relationship(back_populates="channel")


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("channel_id", "idempotency_key", name="uq_job_channel_idempotency"),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    channel_id: Mapped[str] = mapped_column(ForeignKey("channels.id"))
    idempotency_key: Mapped[str]
    topic: Mapped[str | None] = mapped_column(default=None)
    script: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Provider snapshot captured at job creation (provider id, model, safe
    # base-URL host, prompt version, sampling params -- NEVER the API key).
    # Retries and resumes follow this snapshot, so changing environment
    # variables mid-flight cannot silently switch an existing job's provider.
    provider_config: Mapped[dict | None] = mapped_column(JSON, default=None)

    status: Mapped[str] = mapped_column(default="CREATED")
    current_stage: Mapped[str | None] = mapped_column(default=None)
    attempt_number: Mapped[int] = mapped_column(default=1)
    retry_count: Mapped[int] = mapped_column(default=0)

    retry_target_stage: Mapped[str | None] = mapped_column(default=None)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    failure_code: Mapped[str | None] = mapped_column(default=None)
    failure_summary: Mapped[str | None] = mapped_column(default=None)
    reason_code: Mapped[str | None] = mapped_column(default=None)

    cancel_requested: Mapped[bool] = mapped_column(default=False)
    parent_job_id: Mapped[str | None] = mapped_column(default=None)

    locked_by: Mapped[str | None] = mapped_column(default=None)
    # Rotated on every lease acquisition and cleared by stale recovery: the
    # fencing token. Stage results/status transitions only commit through a
    # guarded UPDATE that matches this value, so a worker whose lease was
    # reclaimed can never write results over the new owner's.
    lease_token: Mapped[str | None] = mapped_column(default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)

    channel: Mapped[Channel] = relationship(back_populates="jobs")
    stage_runs: Mapped[list[StageRun]] = relationship(back_populates="job")
    assets: Mapped[list[Asset]] = relationship(back_populates="job")


class StageRun(Base):
    __tablename__ = "stage_runs"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    stage: Mapped[str]
    attempt: Mapped[int]
    status: Mapped[str]
    error_detail: Mapped[str | None] = mapped_column(default=None)
    started_at: Mapped[datetime] = mapped_column(default=_now)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)

    job: Mapped[Job] = relationship(back_populates="stage_runs")


class Asset(Base):
    """Append-only: a reject/retry of the ASSET stage never deletes a prior
    attempt's rows, it inserts a new attempt and flips is_current on the old
    ones to False (see worker.runner and ADR/docs/OPERATIONS.md). Rendering
    and resume always query is_current=True rows only; every earlier attempt
    stays in the table for audit."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    scene_index: Mapped[int]
    source_provider: Mapped[str]
    source_url: Mapped[str | None] = mapped_column(default=None)
    author: Mapped[str | None] = mapped_column(default=None)
    license_type: Mapped[str | None] = mapped_column(default=None)
    local_path: Mapped[str]
    checksum_sha256: Mapped[str]
    mime_type: Mapped[str]
    downloaded_at: Mapped[datetime] = mapped_column(default=_now)

    # Phase 2D: provenance/license metadata (v4, additive) and append-only
    # history bookkeeping. attempt_number/is_current default to (1, True) so
    # pre-v4 rows read as a single current attempt with no history gap.
    attempt_number: Mapped[int] = mapped_column(default=1)
    is_current: Mapped[bool] = mapped_column(default=True)
    provider_asset_id: Mapped[str | None] = mapped_column(default=None)
    query_text: Mapped[str | None] = mapped_column(default=None)
    selection_score: Mapped[float | None] = mapped_column(default=None)
    source_page_url: Mapped[str | None] = mapped_column(default=None)
    creator_url: Mapped[str | None] = mapped_column(default=None)
    commercial_use_allowed: Mapped[bool | None] = mapped_column(default=None)
    modification_allowed: Mapped[bool | None] = mapped_column(default=None)
    attribution_text: Mapped[str | None] = mapped_column(default=None)
    width: Mapped[int | None] = mapped_column(default=None)
    height: Mapped[int | None] = mapped_column(default=None)
    duration_sec: Mapped[float | None] = mapped_column(default=None)
    fps: Mapped[float | None] = mapped_column(default=None)
    request_id: Mapped[str | None] = mapped_column(default=None)

    job: Mapped[Job] = relationship(back_populates="assets")


class ApprovalDecision(Base):
    __tablename__ = "approval_decisions"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    decision: Mapped[str]
    reason: Mapped[str | None] = mapped_column(default=None)
    regenerate_from_stage: Mapped[str | None] = mapped_column(default=None)
    decided_at: Mapped[datetime] = mapped_column(default=_now)


class Publication(Base):
    """One upload/publish attempt of a job's approved final video to one
    provider account. Deliberately separate from Job (see
    core.state_machine's PublicationStatus docstring and
    docs/PUBLISHING.md) -- a job finishing render is not the same fact as a
    video reaching a platform.

    Idempotency (Phase 3A design, see docs/PUBLISHING.md): the DB unique
    constraint on (provider, account_reference, job_id, final_video_checksum)
    is the actual duplicate-upload guard, not an application-level check --
    a second concurrent create_publication() call for the same tuple hits
    the constraint and the service returns the existing row. If the job's
    final video changes (new checksum, e.g. after a reject/re-render), that
    is a genuinely different tuple and a new Publication is allowed.

    Never stores: OAuth access/refresh tokens, client secret, the raw
    resumable-upload session URI, or any provider response body --
    upload_session_reference is an opaque local reference into the secret
    backend (see publisher.secret_store), never the URI itself.
    """

    __tablename__ = "publications"
    __table_args__ = (
        UniqueConstraint(
            "provider", "account_reference", "job_id", "final_video_checksum",
            name="uq_publication_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"))
    provider: Mapped[str]
    account_reference: Mapped[str]
    status: Mapped[str] = mapped_column(default="CREATED")
    privacy_status: Mapped[str] = mapped_column(default="private")
    provider_video_id: Mapped[str | None] = mapped_column(default=None)
    publication_url: Mapped[str | None] = mapped_column(default=None)
    idempotency_key: Mapped[str]
    final_video_checksum: Mapped[str]

    # Publisher snapshot captured at creation (provider id, adapter version,
    # safe account reference, default privacy/category/madeForKids policy,
    # template versions -- never a credential). Mirrors Job.provider_config.
    publisher_config: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Deterministic upload metadata actually sent (title/description/tags/
    # category/privacy/madeForKids) -- see providers.base.PublicationMetadata.
    metadata_snapshot: Mapped[dict | None] = mapped_column(JSON, default=None)
    # Deterministic hash of (provider, account, job, checksum, metadata_snapshot)
    # -- see pipeline.publish_metadata.metadata_fingerprint. Lets
    # core.publish_reconciliation confirm a recovered/retried upload still
    # matches the intended one, without ever embedding an internal id in the
    # user-visible title/description.
    metadata_fingerprint: Mapped[str | None] = mapped_column(default=None)

    upload_session_reference: Mapped[str | None] = mapped_column(default=None)
    bytes_uploaded: Mapped[int] = mapped_column(default=0)
    total_bytes: Mapped[int | None] = mapped_column(default=None)

    retry_count: Mapped[int] = mapped_column(default=0)
    retry_target_status: Mapped[str | None] = mapped_column(default=None)
    next_retry_at: Mapped[datetime | None] = mapped_column(default=None)
    failure_code: Mapped[str | None] = mapped_column(default=None)
    failure_summary: Mapped[str | None] = mapped_column(default=None)
    provider_request_id: Mapped[str | None] = mapped_column(default=None)

    cancel_requested: Mapped[bool] = mapped_column(default=False)

    locked_by: Mapped[str | None] = mapped_column(default=None)
    lease_token: Mapped[str | None] = mapped_column(default=None)
    heartbeat_at: Mapped[datetime | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)
    published_at: Mapped[datetime | None] = mapped_column(default=None)
    processing_completed_at: Mapped[datetime | None] = mapped_column(default=None)

    job: Mapped[Job] = relationship()
    audit_events: Mapped[list[PublicationAuditEvent]] = relationship(back_populates="publication")


class PublicationAuditEvent(Base):
    """Append-only event log for one Publication (eligibility_checked,
    publication_created, auth_refreshed, upload_session_created,
    chunk_uploaded, upload_resumed, upload_completed, processing_started,
    processing_completed, publication_failed, publication_cancelled,
    privacy_selected, publication_reconciled, publication_retried -- see
    worker.publish_runner, core.publish_reconciliation, and
    core.publish_retry). `detail` carries only
    safe structured fields (byte ranges, safe session-reference prefixes,
    status codes) -- never a full URL, request/response body, or secret;
    callers are responsible for redacting before constructing it, the same
    discipline observability.log_stage_event already documents."""

    __tablename__ = "publication_audit_events"

    id: Mapped[str] = mapped_column(primary_key=True, default=new_uuid)
    publication_id: Mapped[str] = mapped_column(ForeignKey("publications.id"))
    event: Mapped[str]
    detail: Mapped[dict | None] = mapped_column(JSON, default=None)
    created_at: Mapped[datetime] = mapped_column(default=_now)

    publication: Mapped[Publication] = relationship(back_populates="audit_events")
