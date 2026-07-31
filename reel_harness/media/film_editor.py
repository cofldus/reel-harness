"""Film assembly beyond hard cuts (Fable F5).

F1 concatenated the selected takes with `-c copy` -- fast, lossless, and
exactly right for proving the pipeline, but it can only ever produce hard
cuts because stream copy cannot blend two clips. This module builds the
filtergraph that can: cross-dissolves between shots, an optional fade in
and out, and an audio crossfade that matches the video so sound does not
jump a frame ahead of picture.

Two deliberate limits, both stated rather than worked around:

- **Re-encoding is unavoidable here.** A transition mixes pixels from two
  clips, so `-c copy` is off the table by definition. `render_final`
  therefore keeps the hard-cut path as the default and uses this one only
  when transitions are actually requested -- paying an encode for a cut
  that could have been a copy would be a worse default.
- **`xfade` requires uniform clip properties.** Every take in a Fable
  project comes from one provider at one resolution and frame rate, so
  that holds by construction; a mismatched input is an ffmpeg error this
  module surfaces rather than silently rescaling into something the
  operator did not ask for.

The argv builders here are pure functions returning `list[str]`, run
through ProcessRunner like every other subprocess in this codebase --
never a shell string.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# Transition names are this project's vocabulary, mapped to ffmpeg's
# xfade transition ids in one place. "cut" is not a transition -- it is
# the absence of one, and it routes to the copy path instead.
TRANSITION_CUT = "cut"
TRANSITION_DISSOLVE = "dissolve"
TRANSITION_FADE_BLACK = "fade_black"

_XFADE_BY_TRANSITION = {
    TRANSITION_DISSOLVE: "fade",
    TRANSITION_FADE_BLACK: "fadeblack",
}

SUPPORTED_TRANSITIONS = frozenset({TRANSITION_CUT, *_XFADE_BY_TRANSITION})

# A transition consumes time from BOTH clips it joins, so a duration
# longer than the shortest clip would leave nothing of one shot. Bounded
# well below the 8s Veo minimum, and validated against the real clips
# anyway.
MAX_TRANSITION_SEC = 2.0
DEFAULT_TRANSITION_SEC = 0.5


class FilmEditError(ValueError):
    """A film cannot be assembled as specified. Raised before ffmpeg runs,
    so the operator learns the reason rather than reading a filtergraph
    parse error."""


@dataclass(frozen=True)
class EditPlan:
    """What to build. Deliberately a value object rather than a pile of
    parameters, so `render_final` can log/persist exactly what it asked
    for and a test can assert on the plan without running ffmpeg."""

    transition: str = TRANSITION_CUT
    transition_sec: float = DEFAULT_TRANSITION_SEC
    fade_in_sec: float = 0.0
    fade_out_sec: float = 0.0
    # Veo generates native audio; muting is an explicit editorial choice,
    # never a silent side effect of assembly.
    mute_audio: bool = False

    @property
    def needs_reencode(self) -> bool:
        """Whether this plan can be satisfied by stream copy. A hard cut
        with no fades can; anything that mixes or modifies pixels cannot."""
        return (
            self.transition != TRANSITION_CUT
            or self.fade_in_sec > 0
            or self.fade_out_sec > 0
            or self.mute_audio
        )


def validate_plan(plan: EditPlan, clip_durations: list[float]) -> None:
    """Every reason an assembly cannot work, checked before ffmpeg runs.

    The duration check is the one that matters: a transition eats time
    from both clips it joins, so asking for a 2s dissolve between two 1s
    clips does not produce a short film -- it produces an ffmpeg error
    several minutes into an encode."""
    if plan.transition not in SUPPORTED_TRANSITIONS:
        raise FilmEditError(
            f"unsupported transition {plan.transition!r} "
            f"(supported: {', '.join(sorted(SUPPORTED_TRANSITIONS))})"
        )
    if not clip_durations:
        raise FilmEditError("a film needs at least one clip")
    for name, value in (
        ("transition_sec", plan.transition_sec),
        ("fade_in_sec", plan.fade_in_sec),
        ("fade_out_sec", plan.fade_out_sec),
    ):
        if value < 0:
            raise FilmEditError(f"{name} must not be negative")
    if plan.transition != TRANSITION_CUT:
        if not (0 < plan.transition_sec <= MAX_TRANSITION_SEC):
            raise FilmEditError(
                f"transition_sec must be between 0 and {MAX_TRANSITION_SEC}, "
                f"got {plan.transition_sec}"
            )
        shortest = min(clip_durations)
        if plan.transition_sec >= shortest:
            raise FilmEditError(
                f"a {plan.transition_sec}s transition does not fit: the shortest clip is "
                f"{shortest}s, and a transition consumes time from both clips it joins"
            )
    total = film_duration(plan, clip_durations)
    if plan.fade_in_sec + plan.fade_out_sec > total:
        raise FilmEditError(
            f"fades ({plan.fade_in_sec}s + {plan.fade_out_sec}s) exceed the film's "
            f"{total}s runtime"
        )


def film_duration(plan: EditPlan, clip_durations: list[float]) -> float:
    """The finished film's length. Each transition OVERLAPS two clips, so
    every one shortens the total -- a caller that summed the clips would
    over-report the runtime by the transition time."""
    total = sum(clip_durations)
    if plan.transition != TRANSITION_CUT and len(clip_durations) > 1:
        total -= plan.transition_sec * (len(clip_durations) - 1)
    return round(total, 3)


def build_filter_complex(
    plan: EditPlan, clip_durations: list[float],
) -> tuple[str, str, str | None]:
    """The xfade/acrossfade chain for one film, as
    (filter_complex, video_label, audio_label). `audio_label` is None
    when the plan mutes audio, which is what tells the caller to emit
    `-an` rather than mapping a stream that was never built.

    Built left to right: clip 0 and 1 are joined, that result is joined
    with clip 2, and so on. Each `offset` is measured from the START of
    the accumulated result, which is why it tracks the running duration
    rather than being a fixed multiple -- getting that wrong produces a
    film where later transitions land in the wrong place, which no test
    of the argv shape alone would catch."""
    xfade = _XFADE_BY_TRANSITION[plan.transition]
    parts: list[str] = []
    video_label = "0:v"
    audio_label = "0:a"
    running = clip_durations[0]

    for index in range(1, len(clip_durations)):
        offset = round(running - plan.transition_sec, 3)
        next_video = f"v{index}"
        next_audio = f"a{index}"
        parts.append(
            f"[{video_label}][{index}:v]xfade=transition={xfade}:"
            f"duration={plan.transition_sec}:offset={offset}[{next_video}]"
        )
        if not plan.mute_audio:
            parts.append(
                f"[{audio_label}][{index}:a]acrossfade=d={plan.transition_sec}[{next_audio}]"
            )
        video_label, audio_label = next_video, next_audio
        running = round(running + clip_durations[index] - plan.transition_sec, 3)

    video_chain = _fade_filters(plan, running)
    if video_chain:
        parts.append(f"[{video_label}]{','.join(video_chain)}[vout]")
        video_label = "vout"
    final_audio: str | None = None if plan.mute_audio else audio_label
    return ";".join(parts), video_label, final_audio


def _fade_filters(plan: EditPlan, total_duration: float) -> list[str]:
    filters: list[str] = []
    if plan.fade_in_sec > 0:
        filters.append(f"fade=t=in:st=0:d={plan.fade_in_sec}")
    if plan.fade_out_sec > 0:
        start = round(total_duration - plan.fade_out_sec, 3)
        filters.append(f"fade=t=out:st={start}:d={plan.fade_out_sec}")
    return filters


def edit_film_argv(
    ffmpeg_path: Path, clip_paths: list[Path], clip_durations: list[float],
    output_path: Path, plan: EditPlan,
) -> list[str]:
    """The full ffmpeg invocation for a transition-bearing assembly.

    Each clip is a separate `-i` rather than a concat list: the concat
    demuxer produces ONE stream, and xfade needs two to blend between."""
    validate_plan(plan, clip_durations)
    if len(clip_paths) != len(clip_durations):
        raise FilmEditError("clip_paths and clip_durations must be the same length")

    argv: list[str] = [str(ffmpeg_path), "-y"]
    for path in clip_paths:
        argv += ["-i", str(path)]

    if len(clip_paths) == 1:
        # Nothing to transition between; only the fades apply.
        video_chain = _fade_filters(plan, clip_durations[0])
        if video_chain:
            argv += ["-vf", ",".join(video_chain)]
        argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
        argv += ["-an"] if plan.mute_audio else ["-c:a", "aac"]
        argv += ["-movflags", "+faststart", str(output_path)]
        return argv

    filter_complex, video_label, audio_label = build_filter_complex(plan, clip_durations)
    argv += ["-filter_complex", filter_complex, "-map", f"[{video_label}]"]
    if audio_label is None:
        argv += ["-an"]
    else:
        argv += ["-map", f"[{audio_label}]", "-c:a", "aac"]
    argv += [
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-movflags", "+faststart", str(output_path),
    ]
    return argv
