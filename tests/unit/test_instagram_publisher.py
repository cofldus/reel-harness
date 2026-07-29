"""Contract tests for the Instagram Content Publishing API adapter
(MockTransport -- no sockets, coexists with the network-block fixture;
NOT a live Instagram E2E). Covers account-info + platform-options
validation only -- container upload lands in a later commit. All tokens
are obviously-fake placeholders."""
from __future__ import annotations

import httpx
import pytest

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherCreatorNotEligibleError,
    PublisherPermissionDeniedError,
    PublisherPublishingLimitReachedError,
    TransientProviderError,
)
from reel_harness.providers.base import CreatorInfo
from reel_harness.providers.instagram_publisher import (
    CAPABILITIES,
    MAX_CAPTION_LENGTH,
    InstagramPublisher,
    InstagramReelsOptions,
    build_caption,
    validate_publish_options,
)

FAKE_TOKEN = "FAKE-INSTAGRAM-ACCESS-TOKEN-000000000"
GRAPH_URL = "https://graph.instagram.com"
API_VERSION = "v25.0"
ACCOUNT_ID = "17841400"


def _publisher(handler, **overrides) -> InstagramPublisher:
    defaults: dict = dict(
        access_token_provider=lambda: FAKE_TOKEN, graph_url=GRAPH_URL, api_version=API_VERSION,
        account_id=ACCOUNT_ID, max_retries=2, retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return InstagramPublisher(transport=httpx.MockTransport(handler), **defaults)


def _identity_response(**overrides) -> dict:
    data = dict(id=ACCOUNT_ID, username="my_reel_account", account_type="BUSINESS")
    data.update(overrides)
    return data


def _limit_response(quota_usage: int = 3, quota_total: int = 100) -> dict:
    return {"data": [{"quota_usage": quota_usage, "config": {"quota_total": quota_total}}]}


# -- capabilities -------------------------------------------------------

def test_capabilities_require_creator_info_and_confirmation() -> None:
    assert CAPABILITIES.requires_creator_info is True
    assert CAPABILITIES.requires_user_confirmation is True
    assert CAPABILITIES.default_privacy == "PUBLIC"
    assert CAPABILITIES.privacy_values == frozenset({"PUBLIC"})
    assert CAPABILITIES.public_privacy_values == frozenset({"PUBLIC"})
    assert CAPABILITIES.supports_upload_only is False
    assert CAPABILITIES.supports_scheduled_publish is False
    assert CAPABILITIES.supports_remote_delete is False
    assert CAPABILITIES.supports_comments_control is False


# -- get_creator_info -----------------------------------------------------

def test_get_creator_info_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get("access_token") == FAKE_TOKEN
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response())
        assert request.url.path == f"/{API_VERSION}/{ACCOUNT_ID}"
        return httpx.Response(200, json=_identity_response())

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info is not None
    assert info.account_identifier == ACCOUNT_ID
    assert info.display_name == "my_reel_account"
    assert info.allowed_privacy_values == frozenset({"PUBLIC"})
    assert info.comments_configurable is False
    assert info.remix_configurable is False
    assert info.max_post_duration_sec == 900.0
    assert info.warnings == []
    publisher.close()


def test_get_creator_info_never_cached_calls_endpoint_every_time() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response())
        return httpx.Response(200, json=_identity_response())

    publisher = _publisher(handler)
    publisher.get_creator_info()
    publisher.get_creator_info()
    assert len(calls) == 4  # 2 calls (identity + limit) per get_creator_info
    publisher.close()


def test_get_creator_info_flags_non_professional_account_type() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response())
        return httpx.Response(200, json=_identity_response(account_type="PERSONAL"))

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert any("account_type" in w for w in info.warnings)
    publisher.close()


def test_get_creator_info_flags_publishing_limit_reached() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response(quota_usage=100, quota_total=100))
        return httpx.Response(200, json=_identity_response())

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert "publishing_limit_reached" in info.warnings
    publisher.close()


def test_get_creator_info_401_is_auth_error() -> None:
    publisher = _publisher(lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_403_is_permission_denied() -> None:
    publisher = _publisher(lambda r: httpx.Response(403))
    with pytest.raises(PublisherPermissionDeniedError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_embedded_oauth_error_is_auth_error() -> None:
    """Meta's error envelope can carry an OAuthException even with HTTP
    200 -- never treated as success."""
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "error": {"message": "bad token", "type": "OAuthException", "code": 190},
    }))
    with pytest.raises(ProviderAuthError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_unknown_embedded_error_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "error": {"message": "internal", "type": "APIError", "code": 1},
    }))
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_malformed_limit_response_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json={"data": [{"config": {}}]})  # missing quota_usage
        return httpx.Response(200, json=_identity_response())

    publisher = _publisher(handler)
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_500_then_success() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        if request.url.path.endswith("/content_publishing_limit"):
            return httpx.Response(200, json=_limit_response())
        return httpx.Response(200, json=_identity_response())

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info is not None
    publisher.close()


