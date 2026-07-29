"""Contract tests for the TikTok Content Posting API adapter (MockTransport
-- no sockets, coexists with the network-block fixture; NOT a live TikTok
E2E). Covers creator_info, chunked upload, processing status, and
platform-options validation. All tokens are obviously-fake placeholders."""
from __future__ import annotations

import json

import httpx
import pytest

from reel_harness.core.errors import (
    MetadataInvalidError,
    ProviderAuthError,
    PublisherAppReviewRequiredError,
    PublisherCreatorNotEligibleError,
    PublisherPermissionDeniedError,
    PublisherPrivacyNotAllowedError,
    TransientProviderError,
    UploadRejectedError,
    UploadSessionExpiredError,
)
from reel_harness.providers.base import CreatorInfo, PublicationMetadata, UploadSessionHandle
from reel_harness.providers.tiktok_publisher import (
    CAPABILITIES,
    MAX_POST_TEXT_UTF16_UNITS,
    TikTokPostOptions,
    TikTokPublisher,
    build_post_text,
    validate_publish_options,
)

FAKE_TOKEN = "FAKE-TIKTOK-ACCESS-TOKEN-000000000"
BASE_URL = "https://open.tiktokapis.com"


def _publisher(handler, **overrides) -> TikTokPublisher:
    defaults: dict = dict(
        access_token_provider=lambda: FAKE_TOKEN, base_url=BASE_URL,
        max_retries=2, retry_backoff_seconds=0.0,
    )
    defaults.update(overrides)
    return TikTokPublisher(transport=httpx.MockTransport(handler), **defaults)


def _creator_info_response(**overrides) -> dict:
    data = dict(
        creator_username="creator1", creator_nickname="Creator One",
        privacy_level_options=["SELF_ONLY", "PUBLIC_TO_EVERYONE"],
        comment_disabled=False, duet_disabled=False, stitch_disabled=False,
        max_video_post_duration_sec=300,
    )
    data.update(overrides)
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


# -- capabilities -------------------------------------------------------

def test_capabilities_require_creator_info_and_confirmation() -> None:
    assert CAPABILITIES.requires_creator_info is True
    assert CAPABILITIES.requires_user_confirmation is True
    assert CAPABILITIES.default_privacy == "SELF_ONLY"
    assert CAPABILITIES.public_privacy_values == frozenset({"PUBLIC_TO_EVERYONE"})
    assert CAPABILITIES.supports_upload_only is False
    assert CAPABILITIES.supports_scheduled_publish is False
    assert CAPABILITIES.supports_remote_delete is False


# -- get_creator_info -----------------------------------------------------

def test_get_creator_info_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("authorization") == f"Bearer {FAKE_TOKEN}"
        assert request.url.path == "/v2/post/publish/creator_info/query/"
        return httpx.Response(200, json=_creator_info_response())

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info is not None
    assert info.account_identifier == "creator1"
    assert info.display_name == "Creator One"
    assert info.allowed_privacy_values == frozenset({"SELF_ONLY", "PUBLIC_TO_EVERYONE"})
    assert info.comments_configurable is True
    assert info.remix_configurable is True
    assert info.max_post_duration_sec == 300.0
    assert info.warnings == []
    publisher.close()


def test_get_creator_info_never_cached_calls_endpoint_every_time() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json=_creator_info_response())

    publisher = _publisher(handler)
    publisher.get_creator_info()
    publisher.get_creator_info()
    assert len(calls) == 2
    publisher.close()


def test_get_creator_info_disabled_interactions_are_not_configurable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_creator_info_response(
            comment_disabled=True, duet_disabled=True, stitch_disabled=False,
        ))

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info.comments_configurable is False
    assert info.remix_configurable is False  # duet OR stitch disabled -> not configurable
    publisher.close()


def test_get_creator_info_unaudited_app_reports_self_only_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_creator_info_response(privacy_level_options=["SELF_ONLY"]))

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info.allowed_privacy_values == frozenset({"SELF_ONLY"})
    publisher.close()


def test_get_creator_info_401_is_auth_error() -> None:
    publisher = _publisher(lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_embedded_auth_error_with_http_200_is_auth_error() -> None:
    """TikTok's {data, error} envelope can carry an error even with HTTP
    200 -- never treated as success."""
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {}, "error": {"code": "access_token_invalid", "message": "x", "log_id": "y"},
    }))
    with pytest.raises(ProviderAuthError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_unknown_embedded_error_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {}, "error": {"code": "internal_error", "message": "x", "log_id": "y"},
    }))
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_403_is_permission_denied() -> None:
    publisher = _publisher(lambda r: httpx.Response(403))
    with pytest.raises(PublisherPermissionDeniedError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_missing_data_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={"error": {"code": "ok"}}))
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_malformed_json_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_missing_privacy_options_field_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"creator_username": "c"}, "error": {"code": "ok"},
    }))
    with pytest.raises(TransientProviderError):
        publisher.get_creator_info()
    publisher.close()


