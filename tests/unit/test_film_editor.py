"""Film assembly beyond hard cuts (media.film_editor).

The arithmetic gets most of the attention, because it is where a wrong
answer is invisible until someone watches the film: a transition OVERLAPS
two clips, so every one shortens the total and shifts where the next one
lands. An argv-shape test alone would pass on a filtergraph that puts
every transition after the first in the wrong place.

The last test actually runs ffmpeg and measures the result, which is the
only way to know the filtergraph is real rather than merely well-formed.
"""
from __future__ import annotations

import pytest

from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.film_editor import (
    DEFAULT_TRANSITION_SEC,
    MAX_TRANSITION_SEC,
    SUPPORTED_TRANSITIONS,
    TRANSITION_CUT,
    TRANSITION_DISSOLVE,
    TRANSITION_FADE_BLACK,
    EditPlan,
    FilmEditError,
    build_filter_complex,
    edit_film_argv,
    film_duration,
    validate_plan,
)

FFMPEG_PRESENT = check_ffmpeg_available().all_available


# -- when a re-encode is needed ------------------------------------------

def test_a_plain_hard_cut_needs_no_reencode() -> None:
    """Stream copy is lossless and fast. Paying for an encode to produce a
    cut that could have been a copy would be strictly worse."""
    assert EditPlan().needs_reencode is False
    assert EditPlan(transition=TRANSITION_CUT).needs_reencode is False


@pytest.mark.parametrize("plan", [
    EditPlan(transition=TRANSITION_DISSOLVE),
    EditPlan(fade_in_sec=0.5),
    EditPlan(fade_out_sec=0.5),
    EditPlan(mute_audio=True),
])
def test_anything_that_touches_pixels_or_audio_needs_a_reencode(plan) -> None:
    assert plan.needs_reencode is True


# -- duration arithmetic -------------------------------------------------

def test_hard_cuts_sum_the_clips() -> None:
    assert film_duration(EditPlan(), [8.0, 8.0, 8.0]) == 24.0


def test_each_transition_shortens_the_film() -> None:
    """A caller that summed the clips would over-report the runtime by the
    whole transition time -- and place the fade-out past the end."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5)
    assert film_duration(plan, [8.0, 8.0, 8.0]) == 23.0  # 24 - 2 x 0.5


def test_a_single_clip_has_no_transition_to_subtract() -> None:
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5)
    assert film_duration(plan, [8.0]) == 8.0


# -- validation ----------------------------------------------------------

def test_an_unknown_transition_is_refused() -> None:
    with pytest.raises(FilmEditError, match="unsupported transition"):
        validate_plan(EditPlan(transition="starwipe"), [8.0, 8.0])


def test_a_transition_longer_than_the_shortest_clip_is_refused() -> None:
    """Asking for a 2s dissolve between two 1s clips does not produce a
    short film -- it produces an ffmpeg error minutes into an encode."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=1.5)
    with pytest.raises(FilmEditError, match="does not fit"):
        validate_plan(plan, [8.0, 1.0])


def test_a_transition_beyond_the_cap_is_refused() -> None:
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=MAX_TRANSITION_SEC + 0.1)
    with pytest.raises(FilmEditError, match="transition_sec must be between"):
        validate_plan(plan, [8.0, 8.0])


def test_fades_longer_than_the_film_are_refused() -> None:
    plan = EditPlan(fade_in_sec=5.0, fade_out_sec=5.0)
    with pytest.raises(FilmEditError, match="exceed the film"):
        validate_plan(plan, [8.0])


def test_negative_durations_are_refused() -> None:
    with pytest.raises(FilmEditError, match="must not be negative"):
        validate_plan(EditPlan(fade_in_sec=-1.0), [8.0])


def test_a_film_with_no_clips_is_refused() -> None:
    with pytest.raises(FilmEditError, match="at least one clip"):
        validate_plan(EditPlan(), [])


