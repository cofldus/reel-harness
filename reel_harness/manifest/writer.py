from __future__ import annotations

from reel_harness.manifest.schema import AssetInfo, LLMInfo, Manifest, RenderInfo, TTSInfo, ValidationInfo
from reel_harness.media.ffprobe_validate import ValidationResult
from reel_harness.pipeline.stages import AssetFetchResult, RenderOutput
from reel_harness.storage.base import StorageBackend


def build_manifest(
    job,
    assets: list[AssetFetchResult],
    tts_provider_id: str,
    tts_voice_id: str,
    render: RenderOutput | None = None,
    validation: ValidationResult | None = None,
    final_video_checksum: str | None = None,
) -> Manifest:
    script = job.script
    return Manifest(
        job_id=job.id,
        created_at=job.created_at,
        topic=job.topic,
        script_title=script["title"],
        llm=LLMInfo(
            provider_id=script["llm_provider_id"],
            model_id=script["llm_model_id"],
            prompt_version=script["prompt_version"],
        ),
        tts=TTSInfo(provider_id=tts_provider_id, voice_id=tts_voice_id),
        assets=[
            AssetInfo(
                scene_index=a.scene_index,
                source_url=a.source_url,
                author=a.author,
                license_type=a.license_type,
                checksum_sha256=a.checksum_sha256,
            )
            for a in assets
        ],
        render=RenderInfo(ffmpeg_version=render.ffmpeg_version, width=render.width, height=render.height)
        if render is not None else RenderInfo(),
        validation=ValidationInfo(
            duration_sec=validation.duration_sec,
            video_codec=validation.video_codec,
            audio_codec=validation.audio_codec,
            has_audio_stream=validation.has_audio_stream,
        ) if validation is not None else ValidationInfo(),
        final_video_checksum_sha256=final_video_checksum,
    )


def write_manifest(storage: StorageBackend, job_id: str, manifest: Manifest) -> None:
    storage.write_bytes(job_id, "manifest.json", manifest.model_dump_json(indent=2).encode("utf-8"))
