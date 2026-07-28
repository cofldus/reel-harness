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