def test_a_hard_cut_plan_ignores_the_transition_duration_check() -> None:
    """transition_sec is meaningless for a cut, so a value that would be
    invalid for a dissolve must not block one."""
    validate_plan(EditPlan(transition=TRANSITION_CUT, transition_sec=99.0), [8.0, 8.0])


# -- the filtergraph -----------------------------------------------------

def test_each_transition_offset_tracks_the_running_duration() -> None:
    """The offset is measured from the START of the accumulated result.
    A fixed multiple would put every transition after the first in the
    wrong place -- the bug this test exists to catch."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5)
    graph, video_label, audio_label = build_filter_complex(plan, [8.0, 8.0, 8.0])

    # First join at 8 - 0.5 = 7.5; the result then runs 15.5s, so the
    # second join is at 15.5 - 0.5 = 15.0 (NOT 15.5, and NOT 2 x 7.5).
    assert "offset=7.5" in graph
    assert "offset=15.0" in graph
    assert video_label == "v2"
    assert audio_label == "a2"


def test_audio_crossfades_alongside_the_video() -> None:
    """Sound must not jump a frame ahead of picture."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5)
    graph, _, _ = build_filter_complex(plan, [8.0, 8.0])
    assert "acrossfade=d=0.5" in graph
    assert "xfade=transition=fade:duration=0.5" in graph


def test_muting_builds_no_audio_chain_at_all() -> None:
    plan = EditPlan(transition=TRANSITION_DISSOLVE, mute_audio=True)
    graph, _, audio_label = build_filter_complex(plan, [8.0, 8.0])
    assert "acrossfade" not in graph
    assert audio_label is None


