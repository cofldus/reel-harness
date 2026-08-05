"""Character-to-voice assignment.

The whole point of synthesising dialogue is that a character sounds like
one person for the whole film, so the property under test is stability:
the same character must get the same voice across renders, and adding
somebody to the cast must not re-voice everyone already in it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reel_harness.pipeline.dialogue_voice import (
    OLDER_VOICES,
    YOUNGER_VOICES,
    assign_voice,
    assign_voices,
)


@dataclass
class _Character:
    id: str
    name: str
    age_range: str = "30s"
    bible: dict = field(default_factory=dict)


def test_the_same_character_always_gets_the_same_voice() -> None:
    """Re-rendering a film, or regenerating one shot, must not change who
    anybody sounds like."""
    character = _Character("c-1", "도윤", "20s")
    assert assign_voice(character).voice == assign_voice(character).voice


def test_an_older_character_is_drawn_from_the_older_pool() -> None:
    assert assign_voice(_Character("c-2", "가게 주인", "60s")).voice in OLDER_VOICES
    assert assign_voice(_Character("c-3", "도윤", "20s")).voice in YOUNGER_VOICES


def test_a_soft_spoken_young_person_is_not_given_an_old_voice() -> None:
    """Register used to outrank age, and a twenty-year-old clerk came out
    voiced as a sixty-year-old because his description said "낮고 조용한"
    -- soft-spoken, which is a manner, not an age."""
    young_but_low = _Character(
        "c-4", "준호", "20s", {"voice_profile": {"style": "낮고 조용한 말투"}},
    )
    assert assign_voice(young_but_low).voice in YOUNGER_VOICES


def test_register_decides_only_when_no_age_was_stated() -> None:
    low = _Character("c-6", "목소리", "", {"voice_profile": {"style": "낮고 굵은 목소리"}})
    assert assign_voice(low).voice in OLDER_VOICES
    assert "no age stated" in assign_voice(low).reason


def test_a_vague_description_is_not_guessed_from() -> None:
    """Only descriptions that state a register are trusted. Inferring
    from a name is exactly how a person gets voiced wrong."""
    vague = _Character("c-5", "지우", "20s", {"voice_profile": {"style": "차분한 목소리"}})
    assert assign_voice(vague).voice in YOUNGER_VOICES


def test_a_cast_does_not_share_one_voice() -> None:
    """Two people in a two-hander sounding identical defeats the point."""
    cast = [_Character(f"c-{n}", f"인물{n}", "30s") for n in range(4)]
    voices = {a.voice for a in assign_voices(cast).values()}
    assert len(voices) == len(cast)


def test_adding_a_character_does_not_revoice_the_others() -> None:
    """Assignment hashes the character's own id rather than its position,
    so a cast change is not a re-cast."""
    first = [_Character("c-1", "도윤", "20s"), _Character("c-2", "주인", "60s")]
    before = {k: v.voice for k, v in assign_voices(first).items()}
    later = [*first, _Character("c-3", "어머니", "50s")]
    after = assign_voices(later)
    assert after["c-1"].voice == before["c-1"]
    assert after["c-2"].voice == before["c-2"]


def test_an_operator_override_wins_and_says_so() -> None:
    assignment = assign_voice(_Character("c-1", "도윤", "20s"), override="onyx")
    assert assignment.voice == "onyx"
    assert "override" in assignment.reason


def test_every_assignment_explains_itself() -> None:
    """An assignment the operator dislikes should be something they can
    see the reason for."""
    assignment = assign_voice(_Character("c-9", "노인", "70s"))
    assert assignment.reason
    assert assignment.character_name == "노인"
