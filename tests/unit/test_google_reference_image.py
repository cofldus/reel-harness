"""Contract tests for the real Google reference-image adapter.

**These prove protocol conformance only, never live success.** Every test
here drives the adapter with an injected fake client; no socket is ever
opened (tests/conftest.py's network block would refuse one anyway), and
no credential exists on this machine. "The adapter maps a SAFETY
finish_reason to ContentPolicyRefusedError" is a fact these tests
establish; "the adapter works against Google" is not, and nothing here
should be read as claiming it.

The response shapes below mirror the installed google-genai SDK's real
types (candidates[].finish_reason, candidate.content.parts[].inline_data,
prompt_feedback.block_reason, errors.ClientError.code), introspected
rather than guessed.
"""
from __future__ import annotations

import pytest

from reel_harness.core.errors import (
    ContentPolicyRefusedError,
    ProviderAuthError,
    ProviderNotConfiguredError,
    TransientProviderError,
)
from reel_harness.providers.base import ReferenceImageRequest
from reel_harness.providers.google_reference_image import (
    SYNTHID_WATERMARK,
    GoogleReferenceImageProvider,
)

try:  # the `google` extra is optional -- see pyproject.toml
    import google.genai  # noqa: F401

    GOOGLE_SDK_PRESENT = True
except ImportError:  # pragma: no cover - depends on which extras are installed
    GOOGLE_SDK_PRESENT = False

