"""Contract tests for the Instagram Content Publishing API adapter
(MockTransport -- no sockets, coexists with the network-block fixture;
NOT a live Instagram E2E). Covers account-info, container creation,
resumable upload, processing status + publish, and platform-options
validation. All tokens are obviously-fake placeholders."""
from __future__ import annotations

import urllib.parse

import httpx
import pytest

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherCreatorNotEligibleError,
    PublisherPermissionDeniedError,
    PublisherPublishingLimitReachedError,
    TransientProviderError,
    UploadRejectedError,
    UploadSessionExpiredError,
)
from reel_harness.providers.base import CreatorInfo, PublicationMetadata, UploadSessionHandle
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

def _metadata(**overrides) -> PublicationMetadata:
    defaults: dict = dict(
        title="A short-form video", description="", tags=[], category_id="",
        privacy_status="PUBLIC", made_for_kids=False,
        platform_options=InstagramReelsOptions().as_platform_options(),
    )
    defaults.update(overrides)
    return PublicationMetadata(**defaults)


def _container_response(**overrides) -> dict:
    data = dict(id="container-id-1")
    data.update(overrides)
    return data


# -- create_upload_session -------------------------------------------------------

def test_create_upload_session_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/{API_VERSION}/{ACCOUNT_ID}/media"
        body = dict(urllib.parse.parse_qsl(request.content.decode()))
        assert body["media_type"] == "REELS"
        assert body["upload_type"] == "resumable"
        assert body["caption"] == "A short-form video"
        assert body["share_to_feed"] == "false"
        return httpx.Response(200, json=_container_response())

    publisher = _publisher(handler)
    handle = publisher.create_upload_session(_metadata(), 1_000_000, "video/mp4", "cid-1")
    assert handle.session_reference == f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/container-id-1"
    assert handle.total_bytes == 1_000_000
    assert handle.chunk_size == 1_000_000  # whole file in one shot -- see docstring
    assert handle.provider_reference == "container-id-1"
    publisher.close()


def test_create_upload_session_uses_build_caption_validation() -> None:
    publisher = _publisher(lambda r: (_ for _ in ()).throw(AssertionError("must not call the network")))
    with pytest.raises(MetadataInvalidError, match="2200"):
        publisher.create_upload_session(
            _metadata(title="x" * (MAX_CAPTION_LENGTH + 1)), 100, "video/mp4", "cid",
        )
    publisher.close()


def test_create_upload_session_missing_container_id_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={}))
    with pytest.raises(TransientProviderError):
        publisher.create_upload_session(_metadata(), 100, "video/mp4", "cid")
    publisher.close()


def test_create_upload_session_embedded_error_maps_to_auth() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "error": {"message": "bad token", "type": "OAuthException"},
    }))
    with pytest.raises(ProviderAuthError):
        publisher.create_upload_session(_metadata(), 100, "video/mp4", "cid")
    publisher.close()


# -- upload_chunk -------------------------------------------------------

def _session(**overrides) -> UploadSessionHandle:
    defaults: dict = dict(
        session_reference=f"https://rupload.facebook.com/ig-api-upload/{API_VERSION}/container-id-1",
        total_bytes=1000, chunk_size=1000, provider_reference="container-id-1",
    )
    defaults.update(overrides)
    return UploadSessionHandle(**defaults)


def test_upload_chunk_success_completes_in_one_shot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"OAuth {FAKE_TOKEN}"
        assert request.headers["offset"] == "0"
        assert request.headers["file_size"] == "1000"
        return httpx.Response(200, json={"success": True, "message": "Upload successful."})

    publisher = _publisher(handler)
    result = publisher.upload_chunk(_session(), b"x" * 1000, 0, 1000)
    assert result.completed is True
    assert result.bytes_uploaded == 1000
    assert result.provider_video_id == "container-id-1"
    publisher.close()


