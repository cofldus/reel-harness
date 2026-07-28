from __future__ import annotations

import hashlib
import struct
import zlib
from pathlib import Path
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import LocalAssetResult, MediaCandidate

FAKE_TEST_LICENSE = "FAKE_TEST_LICENSE"

FakeMode = Literal["ok", "empty", "timeout", "corrupted"]


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def make_minimal_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """Builds a genuinely valid PNG from scratch (stdlib only, no Pillow needed).

    Used as a stand-in "downloaded stock asset" for tests — small, deterministic,
    and real enough to be checksummed and written to disk like a real download.
    """
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = bytes([0]) + bytes(rgb) * width
    idat = zlib.compress(row * height)
    return signature + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


class FakeStockMediaProvider:
    """Stand-in for a real stock-media provider (Pexels-like). Phase 0/1 only.

    Every asset it returns is stamped with FAKE_TEST_LICENSE so it can never pass
    a real publish-gate license check by accident.
    """

    provider_id = "fake"

    def __init__(self, mode: FakeMode = "ok") -> None:
        self.mode = mode

    def search(
        self, query: str, orientation: str, min_duration: float,
        *,
        max_duration: float | None = None,
        min_width: int | None = None,
        min_height: int | None = None,
        per_page: int = 15,
        page: int = 1,
        safe_search: bool = True,
        exclude_provider_asset_ids: frozenset[str] = frozenset(),
    ) -> list[MediaCandidate]:
        if self.mode == "timeout":
            raise TransientProviderError("fake stock media search timed out")
        if self.mode == "empty":
            return []
        candidate_id = hashlib.sha256(f"{query}:{page}".encode()).hexdigest()[:12]
        if candidate_id in exclude_provider_asset_ids:
            return []
        return [
            MediaCandidate(
                candidate_id=candidate_id,
                source_url=f"fake://stock-media/{candidate_id}",
                author="Fake Test Author",
                license_type=FAKE_TEST_LICENSE,
                license_url="fake://license/test-only",
                provider_id=self.provider_id,
                download_url=f"fake://stock-media/{candidate_id}/download",
                creator_url="fake://stock-media/author/fake-test-author",
                commercial_use_allowed=True,
                modification_allowed=True,
                attribution_text="Fake Test Author (fake://license/test-only)",
                width=1080,
                height=1920,
                duration_sec=max(min_duration, 4.0),
                fps=30.0,
                file_size_bytes=None,
                content_type="image/png",
                provider_rank=0,
                provider_request_id=f"fake-req-{candidate_id}",
            )
        ]

    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = dest_dir / f"{candidate.candidate_id}.png"
        if self.mode == "corrupted":
            data = b"\x00\x01not-a-real-png"
        else:
            color = (
                int(candidate.candidate_id[0:2], 16),
                int(candidate.candidate_id[2:4], 16),
                int(candidate.candidate_id[4:6], 16),
            )
            data = make_minimal_png(64, 64, color)
        local_path.write_bytes(data)
        return LocalAssetResult(
            local_path=local_path,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            mime_type="image/png",
            source_url=candidate.source_url,
            author=candidate.author,
            license_type=candidate.license_type,
            provider_id=self.provider_id,
            provider_asset_id=candidate.candidate_id,
            source_page_url=candidate.source_url,
            creator_url=candidate.creator_url,
            commercial_use_allowed=candidate.commercial_use_allowed,
            modification_allowed=candidate.modification_allowed,
            attribution_text=candidate.attribution_text,
            width=candidate.width,
            height=candidate.height,
            duration_sec=candidate.duration_sec,
            fps=candidate.fps,
            request_id=candidate.provider_request_id,
        )
