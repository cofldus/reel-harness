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
