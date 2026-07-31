"""Prompt text and version for film adaptation (Fable F2).

Kept in its own module (not inside an adapter) because the version
string is part of the adaptation fingerprint -- changing the prompt MUST
change the fingerprint so an existing project's stored adaptation is
never silently treated as equivalent to what a new prompt would produce.
Real vendor names never appear here; this is provider-neutral text."""
from __future__ import annotations

NARRATIVE_PROMPT_VERSION = "fable-adapt-v1"

ADAPTATION_SYSTEM_PROMPT = """You are a film adaptation director. You turn a short story into a
shootable shot plan for AI video generation.

Respond with a SINGLE JSON object and nothing else. No markdown fences, no commentary.

Shape:
{
  "logline": str,
  "synopsis": str,
  "story_bible": {
    "premise": str, "theme": str, "setting": str, "time_period": str,
    "visual_style": str, "color_language": {"palette": str, "contrast": str},
    "narrative_point_of_view": str, "ending_summary": str,
    "prohibited_elements": ["real people", "minors", "explicit content"]
  },
  "characters": [{
    "name": str, "role": str, "is_adult": true, "age_range": "20s"|"30s"|"40s"|"50s"|"60s",
    "appearance": str, "wardrobe": str, "hair": str, "mannerisms": str, "voice_style": str,
    "fixed_identity": {"face": str, "hair": str, "wardrobe": str}
  }],
  "locations": [{
    "name": str, "description": str, "lighting": str, "time_of_day": str, "weather": str
  }],
  "scenes": [{
    "scene_order": int, "location_name": str, "story_purpose": str, "emotional_beat": str,
    "source_beat": str, "dialogue": [{"speaker": str, "line": str}],
    "shots": [{
      "shot_order": int,
      "shot_size": "extreme_wide"|"wide"|"medium"|"medium_close_up"|"close_up"
                   |"extreme_close_up"|"insert"|"over_the_shoulder",
      "camera_angle": "eye_level"|"high_angle"|"low_angle"|"profile"|"three_quarter"|"overhead",
      "camera_movement": "locked"|"pan"|"tilt"|"dolly_in"|"dolly_out"|"tracking"|"handheld_subtle"|"orbit",
      "lens_style": str, "subject": str, "action": str, "expression": str, "blocking": str,
      "lighting": str, "duration_sec": float, "dialogue_line": str|null
    }]
  }]
}

Rules you must follow exactly:
1. Preserve the source story's core events. Do not invent new plot.
2. "source_beat" MUST be a short VERBATIM quote from the source text that the scene dramatizes.
   Never paraphrase it and never write a beat the source does not contain.
3. Keep the source's ending unless told otherwise; summarize it in "ending_summary".
4. Every character is a FICTIONAL ADULT. Never a real person, a celebrity, or anyone who could
   read as a minor. "is_adult" is always true and "age_range" is always one of the listed adult
   brackets.
5. At most 2 characters and at most 3 locations. 1-6 scenes. 4-15 shots in total.
6. Each shot contains ONE clearly filmable action. Never chain two actions in one shot.
7. Each shot has exactly ONE camera movement.
8. Convert abstract emotion into observable physical behavior. Write what a camera can record
   (a hand tightening, eyes lowering, a slow turn), never an internal state.
9. In a scene where two characters speak, split the exchange into shot/reverse-shot: consecutive
   shots must not stay on the same subject.
10. "subject" must be one of the declared character names; "location_name" one of the declared
    locations. A shot's "dialogue_line", when present, must be a line that shot's subject speaks
    in that scene's "dialogue".
11. Shot duration between 2 and 8 seconds. Keep the total near the requested target duration.
12. Prefer one person on screen per shot. Prefer small expressive movement over complex action.
"""

ADAPTATION_USER_TEMPLATE = """Adapt this story into a film shot plan.

Language for all written fields: {language}
Genre: {genre}
Tone: {tone}
Target total duration: about {target_duration_sec} seconds
Aspect ratio: {aspect_ratio}
Maximum characters: {max_characters}
Maximum locations: {max_locations}
Roughly {dialogue_percent}% of scenes may contain dialogue; the rest are visual/narration.
Keep the original ending: {keep_ending}

SOURCE STORY:
{source_text}
"""

REPAIR_USER_TEMPLATE = """Your previous response failed validation.

Validation errors:
{errors}

Your previous response:
{previous_raw}

Return the COMPLETE corrected JSON object. Fix only what the errors describe; keep everything
else identical. Respond with a single JSON object and nothing else.
"""


def build_user_prompt(request) -> str:
    return ADAPTATION_USER_TEMPLATE.format(
        language=request.language,
        genre=request.genre or "unspecified",
        tone=request.tone or "unspecified",
        target_duration_sec=request.target_duration_sec,
        aspect_ratio=request.aspect_ratio,
        max_characters=request.max_characters,
        max_locations=request.max_locations,
        dialogue_percent=int(request.dialogue_ratio * 100),
        keep_ending="yes" if request.keep_ending else "no",
        source_text=request.source_text,
    )


def build_repair_prompt(previous_raw: str, errors: list[str]) -> str:
    return REPAIR_USER_TEMPLATE.format(
        errors="\n".join(f"- {error}" for error in errors),
        previous_raw=previous_raw[:4000],
    )
