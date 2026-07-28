from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reel_harness.core.errors import (
    DependencyError,
    ReviewRequiredSignal,
    TransientProviderError,
    ValidationFailedError,
)
from reel_harness.core.state_machine import ReasonCode
from reel_harness.media import ffmpeg_render, ffprobe_validate
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.ffprobe_validate import ValidationResult
from reel_harness.media.runner import run
from reel_harness.pipeline.policy import check_policy
from reel_harness.pipeline.script_schema import parse_script
from reel_harness.providers.base import ChannelContext, TTSResult

RENDER_WIDTH = 360
RENDER_HEIGHT = 640


@dataclass
class AssetFetchResult:
    scene_index: int
    local_path: Path
    checksum_sha256: str
    mime_type: str
    source_url: str
    author: str | None
    license_type: str | None


@dataclass
class RenderOutput:
    video_path: Path
    ffmpeg_version: str | None
    width: int
    height: int


def _channel_context(channel) -> ChannelContext:
    return ChannelContext(
        channel_id=channel.id, niche=channel.niche, language=channel.language, style_preset=channel.style_preset,
    )


def run_topic_generating(job, channel, llm) -> None:
    result = llm.generate_topic(_channel_context(channel))
    job.topic = result.topic


def run_script_generating(job, channel, llm) -> None:
    result = llm.generate_script(job.topic, _channel_context(channel))
    script = parse_script(result.raw_text)
    job.script = {
        "title": script.title,
        "scenes": [scene.model_dump() for scene in script.scenes],
        "llm_provider_id": result.provider_id,
        "llm_model_id": result.model_id,
        "prompt_version": result.prompt_version,
    }
    # Correlation metadata only -- never the request/response body or a secret.
    request_id = getattr(result, "request_id", None)
    if request_id:
        job.script["llm_request_id"] = request_id
    usage = getattr(result, "usage", None)
    if usage:
        job.script["llm_token_usage"] = usage


def run_policy_checking(job) -> None:
    check_policy(job.script)


def run_asset_fetching(job, stock_media, storage) -> list[AssetFetchResult]:
    results: list[AssetFetchResult] = []
    for index, scene in enumerate(job.script["scenes"]):
        candidates = stock_media.search(
            scene["visual_query"], orientation="portrait", min_duration=scene["duration_hint_sec"],
        )
        if not candidates:
            raise ReviewRequiredSignal(
                ReasonCode.ASSET_NOT_FOUND.value, f"scene {index}: no candidates for {scene['visual_query']!r}",
            )
        dest_dir = storage.job_dir(job.id) / "assets" / f"scene_{index}"
        asset = stock_media.download(candidates[0], dest_dir)
        results.append(
            AssetFetchResult(
                scene_index=index,
                local_path=asset.local_path,
                checksum_sha256=asset.checksum_sha256,
                mime_type=asset.mime_type,
                source_url=asset.source_url,
                author=asset.author,
                license_type=asset.license_type,
            )
        )
    return results


def run_tts_generating(job, tts, storage, dest_root: Path | None = None) -> list[TTSResult]:
    """`dest_root=None` writes each scene's audio to the official
    jobs/{id}/tts/scene_{i}/ location. The fenced worker instead passes a
    worker-private temp root and promotes validated files to the official
    location only under a held lease -- see worker.runner."""
    root = dest_root if dest_root is not None else storage.job_dir(job.id) / "tts"
    default_voice = getattr(tts, "voice_id", None) or "fake-voice-1"
    results: list[TTSResult] = []
    for index, scene in enumerate(job.script["scenes"]):
        dest_dir = root / f"scene_{index}"
        results.append(tts.synthesize(scene["voiceover"], voice_id=default_voice, lang="en", dest_dir=dest_dir))
    return results


