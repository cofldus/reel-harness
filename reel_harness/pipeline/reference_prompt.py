"""Canonical, provider-neutral reference-image prompt assembly (Fable F3).

The sibling of pipeline.shot_prompt, for stills rather than clips. One
character becomes FOUR prompts -- a face portrait, a three-quarter view, a
full-body view, and a wardrobe detail -- compiled from the same
`fixed_identity` block the shot compiler injects into every shot, so the
reference sheet and the eventual footage describe one actor rather than
two.

The generation ORDER is the whole point and is encoded here rather than
left to the caller: `REFERENCE_VIEWS[0]` is the face, and every later view
is generated WITH the face image fed back as a character reference.
Generating the four independently produces four different people -- the
exact failure this sheet exists to prevent -- so the order is a property
of the vocabulary, not a scheduling detail.

Provider-neutral: no vendor's prompt dialect appears here. The Google
adapter (F3 commit 4) translates this canonical form into its own request
shape, exactly as the Veo adapter will for shots.

Version note: COMPILER_VERSION is part of the fingerprint, so changing
the assembly rules changes every fingerprint -- deliberately, since a
differently-compiled prompt is a different (paid) generation request.
"""
from __future__ import annotations

import hashlib
from enum import StrEnum

from reel_harness.pipeline.shot_prompt import fixed_identity_values

COMPILER_VERSION = "fable-reference-v1"

# What every reference still is generated at. 1K is deliberate and
# sufficient rather than cheap: the surveyed video models cap
# reference-driven runs at 720p, so paying for 2K/4K buys nothing a shot
# could ever use (see docs/STATUS.md's provider research).
DEFAULT_REFERENCE_RESOLUTION = "1k"

# Portrait regardless of the film's aspect ratio. A reference still frames
# a standing person, not a frame of the movie -- a 16:9 full-body view
# would waste most of its pixels on the empty space beside the actor.
REFERENCE_ASPECT_RATIO = "9:16"


class ReferenceView(StrEnum):
    """The views of a character reference sheet. FACE is first by
    contract -- see the module docstring.

    BACK exists because films end with people walking away. Every other
    view faces the camera, so a shot of someone from behind had nothing
    to match and the model invented a back each time -- the old man's
    receding figure, which is the last image of the story, differed from
    shot to shot. One more image per character is cheap next to a video
    second."""

    FACE = "face"
    THREE_QUARTER = "three_quarter"
    FULL_BODY = "full_body"
    WARDROBE = "wardrobe"
    BACK = "back"


# Ordered, and the order is load-bearing: index 0 is generated from text
# alone; every later view is chained off it.
REFERENCE_VIEWS: tuple[ReferenceView, ...] = (
    ReferenceView.FACE, ReferenceView.THREE_QUARTER,
    ReferenceView.FULL_BODY, ReferenceView.WARDROBE, ReferenceView.BACK,
)

# Per-view framing. Written as camera direction rather than art direction
# so the sheet reads as one actor photographed four times, which is what
# a downstream video model needs, rather than four illustrations.
_VIEW_FRAMING: dict[ReferenceView, str] = {
    ReferenceView.FACE: (
        "head-and-shoulders portrait, facing camera directly, neutral expression, "
        "even soft lighting, plain neutral background"
    ),
    ReferenceView.THREE_QUARTER: (
        "three-quarter view from the waist up, head turned 45 degrees from camera, "
        "neutral expression, even soft lighting, plain neutral background"
    ),
    ReferenceView.FULL_BODY: (
        "full-body standing view, head to feet in frame, arms relaxed at the sides, "
        "even soft lighting, plain neutral background"
    ),
    ReferenceView.WARDROBE: (
        "wardrobe detail, garment and fabric texture clearly visible, "
        "even soft lighting, plain neutral background"
    ),
    ReferenceView.BACK: (
        "full-body view from directly behind, facing away from camera, "
        "back of the head and shoulders and the garment's back clearly visible, "
        "arms relaxed at the sides, even soft lighting, plain neutral background"
    ),
}

# Applied to every view. A reference still is a continuity document, not a
# shot: anything that would bake a scene's mood into the actor's permanent
# look is prohibited here even though it is desirable in a shot prompt.
_REFERENCE_QUALITY = (
    "photorealistic reference photograph of a fictional adult actor, sharp focus, "
    "consistent facial structure"
)
_REFERENCE_PROHIBITED = (
    "no distorted face, no extra or malformed fingers, no duplicated limbs, "
    "no text overlays, no watermark, no dramatic or coloured lighting, "
    "no scene background, no props, not a real or recognizable person"
)


def compile_reference_prompt(view: ReferenceView, character, project) -> str:
    """Assembles one view's canonical prompt. `character` is a
    FableCharacter and `project` a StoryProject; both are passed in
    already loaded so this stays a pure function, exactly as
    compile_shot_prompt is."""
    bible = character.bible or {}
    identity_values = fixed_identity_values(bible)

    # Wardrobe is normally part of fixed_identity, so it is already in the
    # identity fragment -- emit the standalone field only when it is not,
    # mirroring compile_shot_prompt's rule. For the WARDROBE view itself
    # the garment is the subject, so it is never suppressed there.
    wardrobe = str(bible.get("wardrobe", ""))
    if wardrobe and wardrobe in identity_values and view is not ReferenceView.WARDROBE:
        wardrobe = ""

    slots: list[str] = [
        f"a single fictional adult actor: {character.name}",
        # The age bracket is stated explicitly in every reference prompt.
        # The adaptation schema already refuses minors, but a reference
        # sheet is what a video model imitates for the whole film, so the
        # constraint is repeated rather than assumed inherited.
        f"{character.age_range} adult" if character.age_range else "adult",
        ", ".join(identity_values),
        wardrobe,
        _VIEW_FRAMING[view],
        str((project.story_bible or {}).get("visual_style", "")),
        _REFERENCE_QUALITY,
        _REFERENCE_PROHIBITED,
    ]
    return ", ".join(slot.strip() for slot in slots if slot and slot.strip())


def reference_fingerprint(character, project) -> str:
    """Deterministic identity of a character's whole reference SHEET --
    every view at once, not one image.

    Sheet-level rather than per-image on purpose: the views are chained,
    so a change that alters the face invalidates the three that were
    generated from it. A single fingerprint makes "is this sheet still the
    one the bible describes?" answerable with one comparison, and makes
    an unchanged re-run a replay rather than four more paid calls."""
    parts = [COMPILER_VERSION, *(
        compile_reference_prompt(view, character, project) for view in REFERENCE_VIEWS
    )]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:32]
