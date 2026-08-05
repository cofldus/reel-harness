"""Contract tests for the real Vertex AI Veo adapter.

**These prove protocol conformance only, never live success.** Every test
drives the adapter with an injected fake client; no socket is opened, and
no credential exists on this machine. "The adapter maps a filtered
response to a moderated status" is a fact these establish; "the adapter
works against Vertex" is not.

The fake operation/response shapes mirror the installed google-genai
SDK's real types (`GenerateVideosOperation.done/error/response`,
`GenerateVideosResponse.rai_media_filtered_count/_reasons`,
`GeneratedVideo.video`, `Video.video_bytes`), introspected rather than
guessed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from reel_harness.core.errors import (
    ProviderAuthError,
    ProviderNotConfiguredError,
    TransientProviderError,
)
from reel_harness.providers.base import CinematicGenerationRequest
from reel_harness.providers.google_cinematic_video import (
    MAX_REFERENCE_IMAGES,
    REFERENCE_DURATION_SEC,
    REFERENCE_RESOLUTION,
    SUPPORTED_LOCATION,
    GoogleCinematicVideoProvider,
)

try:
    import google.genai  # noqa: F401

    GOOGLE_SDK_PRESENT = True
except ImportError:  # pragma: no cover - depends on installed extras
    GOOGLE_SDK_PRESENT = False

pytestmark = pytest.mark.skipif(
    not GOOGLE_SDK_PRESENT, reason="requires the optional `google` extra (google-genai)",
)

MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"fake-video-payload"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-reference"


class _Video:
    def __init__(self, data=MP4_BYTES) -> None:
        self.video_bytes = data
        self.uri = "https://example.invalid/video"
        self.mime_type = "video/mp4"


class _GeneratedVideo:
    def __init__(self, video=None) -> None:
        self.video = video or _Video()


class _Response:
    def __init__(self, videos=None, filtered=0, reasons=()) -> None:
        self.generated_videos = list(videos) if videos is not None else [_GeneratedVideo()]
        self.rai_media_filtered_count = filtered
        self.rai_media_filtered_reasons = list(reasons)


class _Operation:
    def __init__(self, name="operations/abc", done=False, error=None, response=None) -> None:
        self.name = name
        self.done = done
        self.error = error
        self.response = response


class _Models:
    def __init__(self, outer) -> None:
        self._outer = outer

    def generate_videos(self, *, model, prompt, config):
        self._outer.calls.append({"model": model, "prompt": prompt, "config": config})
        if self._outer.submit_error is not None:
            raise self._outer.submit_error
        return self._outer.operation


class _Operations:
    def __init__(self, outer) -> None:
        self._outer = outer

    def get(self, operation):
        self._outer.polls += 1
        return self._outer.operation


class _Files:
    def __init__(self, outer) -> None:
        self._outer = outer

    def download(self, *, file):
        self._outer.downloads += 1
        file.video_bytes = MP4_BYTES


class _FakeClient:
    def __init__(self, operation=None, submit_error=None) -> None:
        self.operation = operation or _Operation()
        self.submit_error = submit_error
        self.calls: list[dict] = []
        self.polls = 0
        self.downloads = 0
        self.models = _Models(self)
        self.operations = _Operations(self)
        self.files = _Files(self)


def _provider(**kwargs):
    defaults = {"project": "test-project", "location": SUPPORTED_LOCATION}
    return GoogleCinematicVideoProvider(**{**defaults, **kwargs})


def _request(**kwargs):
    defaults = {
        "prompt": "a single fictional adult actor walks to the window",
        "duration_sec": REFERENCE_DURATION_SEC,
        "aspect_ratio": "9:16",
        "resolution": REFERENCE_RESOLUTION,
        "correlation_id": "p:s:1:fp",
    }
    return CinematicGenerationRequest(**{**defaults, **kwargs})


# -- documented limits, enforced locally ---------------------------------

def test_too_many_reference_images_are_refused_before_submission(tmp_path) -> None:
    reference = tmp_path / "ref.png"
    reference.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="exceeds the documented maximum"):
        _provider(client=_FakeClient()).validate_request(
            _request(reference_image_paths=[reference] * (MAX_REFERENCE_IMAGES + 1)),
        )


def test_reference_driven_runs_are_fixed_at_eight_seconds(tmp_path) -> None:
    """The API fixes duration when references are attached, so asking for
    anything else would silently get something different -- which costs a
    generation to discover."""
    reference = tmp_path / "ref.png"
    reference.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="fixed at 8.0s"):
        _provider(client=_FakeClient()).validate_request(
            _request(reference_image_paths=[reference], duration_sec=4.0),
        )


def test_reference_driven_runs_are_fixed_at_720p(tmp_path) -> None:
    reference = tmp_path / "ref.png"
    reference.write_bytes(PNG_BYTES)
    with pytest.raises(ValueError, match="fixed at 720p"):
        _provider(client=_FakeClient()).validate_request(
            _request(reference_image_paths=[reference], resolution="1080p"),
        )


def test_a_missing_reference_file_is_refused(tmp_path) -> None:
    with pytest.raises(ValueError, match="does not exist"):
        _provider(client=_FakeClient()).validate_request(
            _request(reference_image_paths=[tmp_path / "nope.png"]),
        )


def test_an_unsupported_aspect_ratio_is_refused() -> None:
    with pytest.raises(ValueError, match="unsupported aspect ratio"):
        _provider(client=_FakeClient()).validate_request(_request(aspect_ratio="1:1"))


# -- request translation -------------------------------------------------

def test_config_states_adults_only_and_carries_the_seed() -> None:
    client = _FakeClient()
    _provider(client=client).create_generation(_request(seed=4242))
    config = client.calls[0]["config"]
    assert config.person_generation == "allow_adult"
    assert config.seed == 4242
    assert config.number_of_videos == 1
    assert config.duration_seconds == 8


def test_reference_images_are_sent_as_asset_type(tmp_path) -> None:
    """ASSET is what transfers identity. STYLE would transfer look, which
    is not what a character reference is for."""
    from google.genai import types

    reference = tmp_path / "face.png"
    reference.write_bytes(PNG_BYTES)
    client = _FakeClient()
    _provider(client=client).create_generation(_request(reference_image_paths=[reference]))

    config = client.calls[0]["config"]
    assert len(config.reference_images) == 1
    assert config.reference_images[0].reference_type == types.VideoGenerationReferenceType.ASSET
    assert config.reference_images[0].image.image_bytes == PNG_BYTES


def test_audio_generation_is_configurable() -> None:
    client = _FakeClient()
    _provider(client=client, generate_audio=False).create_generation(_request())
    assert client.calls[0]["config"].generate_audio is False


def test_the_handle_carries_the_operation_name() -> None:
    handle = _provider(client=_FakeClient()).create_generation(_request())
    assert handle.provider_job_reference == "operations/abc"
    assert handle.provider_id == "google"


def test_an_operation_without_a_name_is_transient() -> None:
    client = _FakeClient(operation=_Operation(name=""))
    with pytest.raises(TransientProviderError, match="no name"):
        _provider(client=client).create_generation(_request())


# -- polling -------------------------------------------------------------

def test_an_unfinished_operation_reads_as_generating() -> None:
    provider = _provider(client=_FakeClient())
    handle = provider.create_generation(_request())
    assert provider.get_generation_status(handle).state == "generating"


def test_a_finished_operation_reads_as_succeeded() -> None:
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response()
    assert provider.get_generation_status(handle).state == "succeeded"


def test_a_safety_filtered_result_is_moderated_not_failed() -> None:
    """A filtered output is a human decision -- the same prompt would be
    filtered again, so it must never route to a blind retry."""
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response(filtered=1, reasons=["violence"])

    status = provider.get_generation_status(handle)
    assert status.state == "moderated"
    assert "violence" in status.moderation_reason


def test_an_operation_error_reads_as_failed_with_its_message() -> None:
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.error = {"code": 3, "message": "bad argument"}
    status = provider.get_generation_status(handle)
    assert status.state == "failed"
    assert "bad argument" in status.failure_reason


def test_a_finished_operation_with_no_video_is_failed() -> None:
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response(videos=[])
    assert provider.get_generation_status(handle).state == "failed"


def test_polling_always_re_reads_the_operation() -> None:
    """A cached done=False would strand a finished generation forever."""
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    provider.get_generation_status(handle)
    provider.get_generation_status(handle)
    assert client.polls == 2


# -- download ------------------------------------------------------------

def test_download_writes_the_bytes_locally(tmp_path) -> None:
    """Generated videos are deleted after two days, so the local file is
    the artifact -- a provider URI is never treated as durable."""
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request(seed=7))
    client.operation.done = True
    client.operation.response = _Response()

    result = provider.download_result(handle, tmp_path)
    assert result.video_path.exists()
    assert result.video_path.read_bytes() == MP4_BYTES
    assert result.checksum_sha256
    assert result.duration_sec == REFERENCE_DURATION_SEC
    assert result.generation_seed == 7
    assert result.license.startswith("GOOGLE_GENERATED:")


def test_download_falls_back_to_the_files_api_when_bytes_are_absent(tmp_path) -> None:
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response(videos=[_GeneratedVideo(_Video(data=None))])

    result = provider.download_result(handle, tmp_path)
    assert client.downloads == 1
    assert result.video_path.read_bytes() == MP4_BYTES


def test_download_reports_the_real_cost(tmp_path) -> None:
    client = _FakeClient()
    provider = _provider(client=client, price_per_second_usd=0.15)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response()

    result = provider.download_result(handle, tmp_path)
    assert result.cost_amount == pytest.approx(0.15 * REFERENCE_DURATION_SEC)
    assert result.cost_currency == "USD"


def test_download_with_no_video_is_transient(tmp_path) -> None:
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    client.operation.done = True
    client.operation.response = _Response(videos=[])
    with pytest.raises(TransientProviderError):
        provider.download_result(handle, tmp_path)


# -- cost ----------------------------------------------------------------

def test_cost_scales_with_duration() -> None:
    estimate = _provider(client=_FakeClient(), price_per_second_usd=0.15).estimate_cost(_request())
    assert estimate.known is True
    assert estimate.amount == pytest.approx(1.2)
    assert estimate.currency == "USD"


def test_an_unset_price_reports_unknown() -> None:
    estimate = _provider(client=_FakeClient(), price_per_second_usd=None).estimate_cost(_request())
    assert estimate.known is False
    assert estimate.amount is None


# -- transport errors ----------------------------------------------------

class _SdkError(Exception):
    def __init__(self, code) -> None:
        super().__init__(f"code {code}")
        self.code = code


@pytest.mark.parametrize("code", [401, 403, 7, 16])
def test_auth_errors_are_never_retried(code) -> None:
    client = _FakeClient(submit_error=_SdkError(code))
    with pytest.raises(ProviderAuthError) as exc:
        _provider(client=client).create_generation(_request())
    assert exc.value.retryable is False


def test_auth_error_never_echoes_a_credential() -> None:
    client = _FakeClient(submit_error=_SdkError(403))
    provider = _provider(client=client, api_key="super-secret", use_vertex=False)
    with pytest.raises(ProviderAuthError) as exc:
        provider.create_generation(_request())
    assert "super-secret" not in str(exc.value)


@pytest.mark.parametrize("code", [429, 8, 500, 503])
def test_rate_limits_and_server_errors_are_retryable(code) -> None:
    client = _FakeClient(submit_error=_SdkError(code))
    with pytest.raises(TransientProviderError) as exc:
        _provider(client=client).create_generation(_request())
    assert exc.value.retryable is True


# -- configuration -------------------------------------------------------

def test_vertex_mode_requires_a_project() -> None:
    provider = GoogleCinematicVideoProvider(project="", use_vertex=True)
    with pytest.raises(ProviderNotConfiguredError, match="GOOGLE_PROJECT"):
        provider.create_generation(_request())


def test_an_unsupported_region_is_refused_with_the_right_one_named() -> None:
    """The GA endpoint serves one region. A project pinned elsewhere would
    fail at generation time, after the operator believed it was set up."""
    provider = GoogleCinematicVideoProvider(
        project="p", location="europe-west4", use_vertex=True,
    )
    with pytest.raises(ProviderNotConfiguredError, match=SUPPORTED_LOCATION):
        provider.create_generation(_request())


def test_selecting_google_without_credentials_fails_at_startup() -> None:
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    settings = Settings(cinematic_provider="google", google_use_vertex=True, google_project="")
    with pytest.raises(ProviderConfigurationError, match="GOOGLE_PROJECT"):
        validate_provider_settings(settings)


def test_selecting_google_in_the_wrong_region_fails_at_startup() -> None:
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    settings = Settings(
        cinematic_provider="google", google_use_vertex=True,
        google_project="p", google_location="europe-west4",
    )
    with pytest.raises(ProviderConfigurationError, match=SUPPORTED_LOCATION):
        validate_provider_settings(settings)


def test_cancel_is_honest_about_being_local_only() -> None:
    """The SDK exposes no cancel for a video operation. Pretending
    otherwise would leave an operator believing a paid generation was
    stopped when it was not."""
    client = _FakeClient()
    provider = _provider(client=client)
    handle = provider.create_generation(_request())
    provider.cancel_generation(handle)  # does not raise, does not claim to have stopped anything
    assert client.calls  # the submission still happened


def test_the_registry_resolves_the_real_adapter() -> None:
    from reel_harness.config import Settings
    from reel_harness.providers.registry import resolve_cinematic_video_provider

    settings = Settings(google_project="p", google_use_vertex=True)
    provider = resolve_cinematic_video_provider("google", settings)
    assert provider.provider_id == "google"
    assert provider.capabilities.character_reference is True


def test_a_snapshot_pinned_project_resolves_the_same_adapter() -> None:
    from reel_harness.config import Settings
    from reel_harness.providers.registry import resolve_cinematic_video_for_snapshot

    settings = Settings(google_project="p", google_use_vertex=True)
    provider = resolve_cinematic_video_for_snapshot({"cinematic_provider": "google"}, settings)
    assert provider.provider_id == "google"


def test_the_sdk_is_never_imported_at_module_level() -> None:
    """The `google` extra is optional, so a machine without it must still
    import the registry and run the whole fake/demo pipeline. That only
    holds if the SDK import stays inside the methods that need it."""
    source = Path(sys.modules[GoogleCinematicVideoProvider.__module__].__file__).read_text(
        encoding="utf-8",
    )
    module_level = [
        line for line in source.splitlines()
        if line.startswith(("import google", "from google"))
    ]
    assert module_level == [], f"SDK imported at module level: {module_level}"


def test_veo_audio_is_off_when_dialogue_is_synthesised_separately() -> None:
    """Telling the video model "no spoken dialogue" is not enough -- it
    was asked politely and spoke anyway, and the film ended up with two
    different voices saying the same line. The audio switch is the one
    instruction it cannot ignore."""
    from reel_harness.config import Settings
    from reel_harness.providers.registry import resolve_cinematic_video_provider

    base = dict(
        google_project="p", google_location="us-central1",
        google_use_vertex=True, cinematic_generate_audio=True,
    )
    speaking = resolve_cinematic_video_provider(
        "google", Settings(**base, fable_dialogue_source="video"),
    )
    assert speaking._generate_audio is True

    silent = resolve_cinematic_video_provider(
        "google", Settings(**base, fable_dialogue_source="tts"),
    )
    assert silent._generate_audio is False