def test_fade_out_starts_from_the_real_end_of_the_film() -> None:
    """Computed from the transition-shortened duration, not the clip sum,
    or the fade would start past the end and never be seen."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5, fade_out_sec=1.0)
    graph, video_label, _ = build_filter_complex(plan, [8.0, 8.0])
    assert "fade=t=out:st=14.5:d=1.0" in graph  # (16 - 0.5) - 1.0
    assert video_label == "vout"


def test_the_named_transitions_map_to_real_xfade_ids() -> None:
    for transition, expected in (
        (TRANSITION_DISSOLVE, "transition=fade"),
        (TRANSITION_FADE_BLACK, "transition=fadeblack"),
    ):
        graph, _, _ = build_filter_complex(EditPlan(transition=transition), [8.0, 8.0])
        assert expected in graph


# -- argv ----------------------------------------------------------------

def test_every_clip_becomes_its_own_input() -> None:
    """The concat demuxer produces ONE stream; xfade needs two to blend
    between, so a concat list cannot be used here."""
    plan = EditPlan(transition=TRANSITION_DISSOLVE)
    argv = edit_film_argv(
        __import__("pathlib").Path("ffmpeg"),
        [__import__("pathlib").Path(f"{i}.mp4") for i in range(3)],
        [8.0, 8.0, 8.0], __import__("pathlib").Path("out.mp4"), plan,
    )
    assert argv.count("-i") == 3
    assert "-filter_complex" in argv
    assert "-movflags" in argv and "+faststart" in argv


def test_a_mismatched_clip_and_duration_count_is_refused() -> None:
    from pathlib import Path

    with pytest.raises(FilmEditError, match="same length"):
        edit_film_argv(
            Path("ffmpeg"), [Path("a.mp4")], [8.0, 8.0], Path("out.mp4"),
            EditPlan(transition=TRANSITION_DISSOLVE),
        )


def test_a_single_clip_uses_a_simple_filter_not_a_filtergraph() -> None:
    from pathlib import Path

    argv = edit_film_argv(
        Path("ffmpeg"), [Path("a.mp4")], [8.0], Path("out.mp4"), EditPlan(fade_in_sec=0.5),
    )
    assert "-vf" in argv
    assert "-filter_complex" not in argv


def test_muting_emits_an_explicit_no_audio_flag() -> None:
    from pathlib import Path

    argv = edit_film_argv(
        Path("ffmpeg"), [Path("a.mp4"), Path("b.mp4")], [8.0, 8.0], Path("out.mp4"),
        EditPlan(transition=TRANSITION_DISSOLVE, mute_audio=True),
    )
    assert "-an" in argv
    assert "-c:a" not in argv


def test_the_argv_is_a_list_never_a_shell_string() -> None:
    from pathlib import Path

    argv = edit_film_argv(
        Path("ffmpeg"), [Path("a.mp4"), Path("b.mp4")], [8.0, 8.0], Path("out.mp4"),
        EditPlan(transition=TRANSITION_DISSOLVE),
    )
    assert isinstance(argv, list)
    assert all(isinstance(part, str) for part in argv)


# -- against real ffmpeg -------------------------------------------------

@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_a_dissolve_actually_renders_and_is_shorter_than_the_clip_sum(tmp_path) -> None:
    """The only test here that proves the filtergraph is REAL rather than
    merely well-formed -- and the measured duration is what confirms the
    overlap arithmetic reached ffmpeg intact."""
    from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
    from reel_harness.media.runner import run

    deps = check_ffmpeg_available()
    clips = []
    for index in range(2):
        clip = tmp_path / f"clip{index}.mp4"
        result = run([
            str(deps.ffmpeg.path), "-y",
            "-f", "lavfi", "-i", "testsrc=duration=2:size=160x120:rate=25",
            "-f", "lavfi", "-i", f"sine=frequency={220 * (index + 1)}:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
            str(clip),
        ], timeout=120)
        assert result.returncode == 0, result.stderr[-400:]
        clips.append(clip)

    out = tmp_path / "film.mp4"
    plan = EditPlan(transition=TRANSITION_DISSOLVE, transition_sec=0.5, fade_out_sec=0.3)
    argv = edit_film_argv(deps.ffmpeg.path, clips, [2.0, 2.0], out, plan)
    result = run(argv, timeout=180)
    assert result.returncode == 0, result.stderr[-600:]
    assert out.is_file()

    probe = run(build_ffprobe_argv(deps.ffprobe.path, out))
    assert probe.returncode == 0
    measured = parse_ffprobe_output(probe.stdout)
    # 2 + 2 - 0.5 = 3.5, with generous encoder tolerance. The point is
    # that it is meaningfully SHORTER than the 4s clip sum.
    assert 3.0 < measured.duration_sec < 3.9
    assert measured.has_audio_stream is True


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg")
def test_the_supported_transitions_are_all_real_ffmpeg_transitions(tmp_path) -> None:
    """Every name this project offers must be one ffmpeg actually
    implements -- an invented id would fail only at the final render, at
    the very end of a paid run."""
    from reel_harness.media.runner import run

    deps = check_ffmpeg_available()
    clips = []
    for index in range(2):
        clip = tmp_path / f"c{index}.mp4"
        run([
            str(deps.ffmpeg.path), "-y",
            "-f", "lavfi", "-i", "testsrc=duration=1:size=160x120:rate=25",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(clip),
        ], timeout=120)
        clips.append(clip)

    for transition in sorted(SUPPORTED_TRANSITIONS - {TRANSITION_CUT}):
        out = tmp_path / f"{transition}.mp4"
        plan = EditPlan(transition=transition, transition_sec=0.2, mute_audio=True)
        result = run(edit_film_argv(deps.ffmpeg.path, clips, [1.0, 1.0], out, plan), timeout=180)
        assert result.returncode == 0, f"{transition}: {result.stderr[-400:]}"
        assert out.is_file()


def test_the_default_transition_duration_is_within_the_cap() -> None:
    assert 0 < DEFAULT_TRANSITION_SEC <= MAX_TRANSITION_SEC
