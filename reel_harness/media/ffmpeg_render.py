from __future__ import annotations

from pathlib import Path


def render_scene_clip(
    ffmpeg_path: Path, image_path: Path, audio_path: Path, output_path: Path, width: int, height: int,
) -> list[str]:
    """Builds the ffmpeg argv for a single still-image-plus-audio scene clip.

    `ffmpeg_path` must be the resolved absolute path from `media.deps` -- never
    the bare string "ffmpeg" -- so REEL_HARNESS_FFMPEG_PATH / a project-local
    .tools/ffmpeg/bin copy is actually honored rather than silently falling
    back to whatever "ffmpeg" resolves to on PATH.

    Phase 1 scope note: this proves the ffmpeg integration end-to-end (image + TTS
    audio -> portrait mp4) but does not yet burn in subtitles or mix BGM. Subtitle
    overlay and BGM mixing are extension points for a later phase (see
    docs/ARCHITECTURE.md) once real script/asset content exists to render.
    """
    return [
        str(ffmpeg_path), "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-shortest",
        str(output_path),
    ]


def render_scene_clip_from_video(
    ffmpeg_path: Path, video_path: Path, audio_path: Path, output_path: Path, width: int, height: int,
) -> list[str]:
    """Builds the ffmpeg argv for a single real-stock-video-plus-audio scene
    clip. `video_path` is already normalized to H.264/yuv420p/muted by the
    stock-media adapter at ASSET time (see media.asset_video) -- this only
    scales/crops to the render target and pairs it with the TTS audio.

    Duration policy: `-stream_loop -1` loops the (silent) video indefinitely,
    and `-shortest` cuts the output to the TTS audio's length. A source clip
    shorter than the narration therefore repeats from its start (loop); one
    longer than the narration is trimmed from its start (deterministic
    zero-offset trim) -- both cases resolve to exactly the narration's
    duration through one ffmpeg invocation, mirroring the still-image path's
    `-loop 1 -shortest` behavior above.
    """
    return [
        str(ffmpeg_path), "-y",
        "-stream_loop", "-1", "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "libx264",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2"
        ),
        "-map", "0:v:0", "-map", "1:a:0",
        "-shortest",
        str(output_path),
    ]


def write_concat_list(clip_paths: list[Path], concat_list_path: Path) -> None:
    """Writes an ffmpeg concat-demuxer list file using POSIX-style forward slashes.

    The reference pipeline this project supersedes broke here on Windows: absolute
    paths with backslashes inside a concat list file are not parsed correctly by
    ffmpeg's concat demuxer. `Path.as_posix()` sidesteps that on every platform.
    """
    lines = [f"file '{path.as_posix()}'" for path in clip_paths]
    concat_list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def concat_clips_argv(ffmpeg_path: Path, concat_list_path: Path, output_path: Path) -> list[str]:
    return [
        str(ffmpeg_path), "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list_path),
        "-c", "copy",
        "-movflags", "+faststart",
        str(output_path),
    ]
