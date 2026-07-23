from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from reel_harness.core.state_machine import (
    InvalidTransitionError,
    JobStatus,
    MissingTransitionFieldsError,
    ReasonCode,
    apply_transition,
)


@dataclass
class FakeJob:
    status: str = JobStatus.CREATED.value
    current_stage: str | None = None
    retry_target_stage: str | None = None
    next_retry_at: datetime | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    reason_code: str | None = None
    extra: dict = field(default_factory=dict)


def test_allowed_transition_applies() -> None:
    job = FakeJob(status=JobStatus.CREATED.value)
    apply_transition(job, JobStatus.QUEUED)
    assert job.status == JobStatus.QUEUED.value


def test_forbidden_transition_raises() -> None:
    job = FakeJob(status=JobStatus.CREATED.value)
    with pytest.raises(InvalidTransitionError):
        apply_transition(job, JobStatus.COMPLETED)
    assert job.status == JobStatus.CREATED.value  # rejected transition must not mutate


def test_terminal_statuses_have_no_automatic_outgoing_transitions() -> None:
    for terminal in (JobStatus.COMPLETED, JobStatus.CANCELLED):
        job = FakeJob(status=terminal.value)
        with pytest.raises(InvalidTransitionError):
            apply_transition(job, JobStatus.QUEUED)


def test_failed_allows_only_manual_retry_wait() -> None:
    job = FakeJob(status=JobStatus.FAILED.value)
    with pytest.raises(InvalidTransitionError):
        apply_transition(job, JobStatus.COMPLETED)
    apply_transition(
        job, JobStatus.RETRY_WAIT,
        retry_target_stage="SCRIPT",
        next_retry_at=datetime.now(UTC),
        failure_code="MANUAL_RETRY",
        failure_summary="operator retry",
    )
    assert job.status == JobStatus.RETRY_WAIT.value


def test_retry_wait_requires_all_bookkeeping_fields() -> None:
    job = FakeJob(status=JobStatus.SCRIPT_GENERATING.value)
    with pytest.raises(MissingTransitionFieldsError):
        apply_transition(job, JobStatus.RETRY_WAIT, retry_target_stage="SCRIPT")
    assert job.status == JobStatus.SCRIPT_GENERATING.value


def test_review_required_requires_reason_code() -> None:
    job = FakeJob(status=JobStatus.POLICY_CHECKING.value)
    with pytest.raises(MissingTransitionFieldsError):
        apply_transition(job, JobStatus.REVIEW_REQUIRED)
    apply_transition(job, JobStatus.REVIEW_REQUIRED, reason_code=ReasonCode.CONTENT_POLICY_REVIEW.value)
    assert job.status == JobStatus.REVIEW_REQUIRED.value


def test_apply_transition_never_touches_current_stage() -> None:
    # current_stage is set separately by the worker, not by the state machine --
    # a job stuck mid-RETRY_WAIT keeps its current_stage while status changes.
    job = FakeJob(status=JobStatus.RENDERING.value, current_stage="RENDER")
    apply_transition(job, JobStatus.VALIDATING)
    assert job.current_stage == "RENDER"