def test_get_creator_info_500_then_success() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return httpx.Response(200, json=_creator_info_response())

    publisher = _publisher(handler)
    info = publisher.get_creator_info()
    assert info is not None
    assert len(calls) == 2
    publisher.close()


# -- chunked-upload methods not yet implemented --------------------------

def _metadata(**overrides) -> PublicationMetadata:
    defaults: dict = dict(
        title="A short-form video", description="", tags=[], category_id="",
        privacy_status="SELF_ONLY", made_for_kids=False,
        platform_options=TikTokPostOptions().as_platform_options(),
    )
    defaults.update(overrides)
    return PublicationMetadata(**defaults)


def _init_response(**overrides) -> dict:
    data = dict(publish_id="publish-id-1", upload_url="https://upload.tiktokapis.com/video/xyz")
    data.update(overrides)
    return {"data": data, "error": {"code": "ok", "message": "", "log_id": "x"}}


# -- create_upload_session -------------------------------------------------------

def test_create_upload_session_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/post/publish/video/init/"
        payload = json.loads(request.content)
        assert payload["post_info"]["title"] == "A short-form video"
        assert payload["post_info"]["privacy_level"] == "SELF_ONLY"
        assert payload["source_info"]["source"] == "FILE_UPLOAD"
        assert payload["source_info"]["video_size"] == 1_000_000
        assert payload["source_info"]["chunk_size"] == 500_000
        assert payload["source_info"]["total_chunk_count"] == 2
        return httpx.Response(200, json=_init_response())

    publisher = _publisher(handler, chunk_size=500_000)
    handle = publisher.create_upload_session(_metadata(), 1_000_000, "video/mp4", "cid-1")
    assert handle.session_reference == "https://upload.tiktokapis.com/video/xyz"
    assert handle.total_bytes == 1_000_000
    assert handle.chunk_size == 500_000
    assert handle.provider_reference == "publish-id-1"
    publisher.close()


def test_create_upload_session_uses_build_post_text_validation() -> None:
    """A post text violating length/forbidden-marker rules must never
    reach the network -- create_upload_session raises before any request."""
    publisher = _publisher(lambda r: (_ for _ in ()).throw(AssertionError("must not call the network")))
    with pytest.raises(MetadataInvalidError, match="2200"):
        publisher.create_upload_session(
            _metadata(title="x" * (MAX_POST_TEXT_UTF16_UNITS + 1)), 100, "video/mp4", "cid",
        )
    publisher.close()


def test_create_upload_session_missing_publish_id_is_transient() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"upload_url": "https://x"}, "error": {"code": "ok"},
    }))
    with pytest.raises(TransientProviderError):
        publisher.create_upload_session(_metadata(), 100, "video/mp4", "cid")
    publisher.close()


def test_create_upload_session_embedded_error_maps_to_auth() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {}, "error": {"code": "access_token_invalid"},
    }))
    with pytest.raises(ProviderAuthError):
        publisher.create_upload_session(_metadata(), 100, "video/mp4", "cid")
    publisher.close()


def test_create_upload_session_429_is_retried() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return httpx.Response(200, json=_init_response())

    publisher = _publisher(handler, retry_backoff_seconds=0.0)
    handle = publisher.create_upload_session(_metadata(), 100, "video/mp4", "cid")
    assert handle.provider_reference == "publish-id-1"
    assert len(calls) == 2
    publisher.close()


# -- upload_chunk -------------------------------------------------------

def _session(**overrides) -> UploadSessionHandle:
    defaults: dict = dict(
        session_reference="https://upload.tiktokapis.com/video/xyz",
        total_bytes=1000, chunk_size=500, provider_reference="publish-id-1",
    )
    defaults.update(overrides)
    return UploadSessionHandle(**defaults)


def test_upload_chunk_partial_is_not_completed_and_never_returns_a_video_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-range"] == "bytes 0-499/1000"
        assert request.headers["content-type"] == "video/mp4"
        return httpx.Response(200)

    publisher = _publisher(handler)
    result = publisher.upload_chunk(_session(), b"x" * 500, 0, 1000)
    assert result.completed is False
    assert result.bytes_uploaded == 500
    assert result.provider_video_id is None
    publisher.close()


def test_upload_chunk_final_chunk_completes_and_returns_the_known_publish_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-range"] == "bytes 500-999/1000"
        return httpx.Response(200)

    publisher = _publisher(handler)
    result = publisher.upload_chunk(_session(), b"x" * 500, 500, 1000)
    assert result.completed is True
    assert result.bytes_uploaded == 1000
    assert result.provider_video_id == "publish-id-1"
    publisher.close()


