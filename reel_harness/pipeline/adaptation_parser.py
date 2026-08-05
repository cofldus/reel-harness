"""Adaptation parsing + whole-document validation (Fable F2).

`parse_adaptation` is the strict single entry point: JSON extraction
(one bounded leniency pass for fenced/wrapped output), pydantic schema
validation (pipeline.adaptation_schema), then the cross-field semantic
rules and the source-fidelity heuristic. Failures raise
AdaptationValidationError carrying EVERY collected error string -- the
repair loop (core.adaptation_service) feeds that list back to the
director verbatim, so error collection is part of the contract, not just
diagnostics.

Fidelity honesty (see the F2 plan): the automated check only rejects
OBVIOUS drift -- a scene whose `source_beat` cannot be matched back into
the real source text (i.e. a fabricated citation), or a dropped ending
when keep_ending was requested. Semantic faithfulness beyond that is the
STORY_REVIEW gate's human decision; this module never claims more."""
from __future__ import annotations

import json
import re

from pydantic import ValidationError

from reel_harness.core.errors import SchemaValidationError
from reel_harness.pipeline.adaptation_schema import AdaptationModel

# Minimum character-bigram Jaccard similarity between a scene's
# source_beat and its best-matching source window for the beat to count
# as genuinely drawn from the source. Deliberately permissive: the goal
# is rejecting fabricated citations, not near-miss paraphrase policing.
_FIDELITY_MIN_SIMILARITY = 0.35


