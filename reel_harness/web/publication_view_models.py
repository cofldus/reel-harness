from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from reel_harness.core.state_machine import PUBLICATION_TERMINAL_STATUSES, PublicationStatus
from reel_harness.publisher.credentials import publisher_account_safe_metadata
from reel_harness.web.labels import (
    PUBLICATION_NEEDS_ACTION_STATUSES,
    privacy_value_label,
    provider_display_name,
    publication_status_label,
)

_PUBLISHER_PROVIDER_IDS = ("youtube", "tiktok", "instagram")

# Mirrors PublicationService.cancel_publication's own precondition exactly
# (core/publish_service.py) -- it refuses PUBLISHED/CANCELLED (terminal) and
# FAILED (the state machine only allows FAILED -> RETRY_WAIT, never
# -> CANCELLED directly -- see state_machine.py and
# test_failed_allows_only_manual_retry_wait), nothing else. Deriving this
# from ALLOWED_PUBLICATION_TRANSITIONS would risk the same mistake Phase
# 5A's job can_cancel almost made: cancel is allowed from far more statuses
# than a transition-table edge to CANCELLED alone would suggest
# (UPLOADING/UPLOAD_PAUSED just set cancel_requested instead of
# transitioning immediately, but they are NOT blocked).
_CANCEL_BLOCKED_STATUSES = frozenset({
    PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED, PublicationStatus.FAILED,
})

# Mirrors core.publish_retry._RETRYABLE_STATUSES exactly -- retry_publication
# refuses every other status (PUBLISHED/CANCELLED/REVIEW_REQUIRED each with
# their own specific reason, any PUBLICATION_ACTIVE_STATUSES member with
# "run reconcile first"). Not re-derived from a transition table.
_RETRYABLE_STATUSES = frozenset({
    PublicationStatus.FAILED, PublicationStatus.AUTH_REQUIRED,
    PublicationStatus.QUOTA_BLOCKED, PublicationStatus.RETRY_WAIT,
})


@dataclass
class PublisherAccountSummary:
    account_reference: str
    channel_id: str | None
    channel_title: str | None
    has_refresh_token: bool
    expires_at: datetime | None
    invalid: bool


@dataclass
class PublisherAccountView:
    provider: str
    display_name: str
    configured: bool
    configuration_reason: str | None
    accounts: list[PublisherAccountSummary] = field(default_factory=list)


@dataclass
class PublicationSummaryView:
    publication_id: str
    job_id: str
    job_topic: str | None
    provider: str
    provider_display: str
    status_label: str
    created_at: datetime
    updated_at: datetime
    detail_url: str


@dataclass
class PublicationDetailView:
    publication_id: str
    job_id: str
    provider: str
    provider_display: str
    account_reference: str
    status_label: str
    privacy_status: str | None
    privacy_status_label: str | None
    provider_video_id: str | None
    publication_url: str | None
    bytes_uploaded: int | None
    total_bytes: int | None
    failure_code: str | None
    failure_summary: str | None
    retry_count: int
    created_at: datetime
    updated_at: datetime
    is_terminal: bool
    needs_action: bool
    can_cancel: bool
    can_retry: bool
    can_refresh: bool
    can_reconcile: bool


@dataclass
class PlatformOptionView:
    provider: str
    display_name: str
    selectable: bool
    disabled_reason: str | None
    accounts: list[str]
    privacy_values: list[tuple[str, str]]  # (raw value, Korean label)
    default_privacy: str
    public_privacy_values: frozenset[str]
    requires_user_confirmation: bool
    public_upload_allowed: bool
    # Display-only: PublicationService.create_publication has no parameter to
    # accept custom platform_options -- the worker always applies
    # providers.registry.default_platform_options(provider) (the most
    # restrictive defaults) when it actually builds the upload metadata, the
    # same way `publish-job`/the CLI already work today. Shown here so the
    # user can see exactly what will be applied before confirming, never as
    # an editable form -- customizing this per-publication is out of scope
    # for Phase 5B (would require changing PublicationService/the worker's
    # metadata-snapshot resume path, real domain-logic surface this phase
    # deliberately does not touch).
    default_platform_options: dict


@dataclass
class PublishSetupView:
    job_id: str
    platforms: list[PlatformOptionView]


def _oauth_client_configured(provider: str, settings) -> tuple[bool, str | None]:
    """Whether PROVIDER's OAuth client itself is configured (app-level
    REEL_HARNESS_*_CLIENT_ID/_SECRET), independent of whether any account has
    actually been connected yet -- same (bool, reason) shape as
    web.router._real_provider_readiness for the LLM/TTS/asset profiles."""
    from reel_harness.config import (
        ProviderConfigurationError,
        validate_instagram_credentials_configured,
        validate_tiktok_credentials_configured,
        validate_youtube_credentials_configured,
    )

    validators = {
        "youtube": validate_youtube_credentials_configured,
        "tiktok": validate_tiktok_credentials_configured,
        "instagram": validate_instagram_credentials_configured,
    }
    try:
        validators[provider](settings)
    except ProviderConfigurationError as exc:
        return False, str(exc)
    return True, None


