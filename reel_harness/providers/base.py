from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ChannelContext:
    channel_id: str
    niche: str
    language: str
    style_preset: dict


@dataclass
class TopicResult:
    topic: str
    provider_id: str
    model_id: str


@dataclass
class ScriptResult:
    raw_text: str
    provider_id: str
    model_id: str
    prompt_version: str


@dataclass
class TTSResult:
    audio_path: Path
    duration_sec: float
    provider_id: str
    voice_id: str


@dataclass
class MediaCandidate:
    candidate_id: str
    source_url: str
    author: str | None
    license_type: str | None
    license_url: str | None


@dataclass
class LocalAssetResult:
    local_path: Path
    checksum_sha256: str
    mime_type: str
    source_url: str
    author: str | None
    license_type: str | None


@dataclass
class PublishResult:
    platform_post_id: str
    url: str


class LLMProvider(Protocol):
    provider_id: str

    def generate_topic(self, ctx: ChannelContext) -> TopicResult: ...
    def generate_script(self, topic: str, ctx: ChannelContext) -> ScriptResult: ...


class TTSProvider(Protocol):
    provider_id: str

    def synthesize(self, text: str, voice_id: str, lang: str, dest_dir: Path) -> TTSResult: ...


class StockMediaProvider(Protocol):
    provider_id: str

    def search(self, query: str, orientation: str, min_duration: float) -> list[MediaCandidate]: ...
    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult: ...


class Publisher(Protocol):
    provider_id: str

    def publish(self, video_path: Path, metadata: dict) -> PublishResult: ...