class AdaptationValidationError(SchemaValidationError):
    """Schema/semantic failure with the full error list preserved for the
    repair loop. Inherits SchemaValidationError so the existing
    stage-retry classification (SCHEMA_INVALID, retryable) applies
    unchanged once repairs are exhausted."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("adaptation failed validation: " + "; ".join(errors[:5]))


def _extract_json(raw_text: str) -> dict:
    """Strict json.loads first; on failure, ONE bounded leniency pass:
    strip markdown fences, then cut from the first '{' to the last '}'.
    Anything still unparseable is a validation error (repair input),
    never a silent guess."""
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text.strip(), flags=re.MULTILINE)
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            raise AdaptationValidationError(
                ["output is not valid JSON and contains no JSON object"]
            ) from None
        try:
            data = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise AdaptationValidationError([f"output is not valid JSON: {exc}"]) from exc
    if not isinstance(data, dict):
        raise AdaptationValidationError(["output JSON must be a single object"])
    return data


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _bigrams(text: str) -> set[str]:
    return {text[i:i + 2] for i in range(len(text) - 1)}


def _beat_similarity(beat: str, source: str) -> float:
    """Best character-bigram Jaccard similarity between the beat and any
    same-length window of the source (whitespace-normalized). A verbatim
    or near-verbatim quote scores high; an invented citation scores low."""
    norm_beat, norm_source = _normalize(beat), _normalize(source)
    if not norm_beat or not norm_source:
        return 0.0
    if norm_beat in norm_source:
        return 1.0
    beat_grams = _bigrams(norm_beat)
    if not beat_grams:
        return 0.0
    window = len(norm_beat)
    step = max(1, window // 2)
    best = 0.0
    for start in range(0, max(1, len(norm_source) - window + 1), step):
        window_grams = _bigrams(norm_source[start:start + window])
        if not window_grams:
            continue
        overlap = len(beat_grams & window_grams) / len(beat_grams | window_grams)
        best = max(best, overlap)
    return best


def _semantic_errors(adaptation: AdaptationModel) -> list[str]:
    """The seven deterministic whole-document rules from the F2 plan --
    everything here is plain code over the validated model, never an LLM
    judgment."""
    errors: list[str] = []
    character_names = {c.name for c in adaptation.characters}
    location_names = {loc.name for loc in adaptation.locations}

    for scene in adaptation.scenes:
        prefix = f"scene {scene.scene_order}"
        if scene.location_name not in location_names:
            errors.append(f"{prefix}: location_name {scene.location_name!r} is not a declared location")

        speakers = {d.speaker for d in scene.dialogue}
        for speaker in speakers:
            if speaker not in character_names:
                errors.append(f"{prefix}: dialogue speaker {speaker!r} is not a declared character")

        previous_subject: str | None = None
        for shot in scene.shots:
            shot_prefix = f"{prefix} shot {shot.shot_order}"
            if shot.subject not in character_names:
                errors.append(
                    f"{shot_prefix}: subject {shot.subject!r} is not a declared character"
                )
            if shot.dialogue_line is not None and shot.dialogue_line:
                spoken_by_subject = any(
                    d.speaker == shot.subject and d.line == shot.dialogue_line
                    for d in scene.dialogue
                )
                if scene.dialogue and not spoken_by_subject:
                    errors.append(
                        f"{shot_prefix}: dialogue_line must belong to the shot's subject "
                        f"({shot.subject!r}) in the scene's dialogue list"
                    )
            # Shot/reverse-shot: in a multi-speaker dialogue scene, two
            # consecutive shots must not stay on the same subject.
            if len(speakers) >= 2 and previous_subject is not None and shot.subject == previous_subject:
                errors.append(
                    f"{shot_prefix}: dialogue scene repeats subject {shot.subject!r} in "
                    "consecutive shots -- split into shot/reverse-shot"
                )
            previous_subject = shot.subject

    orders = [scene.scene_order for scene in adaptation.scenes]
    if sorted(orders) != list(range(1, len(orders) + 1)):
        errors.append(f"scene_order values must be 1..{len(orders)} without gaps, got {orders}")
    for scene in adaptation.scenes:
        shot_orders = [s.shot_order for s in scene.shots]
        if sorted(shot_orders) != list(range(1, len(shot_orders) + 1)):
            errors.append(
                f"scene {scene.scene_order}: shot_order values must be 1..{len(shot_orders)} "
                f"without gaps, got {shot_orders}"
            )
    return errors


def _fidelity_errors(adaptation: AdaptationModel, source_text: str, keep_ending: bool) -> list[str]:
    errors: list[str] = []
    for scene in adaptation.scenes:
        similarity = _beat_similarity(scene.source_beat, source_text)
        if similarity < _FIDELITY_MIN_SIMILARITY:
            errors.append(
                f"scene {scene.scene_order}: source_beat does not match the source text "
                f"(best similarity {similarity:.2f}) -- quote the actual source passage this "
                "scene dramatizes"
            )
    if keep_ending and not adaptation.story_bible.ending_summary.strip():
        errors.append("keep_ending was requested but ending_summary is empty")
    return errors


# Reference-driven shots are a fixed length, so a target duration is a
# target SHOT COUNT. Kept here rather than imported from the web layer:
# this is a property of the generation pipeline, not of a form.
SHOT_SECONDS = 8

# Any of the quote marks Korean and English prose actually use.
_SPOKEN_RE = re.compile(r'[“"‘「『][^”"’」』]{2,}')


def _craft_errors(
    adaptation: AdaptationModel, source_text: str, target_shot_count: int | None,
) -> list[str]:
    """Film-grammar rules, measured rather than assumed.

    Nine real GPT-4o runs across three stories showed the adaptation is
    faithful but cinematically flat: the one line of quoted speech in a
    source was dropped entirely in some runs, shot counts ranged from
    half to one-and-a-half times what was asked for, camera angle
    collapsed to a single value in most runs, and 2 of 9 runs produced a
    plan where the camera never moved at all.

    None of that is caught by schema or fidelity checks -- the documents
    were perfectly valid and perfectly faithful. These four rules turn
    each measured failure into a repair-loop error, which is the one
    mechanism that already exists for "the model can do better, ask
    again".

    Every rule is skipped for plans too short to have variety, so a
    deliberate two-shot piece is never nagged about camera coverage.
    """
    errors: list[str] = []
    shots = [shot for scene in adaptation.scenes for shot in scene.shots]
    if not shots:
        return errors

    if target_shot_count:
        # Exact, not +/-1. The tolerance was borrowed from pipelines where
        # a shot's length is the writer's choice, so one shot either way
        # is a rounding difference. Here every reference-driven shot is
        # exactly SHOT_SECONDS long, so one extra shot is eight extra
        # seconds -- a 32-second film came back at 40, a quarter longer
        # than was asked for and a quarter dearer.
        if len(shots) != target_shot_count:
            errors.append(
                f"the plan has {len(shots)} shots but the requested runtime needs exactly "
                f"{target_shot_count} -- every shot is {SHOT_SECONDS} seconds, so the count "
                "IS the runtime; add or merge shots to fit"
            )

    # Speech that exists in the source must survive into the film. The
    # user is told to write dialogue in quotes; dropping it makes that
    # instruction a lie.
    if _SPOKEN_RE.search(source_text or ""):
        spoken = sum(1 for shot in shots if (shot.dialogue_line or "").strip())
        if spoken == 0:
            errors.append(
                "the source contains quoted speech but no shot carries a dialogue_line -- "
                "assign each spoken line to the shot where it is said"
            )

    # A scene is a place and a continuous moment. Splitting one location
    # into a scene per beat -- which a real run did, four times over
    # inside the same bus -- tells the audience that time or place jumped
    # when it did not. Only checked once there is more than one scene, so
    # a single-scene piece is never asked to subdivide itself.
    if len(adaptation.scenes) > 1:
        thin = [scene.scene_order for scene in adaptation.scenes if len(scene.shots) < 2]
        if thin:
            errors.append(
                f"scene(s) {thin} hold only one shot each -- a scene is a place and a "
                "continuous moment, so either give each scene at least two shots or merge "
                "them into one scene"
            )

    # Dialogue density. Prose narrates what a screenplay lets people say,
    # and an adaptation that only forwards the source's quoted lines
    # produces a film where almost nobody speaks -- a real run gave one
    # spoken line across four shots. Writing lines is an adaptation's job;
    # writing EVENTS is not, which the prompt constrains separately.
    if len(shots) >= 2:
        spoken = sum(1 for shot in shots if (shot.dialogue_line or "").strip())
        wanted = len(shots) // 2
        if spoken < wanted:
            errors.append(
                f"only {spoken} of {len(shots)} shots carry a dialogue_line -- at least "
                f"{wanted} should. Where the source describes an exchange or a reaction "
                "without quoting it, write the line the character would speak; invent "
                "lines, never events"
            )

    if len(shots) >= 3:
        angles = {shot.camera_angle for shot in shots}
        if len(angles) < 2:
            errors.append(
                f"every shot uses camera_angle {next(iter(angles))!r} -- vary the angle so the "
                "sequence reads as coverage rather than one setup"
            )
        movements = {shot.camera_movement for shot in shots}
        if movements == {"locked"}:
            errors.append(
                "every shot is locked off, so the camera never moves -- give at least one shot "
                "a motivated move"
            )
    return errors


def parse_adaptation(
    raw_text: str, *, source_text: str, keep_ending: bool = True,
    target_shot_count: int | None = None,
) -> AdaptationModel:
    """Extraction -> schema -> semantic -> fidelity + craft, all errors
    collected per layer (a layer only runs when the previous one passed,
    so the repair feedback is always about the outermost real problem)."""
    data = _extract_json(raw_text)
    try:
        adaptation = AdaptationModel.model_validate(data)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        raise AdaptationValidationError(errors) from exc

    errors = _semantic_errors(adaptation)
    errors.extend(_fidelity_errors(adaptation, source_text, keep_ending))
    errors.extend(_craft_errors(adaptation, source_text, target_shot_count))
    if errors:
        raise AdaptationValidationError(errors)
    return adaptation
