"""CharacterReferenceProvider contract via the fake adapter
(providers.fake_reference_image) and its registry wiring: capability
reporting, request validation including the character-reference cap,
deterministic output, provenance fields, and the content-policy refusal
path that must reach a human rather than auto-fail or auto-retry."""
from __future__ import annotations

import pytest

from reel_harness.core.errors import (
    ContentPolicyRefusedError,
    ProviderNotConfiguredError,
    TransientProviderError,
)
from reel_harness.providers.base import ReferenceImageRequest
from reel_harness.providers.fake_reference_image import FakeReferenceImageProvider
from reel_harness.providers.fake_stock_media import FAKE_TEST_LICENSE
from reel_harness.providers.registry import (
    cinematic_provider_snapshot,
    resolve_reference_image_for_snapshot,
    resolve_reference_image_provider,
)


def _request(**overrides) -> ReferenceImageRequest:
    defaults = dict(
        prompt="a fictional adult woman, oval face, black short hair, neutral portrait",
        aspect_ratio="9:16", resolution="1k", correlation_id="proj1:char1:face",
    )
    defaults.update(overrides)
    return ReferenceImageRequest(**defaults)


def test_capabilities_are_reported_including_watermark_status() -> None:
    caps = FakeReferenceImageProvider().capabilities
    assert caps.character_reference is True
    assert caps.max_character_references == 4
    # A fake image carries no provenance watermark -- recorded, not implied.
    assert caps.watermarked is False


def test_validate_rejects_unsupported_parameters(tmp_path) -> None:
    provider = FakeReferenceImageProvider()
    with pytest.raises(ValueError, match="resolution"):
        provider.validate_request(_request(resolution="4k"))
    with pytest.raises(ValueError, match="aspect ratio"):
        provider.validate_request(_request(aspect_ratio="21:9"))
    with pytest.raises(ValueError, match="does not exist"):
        provider.validate_request(_request(character_reference_paths=[tmp_path / "missing.png"]))


def test_validate_enforces_the_character_reference_cap(tmp_path) -> None:
    provider = FakeReferenceImageProvider()
    references = []
    for index in range(5):
        path = tmp_path / f"ref{index}.png"
        path.write_bytes(b"x")
        references.append(path)
    with pytest.raises(ValueError, match="exceeds the maximum"):
        provider.validate_request(_request(character_reference_paths=references))


def test_generation_is_deterministic_and_carries_provenance(tmp_path) -> None:
    provider = FakeReferenceImageProvider()
    first = provider.generate_reference(_request(), tmp_path / "refs")
    second = provider.generate_reference(_request(), tmp_path / "refs")

    assert first.image_path.exists()
    assert first.image_path.read_bytes() == second.image_path.read_bytes()
    assert first.checksum_sha256 == second.checksum_sha256
    assert first.license == FAKE_TEST_LICENSE
    assert first.watermark is None
    assert first.cost_amount == 0.01 and first.cost_currency == "FAKE"
    # Real PNG bytes, not a placeholder blob.
    assert first.image_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


def test_different_views_produce_different_images(tmp_path) -> None:
    provider = FakeReferenceImageProvider()
    face = provider.generate_reference(
        _request(correlation_id="proj1:char1:face"), tmp_path / "refs",
    )
    body = provider.generate_reference(
        _request(correlation_id="proj1:char1:full_body"), tmp_path / "refs",
    )
    assert face.image_path != body.image_path
    assert face.checksum_sha256 != body.checksum_sha256


def test_aspect_ratio_changes_the_image_shape(tmp_path) -> None:
    provider = FakeReferenceImageProvider()
    portrait = provider.generate_reference(_request(aspect_ratio="9:16"), tmp_path / "a")
    landscape = provider.generate_reference(_request(aspect_ratio="16:9"), tmp_path / "b")
    assert portrait.image_path.stat().st_size != landscape.image_path.stat().st_size


def test_estimate_cost_is_known_and_deterministic() -> None:
    estimate = FakeReferenceImageProvider().estimate_cost(_request())
    assert estimate.known is True
    assert estimate.amount == 0.01


def test_content_policy_refusal_is_its_own_non_retryable_error(tmp_path) -> None:
    """A safety refusal must be distinguishable from a transient failure:
    retrying the same prompt just reaches the same refusal, so it routes
    to human review instead."""
    provider = FakeReferenceImageProvider(mode="refused")
    with pytest.raises(ContentPolicyRefusedError) as excinfo:
        provider.generate_reference(_request(), tmp_path / "refs")
    assert excinfo.value.code == "CONTENT_POLICY_REVIEW"
    assert excinfo.value.retryable is False


def test_transient_failure_stays_retryable(tmp_path) -> None:
    provider = FakeReferenceImageProvider(mode="timeout")
    with pytest.raises(TransientProviderError) as excinfo:
        provider.generate_reference(_request(), tmp_path / "refs")
    assert excinfo.value.retryable is True


def test_registry_resolves_fake_and_rejects_unknown() -> None:
    assert resolve_reference_image_provider("fake").provider_id == "fake"
    with pytest.raises(NotImplementedError):
        resolve_reference_image_provider("nano-banana")


def test_snapshot_pins_the_reference_provider_and_resolution_ladder(tmp_path) -> None:
    snapshot = cinematic_provider_snapshot(None)
    assert snapshot["reference_image_provider"] == "fake"
    assert resolve_reference_image_for_snapshot(snapshot, None).provider_id == "fake"

    pinned = resolve_reference_image_for_snapshot(
        {"reference_image_provider": "nano-banana"}, None,
    )
    with pytest.raises(ProviderNotConfiguredError):
        pinned.generate_reference(_request(), tmp_path / "refs")


def test_unknown_provider_setting_fails_startup_validation() -> None:
    from reel_harness.config import ProviderConfigurationError, Settings, validate_provider_settings

    with pytest.raises(ProviderConfigurationError, match="reference image provider"):
        validate_provider_settings(Settings(_env_file=None, reference_image_provider="nano-banana"))
