"""Canonical, provider-neutral shot prompt assembly (Fable F2).

One shot becomes one deterministic prompt string built from fifteen
slots in a FIXED order (the Fable spec's §12 ordering). Two properties
matter more than prose quality:

1. **Determinism** -- the same shot always compiles to the same string,
   so `prompt_fingerprint` is a stable identity. That fingerprint is what
   makes take generation idempotent (see db.cinematic_models.FableTake's
   unique constraint): a worker retrying after a lost provider response
   recognizes the take it already paid for instead of buying another.
2. **Fixed identity always present** -- the character's immutable
   appearance elements are injected into EVERY shot, which is the only
   mechanism keeping the same virtual actor recognizable across
   separately-generated clips.

Provider-neutral by design: no vendor's prompt dialect appears here.
Adapters may translate this canonical form into their own syntax (the
real Veo adapter in F5); nothing upstream needs to know which.

Version note: COMPILER_VERSION is part of the fingerprint, so changing
the assembly rules changes every fingerprint -- deliberately, since a
differently-compiled prompt is a different generation request."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# v2 adds the spoken-dialogue slots. Bumped deliberately: the fingerprint
# is part of a take's identity, so a shot compiled before dialogue existed
# and the same shot compiled after are genuinely different requests and
# must not be mistaken for a replay of each other.
COMPILER_VERSION = "fable-shot-v3"

# Human-readable language names for the speech instruction. A model is
# told "speaking in Korean", not "ko" -- the ISO code is what this
# codebase stores, not what a prompt should say.
_SPOKEN_LANGUAGE_NAMES = {
    "ko": "Korean",
    "en": "English",
}

# Appended to every prompt: the quality floor and the artifact
# prohibitions that apply regardless of shot content.
_VISUAL_QUALITY = "photorealistic cinematic footage, natural human motion, filmic depth of field"
_PROHIBITED_ARTIFACTS = (
    "no distorted faces, no extra or malformed fingers, no duplicated limbs, "
    "no morphing clothing, no text overlays, no watermark, no subtitles, "
    "no burned-in captions"
)

# The identity keys copied verbatim into every shot, in this order, so a
# character's look cannot drift between shots. Unknown keys in
# fixed_identity are appended afterwards (sorted) rather than dropped.
_IDENTITY_KEY_ORDER = ("face", "appearance", "hair", "wardrobe", "accessories")


def fixed_identity_values(character_bible: dict | None) -> list[str]:
    """The character's immutable appearance elements, in a stable order.

    Public because the reference-sheet compiler (pipeline.reference_prompt)
    must inject the SAME fragment: a reference still that describes a
    different person from the shot prompts would defeat the entire point
    of generating one."""
    if not character_bible:
        return []
    fixed = character_bible.get("fixed_identity") or {}
    if not isinstance(fixed, dict):
        return []
    parts: list[str] = []
    seen: set[str] = set()
    for key in _IDENTITY_KEY_ORDER:
        value = fixed.get(key)
        if value:
            parts.append(str(value))
            seen.add(key)
    for key in sorted(set(fixed) - seen):
        value = fixed.get(key)
        if value:
            parts.append(str(value))
    return parts


def compile_shot_prompt(
    shot, project, character_bible: dict | None = None, location: dict | None = None,
    position: ShotPosition | None = None,
) -> str:
    """Assembles the canonical prompt. `shot` is a FableShot,
    `project` a StoryProject; `character_bible` and `location` are the
    already-loaded records for this shot's subject and scene (passed in
    rather than queried so this stays a pure function)."""
    bible = project.story_bible or {}
    location = location or {}
    continuity = shot.continuity_requirements or {}

    # Wardrobe is usually ALSO part of fixed_identity (it must stay
    # constant across shots), so emit it in slot 3 only when the identity
    # fragment doesn't already carry it -- repeating it verbatim adds no
    # information and dilutes the prompt.
    identity_values = fixed_identity_values(character_bible)
    wardrobe = str((character_bible or {}).get("wardrobe", ""))
    if wardrobe and wardrobe in identity_values:
        wardrobe = ""

    slots: list[str] = [
        # 1 subject, 2 fixed identity, 3 wardrobe
        f"a single fictional adult actor: {shot.subject}" if shot.subject else "a single fictional adult actor",
        ", ".join(identity_values),
        wardrobe,
        # 4 location (+ its continuity anchors)
        ", ".join(str(part) for part in (
            location.get("name", ""), location.get("description", ""),
            (location.get("continuity") or {}).get("time_of_day", ""),
            (location.get("continuity") or {}).get("weather", ""),
        ) if part),
        # 5 action, 6 expression, 7 blocking
        shot.action or "",
        shot.expression or "",
        shot.blocking or "",
        # 8 shot size, 9 lens, 10 camera movement
        (shot.shot_size or "").replace("_", " "),
        shot.lens_style or "",
        (shot.camera_movement or "").replace("_", " "),
        # 11 lighting, 12 atmosphere
        shot.lighting or (location.get("continuity") or {}).get("lighting", ""),
        str(bible.get("visual_style", "")),
        # 13 temporal continuity: where this shot sits in the film, and
        # what the audience just saw. Without it every shot is generated
        # with identical context -- all four sharing one scene's beat --
        # and the result is four unrelated clips rather than a sequence.
        _narrative_position(position),
        str(continuity.get("source_beat", "")),
        # 14 spoken dialogue. Three things have to be present together or
        # the shot comes back silent: WHAT is said, that it is SPOKEN
        # aloud rather than captioned, and in WHICH language. The stored
        # dialogue_line was previously compiled into nothing at all, so a
        # speaking shot was generated as a silent one and the project's
        # language never reached the model.
        _dialogue_slot(shot, project),
        # 15 quality floor, 16 prohibitions
        _VISUAL_QUALITY,
        _PROHIBITED_ARTIFACTS,
    ]
    return ", ".join(slot.strip() for slot in slots if slot and slot.strip())


@dataclass(frozen=True)
class ShotPosition:
    """Where one shot sits in the finished film.

    Passed in rather than derived, because a shot row knows its order
    WITHIN a scene but nothing about the scene's place in the film -- and
    "shot 1" of scene 3 is not the opening of anything."""

    index: int          # 1-based, across the whole film
    total: int
    previous_action: str | None = None


def _narrative_position(position: ShotPosition | None) -> str:
    """States the shot's place in the sequence and what immediately
    precedes it.

    A model given only "a woman looks out of a window" makes a clip. The
    same model told it is the third of four, following a specific action,
    makes a shot of a film -- it is the only signal in the prompt that
    the clips belong to one continuous piece."""
    if position is None or position.total <= 1:
        return ""
    if position.index == 1:
        where = f"opening shot of a {position.total}-shot short film"
    elif position.index == position.total:
        where = f"final shot of a {position.total}-shot short film, resolving it"
    else:
        where = f"shot {position.index} of {position.total} in a continuous short film"
    if position.previous_action:
        where += f", continuing directly from: {position.previous_action}"
    return where


def _dialogue_slot(shot, project) -> str:
    """The spoken-line fragment, or empty for a silent shot.

    The line is quoted verbatim so the model reproduces the words rather
    than paraphrasing the sentiment, and the speaker is named so a
    two-hander does not put the line in the wrong mouth."""
    line = (getattr(shot, "dialogue_line", None) or "").strip()
    if not line:
        return ""
    language = _SPOKEN_LANGUAGE_NAMES.get(
        (getattr(project, "language", "") or "").lower(),
    )
    speaker = shot.subject or "the actor"
    spoken = f'{speaker} speaks aloud, lip-synced, saying "{line}"'
    if language:
        spoken += f" in {language}"
    return spoken


def prompt_fingerprint(prompt: str) -> str:
    """Stable identity of a compiled prompt. Versioned so a change to the
    assembly rules is never mistaken for the same request."""
    return hashlib.sha256(f"{COMPILER_VERSION}\x1f{prompt}".encode()).hexdigest()[:32]
