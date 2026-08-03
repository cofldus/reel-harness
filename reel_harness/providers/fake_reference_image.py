"""Deterministic stand-in for a real reference-image generator. Zero
network, unit/integration tests only.

Produces genuinely valid PNGs (reusing the same hand-rolled encoder the
fake stock-media provider uses) whose colour is derived from the request
-- so two calls with the same request yield byte-identical images, and
different views of the same character yield visibly different ones. It
never imitates real image quality and never claims to; every result is
stamped FAKE_TEST_LICENSE so it can never pass a real publish gate."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from reel_harness.core.errors import TransientProviderError
from reel_harness.providers.base import (
    CinematicCostEstimate,
    ImageCapabilities,
    ReferenceImageRequest,
    ReferenceImageResult,
)
from reel_harness.providers.fake_stock_media import FAKE_TEST_LICENSE, make_minimal_png

FakeReferenceMode = Literal["ok", "refused", "timeout"]

# Mirrors the shape of a real character-reference-capable model without
# copying any vendor's exact numbers.
_FAKE_IMAGE_CAPABILITIES = ImageCapabilities(
    text_to_image=True,
    character_reference=True,
    max_character_references=4,
    supported_resolutions=frozenset({"512", "1k"}),
    supported_aspect_ratios=frozenset({"9:16", "16:9", "1:1"}),
    watermarked=False,  # a fake image carries no provenance watermark
)

_RESOLUTION_PIXELS = {"512": (36, 64), "1k": (72, 128)}


class FakeReferenceImageProvider:
    provider_id = "fake"
    model_id = "fake-reference-v1"
    capabilities = _FAKE_IMAGE_CAPABILITIES

    def __init__(self, mode: FakeReferenceMode = "ok") -> None:
        self.mode = mode
        self.generated = 0

    def validate_request(self, request: ReferenceImageRequest) -> None:
        if request.resolution not in self.capabilities.supported_resolutions:
            raise ValueError(
                f"unsupported resolution {request.resolution!r} -- fake provider supports "
                f"{sorted(self.capabilities.supported_resolutions)}"
            )
        if request.aspect_ratio not in self.capabilities.supported_aspect_ratios:
            raise ValueError(f"unsupported aspect ratio {request.aspect_ratio!r}")
        if len(request.character_reference_paths) > self.capabilities.max_character_references:
            raise ValueError(
                f"{len(request.character_reference_paths)} character references exceeds the "
                f"maximum of {self.capabilities.max_character_references}"
            )
        for reference in request.character_reference_paths:
            if not Path(reference).exists():
                raise ValueError(f"character reference does not exist: {reference}")

    def estimate_cost(self, request: ReferenceImageRequest) -> CinematicCostEstimate:
        # Obviously-fake unit so budget tests have a real number to sum
        # without imitating any vendor's price list.
        return CinematicCostEstimate(
            known=True, amount=0.01, currency="FAKE",
            detail="fake provider: 0.01 FAKE per reference image",
        )

    def generate_reference(
        self, request: ReferenceImageRequest, dest_dir: Path,
    ) -> ReferenceImageResult:
        if self.mode == "timeout":
            raise TransientProviderError("fake reference image generation timed out")
        if self.mode == "refused":
            from reel_harness.core.errors import ContentPolicyRefusedError

            raise ContentPolicyRefusedError("fake provider forced a content-policy refusal")
        self.validate_request(request)

        digest = hashlib.sha256(
            f"{request.prompt}:{request.correlation_id}:{request.seed}".encode()
        ).hexdigest()
        width, height = _RESOLUTION_PIXELS[request.resolution]
        if request.aspect_ratio == "16:9":
            width, height = height, width
        elif request.aspect_ratio == "1:1":
            height = width
        colour = (int(digest[0:2], 16), int(digest[2:4], 16), int(digest[4:6], 16))
        data = make_minimal_png(width, height, colour)

        dest_dir.mkdir(parents=True, exist_ok=True)
        image_path = dest_dir / f"reference-{digest[:16]}.png"
        image_path.write_bytes(data)
        self.generated += 1

        return ReferenceImageResult(
            image_path=image_path,
            provider_id=self.provider_id,
            model_id=self.model_id,
            license=FAKE_TEST_LICENSE,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            watermark=None,
            seed=request.seed,
            request_id=f"fake-ref-req-{digest[:12]}",
            cost_amount=0.01,
            cost_currency="FAKE",
        )
