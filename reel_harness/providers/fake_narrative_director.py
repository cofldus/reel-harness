"""Deterministic stand-in for a real Narrative Director. Zero network,
unit/integration tests only.

Unlike a stub, this produces a COMPLETE adaptation document that passes
the real parser, schema, semantic rules, and fidelity heuristic -- the
whole pipeline runs for real against it, nothing is bypassed. Scene
`source_beat` values are genuine substrings of the caller's own source
text (split on sentence boundaries), which is exactly what makes the
fidelity check meaningful rather than vacuous.

`mode` exercises the repair loop and safety paths without a network:
  ok             -- valid on the first attempt
  invalid_once   -- schema-invalid first, valid on the first repair
  always_invalid -- never valid (repair-exhaustion path)
  minor_character-- adult-only rejection path
  timeout        -- transient provider failure
"""
from __future__ import annotations

import json
import re
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import AdaptationRequest, AdaptationResult
from reel_harness.providers.narrative_prompts import NARRATIVE_PROMPT_VERSION

FakeDirectorMode = Literal["ok", "invalid_once", "always_invalid", "minor_character", "timeout"]

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?。？！])\s+|\n+")


def _source_beats(source_text: str, count: int) -> list[str]:
    """Real sentences from the source, so the fidelity check has genuine
    quotes to match. Falls back to overlapping windows when the source
    has fewer sentences than scenes."""
    sentences = [s.strip() for s in _SENTENCE_SPLIT.split(source_text.strip()) if s.strip()]
    if not sentences:
        sentences = [source_text.strip()[:160] or "story"]
    beats: list[str] = []
    for index in range(count):
        beats.append(sentences[index % len(sentences)][:160])
    return beats


class FakeNarrativeDirector:
    provider_id = "fake"
    model_id = "fake-narrative-v1"

    def __init__(self, mode: FakeDirectorMode = "ok") -> None:
        self.mode = mode
        self.adapt_calls = 0
        self.repair_calls = 0
        self.last_errors: list[str] = []

    # -- Protocol ---------------------------------------------------------

    def adapt_story(self, request: AdaptationRequest) -> AdaptationResult:
        self.adapt_calls += 1
        if self.mode == "timeout":
            raise TransientProviderError("fake narrative director timed out")
        if self.mode in ("invalid_once", "always_invalid"):
            return self._result(self._invalid_document())
        return self._result(self._document(request))

    def repair_adaptation(
        self, request: AdaptationRequest, previous_raw: str, errors: list[str],
    ) -> AdaptationResult:
        self.repair_calls += 1
        self.last_errors = list(errors)
        if self.mode == "always_invalid":
            return self._result(self._invalid_document())
        # "invalid_once" (and any other mode reaching repair) now produces
        # a document that actually satisfies the validators.
        return self._result(self._document(request))

    # -- Document construction -------------------------------------------

    def _result(self, document: dict | str) -> AdaptationResult:
        raw = document if isinstance(document, str) else json.dumps(document, ensure_ascii=False)
        return AdaptationResult(
            raw_text=raw, provider_id=self.provider_id, model_id=self.model_id,
            prompt_version=NARRATIVE_PROMPT_VERSION,
        )

    def _invalid_document(self) -> str:
        return "```json\n{\"logline\": \"missing everything else\"}\n```"

    def _document(self, request: AdaptationRequest) -> dict:
        scene_count = 2
        shots_per_scene = 2
        beats = _source_beats(request.source_text, scene_count)
        age_range = "teens" if self.mode == "minor_character" else "30s"
        character_name = "지우"
        location_name = "호텔 방"

        scenes = []
        for scene_index in range(scene_count):
            shots = []
            for shot_index in range(shots_per_scene):
                shots.append({
                    "shot_order": shot_index + 1,
                    "shot_size": "medium" if shot_index == 0 else "medium_close_up",
                    "camera_angle": "eye_level",
                    "camera_movement": "locked" if shot_index == 0 else "dolly_in",
                    "lens_style": "50mm",
                    "subject": character_name,
                    "action": (
                        "창밖을 바라본다" if scene_index == 0 else "천천히 문 쪽으로 돌아선다"
                    ),
                    "expression": "절제된 불안",
                    "blocking": "창가에 선 채",
                    "lighting": "soft practical",
                    "duration_sec": 2.0,
                    "dialogue_line": None,
                })
            scenes.append({
                "scene_order": scene_index + 1,
                "location_name": location_name,
                "story_purpose": "도입" if scene_index == 0 else "전환",
                "emotional_beat": "정적인 불안",
                "source_beat": beats[scene_index],
                "dialogue": [],
                "shots": shots,
            })

        return {
            "logline": "한 인물이 비 오는 밤의 방에서 결심에 이른다.",
            "synopsis": request.source_text.strip()[:400] or "짧은 이야기",
            "story_bible": {
                "premise": request.source_text.strip()[:200] or "짧은 이야기",
                "theme": request.tone or "quiet tension",
                "setting": "비 오는 밤의 실내",
                "time_period": "현대",
                "visual_style": "soft practical lighting, muted palette",
                "color_language": {"palette": "cool neutrals", "contrast": "low"},
                "narrative_point_of_view": "third person",
                "ending_summary": "인물은 결심한 듯 움직인다.",
                "prohibited_elements": ["real people", "minors", "explicit content"],
            },
            "characters": [{
                "name": character_name, "role": "protagonist", "is_adult": True,
                "age_range": age_range,
                "appearance": "oval face, calm eyes", "wardrobe": "grey coat",
                "hair": "black short hair", "mannerisms": "slow deliberate movements",
                "voice_style": "low, restrained",
                "fixed_identity": {
                    "face": "oval face, calm eyes", "hair": "black short hair",
                    "wardrobe": "grey coat",
                },
            }],
            "locations": [{
                "name": location_name, "description": "a room at night, rain outside",
                "lighting": "soft practicals", "time_of_day": "night", "weather": "rain",
            }],
            "scenes": scenes,
        }