# -- upload methods not yet implemented --------------------------

def test_upload_methods_not_yet_implemented() -> None:
    publisher = _publisher(lambda r: httpx.Response(200))
    with pytest.raises(NotImplementedError):
        publisher.create_upload_session(None, 0, "video/mp4", "cid")
    with pytest.raises(NotImplementedError):
        publisher.upload_chunk(None, b"", 0, 0)
    with pytest.raises(NotImplementedError):
        publisher.query_upload_offset(None, 0)
    with pytest.raises(NotImplementedError):
        publisher.get_processing_status("id")
    publisher.close()


# -- build_caption -------------------------------------------------------

def test_build_caption_accepts_a_normal_caption() -> None:
    assert build_caption("A perfectly normal short-form video caption #cooking") == \
        "A perfectly normal short-form video caption #cooking"


def test_build_caption_rejects_over_length() -> None:
    with pytest.raises(MetadataInvalidError, match="2200"):
        build_caption("x" * (MAX_CAPTION_LENGTH + 1))


def test_build_caption_accepts_exactly_the_limit() -> None:
    build_caption("x" * MAX_CAPTION_LENGTH)


def test_build_caption_rejects_too_many_hashtags() -> None:
    caption = " ".join(f"#tag{i}" for i in range(31))
    with pytest.raises(MetadataInvalidError, match="hashtags"):
        build_caption(caption)


def test_build_caption_accepts_exactly_the_hashtag_limit() -> None:
    caption = " ".join(f"#tag{i}" for i in range(30))
    build_caption(caption)


def test_build_caption_rejects_too_many_mentions() -> None:
    caption = " ".join(f"@user{i}" for i in range(21))
    with pytest.raises(MetadataInvalidError, match="@mentions"):
        build_caption(caption)


@pytest.mark.parametrize("forbidden", [
    r"see C:\Users\me\umma\jobs\secret.mp4",
    "asset at /jobs/12345678-1234-1234-1234-123456789012/final",
    "job 12345678-1234-1234-1234-123456789012 is done",
    "api_key: sk-abc123",
    "client_secret=abc123",
    "signed url ...&Signature=abc&Expires=1234567890",
    "callback at http://127.0.0.1:8080/cb",
])
def test_build_caption_rejects_forbidden_markers(forbidden: str) -> None:
    with pytest.raises(MetadataInvalidError, match="disallowed"):
        build_caption(forbidden)


# -- InstagramReelsOptions -------------------------------------------------------

def test_reels_options_defaults_are_conservative() -> None:
    options = InstagramReelsOptions()
    assert options.share_to_feed is False
    assert options.collaborators == ()
    assert options.cover_url is None


def test_reels_options_as_platform_options_shape() -> None:
    options = InstagramReelsOptions(share_to_feed=True, thumb_offset_ms=500, collaborators=("a", "b"))
    shape = options.as_platform_options()
    assert shape == {
        "share_to_feed": True, "thumb_offset_ms": 500, "cover_url": None,
        "collaborators": ["a", "b"], "location_id": None, "audio_name": None,
    }


# -- validate_publish_options -------------------------------------------------------

def _account_info(**overrides) -> CreatorInfo:
    defaults: dict = dict(
        account_identifier=ACCOUNT_ID, display_name="my_reel_account",
        allowed_privacy_values=frozenset({"PUBLIC"}), comments_configurable=False,
        remix_configurable=False, max_post_duration_sec=900.0,
    )
    defaults.update(overrides)
    return CreatorInfo(**defaults)


def test_validate_publish_options_passes_for_defaults() -> None:
    validate_publish_options(_account_info(), InstagramReelsOptions())


def test_validate_publish_options_rejects_too_many_collaborators() -> None:
    with pytest.raises(MetadataInvalidError, match="collaborators"):
        validate_publish_options(_account_info(), InstagramReelsOptions(collaborators=("a", "b", "c", "d")))


def test_validate_publish_options_rejects_when_publishing_limit_reached() -> None:
    with pytest.raises(PublisherPublishingLimitReachedError):
        validate_publish_options(
            _account_info(warnings=["publishing_limit_reached"]), InstagramReelsOptions(),
        )


def test_validate_publish_options_rejects_when_account_not_eligible() -> None:
    with pytest.raises(PublisherCreatorNotEligibleError):
        validate_publish_options(
            _account_info(warnings=["account_type='PERSONAL' is not a Reels-eligible professional account type"]),
            InstagramReelsOptions(),
        )