def run_rendering(
    job, assets: list[AssetFetchResult], tts_results: list[TTSResult], storage,
    width: int = RENDER_WIDTH, height: int = RENDER_HEIGHT,
    output_path: Path | None = None,
) -> RenderOutput:
    """`width`/`height` default to the fast-test resolution used by the normal
    per-job pipeline. A separate production-smoke check calls this with
    width=1080, height=1920 to prove the same ffmpeg toolchain produces a
    real target-resolution vertical video, without changing the resolution
    every ordinary Fake-provider job renders at (see docs/STATUS.md).

    `output_path=None` renders straight to the official final/final.mp4
    (removing any stale copy first). The fenced worker instead passes a
    worker-private temp path and promotes it to the official name only after
    proving it still owns the lease -- see worker.runner.
    """
    deps = check_ffmpeg_available()
    if not deps.ffmpeg_available:
        raise DependencyError("ffmpeg executable not found on PATH")
    ffmpeg_path = deps.ffmpeg.path
    assert ffmpeg_path is not None  # guaranteed by ffmpeg_available above

    if output_path is None:
        # Stale-output policy: a final.mp4 left over from an earlier attempt
        # must never be mistaken for this run's result, so it is removed before
        # any rendering starts. If this run fails midway there is no final.mp4.
        output_path = storage.job_dir(job.id) / "final" / "final.mp4"
        if output_path.exists():
            output_path.unlink()

    work_dir = storage.job_dir(job.id) / "render"
    work_dir.mkdir(parents=True, exist_ok=True)
    clip_paths: list[Path] = []
    for asset, tts_result in zip(assets, tts_results, strict=True):
        clip_path = work_dir / f"scene_{asset.scene_index}.mp4"
        argv = ffmpeg_render.render_scene_clip(
            ffmpeg_path, asset.local_path, tts_result.audio_path, clip_path, width, height,
        )
        result = run(argv, timeout=60)
        if result.returncode != 0:
            raise TransientProviderError(f"ffmpeg scene render failed: {result.stderr[-500:]}")
        clip_paths.append(clip_path)

    concat_list_path = work_dir / "concat_list.txt"
    ffmpeg_render.write_concat_list(clip_paths, concat_list_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    result = run(ffmpeg_render.concat_clips_argv(ffmpeg_path, concat_list_path, output_path), timeout=60)
    if result.returncode != 0:
        raise TransientProviderError(f"ffmpeg concat failed: {result.stderr[-500:]}")
    return RenderOutput(video_path=output_path, ffmpeg_version=deps.ffmpeg.version, width=width, height=height)


def run_validating(
    job, video_path: Path, expected_width: int = RENDER_WIDTH, expected_height: int = RENDER_HEIGHT,
    policy: ffprobe_validate.ValidationPolicy | None = None,
) -> ValidationResult:
    """Validates the actual rendered file against the (production-safe by
    default) ValidationPolicy: existence, non-empty, ffprobe-parseable,
    exact resolution, H.264/AAC, audio stream present, duration bounds, and
    faststart atom ordering. Every violated condition is reported together in
    the TECHNICAL_VALIDATION_FAILED summary."""
    policy = policy if policy is not None else ffprobe_validate.DEFAULT_VALIDATION_POLICY

    if not video_path.is_file() or video_path.stat().st_size == 0:
        raise ValidationFailedError("final video is missing or empty")

    deps = check_ffmpeg_available()
    if not deps.ffprobe_available:
        raise DependencyError("ffprobe executable not found on PATH")
    ffprobe_path = deps.ffprobe.path
    assert ffprobe_path is not None  # guaranteed by ffprobe_available above

    result = run(ffprobe_validate.build_ffprobe_argv(ffprobe_path, video_path), timeout=30)
    if result.returncode != 0:
        raise ValidationFailedError(f"ffprobe failed: {result.stderr[-300:]}")

    try:
        validation = ffprobe_validate.parse_ffprobe_output(result.stdout)
    except (ValueError, KeyError) as exc:
        raise ValidationFailedError(f"could not parse ffprobe output: {exc}") from exc

    failures = ffprobe_validate.check_against_policy(validation, expected_width, expected_height, policy)
    if policy.require_faststart and not ffprobe_validate.has_faststart(video_path):
        failures.append("moov atom does not precede mdat (faststart not applied)")
    if failures:
        raise ValidationFailedError("; ".join(failures))
    return validation
