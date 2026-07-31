from __future__ import annotations

import dataclasses

from reel_harness.config import Settings
from reel_harness.core.publish_retry import _RETRYABLE_STATUSES
from reel_harness.core.publish_service import PublicationInvalidActionError, PublicationService
from reel_harness.core.state_machine import PUBLICATION_TERMINAL_STATUSES, PublicationStatus
from reel_harness.db.models import Publication
from reel_harness.publisher.credentials import InMemoryCredentialBackend, OAuthCredential
from reel_harness.web.publication_view_models import (
    PublicationDetailView,
    build_publication_detail_view,
    build_publication_summary_view,
    build_publish_setup_view,
    build_publisher_account_view,
)
from reel_harness.worker.publish_lease import lease_specific_publication


def _make_publication(session_factory, job_id: str = "job-1", status: str = "READY_TO_UPLOAD", **extra) -> str:
    with session_factory() as session:
        pub = Publication(
            job_id=job_id, provider="youtube", account_reference="acct-1",
            status=status, privacy_status="private",
            idempotency_key=f"idem-{job_id}-{status}", final_video_checksum="checksum-1",
            **extra,
        )
        session.add(pub)
        session.commit()
        session.refresh(pub)
        return pub.id


def _refetch(session_factory, pub_id: str) -> Publication:
    with session_factory() as session:
        pub = session.get(Publication, pub_id)
        session.expunge(pub)
        return pub


def test_publication_detail_view_labels_and_provider_display(session_factory) -> None:
    pub_id = _make_publication(session_factory, status="UPLOADING")
    view = build_publication_detail_view(_refetch(session_factory, pub_id))
    assert view.status_label == "업로드 중"
    assert view.provider_display == "YouTube"
    assert view.privacy_status_label == "비공개"
    assert view.is_terminal is False


def test_publication_summary_view_carries_job_topic_and_detail_url(session_factory) -> None:
    pub_id = _make_publication(session_factory)
    view = build_publication_summary_view(_refetch(session_factory, pub_id), job_topic="내 영상 주제")
    assert view.job_topic == "내 영상 주제"
    assert view.detail_url == f"/publications/{pub_id}"


def test_can_cancel_matches_cancel_publications_real_precondition(session_factory, storage) -> None:
    """Mirrors PublicationService.cancel_publication's own raised-error
    condition exactly (blocks PUBLISHED/CANCELLED/FAILED). A mismatch here
    would hide a valid Cancel button, show one that 409s, or -- as found
    while writing this test -- show one that crashes with a raw
    InvalidTransitionError (FAILED -> CANCELLED is not an allowed state
    machine transition; cancel_publication now refuses FAILED explicitly
    instead of attempting it)."""
    service = PublicationService(session_factory, storage)

    for blocked_status in ("PUBLISHED", "CANCELLED", "FAILED"):
        pub_id = _make_publication(session_factory, job_id=f"job-{blocked_status}", status=blocked_status)
        view = build_publication_detail_view(_refetch(session_factory, pub_id))
        assert view.can_cancel is False, blocked_status
        try:
            service.cancel_publication(pub_id)
        except PublicationInvalidActionError:
            pass
        else:
            raise AssertionError(f"expected cancel_publication to refuse status {blocked_status}")

    for allowed_status in ("READY_TO_UPLOAD", "UPLOADING", "UPLOAD_COMPLETED", "PROCESSING"):
        pub_id = _make_publication(session_factory, job_id=f"job-{allowed_status}", status=allowed_status)
        view = build_publication_detail_view(_refetch(session_factory, pub_id))
        assert view.can_cancel is True, allowed_status
        service.cancel_publication(pub_id)  # must not raise


def test_can_retry_matches_every_real_retryable_status(session_factory) -> None:
    """Mirrors core.publish_retry's actual _RETRYABLE_STATUSES set for every
    PublicationStatus value, not a locally-guessed subset."""
    for status in PublicationStatus:
        pub_id = _make_publication(session_factory, job_id=f"job-retry-{status.value}", status=status.value)
        view = build_publication_detail_view(_refetch(session_factory, pub_id))
        assert view.can_retry == (status in _RETRYABLE_STATUSES), status.value


def test_can_refresh_matches_lease_specific_publications_real_precondition(session_factory) -> None:
    """Mirrors worker.publish_lease.lease_specific_publication's actual gate
    (PROCESSING and currently unlocked)."""
    processing_unlocked = _make_publication(session_factory, job_id="job-refresh-1", status="PROCESSING")
    view = build_publication_detail_view(_refetch(session_factory, processing_unlocked))
    assert view.can_refresh is True
    with session_factory() as session:
        assert lease_specific_publication(session, processing_unlocked, worker_id="test-worker") is True

    processing_locked = _make_publication(
        session_factory, job_id="job-refresh-2", status="PROCESSING", locked_by="some-worker",
    )
    view_locked = build_publication_detail_view(_refetch(session_factory, processing_locked))
    assert view_locked.can_refresh is False
    with session_factory() as session:
        assert lease_specific_publication(session, processing_locked, worker_id="test-worker") is False

    not_processing = _make_publication(session_factory, job_id="job-refresh-3", status="UPLOADING")
    view_not_processing = build_publication_detail_view(_refetch(session_factory, not_processing))
    assert view_not_processing.can_refresh is False
    with session_factory() as session:
        assert lease_specific_publication(session, not_processing, worker_id="test-worker") is False


