from __future__ import annotations

from pathlib import Path

from reel_harness.media.ffmpeg_render import concat_clips_argv, render_scene_clip, write_concat_list
from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output


def test_render_scene_clip_uses_list_args_not_a_shell_string() -> None:
    argv = render_scene_clip(
        Path("/opt/ffmpeg/bin/ffmpeg"), Path("scene.png"), Path("scene.wav"), Path("out.mp4"), 360, 640,
    )
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)
    assert argv[0] == str(Path("/opt/ffmpeg/bin/ffmpeg"))
    assert "-shortest" in argv


def test_write_concat_list_uses_posix_style_paths_even_from_windows_path(tmp_path) -> None:
    # This is the exact bug the reference pipeline hit: an absolute Windows path
    # with backslashes inside a concat-demuxer list file is not parsed correctly
    # by ffmpeg. as_posix() must always be used here.
    windows_style_clip = Path(r"C:\Users\someone\jobs\job-1\render\scene_0.mp4")
    concat_list_path = tmp_path / "concat.txt"
    write_concat_list([windows_style_clip], concat_list_path)
    content = concat_list_path.read_text(encoding="utf-8")
    assert "\\" not in content
    assert "C:/Users/someone/jobs/job-1/render/scene_0.mp4" in content


def test_concat_clips_argv_is_a_list_and_uses_safe_paths() -> None:
    argv = concat_clips_argv(Path("/opt/ffmpeg/bin/ffmpeg"), Path("concat.txt"), Path("final.mp4"))
    assert isinstance(argv, list)
    assert "-f" in argv and "concat" in argv


def test_build_ffprobe_argv_is_a_list() -> None:
    argv = build_ffprobe_argv(Path("/opt/ffmpeg/bin/ffprobe"), Path("final.mp4"))
    assert isinstance(argv, list)
    assert argv[0] == str(Path("/opt/ffmpeg/bin/ffprobe"))


def test_parse_ffprobe_output_extracts_expected_fields() -> None:
    stdout = """
    {
      "streams": [
        {"codec_type": "video", "width": 360, "height": 640, "codec_name": "h264"},
        {"codec_type": "audio", "codec_name": "aac"}
      ],
      "format": {"duration": "12.5"}
    }
    """
    result = parse_ffprobe_output(stdout)
    assert (result.width, result.height) == (360, 640)
    assert result.video_codec == "h264"
    assert result.has_audio_stream is True
    assert result.audio_codec == "aac"
    assert result.duration_sec == 12.5


def test_parse_ffprobe_output_detects_missing_audio_stream() -> None:
    stdout = (
        '{"streams": [{"codec_type": "video", "width": 360, "height": 640, "codec_name": "h264"}], '
        '"format": {"duration": "5.0"}}'
    )
    result = parse_ffprobe_output(stdout)
    assert result.has_audio_stream is False
    assert result.audio_codec is None
