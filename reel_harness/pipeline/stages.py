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
from reel_harness.pipeline import asset_query, asset_selection
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
    # Phase 2D: provenance/license/selection metadata carried from the
    # selected MediaCandidate/LocalAssetResult through to the Asset DB row
    # and manifest. Defaults keep existing (Fake-provider, test) call sites
    # unchanged.
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
    fps: float | None = None
    request_id: str | None = None
    query_text: str = ""
    selection_score: float = 0.0


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


def run_asset_fetching(
    job, stock_media, storage, *,
    dest_root: Path | None = None,
    search_policy: dict | None = None,
) -> list[AssetFetchResult]:
    """Deterministic per-scene search -> select -> download.

    For each scene: build a sanitized query from the scene's own visual_query
    (never the narration), search, and pick the highest-scoring eligible
    candidate (license/technical hard filters, then aspect/resolution/
    duration/provider-rank scoring, tied broken by provider asset id -- see
    pipeline.asset_selection). If nothing eligible comes back, the query is
    deterministically relaxed (broader text only -- orientation/min
    resolution/duration bounds/safe_search never relax) and searched again.
    Exhausting the relaxation ladder with nothing eligible raises
    ASSET_NOT_FOUND rather than ever loosening a license condition to force a
    success. An asset already selected for an earlier scene in this job is
    excluded from later scenes' candidates.

    `dest_root=None` writes each scene's asset to the official
    jobs/{id}/assets/scene_{i}/ location. The fenced worker instead passes a
    worker-private temp root and promotes validated files to the official
    location only under a held lease -- see worker.runner.
    """
    policy = search_policy or {}
    orientation = policy.get("orientation", "portrait")
    min_width = policy.get("min_width", 480)
    min_height = policy.get("min_height", 480)
    min_duration_floor = policy.get("min_duration_sec", 1.0)
    max_duration = policy.get("max_duration_sec", 60.0)
    safe_search = policy.get("safe_search", True)
    per_page = policy.get("per_page", 15)

    selection_policy = asset_selection.SelectionPolicy(
        min_width=min_width, min_height=min_height,
        min_duration_sec=min_duration_floor, max_duration_sec=max_duration,
        target_orientation=orientation,
    )

    root = dest_root if dest_root is not None else storage.job_dir(job.id) / "assets"
    results: list[AssetFetchResult] = []
    used_ids: set[str] = set()
    for index, scene in enumerate(job.script["scenes"]):
        query = asset_query.build_scene_query(
            scene, orientation=orientation, min_width=min_width, min_height=min_height,
            min_duration=min_duration_floor, max_duration=max_duration, safe_search=safe_search,
        )
        chosen = None
        current = query
        while True:
            candidates = stock_media.search(
                current.text, orientation=current.orientation, min_duration=current.min_duration,
                max_duration=current.max_duration, min_width=current.min_width, min_height=current.min_height,
                per_page=per_page, safe_search=current.safe_search,
                exclude_provider_asset_ids=frozenset(used_ids),
            )
            chosen = asset_selection.select_asset(candidates, selection_policy, exclude_ids=frozenset(used_ids))
            if chosen is not None:
                break
            relaxed = asset_query.relax_query(current)
            if relaxed is None:
                break
            current = relaxed
        if chosen is None:
            raise ReviewRequiredSignal(
                ReasonCode.ASSET_NOT_FOUND.value,
                f"scene {index}: no eligible candidates for {query.text!r} "
                "after exhausting query relaxation",
            )
        used_ids.add(chosen.candidate_id)
        score = asset_selection.score_candidate(chosen, selection_policy)

        dest_dir = root / f"scene_{index}"
        asset = stock_media.download(chosen, dest_dir)
        results.append(
            AssetFetchResult(
                scene_index=index,
                local_path=asset.local_path,
                checksum_sha256=asset.checksum_sha256,
                mime_type=asset.mime_type,
                source_url=asset.source_url,
                author=asset.author,
                license_type=asset.license_type,
                provider_id=asset.provider_id,
                provider_asset_id=asset.provider_asset_id,
                source_page_url=asset.source_page_url,
                creator_url=asset.creator_url,
                commercial_use_allowed=asset.commercial_use_allowed,
                modification_allowed=asset.modification_allowed,
                attribution_text=asset.attribution_text,
                width=asset.width,
                height=asset.height,
                duration_sec=asset.duration_sec,
                fps=asset.fps,
                request_id=asset.request_id,
                query_text=query.text,
                selection_score=score,
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
    burn_subtitles: bool = False,
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

    `burn_subtitles` (Settings.render_burn_subtitles, off by default -- see
    ProviderBundle.render_burn_subtitles) overlays each scene's
    Script.subtitle text via ffmpeg drawtext. This is a generic pipeline
    feature, not tied to any one provider (see providers/registry.py's own
    rule against vendor-name branching outside the registry) -- it is simply
    most useful with Demo Mode's silent-colored-placeholder output.
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

    # A Windows absolute path's drive-letter colon cannot survive ffmpeg's
    # own filtergraph option-value escaping in practice (verified against a
    # real ffmpeg build -- every backslash-escaping variant still terminated
    # the option early at the colon). The caption text file and the font
    # file are therefore written into work_dir itself and referenced by bare
    # filename with `cwd=work_dir` passed to the ffmpeg subprocess -- see
    # media.ffmpeg_render._drawtext_filter's docstring.
    font_file_name: str | None = None
    if burn_subtitles:
        from reel_harness.media.deps import resolve_font_path

        resolved_font = resolve_font_path()
        if resolved_font is not None:
            font_file_name = "caption_font" + resolved_font.suffix
            font_dest = work_dir / font_file_name
            if not font_dest.exists():
                font_dest.write_bytes(resolved_font.read_bytes())

    clip_paths: list[Path] = []
    for asset, tts_result in zip(assets, tts_results, strict=True):
        clip_path = work_dir / f"scene_{asset.scene_index}.mp4"
        subtitle_file_name: str | None = None
        run_cwd: str | None = None
        if burn_subtitles:
            subtitle_text = job.script["scenes"][asset.scene_index]["subtitle"]
            subtitle_file_name = f"subtitle_{asset.scene_index}.txt"
            (work_dir / subtitle_file_name).write_text(subtitle_text, encoding="utf-8")
            run_cwd = str(work_dir)
        if asset.mime_type.startswith("video/"):
            # Real stock-video asset (already normalized to H.264/yuv420p/
            # muted at ASSET time -- see media.asset_video): loop it
            # indefinitely and let -shortest cut to the TTS audio length, so
            # a clip shorter than the narration repeats from its start and one
            # longer than the narration is trimmed from its start. One ffmpeg
            # invocation handles both duration-policy cases (see
            # docs/OPERATIONS.md).
            argv = ffmpeg_render.render_scene_clip_from_video(
                ffmpeg_path, asset.local_path, tts_result.audio_path, clip_path, width, height,
                subtitle_file_name=subtitle_file_name, font_file_name=font_file_name,
            )
        else:
            argv = ffmpeg_render.render_scene_clip(
                ffmpeg_path, asset.local_path, tts_result.audio_path, clip_path, width, height,
                subtitle_file_name=subtitle_file_name, font_file_name=font_file_name,
            )
        result = run(argv, cwd=run_cwd, timeout=60)
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
