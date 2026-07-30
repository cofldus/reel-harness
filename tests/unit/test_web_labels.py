from __future__ import annotations

from reel_harness.core.state_machine import JobStatus, Stage
from reel_harness.web.labels import (
    JOB_STATUS_LABELS,
    NEEDS_ACTION_STATUSES,
    STAGE_LABELS,
    job_status_label,
    stage_label,
)


def test_every_job_status_has_a_label() -> None:
    for status in JobStatus:
        assert status in JOB_STATUS_LABELS
        assert JOB_STATUS_LABELS[status]


def test_every_stage_has_a_label() -> None:
    for stage in Stage:
        assert stage in STAGE_LABELS
        assert STAGE_LABELS[stage]


def test_job_status_label_falls_back_to_raw_value_for_unknown() -> None:
    assert job_status_label("SOME_FUTURE_STATUS") == "SOME_FUTURE_STATUS"


def test_stage_label_handles_none() -> None:
    assert stage_label(None) is None


def test_stage_label_falls_back_to_raw_value_for_unknown() -> None:
    assert stage_label("SOME_FUTURE_STAGE") == "SOME_FUTURE_STAGE"


def test_needs_action_statuses_are_a_subset_of_job_status() -> None:
    assert NEEDS_ACTION_STATUSES == {JobStatus.FAILED, JobStatus.REVIEW_REQUIRED, JobStatus.READY}
    assert NEEDS_ACTION_STATUSES.issubset(set(JobStatus))
