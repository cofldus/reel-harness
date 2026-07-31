"""FakeCinematicVideoProvider contract (providers.fake_cinematic_video)
and the cinematic registry entries -- the submit/poll/download lifecycle
the real adapter will implement, exercised deterministically with zero
network. Clip materialization uses the REAL local ffmpeg (skipped where
absent), because the fake provider never bypasses the BLOCKED_DEPENDENCY
discipline."""
from __future__ import annotations

import pytest

from reel_harness.core.errors import ProviderNotConfiguredError
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.base import CinematicGenerationRequest
from reel_harness.providers.fake_cinematic_video import FakeCinematicVideoProvider
from reel_harness.providers.fake_stock_media import FAKE_TEST_LICENSE
from reel_harness.providers.registry import (
    cinematic_provider_snapshot,
    resolve_cinematic_video_for_snapshot,
    resolve_cinematic_video_provider,
)

FFMPEG_PRESENT = check_ffmpeg_available().all_available


def _request(**overrides) -> CinematicGenerationRequest:
    defaults = dict(
        prompt="a virtual adult actor turns slowly toward the window",
        duration_sec=2.0, aspect_ratio="9:16", resolution="360p",
        correlation_id="proj1:shot1:take1",
    )
    defaults.update(overrides)
    return CinematicGenerationRequest(**defaults)


def test_capabilities_are_exposed_and_never_claim_unsupported_features() -> None:
    provider = FakeCinematicVideoProvider()
    caps = provider.capabilities
    assert caps.native_audio is False  # the fake never pretends to generate audio
    assert caps.lip_sync is False
    assert "9:16" in caps.supported_aspect_ratios


def test_validate_request_rejects_unsupported_parameters() -> None:
    provider = FakeCinematicVideoProvider()
    with pytest.raises(ValueError, match="duration"):
        provider.validate_request(_request(duration_sec=99.0))
    with pytest.raises(ValueError, match="aspect ratio"):
        provider.validate_request(_request(aspect_ratio="4:3"))
    with pytest.raises(ValueError, match="resolution"):
        provider.validate_request(_request(resolution="8k"))
    with pytest.raises(ValueError, match="reference image"):
        provider.validate_request(_request(reference_image_paths=["/nonexistent/ref.png"]))


def test_estimate_cost_is_deterministic_and_obviously_fake() -> None:
    provider = FakeCinematicVideoProvider()
    estimate = provider.estimate_cost(_request(duration_sec=4.0))
    assert estimate.known is True
    assert estimate.amount == 0.04
    assert estimate.currency == "FAKE"


def test_identical_submissions_share_a_provider_reference() -> None:
    """The deterministic reference is what lets idempotency tests observe
    duplicate submissions -- same prompt + correlation_id, same reference."""
    provider = FakeCinematicVideoProvider()
    first = provider.create_generation(_request())
    second = provider.create_generation(_request())
    assert first.provider_job_reference == second.provider_job_reference
    different = provider.create_generation(_request(correlation_id="proj1:shot1:take2"))
    assert different.provider_job_reference != first.provider_job_reference


def test_status_lifecycle_generating_then_succeeded() -> None:
    provider = FakeCinematicVideoProvider(mode="generating_once")
    handle = provider.create_generation(_request())
    assert provider.get_generation_status(handle).state == "generating"
    assert provider.get_generation_status(handle).state == "succeeded"


def test_moderated_state_is_distinct_from_failed() -> None:
    provider = FakeCinematicVideoProvider(mode="moderated")
    handle = provider.create_generation(_request())
    status = provider.get_generation_status(handle)
    assert status.state == "moderated"
    assert status.moderation_reason
    assert status.failure_reason is None


def test_cancel_removes_the_generation() -> None:
    provider = FakeCinematicVideoProvider()
    handle = provider.create_generation(_request())
    provider.cancel_generation(handle)
    assert provider.get_generation_status(handle).state == "failed"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to materialize the fake clip")
def test_download_result_produces_a_real_probeable_mp4(tmp_path) -> None:
    from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
    from reel_harness.media.runner import run

    provider = FakeCinematicVideoProvider()
    handle = provider.create_generation(_request(duration_sec=2.0))
    result = provider.download_result(handle, tmp_path / "takes")

    assert result.video_path.exists()
    assert result.license == FAKE_TEST_LICENSE
    assert result.checksum_sha256
    deps = check_ffmpeg_available()
    probe = run(build_ffprobe_argv(deps.ffprobe.path, result.video_path))
    assert probe.returncode == 0
    validated = parse_ffprobe_output(probe.stdout)
    assert validated.video_codec == "h264"
    assert validated.width == 360
    assert validated.height == 640


def test_registry_resolves_fake_and_rejects_unknown() -> None:
    provider = resolve_cinematic_video_provider("fake")
    assert provider.provider_id == "fake"
    with pytest.raises(NotImplementedError):
        resolve_cinematic_video_provider("veo")


def test_snapshot_and_resolution_ladder() -> None:
    snapshot = cinematic_provider_snapshot(None)
    assert snapshot == {"cinematic_provider": "fake"}

    resolved = resolve_cinematic_video_for_snapshot(snapshot, None)
    assert resolved.provider_id == "fake"

    # A project pinned to an unregistered provider fails loudly on use --
    # never a silent fallback to a different provider.
    pinned = resolve_cinematic_video_for_snapshot({"cinematic_provider": "veo"}, None)
    with pytest.raises(ProviderNotConfiguredError):
        pinned.create_generation(_request())


def test_unknown_cinematic_provider_setting_fails_startup_validation() -> None:
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    with pytest.raises(ProviderConfigurationError, match="cinematic provider"):
        validate_provider_settings(Settings(_env_file=None, cinematic_provider="veo"))
