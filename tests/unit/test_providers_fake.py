from __future__ import annotations

import json
import wave

import pytest

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import ChannelContext
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FAKE_TEST_LICENSE, FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider

CTX = ChannelContext(channel_id="c1", niche="cooking", language="en", style_preset={})


def test_fake_llm_generate_topic_is_deterministic() -> None:
    llm = FakeLLMProvider()
    assert llm.generate_topic(CTX).topic == llm.generate_topic(CTX).topic


def test_fake_llm_malformed_mode_returns_unparseable_text() -> None:
    llm = FakeLLMProvider(mode="malformed")
    result = llm.generate_script("topic", CTX)
    with pytest.raises(json.JSONDecodeError):
        json.loads(result.raw_text)


def test_fake_llm_timeout_mode_raises_transient_error() -> None:
    llm = FakeLLMProvider(mode="timeout")
    with pytest.raises(TransientProviderError):
        llm.generate_script("topic", CTX)


def test_fake_tts_duration_scales_with_text_length(tmp_path) -> None:
    tts = FakeTTSProvider()
    short = tts.synthesize("hi", voice_id="v1", lang="en", dest_dir=tmp_path / "short")
    long_text = "a much longer piece of voiceover text than the other one"
    long = tts.synthesize(long_text, voice_id="v1", lang="en", dest_dir=tmp_path / "long")
    assert long.duration_sec > short.duration_sec
    with wave.open(str(short.audio_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1


def test_fake_tts_timeout_mode_raises_transient_error(tmp_path) -> None:
    tts = FakeTTSProvider(mode="timeout")
    with pytest.raises(TransientProviderError):
        tts.synthesize("hi", voice_id="v1", lang="en", dest_dir=tmp_path)


def test_fake_stock_media_marks_assets_with_fake_license(tmp_path) -> None:
    provider = FakeStockMediaProvider()
    candidates = provider.search("cats", orientation="portrait", min_duration=4.0)
    assert len(candidates) == 1
    assert candidates[0].license_type == FAKE_TEST_LICENSE
    asset = provider.download(candidates[0], tmp_path)
    assert asset.license_type == FAKE_TEST_LICENSE
    assert asset.local_path.exists()


def test_fake_stock_media_empty_mode_returns_no_candidates() -> None:
    provider = FakeStockMediaProvider(mode="empty")
    assert provider.search("cats", orientation="portrait", min_duration=4.0) == []


def test_fake_stock_media_timeout_mode_raises_transient_error() -> None:
    provider = FakeStockMediaProvider(mode="timeout")
    with pytest.raises(TransientProviderError):
        provider.search("cats", orientation="portrait", min_duration=4.0)


def test_fake_stock_media_download_is_deterministic_and_checksummed(tmp_path) -> None:
    provider = FakeStockMediaProvider()
    candidate = provider.search("cats", orientation="portrait", min_duration=4.0)[0]
    asset_1 = provider.download(candidate, tmp_path / "a")
    asset_2 = provider.download(candidate, tmp_path / "b")
    assert asset_1.checksum_sha256 == asset_2.checksum_sha256
