from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from reel_harness.core.state_machine import (
    InvalidTransitionError,
    MissingTransitionFieldsError,
    PublicationStatus,
    apply_publication_transition,
)


@dataclass
class FakePublication:
    status: str = PublicationStatus.CREATED.value
    retry_target_status: str | None = None
    next_retry_at: datetime | None = None
    failure_code: str | None = None
    failure_summary: str | None = None
    upload_session_reference: str | None = None


def test_allowed_transition_applies() -> None:
    pub = FakePublication(status=PublicationStatus.CREATED.value)
    apply_publication_transition(pub, PublicationStatus.ELIGIBILITY_CHECKING)
    assert pub.status == PublicationStatus.ELIGIBILITY_CHECKING.value


def test_forbidden_transition_raises_and_does_not_mutate() -> None:
    pub = FakePublication(status=PublicationStatus.CREATED.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.PUBLISHED)
    assert pub.status == PublicationStatus.CREATED.value


def test_eligibility_failure_can_never_reach_uploading_directly() -> None:
    pub = FakePublication(status=PublicationStatus.ELIGIBILITY_CHECKING.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.UPLOADING)


def test_uploading_requires_an_existing_upload_session_reference() -> None:
    pub = FakePublication(status=PublicationStatus.UPLOAD_SESSION_CREATED.value)
    with pytest.raises(MissingTransitionFieldsError):
        apply_publication_transition(pub, PublicationStatus.UPLOADING)
    apply_publication_transition(pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")
    assert pub.status == PublicationStatus.UPLOADING.value


def test_upload_session_created_requires_a_session_reference_too() -> None:
    pub = FakePublication(status=PublicationStatus.READY_TO_UPLOAD.value)
    with pytest.raises(MissingTransitionFieldsError):
        apply_publication_transition(pub, PublicationStatus.UPLOAD_SESSION_CREATED)


def test_terminal_statuses_have_no_outgoing_transitions() -> None:
    for terminal in (PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED):
        pub = FakePublication(status=terminal.value)
        with pytest.raises(InvalidTransitionError):
            apply_publication_transition(pub, PublicationStatus.READY_TO_UPLOAD)


def test_published_cannot_be_re_uploaded_in_the_same_attempt() -> None:
    pub = FakePublication(status=PublicationStatus.PUBLISHED.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")


def test_cancelled_cannot_resume_upload() -> None:
    pub = FakePublication(status=PublicationStatus.CANCELLED.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.UPLOADING, upload_session_reference="ref-1")


def test_failed_allows_only_manual_retry_wait() -> None:
    pub = FakePublication(status=PublicationStatus.FAILED.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.PUBLISHED)
    apply_publication_transition(
        pub, PublicationStatus.RETRY_WAIT,
        retry_target_status="READY_TO_UPLOAD", next_retry_at=datetime.now(UTC),
        failure_code="MANUAL_RETRY", failure_summary="operator retry",
    )
    assert pub.status == PublicationStatus.RETRY_WAIT.value


def test_retry_wait_requires_all_bookkeeping_fields() -> None:
    pub = FakePublication(status=PublicationStatus.UPLOADING.value)
    with pytest.raises(MissingTransitionFieldsError):
        apply_publication_transition(pub, PublicationStatus.RETRY_WAIT, retry_target_status="UPLOADING")
    assert pub.status == PublicationStatus.UPLOADING.value


def test_processing_completion_reaches_published_only_from_processing() -> None:
    pub = FakePublication(status=PublicationStatus.UPLOAD_COMPLETED.value)
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.PUBLISHED)
    apply_publication_transition(pub, PublicationStatus.PROCESSING)
    apply_publication_transition(pub, PublicationStatus.PUBLISHED)
    assert pub.status == PublicationStatus.PUBLISHED.value


def test_cancel_allowed_after_upload_completed_and_during_processing() -> None:
    # Local-only bookkeeping -- see docs/OPERATIONS.md: this never implies an
    # automatic remote delete, which is a separate, explicit action.
    for status in (PublicationStatus.UPLOAD_COMPLETED, PublicationStatus.PROCESSING):
        pub = FakePublication(status=status.value)
        apply_publication_transition(pub, PublicationStatus.CANCELLED)
        assert pub.status == PublicationStatus.CANCELLED.value


def test_review_required_only_leads_to_cancelled_not_a_resume() -> None:
    pub = FakePublication(status=PublicationStatus.ELIGIBILITY_CHECKING.value)
    apply_publication_transition(
        pub, PublicationStatus.REVIEW_REQUIRED,
        failure_code="PUBLISH_NOT_ELIGIBLE", failure_summary="checksum mismatch",
    )
    with pytest.raises(InvalidTransitionError):
        apply_publication_transition(pub, PublicationStatus.READY_TO_UPLOAD)
    apply_publication_transition(pub, PublicationStatus.CANCELLED)


def test_auth_required_and_quota_blocked_route_back_via_retry_wait() -> None:
    for status in (PublicationStatus.AUTH_REQUIRED, PublicationStatus.QUOTA_BLOCKED):
        pub = FakePublication(status=status.value)
        apply_publication_transition(
            pub, PublicationStatus.RETRY_WAIT,
            retry_target_status="READY_TO_UPLOAD", next_retry_at=datetime.now(UTC),
            failure_code="UPSTREAM_AUTH", failure_summary="reauth required",
        )
        assert pub.status == PublicationStatus.RETRY_WAIT.value
