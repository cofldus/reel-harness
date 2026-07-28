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


# Stages a user-facing reject/retry command may name as the resume target.
# TOPIC is excluded (regenerating a topic is meaningless for user-supplied
# topics; SCRIPT regeneration covers auto-topic jobs) and PUBLISH is excluded
# (no publish pipeline exists yet). The worker's own automatic RETRY_WAIT
# bookkeeping is not restricted by this set.
RESUMABLE_STAGES: frozenset[Stage] = frozenset({
    Stage.SCRIPT, Stage.POLICY, Stage.ASSET, Stage.TTS, Stage.RENDER, Stage.VALIDATE,
})


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


# --- Publication state machine (Phase 3A) --------------------------------
# A Publication tracks *upload/publish* progress and is deliberately a
# separate entity/state machine from Job (see docs/PUBLISHING.md): a job's
# render pipeline finishing (JobStatus.COMPLETED) is not the same fact as a
# video actually reaching YouTube, and conflating the two would make it
# impossible to represent "render done, upload still in progress" or retry
# just the upload without re-touching the render.

class PublicationStatus(StrEnum):
    CREATED = "CREATED"
    ELIGIBILITY_CHECKING = "ELIGIBILITY_CHECKING"
    READY_TO_UPLOAD = "READY_TO_UPLOAD"
    UPLOAD_SESSION_CREATED = "UPLOAD_SESSION_CREATED"
    UPLOADING = "UPLOADING"
    UPLOAD_PAUSED = "UPLOAD_PAUSED"
    UPLOAD_COMPLETED = "UPLOAD_COMPLETED"
    PROCESSING = "PROCESSING"
    PUBLISHED = "PUBLISHED"
    RETRY_WAIT = "RETRY_WAIT"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    QUOTA_BLOCKED = "QUOTA_BLOCKED"
    REVIEW_REQUIRED = "REVIEW_REQUIRED"


PUBLICATION_TERMINAL_STATUSES = frozenset({PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED})

# Statuses a publication worker can hold a lease against -- mirrors
# worker.policy.ACTIVE_STAGE_STATUSES for stale-lease recovery.
PUBLICATION_ACTIVE_STATUSES: frozenset[PublicationStatus] = frozenset({
    PublicationStatus.ELIGIBILITY_CHECKING, PublicationStatus.UPLOAD_SESSION_CREATED,
    PublicationStatus.UPLOADING, PublicationStatus.PROCESSING,
})

