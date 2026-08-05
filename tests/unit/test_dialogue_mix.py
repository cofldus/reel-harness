"""Mixing synthesised dialogue over a film that already has ambience."""
from __future__ import annotations

from pathlib import Path

import pytest

from reel_harness.media.dialogue_mix import (
    DialogueCue,
    DialogueTimingError,
    check_fits,
    cue_start_times,
    mix_dialogue_argv,
)

FFMPEG = Path("/usr/bin/ffmpeg")


def _cue(start: float, duration: float = 3.0, order: int = 1) -> DialogueCue:
    return DialogueCue(Path(f"line{order}.wav"), start, duration, order, "도윤")


def test_cues_start_where_their_shot_starts() -> None:
    assert cue_start_times([8.0, 8.0, 8.0]) == [0.0, 8.0, 16.0]
    assert cue_start_times([]) == []


def test_a_line_that_overruns_its_shot_is_refused_not_squeezed() -> None:
    """Speeding a line up to fit would hide a script problem and make the
    delivery worse. The adaptation wrote too many words; say so."""
    with pytest.raises(DialogueTimingError) as excinfo:
        check_fits(_cue(0.0, duration=10.0, order=3), shot_duration=8.0)
    assert "shot 3" in str(excinfo.value)
    assert "shorten the line" in str(excinfo.value)


def test_a_synthesiser_s_trailing_silence_does_not_fail_a_good_line() -> None:
    check_fits(_cue(0.0, duration=8.1), shot_duration=8.0)


def test_each_line_is_delayed_to_its_own_shot() -> None:
    argv = mix_dialogue_argv(
        FFMPEG, Path("film.mp4"),
        [_cue(0.0, order=1), _cue(16.0, order=3)],
        Path("out.mp4"),
    )
    graph = argv[argv.index("-filter_complex") + 1]
    assert "adelay=0|0" in graph
    assert "adelay=16000|16000" in graph
    # One bed plus two lines.
    assert "amix=inputs=3" in graph


def test_the_picture_is_copied_never_re_encoded() -> None:
    """Mixing audio must not cost a generation of video quality."""
    argv = mix_dialogue_argv(FFMPEG, Path("film.mp4"), [_cue(0.0)], Path("out.mp4"))
    assert argv[argv.index("-c:v") + 1] == "copy"


def test_ambience_is_ducked_rather_than_removed() -> None:
    """Rain under a line is what makes it sound recorded in the scene."""
    argv = mix_dialogue_argv(FFMPEG, Path("film.mp4"), [_cue(0.0)], Path("out.mp4"))
    graph = argv[argv.index("-filter_complex") + 1]
    assert "[0:a]volume=0.45[bed]" in graph


def test_every_cue_becomes_an_input() -> None:
    cues = [_cue(0.0, order=1), _cue(8.0, order=2), _cue(16.0, order=3)]
    argv = mix_dialogue_argv(FFMPEG, Path("film.mp4"), cues, Path("out.mp4"))
    assert argv.count("-i") == 1 + len(cues)


def test_mixing_nothing_is_a_programming_error() -> None:
    """A film with no dialogue should never reach the mixer at all."""
    with pytest.raises(ValueError):
        mix_dialogue_argv(FFMPEG, Path("film.mp4"), [], Path("out.mp4"))


def test_the_argv_is_a_list_of_strings() -> None:
    """The project's subprocess rule, and doubly so where filenames come
    from an adaptation."""
    argv = mix_dialogue_argv(FFMPEG, Path("film.mp4"), [_cue(0.0)], Path("out.mp4"))
    assert all(isinstance(part, str) for part in argv)


def test_audio_duration_is_probed_with_the_right_tool() -> None:
    """The film validator asks for a video stream and raises when a WAV
    has none -- probing speech with it reads as "the render broke" rather
    than "wrong tool", which is exactly how it was first written."""
    from reel_harness.media.dialogue_mix import audio_duration_argv, parse_audio_duration

    argv = audio_duration_argv(Path("ffprobe"), Path("line.wav"))
    assert "format=duration" in argv
    assert "line.wav" in argv[-1]

    assert parse_audio_duration("7.42\n") == pytest.approx(7.42)
    # A missing or malformed value must not crash a render.
    assert parse_audio_duration("") == 0.0
    assert parse_audio_duration("N/A") == 0.0