def test_upload_chunk_404_is_session_expired() -> None:
    publisher = _publisher(lambda r: httpx.Response(404))
    with pytest.raises(UploadSessionExpiredError):
        publisher.upload_chunk(_session(), b"x" * 500, 0, 1000)
    publisher.close()


@pytest.mark.parametrize("status_code", [500, 502, 503, 504])
def test_upload_chunk_5xx_is_transient(status_code: int) -> None:
    publisher = _publisher(lambda r: httpx.Response(status_code))
    with pytest.raises(TransientProviderError):
        publisher.upload_chunk(_session(), b"x" * 500, 0, 1000)
    publisher.close()


@pytest.mark.parametrize("status_code", [400, 403])
def test_upload_chunk_rejected_content(status_code: int) -> None:
    publisher = _publisher(lambda r: httpx.Response(status_code))
    with pytest.raises(UploadRejectedError):
        publisher.upload_chunk(_session(), b"x" * 500, 0, 1000)
    publisher.close()


def test_upload_chunk_does_not_retry_through_request_with_retry() -> None:
    """upload_chunk PUTs directly against the session URI (a bearer-style
    capability URL, never re-authorized with the adapter's own access
    token) -- unlike _request_with_retry-based calls, it must not attach
    an Authorization header."""
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in {k.lower() for k in request.headers}
        return httpx.Response(200)

    publisher = _publisher(handler)
    publisher.upload_chunk(_session(), b"x" * 500, 0, 1000)
    publisher.close()


# -- query_upload_offset -------------------------------------------------------

def test_query_upload_offset_always_raises_session_expired() -> None:
    """No documented way to query TikTok's confirmed offset -- see
    docs/PUBLISHING.md. This forces a fresh session rather than guessing."""
    publisher = _publisher(lambda r: httpx.Response(200))
    with pytest.raises(UploadSessionExpiredError):
        publisher.query_upload_offset(_session(), 1000)
    publisher.close()


# -- get_processing_status -------------------------------------------------------

def test_get_processing_status_succeeded() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"status": "PUBLISH_COMPLETE", "publicly_available_post_id": "post-1"},
        "error": {"code": "ok"},
    }))
    status = publisher.get_processing_status("publish-id-1")
    assert status.processing_status == "succeeded"
    assert status.failure_reason is None
    assert status.publication_url is None  # never fabricated -- see docs/PUBLISHING.md
    publisher.close()


def test_get_processing_status_still_processing() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"status": "PROCESSING_UPLOAD"}, "error": {"code": "ok"},
    }))
    status = publisher.get_processing_status("publish-id-1")
    assert status.processing_status == "processing"
    publisher.close()


def test_get_processing_status_failed_reports_reason() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"status": "FAILED", "fail_reason": "duration_check_failed"}, "error": {"code": "ok"},
    }))
    status = publisher.get_processing_status("publish-id-1")
    assert status.processing_status == "failed"
    assert status.failure_reason == "duration_check_failed"
    publisher.close()


def test_get_processing_status_sent_to_inbox_is_failed_not_forever_processing() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"status": "SEND_TO_USER_INBOX"}, "error": {"code": "ok"},
    }))
    status = publisher.get_processing_status("publish-id-1")
    assert status.processing_status == "failed"
    assert status.failure_reason == "sent_to_inbox_not_published"
    publisher.close()


def test_get_processing_status_unknown_status_keeps_polling_never_succeeds() -> None:
    publisher = _publisher(lambda r: httpx.Response(200, json={
        "data": {"status": "SOME_FUTURE_STATUS_THIS_ADAPTER_DOES_NOT_KNOW"}, "error": {"code": "ok"},
    }))
    status = publisher.get_processing_status("publish-id-1")
    assert status.processing_status == "processing"
    publisher.close()


# -- build_post_text -------------------------------------------------------

def test_build_post_text_accepts_a_normal_title() -> None:
    assert build_post_text("A perfectly normal short-form video title") == \
        "A perfectly normal short-form video title"


def test_build_post_text_rejects_over_length() -> None:
    with pytest.raises(MetadataInvalidError, match="2200"):
        build_post_text("x" * (MAX_POST_TEXT_UTF16_UNITS + 1))


def test_build_post_text_accepts_exactly_the_limit() -> None:
    build_post_text("x" * MAX_POST_TEXT_UTF16_UNITS)


