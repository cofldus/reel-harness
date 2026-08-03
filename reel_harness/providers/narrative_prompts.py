"""Prompt text and version for film adaptation (Fable F2).

Kept in its own module (not inside an adapter) because the version
string is part of the adaptation fingerprint -- changing the prompt MUST
change the fingerprint so an existing project's stored adaptation is
never silently treated as equivalent to what a new prompt would produce.
Real vendor names never appear here; this is provider-neutral text."""
from __future__ import annotations

NARRATIVE_PROMPT_VERSION = "fable-adapt-v2"

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
Roughly {dialogue_percent}% of scenes should carry spoken dialogue. Prefer the source's own
lines over invented ones, and prefer speaking them over narrating that they were spoken.
Keep the original ending: {keep_ending}

SOURCE STORY:
{source_text}
"""

# Measured against nine real GPT-4o runs: faithful, but flat. Angle
# collapsed to one value in most runs, 2 of 9 never moved the camera, the
# one line of quoted speech in a source was dropped, and shot counts
# ranged from half to one-and-a-half times what was asked. Asking up
# front is cheaper than repairing after.
CRAFT_RULES = """
SHOOTING REQUIREMENTS -- these are checked, and a plan that misses them is sent back:
- Produce approximately {shot_count} shots in total (one shot per {shot_seconds} seconds of
  the requested runtime). Within one of that number is fine; half of it is not.
- Every line of speech that appears in quotation marks in the source MUST survive as the
  dialogue_line of the shot where it is spoken. Do not summarise it into an action.
- Do not shoot the whole sequence from one camera_angle. Vary the angle so the scene reads
  as coverage rather than a single setup.
- Do not leave every shot locked off. At least one shot needs a motivated camera move, and
  the move must come from the story beat rather than from decoration.
- A scene is a place and a continuous moment, not a single beat. If you write more than one
  scene, each one must hold at least two shots. Do not split one location into a scene per
  shot -- cutting between scenes tells the audience that time or place jumped.
- The shots must form an ARC across the whole plan, not four views of one moment. The first
  shot establishes the situation, the middle shots turn it, and the LAST shot must land the
  source story's ending -- the thing that makes it a story rather than a scene. A plan whose
  shots could be reordered without loss has not adapted the story, it has photographed it.
- Cover the whole source, not one paragraph of it. Every scene needs a DIFFERENT
  "source_beat", drawn from a different part of the text and in the order the text tells it.
  Two scenes quoting the same sentence means the plan is standing still.
- Give the audience the words. When the source contains spoken lines, put them in
  "dialogue_line" rather than describing that someone spoke -- silent footage of a
  conversation is the most common way an adaptation loses its story.
"""


# A worked example, not just rules.
#
# Measured: the rules alone left the model repairing on 2 of 3 stories,
# once using its full budget of two retries. A repair is another billed
# call and another chance to fail outright, so the cheapest fix is to
# show one finished answer rather than describe it three more times.
#
# Deliberately SHORT and deliberately not one of the sample stories, so
# it teaches shape without supplying content to copy. It shows the four
# things that were actually going wrong: one scene holding several shots
# rather than a scene per beat, angle varying across the sequence, one
# motivated camera move, and quoted speech landing on the shot where it
# is spoken.
WORKED_EXAMPLE = """
EXAMPLE -- study the SHAPE, never reuse its content.

Source: 늦은 오후, 민재는 편의점 앞 의자에 앉아 있었다. 그는 캔을 따다 말고 손을 멈췄다.
길 건너에서 누군가 그의 이름을 불렀다. "민재야." 그는 캔을 내려놓고 천천히 일어섰다.

