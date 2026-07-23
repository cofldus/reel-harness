from __future__ import annotations

import json
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import ChannelContext, ScriptResult, TopicResult

PROMPT_VERSION = "fake-script-v1"

FakeMode = Literal["ok", "malformed", "too_few_scenes", "timeout"]


class FakeLLMProvider:
    """Deterministic stand-in for a real LLM provider. Used by Phase 0/1 only.

    `mode` lets tests exercise the failure paths (malformed JSON, schema violations,
    transient errors) without touching a real network or a real model.
    """

    provider_id = "fake"
    model_id = "fake-deterministic-v1"

    def __init__(self, mode: FakeMode = "ok", scene_count: int = 3) -> None:
        self.mode = mode
        self.scene_count = scene_count

    def generate_topic(self, ctx: ChannelContext) -> TopicResult:
        topic = f"{ctx.niche} tip for {ctx.language} viewers"
        return TopicResult(topic=topic, provider_id=self.provider_id, model_id=self.model_id)

    def generate_script(self, topic: str, ctx: ChannelContext) -> ScriptResult:
        if self.mode == "timeout":
            raise TransientProviderError("fake LLM timed out")
        if self.mode == "malformed":
            return ScriptResult(
                raw_text="{not valid json",
                provider_id=self.provider_id,
                model_id=self.model_id,
                prompt_version=PROMPT_VERSION,
            )

        scene_count = 1 if self.mode == "too_few_scenes" else self.scene_count
        scenes = [
            {
                "voiceover": f"{topic} - scene {i + 1} voiceover.",
                "subtitle": f"Scene {i + 1}",
                "visual_query": f"{ctx.niche} scene {i + 1}",
                "duration_hint_sec": 4.0,
            }
            for i in range(scene_count)
        ]
        raw_text = json.dumps({"title": topic, "scenes": scenes})
        return ScriptResult(
            raw_text=raw_text,
            provider_id=self.provider_id,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
        )

