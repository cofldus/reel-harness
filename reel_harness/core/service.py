from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from reel_harness.core.state_machine import JobStatus, Stage, apply_transition
from reel_harness.db.models import ApprovalDecision, Channel, Job
from reel_harness.manifest.schema import ApprovalInfo, Manifest
from reel_harness.manifest.writer import write_manifest
from reel_harness.storage.base import StorageBackend


class JobNotFoundError(Exception):
    pass


class InvalidActionError(Exception):
    pass


class JobService:
    """Used by both the CLI and the FastAPI layer so job lifecycle rules live in
    exactly one place. Each method opens and closes its own session -- callers
    never see a live SQLAlchemy session, only detached ORM objects."""

    def __init__(self, session_factory, storage: StorageBackend | None = None) -> None:
        self._session_factory = session_factory
        self._storage = storage

    def _record_approval_in_manifest(self, job_id: str, decision: str, decided_at: datetime) -> None:
        """Best-effort: if manifest.json exists for this job (it does once a
        real RENDER/VALIDATE pass has completed), stamp the approval decision
        and timestamp into it so the manifest -- not just the DB -- carries the
        approval record. Silently does nothing if no manifest exists yet
        (e.g. RENDERING is blocked and REVIEW_REQUIRED was reached by another
        path) or no storage backend was configured.
        """
        if self._storage is None or not self._storage.exists(job_id, "manifest.json"):
            return
        raw = self._storage.read_bytes(job_id, "manifest.json")
        manifest = Manifest.model_validate_json(raw)
        manifest.approval = ApprovalInfo(decision=decision, decided_at=decided_at)
        write_manifest(self._storage, job_id, manifest)

    def create_channel(
        self, name: str, niche: str, language: str, style_preset: dict | None = None, auto_approve: bool = False,
    ) -> Channel:
        with self._session_factory() as session:
            channel = Channel(
                name=name, niche=niche, language=language,
                style_preset=style_preset or {}, auto_approve=auto_approve,
            )
            session.add(channel)
            session.commit()
            session.refresh(channel)
            session.expunge(channel)
            return channel

    def get_channel(self, channel_id: str) -> Channel:
        with self._session_factory() as session:
            channel = session.get(Channel, channel_id)
            if channel is None:
                raise JobNotFoundError(channel_id)
            session.expunge(channel)
            return channel

    def create_job(self, channel_id: str, idempotency_key: str, topic: str | None = None) -> tuple[Job, bool]:
        """Returns (job, idempotent_replay). idempotent_replay is True if an
        existing job with the same (channel_id, idempotency_key) was found instead
        of creating a new one -- including the race where two callers hit the
        unique constraint at the same time."""
        with self._session_factory() as session:
            existing = session.execute(
                select(Job).where(Job.channel_id == channel_id, Job.idempotency_key == idempotency_key),
            ).scalar_one_or_none()
            if existing is not None:
                session.expunge(existing)
                return existing, True

            job = Job(
                channel_id=channel_id, idempotency_key=idempotency_key,
                topic=topic, status=JobStatus.CREATED.value,
            )
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.execute(
                    select(Job).where(Job.channel_id == channel_id, Job.idempotency_key == idempotency_key),
                ).scalar_one()
                session.expunge(existing)
                return existing, True

            apply_transition(job, JobStatus.QUEUED)
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job, False

    def get_job(self, job_id: str) -> Job:
        with self._session_factory() as session:
            job = session.get(Job, job_id)
            if job is None:
                raise JobNotFoundError(job_id)
            session.expunge(job)
            return job

    def list_jobs(self, status: str | None = None) -> list[Job]:
        with self._session_factory() as session:
            stmt = select(Job)
            if status:
                stmt = stmt.where(Job.status == status)
            jobs = list(session.execute(stmt.order_by(Job.created_at.desc())).scalars().all())
            for job in jobs:
                session.expunge(job)
            return jobs

    def request_cancel(self, job_id: str) -> Job:
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status in {JobStatus.COMPLETED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value}:
                raise InvalidActionError(f"cannot cancel a job in terminal status {job.status}")
            job.cancel_requested = True
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def approve(self, job_id: str) -> Job:
        decided_at = datetime.now(UTC)
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != JobStatus.REVIEW_REQUIRED.value:
                raise InvalidActionError(f"job is not awaiting review (status={job.status})")
            session.add(ApprovalDecision(job_id=job.id, decision="approve", decided_at=decided_at))
            apply_transition(job, JobStatus.READY)
            apply_transition(job, JobStatus.COMPLETED)
            session.commit()
            session.refresh(job)
            session.expunge(job)
        self._record_approval_in_manifest(job_id, decision="approve", decided_at=decided_at)
        return job

    def reject(self, job_id: str, reason: str, regenerate_from_stage: str) -> Job:
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != JobStatus.REVIEW_REQUIRED.value:
                raise InvalidActionError(f"job is not awaiting review (status={job.status})")
            try:
                Stage(regenerate_from_stage)
            except ValueError as exc:
                raise InvalidActionError(f"unknown stage: {regenerate_from_stage!r}") from exc

            session.add(
                ApprovalDecision(
                    job_id=job.id, decision="reject", reason=reason, regenerate_from_stage=regenerate_from_stage,
                ),
            )
            job.attempt_number += 1
            job.retry_count = 0
            apply_transition(
                job, JobStatus.RETRY_WAIT,
                retry_target_stage=regenerate_from_stage,
                next_retry_at=datetime.now(UTC),
                failure_code="USER_REJECTED",
                failure_summary=reason[:500],
            )
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    def retry_from_stage(self, job_id: str, stage: str) -> Job:
        """Operator override for a FAILED job -- not part of the automatic retry
        path, which only ever resumes via RETRY_WAIT set by the worker itself."""
        with self._session_factory() as session:
            job = self._require_job(session, job_id)
            if job.status != JobStatus.FAILED.value:
                raise InvalidActionError(f"can only manually retry a FAILED job (status={job.status})")
            try:
                Stage(stage)
            except ValueError as exc:
                raise InvalidActionError(f"unknown stage: {stage!r}") from exc

            job.retry_count = 0
            apply_transition(
                job, JobStatus.RETRY_WAIT,
                retry_target_stage=stage,
                next_retry_at=datetime.now(UTC),
                failure_code="MANUAL_RETRY",
                failure_summary=f"manually retried from {stage} by operator",
            )
            session.commit()
            session.refresh(job)
            session.expunge(job)
            return job

    @staticmethod
    def _require_job(session, job_id: str) -> Job:
        job = session.get(Job, job_id)
        if job is None:
            raise JobNotFoundError(job_id)
        return job
