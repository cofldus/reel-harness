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


def has_audio_stream(ffprobe_stdout: str) -> bool:
    """Whether a probe found an audio track.

    A film generated with the video model's audio switched off has no
    audio stream at all, and a filtergraph that references [0:a] on such
    a file fails with "Error binding filtergraph inputs/outputs" -- an
    ffmpeg message that says nothing about the actual cause.
    """
    return "audio" in (ffprobe_stdout or "").split()


def film_streams_argv(ffprobe_path: Path, film_path: Path) -> list[str]:
    return [
        str(ffprobe_path), "-v", "error",
        "-show_entries", "stream=codec_type",
        "-of", "csv=p=0",
        str(film_path),
    ]


def mix_dialogue_argv(
    ffmpeg_path: Path, film_path: Path, cues: list[DialogueCue], output_path: Path,
    *, ambience_gain: float = 0.45, dialogue_gain: float = 1.0,
    has_ambience: bool = True,
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
    # A silent bed when the film has no audio of its own. amix needs
    # something to mix INTO, and lines summed against nothing produce a
    # track that ends with the last cue rather than with the picture.
    if not has_ambience:
        argv += ["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"]
    for cue in cues:
        argv += ["-i", str(cue.audio_path)]

    # Delay each spoken line to its shot's start, then sum everything.
    # adelay wants milliseconds, and both channels.
    bed_input = "0:a" if has_ambience else "1:a"
    first_cue_index = 1 if has_ambience else 2
    filters: list[str] = [f"[{bed_input}]volume={ambience_gain if has_ambience else 1.0}[bed]"]
    labels = ["[bed]"]
    for index, cue in enumerate(cues, start=first_cue_index):
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
        # anullsrc never ends, so the picture decides where the file does.
        *(["-shortest"] if not has_ambience else []),
        # Copy the picture: mixing audio must never re-encode video and
        # cost a generation's worth of quality.
        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return argv


def audio_duration_argv(ffprobe_path: Path, audio_path: Path) -> list[str]:
    """Duration of an audio file, in seconds, on stdout.

    Separate from ffprobe_validate's builder, which asks for a video
    stream and raises when a WAV has none -- probing speech with the film
    validator is a mistake that reads as "the render broke" rather than
    "wrong tool".
    """
    return [
        str(ffprobe_path), "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]


def parse_audio_duration(stdout: str) -> float:
    """Zero when the value is missing or unparseable.

    A missing duration must not crash a render: the fit check simply
    cannot fire, which is the same position the code was in before this
    existed.
    """
    text = (stdout or "").strip().splitlines()
    if not text:
        return 0.0
    try:
        return float(text[0])
    except ValueError:
        return 0.0
