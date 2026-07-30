from __future__ import annotations

from pathlib import Path


def _drawtext_filter(subtitle_file_name: str, font_file_name: str | None) -> str:
    """Builds a drawtext filter referencing FILENAMES ONLY (never a full
    path). A Windows absolute path's drive-letter colon (`C:\\...`) cannot
    be made to survive ffmpeg's own filtergraph option-value escaping in
    practice -- every documented backslash-escaping variant (`\\:`, `\\\\:`,
    `\\\\\\:`, quoting the value in `'...'`) was tried against a real
    ffmpeg build and each one still terminated the option early at the
    colon. A bare relative filename has no colon at all, so this sidesteps
    the problem entirely -- the caller (pipeline.stages.run_rendering) is
    responsible for writing the subtitle text file and the font file into
    the SAME directory it passes as `cwd` to media.runner.run() for this
    render call."""
    parts = [f"textfile={subtitle_file_name}"]
    if font_file_name:
        parts.append(f"fontfile={font_file_name}")
    else:
        # No resolved font file -- ffmpeg's own fontconfig fallback (the
        # local build has --enable-fontconfig) picks some installed
        # sans-serif rather than failing outright.
        parts.append("font=sans-serif")
    parts.extend([
        # fontsize as a w-relative expression (not a fixed pixel count) so a
        # caption fits both the fast-test 360-wide render and a real
        # 1080-wide one without separately tuning each.
        "fontcolor=white", "fontsize=w/16", "box=1", "boxcolor=black@0.5", "boxborderw=(w/60)",
        "x=(w-text_w)/2", "y=h-th-(h/12)",
    ])
    return "drawtext=" + ":".join(parts)


def render_scene_clip(
    ffmpeg_path: Path, image_path: Path, audio_path: Path, output_path: Path, width: int, height: int,
    subtitle_file_name: str | None = None, font_file_name: str | None = None,
) -> list[str]:
    """Builds the ffmpeg argv for a single still-image-plus-audio scene clip.

    `ffmpeg_path` must be the resolved absolute path from `media.deps` -- never
    the bare string "ffmpeg" -- so REEL_HARNESS_FFMPEG_PATH / a project-local
    .tools/ffmpeg/bin copy is actually honored rather than silently falling
    back to whatever "ffmpeg" resolves to on PATH.

    `subtitle_file_name` (Settings.render_burn_subtitles, off by default)
    burns that text onto the video via drawtext -- see `_drawtext_filter`'s
    docstring for why it must be a bare filename, not a path, and why the
    caller must invoke this argv with `cwd` set to that filename's directory.
    BGM mixing remains an extension point for a later phase (see
    docs/ARCHITECTURE.md).
    """
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
    ]
    if subtitle_file_name:
        filters.append(_drawtext_filter(subtitle_file_name, font_file_name))
    return [
        str(ffmpeg_path), "-y",
        "-loop", "1", "-i", str(image_path),
        "-i", str(audio_path),
        "-c:v", "libx264", "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", ",".join(filters),
        "-shortest",
        str(output_path),
    ]


def render_scene_clip_from_video(
    ffmpeg_path: Path, video_path: Path, audio_path: Path, output_path: Path, width: int, height: int,
    subtitle_file_name: str | None = None, font_file_name: str | None = None,
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

    `subtitle_file_name`/`font_file_name`: see render_scene_clip's docstring
    and `_drawtext_filter` -- same bare-filename-plus-cwd contract.
    """
    filters = [
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2",
    ]
    if subtitle_file_name:
        filters.append(_drawtext_filter(subtitle_file_name, font_file_name))
    return [
        str(ffmpeg_path), "-y",
        "-stream_loop", "-1", "-i", str(video_path),
        "-i", str(audio_path),
        "-c:v", "libx264",
        "-c:a", "aac", "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", ",".join(filters),
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
