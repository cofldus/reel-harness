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
COMPILER_VERSION = "fable-shot-v6"

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
# Every shot in a scene is generated separately, so nothing but the words
# keeps the set from being redesigned between cuts. Naming it explicitly
# is the cheapest continuity available without frame-to-frame
# conditioning.
_SET_CONTINUITY = (
    "the same physical set, camera height and lighting as the other shots in this scene, "
    "consistent screen direction, matched colour and exposure so the cut is invisible, "
    "no relocation, no redecoration, no change of time of day"
)

# The body list is not decoration. A real generated shot came back with a
# man whose torso faced the counter while his legs stretched sideways at
# roughly a right angle, as though the hips had come apart. Faces, fingers
# and duplicated limbs were already forbidden; nothing forbade the joint
# itself being wrong, which is the artifact most likely to make a frame
# unusable because it is visible at a glance and cannot be cropped out.
_PROHIBITED_ARTIFACTS = (
    "anatomically correct body, natural joints and correct proportions, "
    "no bent or broken torso, no dislocated hips, no limbs at impossible angles, "
    "no distorted faces, no extra or malformed fingers, no duplicated limbs, "
    "no detached or floating body parts, no morphing clothing, "
    "no text overlays, no watermark, no subtitles, no burned-in captions"
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
        _narrative_position(position, shot.subject or ""),
        str(continuity.get("source_beat", "")),
        # 14 spoken dialogue. Three things have to be present together or
        # the shot comes back silent: WHAT is said, that it is SPOKEN
        # aloud rather than captioned, and in WHICH language. The stored
        # dialogue_line was previously compiled into nothing at all, so a
        # speaking shot was generated as a silent one and the project's
        # language never reached the model.
        _dialogue_slot(shot, project, character_bible),
        # 15 continuity contract, 16 quality floor, 17 prohibitions
        _SET_CONTINUITY,
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
    # What the previous frame actually LEFT ON SCREEN. An action alone
    # says what happened, not where anyone ended up, so the next shot
    # re-invents the staging and the cut reads as a jump.
    previous_subject: str | None = None
    previous_blocking: str | None = None
    previous_shot_size: str | None = None


def _narrative_position(position: ShotPosition | None, subject: str = "") -> str:
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
        # Describe the frame being cut FROM, not merely the verb in it.
        # Watching a real film, the shots did not feel continuous: each
        # clip is generated independently, and "continuing from: she
        # looks out of the window" leaves the model free to restage the
        # room, move the actor and relight the set between every cut.
        tail = f"the previous shot showed {position.previous_subject or 'the actor'} "
        tail += position.previous_action
        if position.previous_blocking:
            tail += f", positioned {position.previous_blocking}"
        if position.previous_shot_size:
            tail += f", framed {position.previous_shot_size.replace('_', ' ')}"
        # Only tell the model to hold a person's staging when the shot is
        # still ABOUT that person. On a cut to someone else -- which is
        # most of a two-hander -- "keep that person where they were" aims
        # the instruction at the wrong actor, and the previous subject
        # gets dragged into a frame they should have left.
        same_subject = bool(
            subject and position.previous_subject
            and subject.strip() == position.previous_subject.strip()
        )
        carry = (
            "keep that person in the same place, in the same clothes, carrying whatever "
            "they were carrying"
            if same_subject else
            "the space, the light and everything already established in it continue "
            "unchanged from that frame"
        )
        where += f", continuing directly from that moment -- {tail}; {carry}"
    return where


def _dialogue_slot(shot, project, character_bible: dict | None = None) -> str:
    """The audio-performance fragment: what is said, by whom, in what
    voice — or an explicit instruction to stay quiet.

    Two things watching a real film exposed here.

    A silent shot used to emit NOTHING, which is not the same as asking
    for silence: the model still generates an audio track, and with no
    instruction it invents a speaker. The first shot of a two-man scene
    came back narrated by a woman who is not in the film. So a shot with
    no line now says so.

    And a speaking shot said only WHAT was said, never how the character
    sounds. Each 8-second clip is generated independently, so with no
    vocal anchor the voice is re-rolled every shot and the same person
    sounds like three different people. `voice_style` was already in the
    character bible, carried all the way from adaptation, and was simply
    never compiled — reference images pinned the face while nothing
    pinned the voice.
    """
    line = (getattr(shot, "dialogue_line", None) or "").strip()
    if not line:
        return "no spoken dialogue in this shot, ambient sound only, no narration, no voiceover"

    language = _SPOKEN_LANGUAGE_NAMES.get(
        (getattr(project, "language", "") or "").lower(),
    )
    speaker = shot.subject or "the actor"
    spoken = f'{speaker} speaks aloud, lip-synced, saying "{line}"'
    if language:
        spoken += f" in {language}"

    # The vocal anchor: whatever the adaptation decided this character
    # sounds like, plus the age band, since age is the single strongest
    # cue for keeping a voice recognisable across separate generations.
    bible = character_bible or {}
    # The service nests it as {"voice_profile": {"style": ...}}; the flat
    # key is accepted too so a bible written by anything else still works.
    profile = bible.get("voice_profile")
    voice = str(
        (profile.get("style") if isinstance(profile, dict) else profile)
        or bible.get("voice_style", "")
        or ""
    ).strip()
    age = str(bible.get("age_range", "") or "").strip()
    anchor = ", ".join(part for part in (voice, f"{age} voice" if age else "") if part)
    if anchor:
        spoken += f", spoken in a {anchor}, the same voice as in every other shot"
    return spoken


def prompt_fingerprint(prompt: str) -> str:
    """Stable identity of a compiled prompt. Versioned so a change to the
    assembly rules is never mistaken for the same request."""
    return hashlib.sha256(f"{COMPILER_VERSION}\x1f{prompt}".encode()).hexdigest()[:32]