def test_upload_chunk_404_is_session_expired() -> None:
    publisher = _publisher(lambda r: httpx.Response(404))
    with pytest.raises(UploadSessionExpiredError):
        publisher.upload_chunk(_session(), b"x" * 1000, 0, 1000)
    publisher.close()


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_upload_chunk_5xx_is_transient(status_code: int) -> None:
    publisher = _publisher(lambda r: httpx.Response(status_code))
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(_session(), b"x" * 1000, 0, 1000)
    publisher.close()


def test_upload_chunk_retriable_failure_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "debug_info": {"retriable": True, "type": "TransientError", "message": "try again"},
    }))
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(_session(), b"x" * 1000, 0, 1000)
    publisher.close()


def test_upload_chunk_non_retriable_failure_is_rejected() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "debug_info": {"retriable": False, "type": "ProcessingFailedError", "message": "bad video"},
    }))
    with pytest.raises(UploadRejectedError):
        publisher.upload_chunk(_session(), b"x" * 1000, 0, 1000)
    publisher.close()


# -- query_upload_offset -------------------------------------------------------

def test_query_upload_offset_always_raises_session_expired() -> None:
    """No documented way to query Instagram's confirmed offset -- see
    docs/PUBLISHING.md. This forces a fresh container rather than
    guessing."""
    publisher = _publisher(lambda r: httpx.Response(200))
    with pytest.raises(UploadSessionExpiredError):
        publisher.query_upload_offset(_session(), 1000)
    publisher.close()


# -- get_processing_status -------------------------------------------------------

def test_get_processing_status_in_progress() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"status_code": "IN_PROGRESS"}))
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "processing"
    publisher.close()


def test_get_processing_status_error() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"status_code": "ERROR"}))
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "failed"
    assert status.failure_reason == "container_error"
    publisher.close()


def test_get_processing_status_expired() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"status_code": "EXPIRED"}))
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "failed"
    assert status.failure_reason == "container_expired"
    publisher.close()


def test_get_processing_status_already_published_never_republishes() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"status_code": "PUBLISHED"})

    publisher = _publisher(handler)
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "succeeded"
    assert status.publication_url is None  # never fabricated
    assert not any("media_publish" in c for c in calls)  # never re-published
    publisher.close()


def test_get_processing_status_finished_publishes_and_fetches_permalink() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media_publish"):
            body = dict(urllib.parse.parse_qsl(request.content.decode()))
            assert body["creation_id"] == "container-id-1"
            return httpx.Response(200, json={"id": "media-id-1"})
        if request.url.path == f"/{API_VERSION}/media-id-1":
            assert request.url.params.get("fields") == "permalink"
            return httpx.Response(200, json={"permalink": "https://www.instagram.com/reel/abc123/"})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    publisher = _publisher(handler)
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "succeeded"
    assert status.publication_url == "https://www.instagram.com/reel/abc123/"
    publisher.close()


def test_get_processing_status_finished_publish_missing_media_id_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={})
        return httpx.Response(200, json={"status_code": "FINISHED"})

    publisher = _publisher(handler)
    with pytest.raises(TransientProviderError):
        publisher.get_processing_status("container-id-1")
    publisher.close()


def test_get_processing_status_permalink_fetch_failure_is_not_fatal() -> None:
    """The publish itself already succeeded by the time this is called --
    a failed permalink lookup must never turn a real success into an
    error."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "media-id-1"})
        if request.url.path == f"/{API_VERSION}/media-id-1":
            return httpx.Response(500)
        return httpx.Response(200, json={"status_code": "FINISHED"})

    publisher = _publisher(handler)
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "succeeded"
    assert status.publication_url is None
    publisher.close()


def test_get_processing_status_unknown_status_keeps_polling_never_succeeds() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "status_code": "SOME_FUTURE_STATUS_THIS_ADAPTER_DOES_NOT_KNOW",
    }))
    status = publisher.get_processing_status("container-id-1")
    assert status.processing_status == "processing"
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
