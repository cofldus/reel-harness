from __future__ import annotations

from reel_harness.config import Settings
from reel_harness.publisher.credentials import InMemoryCredentialBackend, OAuthCredential
from reel_harness.web.publication_forms import validate_create_publication_form


def _settings(**overrides) -> Settings:
    base: dict = dict(allow_public_upload=False)
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _backend_with_youtube_account() -> InMemoryCredentialBackend:
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=None, scope="",
        provider="youtube", account_reference="default",
    ))
    return backend


def test_valid_youtube_private_form_passes() -> None:
    result = validate_create_publication_form(
        "youtube", "default", "private", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=_backend_with_youtube_account(),
    )
    assert result.ok
    assert result.value.provider == "youtube"
    assert result.errors == {}


def test_unknown_provider_rejected() -> None:
    result = validate_create_publication_form(
        "facebook", "default", "private", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=InMemoryCredentialBackend(),
    )
    assert not result.ok
    assert "provider" in result.errors


def test_unconnected_account_rejected() -> None:
    result = validate_create_publication_form(
        "youtube", "not-connected", "private", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=InMemoryCredentialBackend(),
    )
    assert not result.ok
    assert "account_reference" in result.errors


def test_empty_account_reference_rejected() -> None:
    result = validate_create_publication_form(
        "youtube", "  ", "private", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=InMemoryCredentialBackend(),
    )
    assert not result.ok
    assert "account_reference" in result.errors


def test_unknown_privacy_status_rejected() -> None:
    result = validate_create_publication_form(
        "youtube", "default", "super-public", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=_backend_with_youtube_account(),
    )
    assert not result.ok
    assert "privacy_status" in result.errors


def test_public_privacy_without_confirm_checkbox_rejected() -> None:
    result = validate_create_publication_form(
        "youtube", "default", "public", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(allow_public_upload=True), credential_backend=_backend_with_youtube_account(),
    )
    assert not result.ok
    assert "confirm_public_upload" in result.errors


def test_public_privacy_with_flag_disabled_rejected_even_if_confirmed() -> None:
    """allow_public_upload=False must genuinely block public privacy --
    checking the confirmation checkbox alone is never enough."""
    result = validate_create_publication_form(
        "youtube", "default", "public", confirm_public_upload=True, confirm_platform_options=False,
        settings=_settings(allow_public_upload=False), credential_backend=_backend_with_youtube_account(),
    )
    assert not result.ok
    assert "confirm_public_upload" in result.errors


def test_public_privacy_confirmed_with_flag_enabled_passes() -> None:
    result = validate_create_publication_form(
        "youtube", "default", "public", confirm_public_upload=True, confirm_platform_options=False,
        settings=_settings(allow_public_upload=True), credential_backend=_backend_with_youtube_account(),
    )
    assert result.ok


def test_instagram_always_requires_platform_options_confirmation() -> None:
    """Instagram's only privacy value is PUBLIC and requires_user_confirmation
    is always True -- both gates must be satisfied, not just one."""
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="tok", refresh_token=None, expires_at=None, scope="",
        provider="instagram", account_reference="default",
    ))
    result = validate_create_publication_form(
        "instagram", "default", "PUBLIC", confirm_public_upload=True, confirm_platform_options=False,
        settings=_settings(allow_public_upload=True), credential_backend=backend,
    )
    assert not result.ok
    assert "confirm_platform_options" in result.errors


def test_instagram_with_both_confirmations_and_flag_enabled_passes() -> None:
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="tok", refresh_token=None, expires_at=None, scope="",
        provider="instagram", account_reference="default",
    ))
    result = validate_create_publication_form(
        "instagram", "default", "PUBLIC", confirm_public_upload=True, confirm_platform_options=True,
        settings=_settings(allow_public_upload=True), credential_backend=backend,
    )
    assert result.ok


def test_tiktok_requires_platform_options_confirmation_even_for_self_only() -> None:
    """TikTok's requires_user_confirmation is True regardless of privacy
    value (SELF_ONLY is not in public_privacy_values, so no public-upload
    gate applies -- but the platform-options review gate still does)."""
    backend = InMemoryCredentialBackend()
    backend.save_credential(OAuthCredential(
        access_token="tok", refresh_token="ref", expires_at=None, scope="",
        provider="tiktok", account_reference="default",
    ))
    result = validate_create_publication_form(
        "tiktok", "default", "SELF_ONLY", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=backend,
    )
    assert not result.ok
    assert "confirm_platform_options" in result.errors


def test_fake_provider_skips_account_connection_check() -> None:
    """fake participates in no credential system -- account_reference just
    needs to be non-empty, never "connected" (nothing ever saves a fake
    credential)."""
    result = validate_create_publication_form(
        "fake", "default", "private", confirm_public_upload=False, confirm_platform_options=False,
        settings=_settings(), credential_backend=InMemoryCredentialBackend(),
    )
    assert result.ok