def test_can_reconcile_hidden_only_for_terminal_statuses(session_factory) -> None:
    for status in PublicationStatus:
        pub_id = _make_publication(session_factory, job_id=f"job-recon-{status.value}", status=status.value)
        view = build_publication_detail_view(_refetch(session_factory, pub_id))
        expected = status not in PUBLICATION_TERMINAL_STATUSES
        assert view.can_reconcile == expected, status.value


def test_publication_detail_view_never_exposes_secret_shaped_fields() -> None:
    field_names = {f.name for f in dataclasses.fields(PublicationDetailView)}
    forbidden_substrings = ("local_path", "secret", "token", "session_reference", "api_key")
    for name in field_names:
        for bad in forbidden_substrings:
            assert bad not in name.lower(), f"PublicationDetailView.{name} looks unsafe to expose"


def _settings(**overrides) -> Settings:
    base: dict = dict(youtube_client_id="client-1", youtube_client_secret="a-fake-client-secret-000000")
    base.update(overrides)
    return Settings(_env_file=None, **base)


def test_publisher_account_view_not_configured() -> None:
    view = build_publisher_account_view("youtube", Settings(_env_file=None), InMemoryCredentialBackend())
    assert view.configured is False
    assert view.configuration_reason
    assert view.accounts == []


def test_publisher_account_view_configured_with_no_accounts() -> None:
    view = build_publisher_account_view("youtube", _settings(), InMemoryCredentialBackend())
    assert view.configured is True
    assert view.configuration_reason is None
    assert view.accounts == []


def test_publisher_account_view_lists_connected_accounts_with_safe_metadata() -> None:
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="secret-access-token-000000", refresh_token="secret-refresh-token-000000",
        expires_at=None, scope="s", provider="youtube", account_reference="default",
        channel_title="My Channel",
    ))
    view = build_publisher_account_view("youtube", _settings(), backend)
    assert len(view.accounts) == 1
    account = view.accounts[0]
    assert account.channel_title == "My Channel"
    assert account.has_refresh_token is True
    assert "secret-access-token-000000" not in str(account)
    assert "secret-refresh-token-000000" not in str(account)


def test_publish_setup_view_disables_unconfigured_platform_with_exact_reason() -> None:
    class _FakeJob:
        id = "job-1"

    view = build_publish_setup_view(_FakeJob(), Settings(_env_file=None), InMemoryCredentialBackend())
    youtube = next(p for p in view.platforms if p.provider == "youtube")
    assert youtube.selectable is False
    assert youtube.disabled_reason
    assert "YOUTUBE_CLIENT_ID" in youtube.disabled_reason or "client" in youtube.disabled_reason.lower()


def test_publish_setup_view_disables_configured_but_unconnected_platform() -> None:
    class _FakeJob:
        id = "job-1"

    view = build_publish_setup_view(_FakeJob(), _settings(), InMemoryCredentialBackend())
    youtube = next(p for p in view.platforms if p.provider == "youtube")
    assert youtube.selectable is False
    assert "연결된 계정이 없습니다" in youtube.disabled_reason


def test_publish_setup_view_selectable_once_connected() -> None:
    class _FakeJob:
        id = "job-1"

    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=None, scope="",
        provider="youtube", account_reference="default",
    ))
    view = build_publish_setup_view(_FakeJob(), _settings(), backend)
    youtube = next(p for p in view.platforms if p.provider == "youtube")
    assert youtube.selectable is True
    assert youtube.disabled_reason is None
    assert youtube.accounts == ["default"]


def test_publish_setup_view_public_upload_gate_reflects_settings_flag() -> None:
    class _FakeJob:
        id = "job-1"

    for flag in (True, False):
        view = build_publish_setup_view(
            _FakeJob(), _settings(allow_public_upload=flag), InMemoryCredentialBackend(),
        )
        youtube = next(p for p in view.platforms if p.provider == "youtube")
        assert youtube.public_upload_allowed is flag


def test_publish_setup_view_instagram_capabilities_show_public_only_and_confirmation_required() -> None:
    class _FakeJob:
        id = "job-1"

    view = build_publish_setup_view(_FakeJob(), Settings(_env_file=None), InMemoryCredentialBackend())
    instagram = next(p for p in view.platforms if p.provider == "instagram")
    assert instagram.privacy_values == [("PUBLIC", "공개(비공개 옵션 없음)")]
    assert instagram.public_privacy_values == frozenset({"PUBLIC"})
    assert instagram.requires_user_confirmation is True


def test_publish_setup_view_default_platform_options_are_the_most_restrictive() -> None:
    class _FakeJob:
        id = "job-1"

    view = build_publish_setup_view(_FakeJob(), Settings(_env_file=None), InMemoryCredentialBackend())
    tiktok = next(p for p in view.platforms if p.provider == "tiktok")
    assert tiktok.default_platform_options["disable_comment"] is True
    assert tiktok.default_platform_options["disable_duet"] is True
    assert tiktok.default_platform_options["disable_stitch"] is True
