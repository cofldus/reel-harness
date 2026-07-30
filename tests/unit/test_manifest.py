from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from reel_harness.manifest.schema import ApprovalInfo, Manifest, is_publish_eligible
from reel_harness.manifest.writer import build_manifest
from reel_harness.media.ffprobe_validate import ValidationResult
from reel_harness.pipeline.stages import AssetFetchResult, RenderOutput


class _FakeJob:
    id = "job-1"
    created_at = datetime.now(UTC)
    topic = "topic"
    script = {
        "title": "A title", "llm_provider_id": "fake",
        "llm_model_id": "fake-deterministic-v1", "prompt_version": "fake-script-v1",
    }


def _asset(
    index: int, license_type: str | None = "FAKE_TEST_LICENSE",
    commercial_use_allowed: bool = False, modification_allowed: bool = False,
    attribution_text: str | None = None,
) -> AssetFetchResult:
    return AssetFetchResult(
        scene_index=index, local_path=Path(f"scene_{index}.png"), checksum_sha256=f"deadbeef{index}",
        mime_type="image/png", source_url=f"fake://asset/{index}", author="Fake Author", license_type=license_type,
        commercial_use_allowed=commercial_use_allowed, modification_allowed=modification_allowed,
        attribution_text=attribution_text,
    )


def _passing_validation() -> ValidationResult:
    return ValidationResult(
        width=1080, height=1920, duration_sec=12.3, video_codec="h264", has_audio_stream=True, audio_codec="aac",
    )


def test_build_manifest_without_render_leaves_render_and_validation_empty() -> None:
    manifest = build_manifest(_FakeJob(), [_asset(0)], "fake", "fake-voice-1")
    assert manifest.render.ffmpeg_version is None
    assert manifest.validation.has_audio_stream is None
    assert manifest.final_video_checksum_sha256 is None


def test_build_manifest_with_render_and_validation_populates_them() -> None:
    render = RenderOutput(video_path=Path("final.mp4"), ffmpeg_version="6.1.1", width=1080, height=1920)
    validation = ValidationResult(
        width=1080, height=1920, duration_sec=12.3, video_codec="h264", has_audio_stream=True, audio_codec="aac",
    )
    manifest = build_manifest(
        _FakeJob(), [_asset(0)], "fake", "fake-voice-1",
        render=render, validation=validation, final_video_checksum="abc123",
    )
    assert manifest.render.ffmpeg_version == "6.1.1"
    assert manifest.render.width == 1080
    assert manifest.validation.video_codec == "h264"
    assert manifest.validation.has_audio_stream is True
    assert manifest.final_video_checksum_sha256 == "abc123"


def test_manifest_roundtrips_through_json_with_new_fields() -> None:
    render = RenderOutput(video_path=Path("final.mp4"), ffmpeg_version="6.1.1", width=1080, height=1920)
    manifest = build_manifest(_FakeJob(), [_asset(0)], "fake", "fake-voice-1", render=render)
    parsed = Manifest.model_validate_json(manifest.model_dump_json())
    assert parsed.render.ffmpeg_version == "6.1.1"


def test_publish_not_eligible_without_approval() -> None:
    manifest = build_manifest(_FakeJob(), [_asset(0, license_type="CC-BY")], "fake", "fake-voice-1")
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_with_fake_test_license_even_if_approved() -> None:
    manifest = build_manifest(_FakeJob(), [_asset(0, license_type="FAKE_TEST_LICENSE")], "fake", "fake-voice-1")
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_with_demo_test_license_even_if_approved() -> None:
    manifest = build_manifest(_FakeJob(), [_asset(0, license_type="DEMO_TEST_LICENSE")], "demo", "demo-voice")
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_with_missing_license() -> None:
    manifest = build_manifest(_FakeJob(), [_asset(0, license_type=None)], "fake", "fake-voice-1")
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_with_no_assets() -> None:
    manifest = build_manifest(_FakeJob(), [], "fake", "fake-voice-1")
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def _eligible_asset(index: int = 0) -> AssetFetchResult:
    return _asset(
        index, license_type="CC-BY-4.0", commercial_use_allowed=True, modification_allowed=True,
        attribution_text="Photo by Someone",
    )


def test_publish_eligible_when_approved_with_real_license_and_full_metadata() -> None:
    manifest = build_manifest(
        _FakeJob(), [_eligible_asset()], "fake", "fake-voice-1", validation=_passing_validation(),
    )
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is True


def test_publish_not_eligible_without_commercial_use_allowed() -> None:
    manifest = build_manifest(
        _FakeJob(), [_asset(0, license_type="CC-BY-4.0", modification_allowed=True, attribution_text="A")],
        "fake", "fake-voice-1", validation=_passing_validation(),
    )
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_without_modification_allowed() -> None:
    manifest = build_manifest(
        _FakeJob(), [_asset(0, license_type="CC-BY-4.0", commercial_use_allowed=True, attribution_text="A")],
        "fake", "fake-voice-1", validation=_passing_validation(),
    )
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_without_attribution_text() -> None:
    manifest = build_manifest(
        _FakeJob(),
        [_asset(0, license_type="CC-BY-4.0", commercial_use_allowed=True, modification_allowed=True)],
        "fake", "fake-voice-1", validation=_passing_validation(),
    )
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False


def test_publish_not_eligible_without_passing_technical_validation() -> None:
    manifest = build_manifest(_FakeJob(), [_eligible_asset()], "fake", "fake-voice-1")  # no validation passed
    manifest.approval = ApprovalInfo(decision="approve", decided_at=datetime.now(UTC))
    assert is_publish_eligible(manifest) is False
