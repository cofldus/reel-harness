"""Which synthetic voice speaks for which character.

The point of moving dialogue out of the video model is that a character
sounds like one person for the whole film. That only holds if the mapping
from character to voice is DETERMINISTIC -- decided from what the
adaptation already wrote down, never from call order or randomness, so
re-rendering a film or regenerating one shot cannot change who anybody
sounds like.

The adaptation gives two usable signals: `age_range`, which is validated
against a whitelist, and `voice_style`, free text the director wrote
(e.g. "낮고 조용한 일상적 말투"). Neither states gender, and guessing
gender from a Korean name is exactly the kind of inference that gets a
person wrong, so it is read from the voice description only when the
description says so outright -- otherwise the pool is chosen by age alone
and the operator can override per character.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

# OpenAI's published voice ids. Grouped by the two things a listener
# actually notices first -- apparent age and register -- rather than by
# vendor marketing labels.
YOUNGER_VOICES: tuple[str, ...] = ("alloy", "echo", "nova", "shimmer")
OLDER_VOICES: tuple[str, ...] = ("onyx", "fable", "ash", "sage")

# Age bands the adaptation schema allows, split at the point where a
# listener stops hearing "young adult".
_OLDER_BANDS = frozenset({"40s", "50s", "60s", "70s", "80s"})

# Words that state a register outright. Only these are trusted; anything
# vaguer is left alone rather than guessed at.
_LOW_MARKERS = ("낮은", "낮고", "굵은", "저음", "low", "deep", "gravelly")
_HIGH_MARKERS = ("높은", "높고", "밝은", "가는", "high", "bright", "light")


@dataclass(frozen=True)
class VoiceAssignment:
    character_id: str
    character_name: str
    voice: str
    # Why this voice, in one phrase -- shown to the operator so an
    # assignment they dislike is something they can see the reason for
    # rather than a number they have to trust.
    reason: str


def _voice_style(bible: dict | None) -> str:
    profile = (bible or {}).get("voice_profile")
    if isinstance(profile, dict):
        return str(profile.get("style") or "")
    return str(profile or (bible or {}).get("voice_style") or "")


def assign_voice(character, override: str | None = None) -> VoiceAssignment:
    """One character's voice. Stable for the life of the character."""
    name = getattr(character, "name", "") or "?"
    character_id = getattr(character, "id", "") or name

    if override:
        return VoiceAssignment(character_id, name, override, "operator override")

    bible = getattr(character, "bible", None) or {}
    style = _voice_style(bible).lower()
    age = str(getattr(character, "age_range", "") or "").strip().lower()

    older = age in _OLDER_BANDS or any(m in style for m in _LOW_MARKERS)
    pool = OLDER_VOICES if older else YOUNGER_VOICES
    reason = (
        f"age {age}" if age in _OLDER_BANDS
        else "low register in voice_style" if older
        else f"age {age}" if age
        else "no age or register stated"
    )

    # Hash the character's own id, not its index: adding a character to
    # the cast must not re-voice everyone already in it.
    digest = hashlib.sha256(character_id.encode()).digest()
    return VoiceAssignment(character_id, name, pool[digest[0] % len(pool)], reason)


def assign_voices(characters, overrides: dict[str, str] | None = None) -> dict[str, VoiceAssignment]:
    """Voices for a whole cast, keyed by character id.

    Distinctness is attempted but never forced: two characters landing on
    the same voice is a real problem for a two-hander, so a collision
    walks the pool for a free voice -- and if the pool is exhausted the
    duplicate stands rather than silently reaching into the other age
    group, which would sound more wrong than a repeat.
    """
    overrides = overrides or {}
    taken: set[str] = set()
    result: dict[str, VoiceAssignment] = {}
    for character in characters:
        assignment = assign_voice(character, overrides.get(getattr(character, "id", "")))
        if assignment.voice in taken and not overrides.get(getattr(character, "id", "")):
            pool = OLDER_VOICES if assignment.voice in OLDER_VOICES else YOUNGER_VOICES
            free = next((v for v in pool if v not in taken), None)
            if free:
                assignment = VoiceAssignment(
                    assignment.character_id, assignment.character_name, free,
                    f"{assignment.reason}, moved off a collision",
                )
        taken.add(assignment.voice)
        result[assignment.character_id] = assignment
    return result
