"""Lay synthesised dialogue over a film that already has its ambience.

The video model generates rain, room tone and footsteps well, and it is
told not to generate speech (see shot_prompt's dialogue slot). This mixes
the spoken lines back in at the moment each shot begins.

Two things this deliberately does NOT do.

It does not stretch or pitch-shift a line to fit. A line that runs past
its shot is a script problem -- the adaptation wrote something too long
for eight seconds -- and quietly speeding it up would hide that while
making the delivery worse. It is reported instead, loudly enough to fix
upstream.

It does not touch the take files. Takes are the paid artefacts; a mix is
derived and must be reproducible from them, so mixing happens once at
final assembly and the takes stay exactly as the provider returned them.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DialogueCue:
    """One spoken line, and where it lands in the finished film."""

    audio_path: Path
    # Seconds from the start of the FILM, not of the shot.
    start_sec: float
    duration_sec: float
    # For error messages: which shot and who is speaking.
    shot_order: int
    speaker: str


class DialogueTimingError(ValueError):
    """A line does not fit the shot it belongs to."""


def cue_start_times(shot_durations: list[float]) -> list[float]:
    """Where each shot begins in the concatenated film."""
    starts: list[float] = []
    elapsed = 0.0
    for duration in shot_durations:
        starts.append(elapsed)
        elapsed += duration
    return starts


def check_fits(cue: DialogueCue, shot_duration: float, tolerance_sec: float = 0.25) -> None:
    """Raise when a line runs past the shot that contains it.

    A small tolerance, because a synthesiser's trailing silence should
    not fail a line that is otherwise fine. Anything beyond that is the
    adaptation having written more words than the shot can hold, and the
    fix belongs there rather than here.
    """
    overrun = cue.duration_sec - shot_duration
    if overrun > tolerance_sec:
        raise DialogueTimingError(
            f"shot {cue.shot_order}: {cue.speaker}'s line runs {overrun:.1f}s past the "
            f"{shot_duration:.0f}s shot ({cue.duration_sec:.1f}s of speech) -- shorten the "
            "line in the adaptation rather than speeding up the delivery"
        )


def mix_dialogue_argv(
    ffmpeg_path: Path, film_path: Path, cues: list[DialogueCue], output_path: Path,
    *, ambience_gain: float = 0.45, dialogue_gain: float = 1.0,
) -> list[str]:
    """The ffmpeg call that lays `cues` over `film_path`.

    Ambience is ducked rather than removed: rain under a line is what
    makes it sound like it was recorded in the scene instead of in a
    booth. A flat gain rather than a sidechain compressor, because the
    cues are known in advance and a constant bed is easier to predict
    than a pumping one.

    `list[str]`, `shell=False`, no string building -- the project's
    subprocess rule, and doubly so here where filenames come from the
    adaptation.
    """
    if not cues:
        raise ValueError("mix_dialogue_argv called with no cues")

    argv: list[str] = [str(ffmpeg_path), "-y", "-i", str(film_path)]
    for cue in cues:
        argv += ["-i", str(cue.audio_path)]

    # Delay each spoken line to its shot's start, then sum everything.
    # adelay wants milliseconds, and both channels.
    filters: list[str] = [f"[0:a]volume={ambience_gain}[bed]"]
    labels = ["[bed]"]
    for index, cue in enumerate(cues, start=1):
        delay_ms = max(0, int(round(cue.start_sec * 1000)))
        filters.append(
            f"[{index}:a]adelay={delay_ms}|{delay_ms},volume={dialogue_gain}[d{index}]"
        )
        labels.append(f"[d{index}]")
    filters.append(
        f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[out]"
    )

    argv += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v", "-map", "[out]",
        # Copy the picture: mixing audio must never re-encode video and
        # cost a generation's worth of quality.
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return argv