def build_publisher_account_view(provider: str, settings, credential_backend) -> PublisherAccountView:
    configured, reason = _oauth_client_configured(provider, settings)
    accounts: list[PublisherAccountSummary] = []
    if configured:
        for alias in credential_backend.list_accounts(provider):
            cred = credential_backend.get_credential(provider, alias)
            if cred is None:
                continue
            safe = publisher_account_safe_metadata(cred)
            accounts.append(PublisherAccountSummary(
                account_reference=safe["account_reference"],
                channel_id=safe["channel_id"],
                channel_title=safe["channel_title"],
                has_refresh_token=safe["has_refresh_token"],
                expires_at=cred.expires_at,
                invalid=safe["invalid"],
            ))
    return PublisherAccountView(
        provider=provider, display_name=provider_display_name(provider),
        configured=configured, configuration_reason=reason, accounts=accounts,
    )


def build_publication_summary_view(pub, job_topic: str | None) -> PublicationSummaryView:
    return PublicationSummaryView(
        publication_id=pub.id,
        job_id=pub.job_id,
        job_topic=job_topic,
        provider=pub.provider,
        provider_display=provider_display_name(pub.provider),
        status_label=publication_status_label(pub.status),
        created_at=pub.created_at,
        updated_at=pub.updated_at,
        detail_url=f"/publications/{pub.id}",
    )


def build_publication_detail_view(pub) -> PublicationDetailView:
    status = PublicationStatus(pub.status)
    return PublicationDetailView(
        publication_id=pub.id,
        job_id=pub.job_id,
        provider=pub.provider,
        provider_display=provider_display_name(pub.provider),
        account_reference=pub.account_reference,
        status_label=publication_status_label(pub.status),
        privacy_status=pub.privacy_status,
        privacy_status_label=(
            privacy_value_label(pub.provider, pub.privacy_status) if pub.privacy_status else None
        ),
        provider_video_id=pub.provider_video_id,
        publication_url=pub.publication_url,
        bytes_uploaded=pub.bytes_uploaded,
        total_bytes=pub.total_bytes,
        failure_code=pub.failure_code,
        failure_summary=pub.failure_summary,
        retry_count=pub.retry_count,
        created_at=pub.created_at,
        updated_at=pub.updated_at,
        is_terminal=status in PUBLICATION_TERMINAL_STATUSES,
        needs_action=status in PUBLICATION_NEEDS_ACTION_STATUSES,
        can_cancel=status not in _CANCEL_BLOCKED_STATUSES,
        can_retry=status in _RETRYABLE_STATUSES,
        can_refresh=status == PublicationStatus.PROCESSING and pub.locked_by is None,
        # Reconcile never actually refuses at the service layer (a terminal
        # publication just gets a no-op "already_consistent" result back) --
        # hiding the button on a terminal publication is a pure UX choice to
        # avoid an obviously-pointless click, NOT a mirrored precondition
        # like the three flags above.
        can_reconcile=status not in PUBLICATION_TERMINAL_STATUSES,
    )


def build_publish_setup_view(job, settings, credential_backend) -> PublishSetupView:
    from reel_harness.providers.registry import default_platform_options, provider_capabilities

    platforms = []
    for provider in _PUBLISHER_PROVIDER_IDS:
        configured, reason = _oauth_client_configured(provider, settings)
        accounts = credential_backend.list_accounts(provider) if configured else []
        caps = provider_capabilities(provider)

        if not configured:
            disabled_reason: str | None = reason
        elif not accounts:
            disabled_reason = "연결된 계정이 없습니다 — 계정 연결 화면에서 먼저 연결하세요."
        else:
            disabled_reason = None

        platforms.append(PlatformOptionView(
            provider=provider,
            display_name=provider_display_name(provider),
            selectable=disabled_reason is None,
            disabled_reason=disabled_reason,
            accounts=accounts,
            privacy_values=[
                (value, privacy_value_label(provider, value)) for value in sorted(caps.privacy_values)
            ],
            default_privacy=caps.default_privacy,
            public_privacy_values=caps.public_privacy_values,
            requires_user_confirmation=caps.requires_user_confirmation,
            public_upload_allowed=settings.allow_public_upload,
            default_platform_options=default_platform_options(provider),
        ))

    return PublishSetupView(job_id=job.id, platforms=platforms)
