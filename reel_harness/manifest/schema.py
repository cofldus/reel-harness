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


class AssetInfo(BaseModel):
    scene_index: int
    source_url: str
    author: str | None
    license_type: str | None
    checksum_sha256: str


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
# providers stamp every asset they produce with this exact string.
NON_PUBLISHABLE_LICENSES = frozenset({"FAKE_TEST_LICENSE"})


class Manifest(BaseModel):
    schema_version: int = MANIFEST_SCHEMA_VERSION
    job_id: str
    created_at: datetime
    topic: str
    script_title: str
    llm: LLMInfo
    tts: TTSInfo
    assets: list[AssetInfo]
    render: RenderInfo = RenderInfo()
    validation: ValidationInfo = ValidationInfo()
    final_video_checksum_sha256: str | None = None
    approval: ApprovalInfo = ApprovalInfo()
    publish: PublishInfo = PublishInfo()


def is_publish_eligible(manifest: Manifest) -> bool:
    """False if any asset carries a non-publishable license (FAKE_TEST_LICENSE
    today), if no asset license is recorded at all, or if the job hasn't been
    approved yet. This is the license/approval half of a publish gate; there is
    no Publisher implementation yet for it to actually guard (see
    docs/ARCHITECTURE.md Extension points), so nothing currently calls this in
    a real publish path -- it exists so that check is not designed after the
    fact once publishing is added.
    """
    if manifest.approval.decision != "approve":
        return False
    if not manifest.assets:
        return False
    for asset in manifest.assets:
        if asset.license_type is None or asset.license_type in NON_PUBLISHABLE_LICENSES:
            return False
    return True
