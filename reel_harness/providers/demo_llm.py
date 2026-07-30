from __future__ import annotations

import json
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import ChannelContext, ScriptResult, TopicResult

PROMPT_VERSION = "demo-script-v1"

FakeMode = Literal["ok", "timeout"]


class DemoLLMProvider:
    """Deterministic, no-network script generator for Demo Mode -- same
    non-LLM template approach as FakeLLMProvider (no real model call, no API
    key), but writes subtitle text that embeds the real topic so Demo Mode's
    burned-in captions (see Settings.render_burn_subtitles) are actually
    meaningful to read, rather than the placeholder "Scene N" fake_llm uses
    for pipeline-mechanics testing."""

    provider_id = "demo"
    model_id = "demo-deterministic-v1"

    def __init__(self, mode: FakeMode = "ok", scene_count: int = 3) -> None:
        self.mode = mode
        self.scene_count = scene_count

    def generate_topic(self, ctx: ChannelContext) -> TopicResult:
        topic = f"{ctx.niche} tip for {ctx.language} viewers"
        return TopicResult(topic=topic, provider_id=self.provider_id, model_id=self.model_id)

    def generate_script(self, topic: str, ctx: ChannelContext) -> ScriptResult:
        if self.mode == "timeout":
            raise TransientProviderError("demo LLM timed out")

        scenes = [
            {
                "voiceover": f"{topic} - part {i + 1} of {self.scene_count}.",
                "subtitle": f"{i + 1}/{self.scene_count}: {topic}"[:120],
                "visual_query": f"{ctx.niche} scene {i + 1}",
                "duration_hint_sec": 4.0,
            }
            for i in range(self.scene_count)
        ]
        raw_text = json.dumps({"title": topic, "scenes": scenes})
        return ScriptResult(
            raw_text=raw_text,
            provider_id=self.provider_id,
            model_id=self.model_id,
            prompt_version=PROMPT_VERSION,
        )
