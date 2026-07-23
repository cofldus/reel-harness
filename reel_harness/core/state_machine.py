from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol


class JobStatus(StrEnum):
    CREATED = "CREATED"
    QUEUED = "QUEUED"
    TOPIC_GENERATING = "TOPIC_GENERATING"
    SCRIPT_GENERATING = "SCRIPT_GENERATING"
    POLICY_CHECKING = "POLICY_CHECKING"
    ASSET_FETCHING = "ASSET_FETCHING"
    TTS_GENERATING = "TTS_GENERATING"
    RENDERING = "RENDERING"
    VALIDATING = "VALIDATING"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"
    READY = "READY"
    PUBLISHING = "PUBLISHING"
    COMPLETED = "COMPLETED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class Stage(StrEnum):
    TOPIC = "TOPIC"
    SCRIPT = "SCRIPT"
    POLICY = "POLICY"
    ASSET = "ASSET"
    TTS = "TTS"
    RENDER = "RENDER"
    VALIDATE = "VALIDATE"
    PUBLISH = "PUBLISH"


class ReasonCode(StrEnum):
    CONTENT_POLICY_REVIEW = "CONTENT_POLICY_REVIEW"
    ASSET_NOT_FOUND = "ASSET_NOT_FOUND"
    TECHNICAL_VALIDATION_FAILED = "TECHNICAL_VALIDATION_FAILED"
    USER_APPROVAL_REQUIRED = "USER_APPROVAL_REQUIRED"
    LICENSE_INFORMATION_MISSING = "LICENSE_INFORMATION_MISSING"


TERMINAL_STATUSES = frozenset({JobStatus.COMPLETED, JobStatus.CANCELLED})

# status is the job's overall state; current_stage (tracked separately on the Job
# record) is which pipeline stage is/was executing. They are never the same field.
ALLOWED_TRANSITIONS: dict[JobStatus, set[JobStatus]] = {
    JobStatus.CREATED: {JobStatus.QUEUED, JobStatus.CANCELLED},
    JobStatus.QUEUED: {JobStatus.TOPIC_GENERATING, JobStatus.SCRIPT_GENERATING, JobStatus.CANCELLED},
    JobStatus.TOPIC_GENERATING: {
        JobStatus.SCRIPT_GENERATING, JobStatus.RETRY_WAIT, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.SCRIPT_GENERATING: {
        JobStatus.POLICY_CHECKING, JobStatus.RETRY_WAIT, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.POLICY_CHECKING: {
        JobStatus.ASSET_FETCHING, JobStatus.REVIEW_REQUIRED, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.ASSET_FETCHING: {
        JobStatus.TTS_GENERATING, JobStatus.RETRY_WAIT, JobStatus.REVIEW_REQUIRED,
        JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.TTS_GENERATING: {
        JobStatus.RENDERING, JobStatus.RETRY_WAIT, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.RENDERING: {
        JobStatus.VALIDATING, JobStatus.RETRY_WAIT, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.VALIDATING: {JobStatus.REVIEW_REQUIRED, JobStatus.FAILED, JobStatus.CANCELLED},
    JobStatus.REVIEW_REQUIRED: {JobStatus.READY, JobStatus.RETRY_WAIT, JobStatus.CANCELLED},
    JobStatus.READY: {JobStatus.PUBLISHING, JobStatus.COMPLETED},
    JobStatus.PUBLISHING: {JobStatus.COMPLETED, JobStatus.RETRY_WAIT, JobStatus.FAILED},
    JobStatus.RETRY_WAIT: {
        JobStatus.TOPIC_GENERATING, JobStatus.SCRIPT_GENERATING, JobStatus.POLICY_CHECKING,
        JobStatus.ASSET_FETCHING, JobStatus.TTS_GENERATING, JobStatus.RENDERING,
        JobStatus.VALIDATING, JobStatus.PUBLISHING, JobStatus.FAILED, JobStatus.CANCELLED,
    },
    JobStatus.FAILED: {JobStatus.RETRY_WAIT},  # operator-triggered manual retry only
    JobStatus.CANCELLED: set(),
    JobStatus.COMPLETED: set(),
}

REQUIRED_FIELDS_FOR_STATUS: dict[JobStatus, tuple[str, ...]] = {
    JobStatus.RETRY_WAIT: ("retry_target_stage", "next_retry_at", "failure_code", "failure_summary"),
    JobStatus.REVIEW_REQUIRED: ("reason_code",),
}


class InvalidTransitionError(Exception):
    pass


class MissingTransitionFieldsError(Exception):
    pass


class JobLike(Protocol):
    status: str
    retry_target_stage: str | None
    next_retry_at: datetime | None
    failure_code: str | None
    failure_summary: str | None
    reason_code: str | None


def check_transition(current_status: JobStatus, new_status: JobStatus, fields: dict) -> None:
    allowed = ALLOWED_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(f"{current_status} -> {new_status} is not an allowed transition")
    required = REQUIRED_FIELDS_FOR_STATUS.get(new_status, ())
    missing = [name for name in required if fields.get(name) is None]
    if missing:
        raise MissingTransitionFieldsError(
            f"transition to {new_status} is missing required fields: {missing}"
        )


def apply_transition(job: JobLike, new_status: JobStatus, **fields: object) -> None:
    """Validate and apply a status transition in place on `job`.

    `job` is any object with a mutable `status` attribute plus the RETRY_WAIT/
    REVIEW_REQUIRED bookkeeping attributes (a `db.models.Job` instance in practice).
    Extra keyword args are set as attributes on `job` after validation passes.
    """
    current = JobStatus(job.status)
    check_transition(current, new_status, fields)
    job.status = new_status.value
    for key, value in fields.items():
        setattr(job, key, value)