# The adapter builds its request out of the SDK's own types
# (types.Part.from_bytes, types.ImageConfig), so these tests genuinely
# need the extra installed -- skipped cleanly without it, matching the
# FFMPEG_PRESENT / REEL_HARNESS_TEST_POSTGRES_URL convention. CI installs
# `--extra google` so they actually run there rather than always skipping.
pytestmark = pytest.mark.skipif(
    not GOOGLE_SDK_PRESENT, reason="requires the optional `google` extra (google-genai)",
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-image-payload"


class _Blob:
    def __init__(self, data, mime_type="image/png") -> None:
        self.data = data
        self.mime_type = mime_type


class _Part:
    def __init__(self, data=None) -> None:
        self.inline_data = _Blob(data) if data is not None else None


class _Content:
    def __init__(self, parts) -> None:
        self.parts = parts


class _Candidate:
    def __init__(self, parts=(), finish_reason="STOP", finish_message=None) -> None:
        self.content = _Content(list(parts))
        self.finish_reason = finish_reason
        self.finish_message = finish_message


class _Feedback:
    def __init__(self, block_reason=None) -> None:
        self.block_reason = block_reason


class _Response:
    def __init__(self, candidates=(), block_reason=None, response_id="resp-1") -> None:
        self.candidates = list(candidates)
        self.prompt_feedback = _Feedback(block_reason)
        self.response_id = response_id


class _FakeClient:
    """Stands in for genai.Client. Records what it was called with, so the
    request-translation assertions have something real to look at."""

    def __init__(self, response=None, error=None) -> None:
        self._response = response
        self._error = error
        self.calls = []
        self.models = self

    def generate_content(self, *, model, contents, config):
        self.calls.append({"model": model, "contents": contents, "config": config})
        if self._error is not None:
            raise self._error
        return self._response


def _ok_response():
    return _Response([_Candidate(parts=[_Part(PNG_BYTES)])])


def _provider(**kwargs):
    return GoogleReferenceImageProvider(api_key="test-key", **kwargs)


def _request(**kwargs):
    defaults = {
        "prompt": "a single fictional adult actor, neutral expression",
        "aspect_ratio": "9:16", "resolution": "1k", "correlation_id": "p:c:face:fp",
    }
    return ReferenceImageRequest(**{**defaults, **kwargs})


# -- the happy path ------------------------------------------------------

def test_generates_and_persists_an_image(tmp_path) -> None:
    client = _FakeClient(_ok_response())
    result = _provider(client=client).generate_reference(_request(), tmp_path)

    assert result.image_path.exists()
    assert result.image_path.read_bytes() == PNG_BYTES
    assert result.provider_id == "google"
    assert result.checksum_sha256
    assert result.request_id == "resp-1"


def test_every_result_records_the_synthid_watermark(tmp_path) -> None:
    """Google watermarks all generated imagery with no removal option.
    Whether Veo accepts watermarked images as character-reference input is
    an OPEN question, so the fact is carried rather than dropped."""
    result = _provider(client=_FakeClient(_ok_response())).generate_reference(_request(), tmp_path)
    assert result.watermark == SYNTHID_WATERMARK
    assert GoogleReferenceImageProvider.capabilities.watermarked is True


def test_license_names_the_source_rather_than_claiming_a_grant(tmp_path) -> None:
    result = _provider(client=_FakeClient(_ok_response())).generate_reference(_request(), tmp_path)
    assert result.license.startswith("GOOGLE_GENERATED:")
    assert result.model_id in result.license


# -- request translation -------------------------------------------------

def test_character_references_are_sent_as_image_parts(tmp_path) -> None:
    """The chaining the whole reference sheet depends on is only real if
    the reference actually reaches the API as an image part."""
    face = tmp_path / "face.png"
    face.write_bytes(PNG_BYTES)
    client = _FakeClient(_ok_response())
    _provider(client=client).generate_reference(
        _request(character_reference_paths=[face]), tmp_path,
    )

    contents = client.calls[0]["contents"]
    assert contents[0].startswith("a single fictional adult actor")
    assert len(contents) == 2, "prompt + one reference image"
    assert contents[1].inline_data.data == PNG_BYTES
    assert contents[1].inline_data.mime_type == "image/png"


def test_config_requests_adults_only_and_the_projects_aspect(tmp_path) -> None:
    client = _FakeClient(_ok_response())
    _provider(client=client).generate_reference(_request(aspect_ratio="16:9"), tmp_path)

    config = client.calls[0]["config"]
    assert config.response_modalities == ["IMAGE"]
    assert config.image_config.aspect_ratio == "16:9"
    assert config.image_config.person_generation == "ALLOW_ADULT"


def test_resolution_is_translated_into_the_vendor_spelling(tmp_path) -> None:
    """This codebase says "1k"; the SDK says "1K". The dialect stays in
    the adapter."""
    client = _FakeClient(_ok_response())
    _provider(client=client).generate_reference(_request(resolution="1k"), tmp_path)
    assert client.calls[0]["config"].image_config.image_size == "1K"


def test_oversized_resolutions_are_not_offered() -> None:
    """Veo caps reference-driven runs at 720p, so 2K/4K would cost more
    and buy nothing -- offering them would be a trap."""
    caps = GoogleReferenceImageProvider.capabilities
    assert caps.supported_resolutions == frozenset({"512", "1k"})
    with pytest.raises(ValueError, match="unsupported resolution"):
        _provider(client=_FakeClient()).validate_request(_request(resolution="4K"))


def test_too_many_character_references_are_refused_locally(tmp_path) -> None:
    face = tmp_path / "f.png"
    face.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="exceeds the documented maximum"):
        _provider(client=_FakeClient()).validate_request(
            _request(character_reference_paths=[face] * 5),
        )


