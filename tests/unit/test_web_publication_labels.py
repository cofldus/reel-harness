from __future__ import annotations

from reel_harness.core.publish_retry import _RETRYABLE_STATUSES
from reel_harness.core.state_machine import PublicationStatus
from reel_harness.web.labels import (
    PRIVACY_VALUE_LABELS,
    PROVIDER_DISPLAY_NAMES,
    PUBLICATION_NEEDS_ACTION_STATUSES,
    PUBLICATION_STATUS_LABELS,
    privacy_value_label,
    provider_display_name,
    publication_status_label,
)


def test_every_publication_status_has_a_label() -> None:
    for status in PublicationStatus:
        assert status in PUBLICATION_STATUS_LABELS
        assert PUBLICATION_STATUS_LABELS[status]


def test_publication_status_label_falls_back_to_raw_value_for_unknown() -> None:
    assert publication_status_label("SOME_FUTURE_STATUS") == "SOME_FUTURE_STATUS"


def test_needs_action_statuses_include_every_real_retryable_status() -> None:
    """Must be derived from core.publish_retry's actual retryable set, not
    guessed independently -- a mismatch here would hide a needed action
    (e.g. stop polling on a status a user could still act on)."""
    for status in _RETRYABLE_STATUSES:
        assert status in PUBLICATION_NEEDS_ACTION_STATUSES
    assert PublicationStatus.REVIEW_REQUIRED in PUBLICATION_NEEDS_ACTION_STATUSES
    assert PUBLICATION_NEEDS_ACTION_STATUSES.issubset(set(PublicationStatus))


def test_needs_action_statuses_exclude_terminal_and_in_progress_statuses() -> None:
    for status in (
        PublicationStatus.PUBLISHED, PublicationStatus.CANCELLED,
        PublicationStatus.UPLOADING, PublicationStatus.PROCESSING,
    ):
        assert status not in PUBLICATION_NEEDS_ACTION_STATUSES


def test_provider_display_name_known_and_unknown() -> None:
    assert provider_display_name("youtube") == "YouTube"
    assert provider_display_name("tiktok") == "TikTok"
    assert provider_display_name("instagram") == "Instagram Reels"
    assert provider_display_name("facebook") == "facebook"  # unknown -> raw value, never a crash


def test_every_real_provider_has_a_display_name() -> None:
    for provider in ("youtube", "tiktok", "instagram", "fake"):
        assert provider in PROVIDER_DISPLAY_NAMES
        assert PROVIDER_DISPLAY_NAMES[provider]


def test_privacy_value_label_known_and_unknown() -> None:
    assert privacy_value_label("youtube", "private") == "비공개"
    assert privacy_value_label("tiktok", "SELF_ONLY") == "나만 보기"
    assert privacy_value_label("instagram", "PUBLIC") == "공개(비공개 옵션 없음)"
    assert privacy_value_label("youtube", "some-future-value") == "some-future-value"


def test_privacy_value_labels_cover_every_real_capability_value() -> None:
    from reel_harness.providers.registry import provider_capabilities

    for provider in ("youtube", "tiktok", "instagram", "fake"):
        caps = provider_capabilities(provider)
        for value in caps.privacy_values:
            assert (provider, value) in PRIVACY_VALUE_LABELS, f"{provider}/{value} has no label"
