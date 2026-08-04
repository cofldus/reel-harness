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
