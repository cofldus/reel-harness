from __future__ import annotations

import json

import pytest

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import ChannelContext
from reel_harness.providers.demo_llm import DemoLLMProvider
from reel_harness.providers.demo_stock_media import DEMO_TEST_LICENSE, DemoStockMediaProvider

CTX = ChannelContext(channel_id="c1", niche="cooking", language="ko", style_preset={})


def test_demo_llm_generate_topic_is_deterministic() -> None:
    llm = DemoLLMProvider()
    assert llm.generate_topic(CTX).topic == llm.generate_topic(CTX).topic
    assert llm.provider_id == "demo"


def test_demo_llm_script_subtitle_embeds_the_real_topic() -> None:
    """Unlike FakeLLMProvider's placeholder "Scene N" subtitle, Demo Mode's
    burned-in captions need to actually say something about the topic."""
    llm = DemoLLMProvider(scene_count=3)
    result = llm.generate_script("김치찌개 맛있게 끓이는 법", CTX)
    script = json.loads(result.raw_text)
    assert len(script["scenes"]) == 3
    for i, scene in enumerate(script["scenes"]):
        assert "김치찌개 맛있게 끓이는 법" in scene["subtitle"]
        assert f"{i + 1}/3" in scene["subtitle"]
        assert len(scene["subtitle"]) <= 120  # Scene.subtitle's max_length
        assert "김치찌개 맛있게 끓이는 법" in scene["voiceover"]


def test_demo_llm_timeout_mode_raises_transient_error() -> None:
    llm = DemoLLMProvider(mode="timeout")
    with pytest.raises(TransientProviderError):
        llm.generate_script("topic", CTX)


def test_demo_stock_media_marks_assets_with_demo_license(tmp_path) -> None:
    provider = DemoStockMediaProvider()
    candidates = provider.search("cooking scene 1", orientation="portrait", min_duration=4.0)
    assert len(candidates) == 1
    assert candidates[0].license_type == DEMO_TEST_LICENSE
    asset = provider.download(candidates[0], tmp_path)
    assert asset.license_type == DEMO_TEST_LICENSE
    assert asset.local_path.exists()
    assert provider.provider_id == "demo"


def test_demo_stock_media_empty_mode_returns_no_candidates() -> None:
    provider = DemoStockMediaProvider(mode="empty")
    assert provider.search("cats", orientation="portrait", min_duration=4.0) == []


def test_demo_stock_media_timeout_mode_raises_transient_error() -> None:
    provider = DemoStockMediaProvider(mode="timeout")
    with pytest.raises(TransientProviderError):
        provider.search("cats", orientation="portrait", min_duration=4.0)


def test_demo_stock_media_download_is_deterministic_and_checksummed(tmp_path) -> None:
    provider = DemoStockMediaProvider()
    candidate = provider.search("cats", orientation="portrait", min_duration=4.0)[0]
    asset_1 = provider.download(candidate, tmp_path / "a")
    asset_2 = provider.download(candidate, tmp_path / "b")
    assert asset_1.checksum_sha256 == asset_2.checksum_sha256


def test_demo_stock_media_colors_are_not_all_identical_across_scenes(tmp_path) -> None:
    """The whole point of the fixed palette (vs. FakeStockMediaProvider's raw
    hash-derived RGB) is that scenes generally look distinct -- confirm at
    least two of five differently-queried scenes land on different palette
    colors, which would not be guaranteed if download() ignored the
    candidate entirely."""
    provider = DemoStockMediaProvider()
    colors = set()
    for i in range(5):
        candidate = provider.search(f"cooking scene {i + 1}", orientation="portrait", min_duration=4.0)[0]
        asset = provider.download(candidate, tmp_path / f"scene_{i}")
        colors.add(asset.local_path.read_bytes()[-100:])  # PNG pixel data differs by color
    assert len(colors) > 1