# Deliberately encodes every forbidden transition called out in
# docs/PUBLISHING.md / the Phase 3A design: eligibility failure can never
# reach UPLOADING; FAILED only ever reaches RETRY_WAIT (operator-triggered,
# mirroring Job.FAILED); CANCELLED and PUBLISHED are terminal (no re-upload
# of the same attempt, no upload resume after cancel); UPLOADING is only
# reachable from a state that already required upload_session_reference to
# be set (UPLOAD_SESSION_CREATED, UPLOAD_PAUSED, or a RETRY_WAIT resume that
# targeted UPLOADING with the session still valid).
ALLOWED_PUBLICATION_TRANSITIONS: dict[PublicationStatus, set[PublicationStatus]] = {
    PublicationStatus.CREATED: {PublicationStatus.ELIGIBILITY_CHECKING, PublicationStatus.CANCELLED},
    PublicationStatus.ELIGIBILITY_CHECKING: {
        PublicationStatus.READY_TO_UPLOAD, PublicationStatus.REVIEW_REQUIRED,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.READY_TO_UPLOAD: {
        PublicationStatus.UPLOAD_SESSION_CREATED, PublicationStatus.AUTH_REQUIRED,
        PublicationStatus.QUOTA_BLOCKED, PublicationStatus.RETRY_WAIT,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.UPLOAD_SESSION_CREATED: {
        PublicationStatus.UPLOADING, PublicationStatus.RETRY_WAIT,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.UPLOADING: {
        PublicationStatus.UPLOAD_PAUSED, PublicationStatus.UPLOAD_COMPLETED,
        PublicationStatus.AUTH_REQUIRED, PublicationStatus.QUOTA_BLOCKED,
        PublicationStatus.RETRY_WAIT, PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.UPLOAD_PAUSED: {
        PublicationStatus.UPLOADING, PublicationStatus.RETRY_WAIT,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    # No CANCELLED->remote-delete implied here: local status only. See
    # docs/OPERATIONS.md's cancellation policy -- remote deletion always
    # requires a separate, explicit command.
    PublicationStatus.UPLOAD_COMPLETED: {
        PublicationStatus.PROCESSING, PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.PROCESSING: {
        PublicationStatus.PUBLISHED, PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.PUBLISHED: set(),
    PublicationStatus.RETRY_WAIT: {
        PublicationStatus.ELIGIBILITY_CHECKING, PublicationStatus.READY_TO_UPLOAD,
        PublicationStatus.UPLOAD_SESSION_CREATED, PublicationStatus.UPLOADING,
        # PROCESSING: a publication-retry --from-stage PROCESSING resume --
        # the upload already completed (a provider_video_id is known) and
        # only the processing-status poll needs to happen again, e.g. after
        # a quota/auth error specifically during that poll (Phase 3B).
        PublicationStatus.PROCESSING,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.AUTH_REQUIRED: {
        PublicationStatus.READY_TO_UPLOAD, PublicationStatus.RETRY_WAIT,
        PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    PublicationStatus.QUOTA_BLOCKED: {
        PublicationStatus.RETRY_WAIT, PublicationStatus.FAILED, PublicationStatus.CANCELLED,
    },
    # Eligibility problems mean the underlying job/asset data needs fixing,
    # not a resume of this attempt -- a fixed job gets a new Publication.
    PublicationStatus.REVIEW_REQUIRED: {PublicationStatus.CANCELLED},
    PublicationStatus.FAILED: {PublicationStatus.RETRY_WAIT},  # operator-triggered manual retry only
    PublicationStatus.CANCELLED: set(),
}

REQUIRED_FIELDS_FOR_PUBLICATION_STATUS: dict[PublicationStatus, tuple[str, ...]] = {
    PublicationStatus.RETRY_WAIT: ("retry_target_status", "next_retry_at", "failure_code", "failure_summary"),
    PublicationStatus.REVIEW_REQUIRED: ("failure_code", "failure_summary"),
    PublicationStatus.FAILED: ("failure_code", "failure_summary"),
    # UPLOADING must never be reached without a session already on record --
    # this is the concrete enforcement of "upload session 없이 UPLOADING 금지".
    PublicationStatus.UPLOADING: ("upload_session_reference",),
    PublicationStatus.UPLOAD_SESSION_CREATED: ("upload_session_reference",),
}


class PublicationLike(Protocol):
    status: str
    retry_target_status: str | None
    next_retry_at: datetime | None
    failure_code: str | None
    failure_summary: str | None
    upload_session_reference: str | None


def check_publication_transition(
    current_status: PublicationStatus, new_status: PublicationStatus, fields: dict,
) -> None:
    allowed = ALLOWED_PUBLICATION_TRANSITIONS.get(current_status, set())
    if new_status not in allowed:
        raise InvalidTransitionError(f"{current_status} -> {new_status} is not an allowed transition")
    required = REQUIRED_FIELDS_FOR_PUBLICATION_STATUS.get(new_status, ())
    missing = [name for name in required if fields.get(name) is None]
    if missing:
        raise MissingTransitionFieldsError(
            f"transition to {new_status} is missing required fields: {missing}"
        )


def apply_publication_transition(
    publication: PublicationLike, new_status: PublicationStatus, **fields: object,
) -> None:
    """Validate and apply a status transition in place on `publication` (a
    `db.models.Publication` instance in practice). Mirrors apply_transition
    above; kept as a separate function (not a generic over both enums)
    because the two state machines' allowed-transition graphs and required-
    field sets are intentionally independent."""
    current = PublicationStatus(publication.status)
    check_publication_transition(current, new_status, fields)
    publication.status = new_status.value
    for key, value in fields.items():
        setattr(publication, key, value)
