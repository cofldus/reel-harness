"""Pure argv-construction tests for media.ffmpeg_render's subtitle burn-in
(Settings.render_burn_subtitles) -- no ffmpeg execution needed here (that's
covered by tests/e2e/test_demo_pipeline_e2e.py); this only checks the argv
shape and the no-op-when-disabled contract."""
from __future__ import annotations

from pathlib import Path

from reel_harness.media.ffmpeg_render import render_scene_clip, render_scene_clip_from_video

FFMPEG = Path("ffmpeg")
IMAGE = Path("scene.png")
VIDEO = Path("scene.mp4")
AUDIO = Path("tts.wav")
OUT = Path("scene_0.mp4")


def test_render_scene_clip_without_subtitles_is_unchanged() -> None:
    """Default (subtitle_file_name=None) must produce the exact same argv
    as before subtitle burn-in existed -- no regression to fake/real
    pipelines that don't opt in."""
    argv = render_scene_clip(FFMPEG, IMAGE, AUDIO, OUT, 360, 640)
    vf = argv[argv.index("-vf") + 1]
    assert "drawtext" not in vf
    assert vf == "scale=360:640:force_original_aspect_ratio=decrease,pad=360:640:(ow-iw)/2:(oh-ih)/2"


def test_render_scene_clip_from_video_without_subtitles_is_unchanged() -> None:
    argv = render_scene_clip_from_video(FFMPEG, VIDEO, AUDIO, OUT, 360, 640)
    vf = argv[argv.index("-vf") + 1]
    assert "drawtext" not in vf


def test_render_scene_clip_with_subtitles_adds_drawtext_with_bare_filenames() -> None:
    argv = render_scene_clip(
        FFMPEG, IMAGE, AUDIO, OUT, 360, 640,
        subtitle_file_name="subtitle_0.txt", font_file_name="caption_font.ttf",
    )
    vf = argv[argv.index("-vf") + 1]
    assert "drawtext=" in vf
    assert "textfile=subtitle_0.txt" in vf
    assert "fontfile=caption_font.ttf" in vf


def test_render_scene_clip_with_subtitles_but_no_font_falls_back_to_fontconfig() -> None:
    argv = render_scene_clip(
        FFMPEG, IMAGE, AUDIO, OUT, 360, 640, subtitle_file_name="subtitle_0.txt", font_file_name=None,
    )
    vf = argv[argv.index("-vf") + 1]
    assert "font=sans-serif" in vf
    assert "fontfile=" not in vf


def test_render_scene_clip_from_video_with_subtitles_adds_drawtext() -> None:
    argv = render_scene_clip_from_video(
        FFMPEG, VIDEO, AUDIO, OUT, 360, 640,
        subtitle_file_name="subtitle_2.txt", font_file_name="caption_font.ttf",
    )
    vf = argv[argv.index("-vf") + 1]
    assert "drawtext=" in vf
    assert "textfile=subtitle_2.txt" in vf
