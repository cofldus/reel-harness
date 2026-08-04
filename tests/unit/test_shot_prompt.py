"""Canonical shot prompt assembly (pipeline.shot_prompt): fixed slot
order, determinism, mandatory fixed-identity injection, fingerprint
stability and versioning. These properties are load-bearing -- the
fingerprint is what makes paid take generation idempotent, and the
identity injection is the only thing keeping one virtual actor
recognizable across separately-generated clips."""
from __future__ import annotations

from types import SimpleNamespace

from reel_harness.pipeline.shot_prompt import (
    _PROHIBITED_ARTIFACTS,
    COMPILER_VERSION,
    compile_shot_prompt,
    prompt_fingerprint,
)


def _shot(**overrides):
    defaults = dict(
        subject="지우", action="창밖을 바라본다", expression="절제된 불안",
        blocking="창가에 선 채", shot_size="medium_close_up", lens_style="50mm",
        camera_movement="dolly_in", camera_angle="eye_level", lighting="soft practical",
        continuity_requirements={"source_beat": "창밖의 비를 바라보았다"},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _project(**overrides):
    bible = {"visual_style": "muted palette, film grain"}
    bible.update(overrides.pop("story_bible", {}))
    return SimpleNamespace(id="p1", story_bible=bible, **overrides)


_CHARACTER_BIBLE = {
    "wardrobe": "grey coat",
    "fixed_identity": {
        "face": "oval face, calm eyes", "hair": "black short hair",
        "wardrobe": "grey coat",
    },
}

_LOCATION = {
    "name": "호텔 방", "description": "a hotel room at night",
    "continuity": {"time_of_day": "night", "weather": "rain", "lighting": "soft practicals"},
}


def test_slots_appear_in_the_fixed_canonical_order() -> None:
    prompt = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    ordered = [
        "지우",                    # 1 subject
        "oval face, calm eyes",   # 2 fixed identity
        "호텔 방",                 # 4 location
        "창밖을 바라본다",           # 5 action
        "절제된 불안",              # 6 expression
        "창가에 선 채",             # 7 blocking
        "medium close up",        # 8 shot size (normalized)
        "50mm",                   # 9 lens
        "dolly in",               # 10 camera movement (normalized)
        "muted palette",          # 12 atmosphere
        "photorealistic",         # 14 quality floor
        "no distorted faces",     # 15 prohibitions
    ]
    positions = [prompt.find(fragment) for fragment in ordered]
    assert all(position >= 0 for position in positions), positions
    assert positions == sorted(positions)


def test_fixed_identity_is_always_injected() -> None:
    """Without this, the same character drifts between separately
    generated clips -- the whole point of the compiler."""
    prompt = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    assert "oval face, calm eyes" in prompt
    assert "black short hair" in prompt
    assert "grey coat" in prompt


def test_wardrobe_is_not_repeated_when_fixed_identity_already_carries_it() -> None:
    """Wardrobe must stay constant across shots, so it normally lives in
    fixed_identity too -- emitting it twice adds no information and
    dilutes the prompt. Found in a real live-adaptation run."""
    prompt = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    assert prompt.count("grey coat") == 1


def test_wardrobe_still_appears_when_absent_from_fixed_identity() -> None:
    bible = {"wardrobe": "grey coat", "fixed_identity": {"hair": "black short hair"}}
    prompt = compile_shot_prompt(_shot(), _project(), bible, _LOCATION)
    assert prompt.count("grey coat") == 1


def test_unknown_identity_keys_are_kept_not_dropped() -> None:
    bible = {"fixed_identity": {"hair": "black short hair", "tattoo": "small wrist tattoo"}}
    prompt = compile_shot_prompt(_shot(), _project(), bible, _LOCATION)
    assert "small wrist tattoo" in prompt


def test_compilation_is_deterministic() -> None:
    first = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    second = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    assert first == second
    assert prompt_fingerprint(first) == prompt_fingerprint(second)


def test_changing_any_shot_field_changes_the_fingerprint() -> None:
    base = prompt_fingerprint(
        compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION),
    )
    changed = prompt_fingerprint(
        compile_shot_prompt(
            _shot(action="천천히 돌아선다"), _project(), _CHARACTER_BIBLE, _LOCATION,
        ),
    )
    assert base != changed


def test_missing_optional_fields_do_not_produce_empty_slots() -> None:
    prompt = compile_shot_prompt(
        _shot(expression="", blocking="", lens_style="", lighting=""),
        _project(), None, {},
    )
    assert ", ," not in prompt
    assert not prompt.startswith(",")
    assert not prompt.rstrip().endswith(",")
    # The prohibitions are the last slot, so the prompt ending with them is
    # what proves no empty slot trailed off the end. Asserted by membership
    # rather than by pinning the exact sentence -- the prohibition list
    # grows (no subtitles, no burned-in captions) and the property under
    # test is "nothing empty at the end", not its current wording.
    assert prompt.rstrip().endswith(_PROHIBITED_ARTIFACTS)


def test_fingerprint_is_versioned() -> None:
    """COMPILER_VERSION participates in the hash, so changing the
    assembly rules is never mistaken for the same request."""
    import hashlib

    prompt = "a, b, c"
    expected = hashlib.sha256(f"{COMPILER_VERSION}\x1f{prompt}".encode()).hexdigest()[:32]
    assert prompt_fingerprint(prompt) == expected