@pytest.mark.parametrize("forbidden", [
    r"see C:\Users\me\umma\jobs\secret.mp4",
    "asset at /jobs/12345678-1234-1234-1234-123456789012/final",
    "job 12345678-1234-1234-1234-123456789012 is done",
    "api_key: sk-abc123",
    "client_secret=abc123",
    "signed url ...&Signature=abc&Expires=1234567890",
    "callback at http://127.0.0.1:8080/cb",
])
def test_build_post_text_rejects_forbidden_markers(forbidden: str) -> None:
    with pytest.raises(MetadataInvalidError, match="disallowed"):
        build_post_text(forbidden)


# -- TikTokPostOptions -------------------------------------------------------

def test_post_options_defaults_are_maximally_restrictive() -> None:
    options = TikTokPostOptions()
    assert options.disable_comment is True
    assert options.disable_duet is True
    assert options.disable_stitch is True
    assert options.is_branded_content is False
    assert options.is_own_brand_content is False
    assert options.is_ai_generated is False


def test_post_options_as_platform_options_shape() -> None:
    options = TikTokPostOptions(disable_comment=False, video_cover_timestamp_ms=500, is_ai_generated=True)
    shape = options.as_platform_options()
    assert shape == {
        "disable_comment": False, "disable_duet": True, "disable_stitch": True,
        "video_cover_timestamp_ms": 500, "brand_content_toggle": False,
        "brand_organic_toggle": False, "is_aigc": True,
    }


# -- validate_publish_options -------------------------------------------------------

def _creator_info(**overrides) -> CreatorInfo:
    defaults: dict = dict(
        account_identifier="creator1", display_name="Creator One",
        allowed_privacy_values=frozenset({"SELF_ONLY", "PUBLIC_TO_EVERYONE"}),
        comments_configurable=True, remix_configurable=True,
    )
    defaults.update(overrides)
    return CreatorInfo(**defaults)


def test_validate_publish_options_passes_for_allowed_privacy_and_defaults() -> None:
    validate_publish_options(_creator_info(), "SELF_ONLY", TikTokPostOptions())


def test_validate_publish_options_rejects_privacy_not_in_allowed_set() -> None:
    """Distinct from the unaudited-app case below: the allowed set here is
    NOT exactly {SELF_ONLY}, so this is an ordinary bad-value rejection,
    not an app-review signal."""
    with pytest.raises(PublisherPrivacyNotAllowedError):
        validate_publish_options(
            _creator_info(allowed_privacy_values=frozenset({"SELF_ONLY", "FOLLOWER_OF_CREATOR"})),
            "PUBLIC_TO_EVERYONE", TikTokPostOptions(),
        )


def test_validate_publish_options_unaudited_app_pattern_raises_app_review_required() -> None:
    """allowed_privacy_values == {SELF_ONLY} exactly (not a superset) is
    the documented signature of an unaudited app -- distinct error from a
    generic bad-privacy-value rejection."""
    with pytest.raises(PublisherAppReviewRequiredError):
        validate_publish_options(
            _creator_info(allowed_privacy_values=frozenset({"SELF_ONLY"})),
            "MUTUAL_FOLLOW_FRIENDS", TikTokPostOptions(),
        )


def test_validate_publish_options_rejects_branded_content_as_self_only() -> None:
    with pytest.raises(MetadataInvalidError, match="branded content"):
        validate_publish_options(_creator_info(), "SELF_ONLY", TikTokPostOptions(is_branded_content=True))


def test_validate_publish_options_allows_branded_content_when_not_self_only() -> None:
    validate_publish_options(
        _creator_info(), "PUBLIC_TO_EVERYONE", TikTokPostOptions(is_branded_content=True, disable_comment=False),
    )


def test_validate_publish_options_rejects_enabling_comments_when_platform_forces_disabled() -> None:
    with pytest.raises(MetadataInvalidError, match="comments"):
        validate_publish_options(
            _creator_info(comments_configurable=False), "SELF_ONLY", TikTokPostOptions(disable_comment=False),
        )


def test_validate_publish_options_allows_disabled_comments_even_when_not_configurable() -> None:
    validate_publish_options(
        _creator_info(comments_configurable=False), "SELF_ONLY", TikTokPostOptions(disable_comment=True),
    )


def test_validate_publish_options_rejects_enabling_remix_when_platform_forces_disabled() -> None:
    with pytest.raises(MetadataInvalidError, match="duet/stitch"):
        validate_publish_options(
            _creator_info(remix_configurable=False), "SELF_ONLY",
            TikTokPostOptions(disable_duet=False, disable_stitch=True),
        )


def test_validate_publish_options_rejects_when_creator_has_warnings() -> None:
    with pytest.raises(PublisherCreatorNotEligibleError):
        validate_publish_options(
            _creator_info(warnings=["account flagged for review"]), "SELF_ONLY", TikTokPostOptions(),
        )