A good plan for 4 shots:
{
  "scenes": [{
    "scene_order": 1,
    "location_name": "편의점 앞",
    "story_purpose": "부름을 듣고 일어서기까지",
    "source_beat": "그는 캔을 따다 말고 손을 멈췄다.",
    "shots": [
      {"shot_order": 1, "shot_size": "wide", "camera_angle": "eye_level",
       "camera_movement": "locked", "subject": "민재",
       "action": "편의점 앞 의자에 앉아 캔을 딴다", "dialogue_line": null},
      {"shot_order": 2, "shot_size": "close_up", "camera_angle": "high_angle",
       "camera_movement": "locked", "subject": "민재",
       "action": "캔을 따던 손이 멈춘다", "dialogue_line": null},
      {"shot_order": 3, "shot_size": "medium", "camera_angle": "three_quarter",
       "camera_movement": "pan", "subject": "민재",
       "action": "소리가 난 쪽으로 고개를 돌린다", "dialogue_line": "민재야."},
      {"shot_order": 4, "shot_size": "medium_close_up", "camera_angle": "low_angle",
       "camera_movement": "dolly_in", "subject": "민재",
       "action": "캔을 내려놓고 천천히 일어선다", "dialogue_line": null}
    ]
  }]
}

Why this passes: four shots for the four requested, ONE scene holding all of them
because the place and the moment never change, four different angles, one motivated
move on the turn, and the spoken line placed on the shot where it is heard.
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
    from reel_harness.pipeline.adaptation_parser import SHOT_SECONDS

    shot_count = max(1, round(request.target_duration_sec / SHOT_SECONDS))
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
    ) + CRAFT_RULES.format(shot_count=shot_count, shot_seconds=SHOT_SECONDS) + WORKED_EXAMPLE


def build_repair_prompt(previous_raw: str, errors: list[str]) -> str:
    return REPAIR_USER_TEMPLATE.format(
        errors="\n".join(f"- {error}" for error in errors),
        previous_raw=previous_raw[:4000],
    )


# --- Source refinement (Fable F6) ------------------------------------------
#
# Versioned separately from the adaptation prompt: the two evolve for
# different reasons, and a refinement wording change must not invalidate
# adaptation provenance.
REFINEMENT_PROMPT_VERSION = "fable-refine-v1"

REFINEMENT_SYSTEM_PROMPT = """\
You rewrite a user's short story so an AI film pipeline can shoot it. You are a
script doctor, not a co-author.

ABSOLUTE RULES -- breaking any of these makes the output useless:
- Do NOT change the plot. Same events, same order, same outcome, same ending.
- Do NOT add or remove characters, and do NOT rename anyone.
- Do NOT add a moral, a twist, or an interpretation the original did not state.
- Keep the author's own sentences wherever they already work. You are adding
  and converting, not replacing prose you were not asked to touch.
- Keep the original language. Do not translate.

WHAT TO FIX, and only this:
1. Characters must be visible. Give each person an approximate age and
   concrete, filmable appearance (hair, clothing, build). A reference image is
   generated from this description, so "tired" is useless and "in a soaked grey
   trench coat, hair tied back" is not.
2. Every scene needs a place and a light source. State the time of day and
   where the light comes from.
3. Speech must be quoted directly. Convert reported speech ("she said she
   would leave") into an actual line of dialogue in quotation marks.
4. Actions must be filmable. Replace inner states no camera can record
   ("he regretted it") with the physical behaviour that shows them.
5. There must be at least one visible change: a decision, a discovery, a
   refusal. If the original already has one, leave it alone.

Return a single JSON object and nothing else:
{"refined_text": "<the rewritten story>",
 "notes": ["<short note, in the story's language, per change you made>"]}
"""

REFINEMENT_USER_TEMPLATE = """\
Language: {language}
Genre hint: {genre}
Tone hint: {tone}

Rewrite the story below under the rules above.

--- STORY ---
{source_text}
--- END STORY ---
"""


def build_refinement_prompt(request) -> str:
    return REFINEMENT_USER_TEMPLATE.format(
        language=request.language,
        genre=request.genre or "unspecified",
        tone=request.tone or "unspecified",
        source_text=request.source_text,
    )
