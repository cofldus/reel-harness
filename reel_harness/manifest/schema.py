from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

MANIFEST_SCHEMA_VERSION = 1


class LLMInfo(BaseModel):
    provider_id: str
    model_id: str
    prompt_version: str


class TTSInfo(BaseModel):
    provider_id: str
    voice_id: str
    model_id: str | None = None


class TTSAudioInfo(BaseModel):
    """Per-scene record of the (normalized) audio actually used for the render."""

    scene_index: int
    checksum_sha256: str | None = None
    duration_sec: float | None = None


class AssetInfo(BaseModel):
    scene_index: int
    source_url: str
    author: str | None
    license_type: str | None
    checksum_sha256: str
    # Phase 2D: provenance/license metadata a real stock-media provider fills
    # in (all None/False for Fake-provider jobs, which is exactly why they
    # stay publish_eligible=False regardless of approval -- see
    # is_publish_eligible below).
    provider_id: str = ""
    provider_asset_id: str = ""
    source_page_url: str | None = None
    creator_url: str | None = None
    commercial_use_allowed: bool = False
    modification_allowed: bool = False
    attribution_text: str | None = None
    width: int | None = None
    height: int | None = None
    duration_sec: float | None = None


class ApprovalInfo(BaseModel):
    decision: str | None = None
    decided_at: datetime | None = None


class PublishInfo(BaseModel):
    platform: str | None = None
    post_id: str | None = None


class RenderInfo(BaseModel):
    """Populated only once RENDERING actually succeeds against a real ffmpeg
    binary -- left at all-None when rendering is BLOCKED_DEPENDENCY, never
    filled with a guessed value."""

    ffmpeg_version: str | None = None
    width: int | None = None
    height: int | None = None


class ValidationInfo(BaseModel):
    """Populated only once VALIDATING actually succeeds against a real ffprobe
    binary -- see RenderInfo."""

    duration_sec: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    has_audio_stream: bool | None = None


# Licenses that must never be treated as cleared for real publishing. Fake
# and Demo providers stamp every asset they produce with one of these exact
# strings -- see providers/fake_stock_media.py and providers/demo_stock_media.py.
NON_PUBLISHABLE_LICENSES = frozenset({"FAKE_TEST_LICENSE", "DEMO_TEST_LICENSE"})


class Manifest(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    job_id: str
    created_at: datetime
    topic: str
    script_title: str
    llm: LLMInfo
    tts: TTSInfo
    tts_audio: list[TTSAudioInfo] = []
    assets: list[AssetInfo]
    render: RenderInfo = RenderInfo()
    validation: ValidationInfo = ValidationInfo()
    final_video_checksum_sha256: str | None = None
    approval: ApprovalInfo = ApprovalInfo()
    publish: PublishInfo = PublishInfo()


def is_publish_eligible(manifest: Manifest) -> bool:
    """False if any asset carries a non-publishable license (FAKE_TEST_LICENSE
    today), has no license recorded at all, does not allow commercial use or
    modification, or has no attribution text recorded; also False if the job
    hasn't been approved, has no assets, or technical validation didn't pass.
    Ambiguous or missing license information fails closed (False), never
    defaults to eligible. This is the license/approval half of a publish
    gate; there is no Publisher implementation yet for it to actually guard
    (see docs/ARCHITECTURE.md Extension points), so nothing currently calls
    this in a real publish path -- it exists so that check is not designed
    after the fact once publishing is added.
    """
    if manifest.approval.decision != "approve":
        return False
    if not manifest.assets:
        return False
    if manifest.validation.video_codec is None or manifest.validation.audio_codec is None:
        return False
    for asset in manifest.assets:
        if asset.license_type is None or asset.license_type in NON_PUBLISHABLE_LICENSES:
            return False
        if not asset.commercial_use_allowed or not asset.modification_allowed:
            return False
        if not asset.attribution_text:
            return False
    return True
