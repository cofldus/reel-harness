from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CreatePublicationFormInput:
    provider: str
    account_reference: str
    privacy_status: str
    confirm_public_upload: bool
    confirm_platform_options: bool


@dataclass
class CreatePublicationFormResult:
    value: CreatePublicationFormInput | None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.value is not None and not self.errors


def validate_create_publication_form(
    provider: str, account_reference: str, privacy_status: str,
    confirm_public_upload: bool, confirm_platform_options: bool,
    *, settings, credential_backend,
) -> CreatePublicationFormResult:
    """Server-side validation -- the browser form is never trusted alone.
    Allow-lists are sourced dynamically from providers.registry
    (provider_capabilities/credential_backend), never hardcoded per-provider
    strings duplicated here -- this is a friendliness pass only, re-rendering
    the same form with per-field errors on a violation; the actual authority
    stays PublicationService.create_publication, whose own exceptions (e.g. a
    race where the account got disconnected between page load and submit)
    the caller catches separately and maps into this same errors shape."""
    from reel_harness.providers.registry import provider_capabilities

    errors: dict[str, str] = {}

    try:
        caps = provider_capabilities(provider)
    except NotImplementedError:
        return CreatePublicationFormResult(value=None, errors={"provider": "지원하지 않는 플랫폼입니다."})

    account_reference = account_reference.strip()
    if not account_reference:
        errors["account_reference"] = "계정을 선택해주세요."
    elif provider != "fake" and account_reference not in credential_backend.list_accounts(provider):
        # "fake" participates in no credential system at all (FakePublisher
        # never needs a saved OAuthCredential) -- only real providers need
        # their account_reference to actually be a connected account.
        errors["account_reference"] = "연결되지 않은 계정입니다 — 계정 연결 화면에서 먼저 연결하세요."

    if privacy_status not in caps.privacy_values:
        allowed = ", ".join(sorted(caps.privacy_values))
        errors["privacy_status"] = f"지원하지 않는 공개 범위입니다 (지원: {allowed})."
    elif privacy_status in caps.public_privacy_values:
        if not confirm_public_upload:
            errors["confirm_public_upload"] = "공개 범위로 게시하려면 확인란에 체크해주세요."
        elif not settings.allow_public_upload:
            errors["confirm_public_upload"] = (
                "공개 업로드가 비활성화되어 있습니다 (REEL_HARNESS_ALLOW_PUBLIC_UPLOAD)."
            )

    if caps.requires_user_confirmation and not confirm_platform_options:
        errors["confirm_platform_options"] = "플랫폼별 기본 옵션을 확인했다는 확인란에 체크해주세요."

    if errors:
        return CreatePublicationFormResult(value=None, errors=errors)
    return CreatePublicationFormResult(value=CreatePublicationFormInput(
        provider=provider, account_reference=account_reference, privacy_status=privacy_status,
        confirm_public_upload=confirm_public_upload, confirm_platform_options=confirm_platform_options,
    ))
