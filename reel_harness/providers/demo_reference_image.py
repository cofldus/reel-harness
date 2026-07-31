"""Demo-tier reference-image provider: a watchable character sheet with
zero network calls and no credential.

Deliberately SYNTHETIC rather than a bundled photo set. A reference sheet
depicts a person, and the only honest way to ship sample "people" with a
local-first tool is not to ship people at all -- a packaged photo of a
real face would be a real person's likeness travelling with the software,
and a packaged AI-generated face would be output this tier explicitly
does not produce. So the demo tier draws flat colour panels instead: a
per-character hue that stays constant across the four views (so a sheet
reads as ONE character) with per-view brightness steps (so the views read
as different). That is enough to exercise and eyeball the casting
workflow, and it can never be mistaken for model output.

Every image is stamped DEMO_TEST_LICENSE, which -- like
FAKE_TEST_LICENSE -- never passes the real publish-eligibility gate (see
manifest.schema.NON_PUBLISHABLE_LICENSES).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from reel_harness.providers.base import (
    CinematicCostEstimate,
    ImageCapabilities,
    ReferenceImageRequest,
    ReferenceImageResult,
)
from reel_harness.providers.demo_stock_media import DEMO_PALETTE, DEMO_TEST_LICENSE
from reel_harness.providers.fake_stock_media import make_minimal_png

# Same shape as the real adapter's capabilities so the demo tier exercises
# the same capability checks, but honestly reporting what it does: it
# accepts character references (they change the output's brightness step,
# nothing more) and watermarks nothing, because it generates nothing to
# watermark.
_DEMO_IMAGE_CAPABILITIES = ImageCapabilities(
    text_to_image=True,
    character_reference=True,
    max_character_references=4,
    supported_resolutions=frozenset({"512", "1k"}),
    supported_aspect_ratios=frozenset({"9:16", "16:9", "1:1"}),
    watermarked=False,
)

_RESOLUTION_PIXELS = {"512": (72, 128), "1k": (108, 192)}

# How much lighter each successive chained view is drawn. Index 0 (the
# face, generated with no references) is the base hue; each later view has
# one more reference image in the request, which is exactly the chain
# depth, so the same character reads as one hue in four shades.
_VIEW_BRIGHTNESS_STEP = 28


class DemoReferenceImageProvider:
    provider_id = "demo"
    model_id = "demo-reference-v1"
    capabilities = _DEMO_IMAGE_CAPABILITIES

    def __init__(self) -> None:
        self.generated = 0

    def validate_request(self, request: ReferenceImageRequest) -> None:
        if request.resolution not in self.capabilities.supported_resolutions:
            raise ValueError(
                f"unsupported resolution {request.resolution!r} -- demo provider supports "
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
        """Free, and says so with a real number rather than `known=False`:
        zero IS the demo tier's price, and reporting it as unknown would
        make a budgeted project refuse to run offline."""
        return CinematicCostEstimate(
            known=True, amount=0.0, currency="USD",
            detail="demo provider: generated locally, no API call, no charge",
        )

    def generate_reference(
        self, request: ReferenceImageRequest, dest_dir: Path,
    ) -> ReferenceImageResult:
        self.validate_request(request)

        # The hue keys off the character's identity, not this view's
        # framing: the prompt's first slot is the actor, so hashing the
        # whole prompt would give each view a different colour and lose
        # the "one character" reading entirely.
        identity = request.correlation_id.rsplit(":", 2)[0] or request.prompt
        digest = hashlib.sha256(identity.encode()).hexdigest()
        red, green, blue = DEMO_PALETTE[int(digest[:8], 16) % len(DEMO_PALETTE)]
        lift = _VIEW_BRIGHTNESS_STEP * len(request.character_reference_paths)
        colour = (min(255, red + lift), min(255, green + lift), min(255, blue + lift))

        width, height = _RESOLUTION_PIXELS[request.resolution]
        if request.aspect_ratio == "16:9":
            width, height = height, width
        elif request.aspect_ratio == "1:1":
            height = width
        data = make_minimal_png(width, height, colour)

        dest_dir.mkdir(parents=True, exist_ok=True)
        view_digest = hashlib.sha256(request.correlation_id.encode()).hexdigest()[:16]
        image_path = dest_dir / f"demo-reference-{view_digest}.png"
        image_path.write_bytes(data)
        self.generated += 1

        return ReferenceImageResult(
            image_path=image_path,
            provider_id=self.provider_id,
            model_id=self.model_id,
            license=DEMO_TEST_LICENSE,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            # Nothing was generated by a model, so there is nothing for a
            # provenance watermark to attest to.
            watermark=None,
            seed=request.seed,
            request_id=f"demo-ref-{digest[:12]}",
            cost_amount=0.0,
            cost_currency="USD",
        )