def test_location_continuity_reaches_the_prompt() -> None:
    prompt = compile_shot_prompt(_shot(), _project(), _CHARACTER_BIBLE, _LOCATION)
    assert "night" in prompt
    assert "rain" in prompt


def test_shot_lighting_wins_over_location_default() -> None:
    prompt = compile_shot_prompt(
        _shot(lighting="harsh overhead"), _project(), _CHARACTER_BIBLE, _LOCATION,
    )
    assert "harsh overhead" in prompt


# -- audio performance ---------------------------------------------------
#
# Both of these come from watching a real film the pipeline produced.
# Neither is theoretical: the first shot came back narrated by a woman who
# is not in the story, and the old man sounded like a different person in
# each of his shots.

def _bible() -> dict:
    return {
        "fixed_identity": {"face": "lined face", "hair": "grey", "wardrobe": "black coat"},
        "wardrobe": "black coat",
        "voice_profile": {"style": "low, restrained"},
        "age_range": "60s",
    }


def test_a_silent_shot_asks_for_silence_rather_than_saying_nothing() -> None:
    """Emitting nothing is not the same as asking for quiet. The model
    generates an audio track either way, and with no instruction it
    invents a speaker -- which is how a two-man scene acquired a female
    narrator."""
    from reel_harness.pipeline.shot_prompt import _dialogue_slot

    shot = _shot(dialogue_line=None)
    text = _dialogue_slot(shot, _project(), _bible())
    assert "no spoken dialogue" in text
    assert "no narration" in text and "no voiceover" in text


def test_a_speaking_shot_carries_the_voice_not_only_the_words() -> None:
    """Each clip is generated independently, so without a vocal anchor the
    voice is re-rolled per shot. `voice_style` reached the character bible
    from adaptation and was never compiled -- the face was pinned by
    reference images while nothing pinned the voice."""
    from reel_harness.pipeline.shot_prompt import _dialogue_slot

    shot = _shot(dialogue_line="우산 있나?")
    text = _dialogue_slot(shot, _project(), _bible())
    assert '"우산 있나?"' in text
    assert "low, restrained" in text
    assert "60s voice" in text
    assert "same voice as in every other shot" in text


def test_a_speaking_shot_still_works_without_a_bible() -> None:
    """A missing bible degrades to words-only rather than raising."""
    from reel_harness.pipeline.shot_prompt import _dialogue_slot

    text = _dialogue_slot(_shot(dialogue_line="가져가세요."), _project(), None)
    assert '"가져가세요."' in text
    assert "same voice" not in text


# -- continuity ----------------------------------------------------------
#
# Watching a real film, the four shots did not read as continuous. Each
# clip is generated independently, so nothing but the words stops the
# model restaging the room, moving the actor and relighting the set
# between cuts.

def test_a_shot_describes_the_frame_it_cuts_from_not_just_the_verb() -> None:
    from reel_harness.pipeline.shot_prompt import ShotPosition, _narrative_position

    text = _narrative_position(ShotPosition(
        index=3, total=4,
        previous_action="우산을 건넨다",
        previous_subject="준호",
        previous_blocking="계산대 뒤에 서서",
        previous_shot_size="medium_close_up",
    ), "준호")
    assert "준호" in text and "우산을 건넨다" in text
    assert "계산대 뒤에 서서" in text
    assert "medium close up" in text
    assert "same place" in text and "carrying whatever" in text


def test_the_first_shot_has_nothing_to_continue_from() -> None:
    from reel_harness.pipeline.shot_prompt import ShotPosition, _narrative_position

    text = _narrative_position(ShotPosition(index=1, total=4))
    assert "opening shot" in text
    assert "continuing directly" not in text


def test_every_shot_carries_the_set_continuity_contract() -> None:
    """Applies to all shots, including the first: the set has to be
    consistent with the shots that follow it too."""
    from reel_harness.pipeline.shot_prompt import compile_shot_prompt

    text = compile_shot_prompt(_shot(dialogue_line=None), _project(), _bible())
    assert "same physical set" in text
    assert "consistent screen direction" in text
    assert "no redecoration" in text


def test_a_cut_to_a_different_person_does_not_hold_the_previous_one() -> None:
    """Most of a two-hander is cuts between people. Telling the model to
    keep the previous subject where they were aims the instruction at the
    wrong actor and drags them into a frame they should have left."""
    from reel_harness.pipeline.shot_prompt import ShotPosition, _narrative_position

    position = ShotPosition(
        index=4, total=4, previous_action="우산을 건넨다",
        previous_subject="준호", previous_blocking="계산대 뒤에 서서",
    )
    same = _narrative_position(position, "준호")
    assert "keep that person in the same place" in same

    switched = _narrative_position(position, "노인")
    assert "keep that person in the same place" not in switched
    assert "the space, the light" in switched
    # The frame being cut from is still described either way.
    assert "준호" in switched and "우산을 건넨다" in switched


def test_the_prompt_forbids_broken_bodies_not_only_bad_faces() -> None:
    """A real generated shot came back with a man whose torso faced the
    counter while his legs stretched sideways at about a right angle.
    Faces, fingers and duplicated limbs were already forbidden; the joint
    being wrong was not, and it is the artifact most likely to make a
    frame unusable -- visible at a glance and impossible to crop out."""
    from reel_harness.pipeline.shot_prompt import compile_shot_prompt

    text = compile_shot_prompt(_shot(), _project(), _bible())
    assert "anatomically correct body" in text
    assert "no bent or broken torso" in text
    assert "impossible angles" in text