def test_a_missing_reference_file_is_refused_before_any_call(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _provider(client=_FakeClient()).validate_request(
            _request(character_reference_paths=[tmp_path / "nope.png"]),
        )


# -- refusals ------------------------------------------------------------

@pytest.mark.parametrize("finish_reason", [
    "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
    "BLOCKLIST", "SPII", "RECITATION", "IMAGE_RECITATION",
])
def test_every_refusal_finish_reason_maps_to_a_policy_refusal(tmp_path, finish_reason) -> None:
    """All of these route to REVIEW_REQUIRED, never a retry: re-asking
    with the same prompt could only reach the same refusal."""
    client = _FakeClient(_Response([_Candidate(finish_reason=finish_reason)]))
    with pytest.raises(ContentPolicyRefusedError) as exc:
        _provider(client=client).generate_reference(_request(), tmp_path)
    assert exc.value.code == "CONTENT_POLICY_REVIEW"
    assert exc.value.retryable is False
    assert finish_reason in str(exc.value)


def test_a_blocked_prompt_is_a_policy_refusal(tmp_path) -> None:
    """A blocked prompt never reached generation at all -- still a human
    decision, not a retry."""
    client = _FakeClient(_Response([], block_reason="PROHIBITED_CONTENT"))
    with pytest.raises(ContentPolicyRefusedError, match="block_reason"):
        _provider(client=client).generate_reference(_request(), tmp_path)


def test_an_enum_finish_reason_is_read_like_a_string(tmp_path) -> None:
    """The real SDK returns enums, not strings."""
    from google.genai import types

    client = _FakeClient(_Response([_Candidate(finish_reason=types.FinishReason.IMAGE_SAFETY)]))
    with pytest.raises(ContentPolicyRefusedError):
        _provider(client=client).generate_reference(_request(), tmp_path)


def test_no_image_without_a_refusal_is_transient_not_a_refusal(tmp_path) -> None:
    """NO_IMAGE may well succeed on a retry, so calling it a policy
    refusal would strand a shot in review for no reason."""
    client = _FakeClient(_Response([_Candidate(finish_reason="NO_IMAGE")]))
    with pytest.raises(TransientProviderError):
        _provider(client=client).generate_reference(_request(), tmp_path)


def test_no_candidates_is_transient(tmp_path) -> None:
    with pytest.raises(TransientProviderError, match="no candidates"):
        _provider(client=_FakeClient(_Response([]))).generate_reference(_request(), tmp_path)


# -- transport errors ----------------------------------------------------

class _SdkError(Exception):
    def __init__(self, code) -> None:
        super().__init__(f"http {code}")
        self.code = code


@pytest.mark.parametrize("code", [401, 403])
def test_auth_errors_are_never_retried(tmp_path, code) -> None:
    client = _FakeClient(error=_SdkError(code))
    with pytest.raises(ProviderAuthError) as exc:
        _provider(client=client).generate_reference(_request(), tmp_path)
    assert exc.value.retryable is False


def test_auth_error_never_echoes_the_credential(tmp_path) -> None:
    client = _FakeClient(error=_SdkError(403))
    provider = GoogleReferenceImageProvider(api_key="super-secret-key", client=client)
    with pytest.raises(ProviderAuthError) as exc:
        provider.generate_reference(_request(), tmp_path)
    assert "super-secret-key" not in str(exc.value)


@pytest.mark.parametrize("code", [429, 500, 503])
def test_rate_limits_and_server_errors_are_retryable(tmp_path, code) -> None:
    client = _FakeClient(error=_SdkError(code))
    with pytest.raises(TransientProviderError) as exc:
        _provider(client=client).generate_reference(_request(), tmp_path)
    assert exc.value.retryable is True


# -- cost ----------------------------------------------------------------

def test_cost_is_the_configured_price() -> None:
    estimate = _provider(price_per_image_usd=0.067, client=_FakeClient()).estimate_cost(_request())
    assert estimate.known is True
    assert estimate.amount == 0.067
    assert estimate.currency == "USD"


def test_an_unset_price_reports_unknown_rather_than_guessing() -> None:
    """A vendor's price list is not this project's to promise. Unknown
    propagates, and a budgeted project then refuses to run rather than
    spending against a number nobody stands behind."""
    estimate = _provider(price_per_image_usd=None, client=_FakeClient()).estimate_cost(_request())
    assert estimate.known is False
    assert estimate.amount is None


# -- configuration -------------------------------------------------------

def test_missing_credentials_fail_loudly_at_client_construction() -> None:
    provider = GoogleReferenceImageProvider(api_key="", use_vertex=False)
    with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_API_KEY"):
        provider.generate_reference(_request(), __import__("pathlib").Path("."))


def test_vertex_mode_requires_a_project_and_location() -> None:
    provider = GoogleReferenceImageProvider(use_vertex=True, project="", location="")
    with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_PROJECT"):
        provider.generate_reference(_request(), __import__("pathlib").Path("."))


def test_selecting_google_without_credentials_fails_at_startup() -> None:
    """Never at first use, halfway through a paid casting run."""
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    settings = Settings(reference_image_provider="google", google_api_key="")
    with pytest.raises(ProviderConfigurationError, match="GOOGLE_API_KEY"):
        validate_provider_settings(settings)
