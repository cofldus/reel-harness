from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import LocalAssetResult, MediaCandidate
from reel_harness.providers.fake_stock_media import make_minimal_png

DEMO_TEST_LICENSE = "DEMO_TEST_LICENSE"

FakeMode = Literal["ok", "empty", "timeout"]

# A fixed, high-contrast palette -- unlike raw hash-derived RGB bytes (what
# FakeStockMediaProvider uses), which frequently land on visually-similar
# dark/desaturated colors, this makes scenes generally look distinct from
# each other. Demo Mode exists specifically so a rendered job is watchable,
# so "different color per scene" needs to actually read as different by eye.
# 16 entries (rather than a smaller set) to keep same-video collisions
# infrequent for the typical 3-10 scene range (script_schema.MIN/MAX_SCENES)
# even though the selection below is hash-based, not collision-free by
# construction.
_PALETTE: tuple[tuple[int, int, int], ...] = (
    (230, 57, 70), (69, 123, 157), (42, 157, 143), (233, 196, 106),
    (155, 93, 229), (244, 162, 97), (38, 70, 83), (231, 111, 81),
    (6, 214, 160), (17, 138, 178), (239, 71, 111), (255, 209, 102),
    (131, 56, 236), (58, 134, 255), (251, 86, 7), (58, 90, 64),
)


class DemoStockMediaProvider:
    """Stand-in stock-media provider for Demo Mode: like FakeStockMediaProvider,
    every "download" is a synthetic local image built from scratch (no
    external API call, no credential) -- but scenes are colored from the
    fixed high-contrast palette above instead of raw hash bytes. Every asset
    is stamped DEMO_TEST_LICENSE, which -- like FAKE_TEST_LICENSE -- never
    passes the real publish-eligibility gate (see manifest.schema.
    NON_PUBLISHABLE_LICENSES)."""

    provider_id = "demo"

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
            raise TransientProviderError("demo stock media search timed out")
        if self.mode == "empty":
            return []
        candidate_id = hashlib.sha256(f"{query}:{page}".encode()).hexdigest()[:12]
        if candidate_id in exclude_provider_asset_ids:
            return []
        return [
            MediaCandidate(
                candidate_id=candidate_id,
                source_url=f"demo://stock-media/{candidate_id}",
                author="Demo Placeholder Artist",
                license_type=DEMO_TEST_LICENSE,
                license_url="demo://license/test-only",
                provider_id=self.provider_id,
                download_url=f"demo://stock-media/{candidate_id}/download",
                creator_url="demo://stock-media/author/demo-placeholder-artist",
                commercial_use_allowed=True,
                modification_allowed=True,
                attribution_text="Demo Placeholder Artist (demo://license/test-only)",
                width=1080,
                height=1920,
                duration_sec=max(min_duration, 4.0),
                fps=30.0,
                file_size_bytes=None,
                content_type="image/png",
                provider_rank=0,
                provider_request_id=f"demo-req-{candidate_id}",
            )
        ]

    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult:
        dest_dir.mkdir(parents=True, exist_ok=True)
        local_path = dest_dir / f"{candidate.candidate_id}.png"
        palette_index = int(hashlib.sha256(candidate.candidate_id.encode()).hexdigest(), 16) % len(_PALETTE)
        data = make_minimal_png(64, 64, _PALETTE[palette_index])
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
