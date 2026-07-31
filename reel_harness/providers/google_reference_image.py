"""Real reference-image generation via Google's `google-genai` SDK.

Chosen because it is the only surveyed option that shares ONE SDK and ONE
credential with Veo video generation (F5), has typed character-reference
support, and is GA -- see docs/STATUS.md's provider research. Do not add
an Imagen adapter: Imagen shuts down 2026-08-17.

Import discipline: `google.genai` is an OPTIONAL dependency (the `google`
extra). It is imported inside methods, never at module import time, so a
machine without the extra can still import the registry, run the whole
fake/demo pipeline, and get a clear install instruction instead of an
ImportError traceback -- the same shape the other optional-dependency
adapters use.

**Watermark**: every image Google generates carries a SynthID watermark
and there is no removal option. That is recorded on every result rather
than ignored, because whether Veo accepts SynthID-watermarked images as
character-reference input is an OPEN question no documentation answers
(see docs/STATUS.md). The `fable-reference-smoke` command exists to find
out before F5 builds on it.

**Verification status**: the request/response mapping below was written
against the installed `google-genai` SDK's real type definitions
(`ImageConfig`, `FinishReason`, `BlockedReason`, `errors.ClientError`) --
introspected, not guessed. It has NEVER been run against the live API on
this machine; there are no credentials here. The tests are contract tests
against an injected fake client and prove protocol conformance only,
never live success.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from reel_harness.core.errors import (
    ContentPolicyRefusedError,
    ProviderAuthError,
    ProviderNotConfiguredError,
    TransientProviderError,
)
from reel_harness.providers.base import (
    CinematicCostEstimate,
    ImageCapabilities,
    ReferenceImageRequest,
    ReferenceImageResult,
)

# Published capability numbers for the chosen model. `max_character_references`
# is 4 because that is the documented limit for character consistency
# specifically (the model accepts more images overall, but only 4 as
# characters), and the reference sheet only ever chains one anyway.
_GOOGLE_IMAGE_CAPABILITIES = ImageCapabilities(
    text_to_image=True,
    character_reference=True,
    max_character_references=4,
    # 2K/4K are deliberately NOT offered. Veo caps reference-driven runs at
    # 720p, so a larger reference costs more and buys nothing any shot
    # could use. Offering a resolution whose only effect is a bigger bill
    # would be a trap, not a feature.
    supported_resolutions=frozenset({"512", "1k"}),
    supported_aspect_ratios=frozenset({"1:1", "9:16", "16:9", "3:4", "4:3"}),
    watermarked=True,
)

# The SDK's ImageConfig.image_size spelling ("512", "1K", "2K", "4K") is
# not this codebase's ("512"/"1k"), so the translation lives here in the
# adapter -- vendor dialects never leak upstream.
_IMAGE_SIZE_BY_RESOLUTION = {"512": "512", "1k": "1K"}

# Every finish_reason that means "the model declined", as opposed to
# "something broke". All of these route to REVIEW_REQUIRED via
# ContentPolicyRefusedError: a human edits the character bible or drops
# the character. Retrying the same prompt could only reach the same
# refusal, which is why ContentPolicyRefusedError is not retryable.
_REFUSAL_FINISH_REASONS = frozenset({
    "SAFETY", "PROHIBITED_CONTENT", "IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
    "BLOCKLIST", "SPII", "RECITATION", "IMAGE_RECITATION",
})

# The watermark Google embeds in every generated image. Recorded as
# provenance, never as something this code applied or could remove.
SYNTHID_WATERMARK = "synthid"

_DEFAULT_MODEL = "gemini-3.1-flash-image"
# Published list price at 1K for the chosen model (docs/STATUS.md's
# research). A hardcoded tariff is a liability, so it is exposed as a
# constructor argument and, when set to None, estimate_cost reports
# `known=False` rather than quoting a number that may have changed.
_DEFAULT_PRICE_PER_IMAGE_USD = 0.067


class GoogleReferenceImageProvider:
    provider_id = "google"
    capabilities = _GOOGLE_IMAGE_CAPABILITIES

    def __init__(
        self, *, api_key: str = "", project: str = "", location: str = "",
        use_vertex: bool = False, model: str = _DEFAULT_MODEL,
        price_per_image_usd: float | None = _DEFAULT_PRICE_PER_IMAGE_USD,
        client: Any | None = None,
    ) -> None:
        self.model_id = model
        self._api_key = api_key
        self._project = project
        self._location = location
        self._use_vertex = use_vertex
        self._price_per_image_usd = price_per_image_usd
        # An injected client is how the contract tests drive this adapter
        # without the SDK ever opening a socket. Production always leaves
        # it None and builds a real one lazily.
        self._client = client

    # -- client -----------------------------------------------------------

    def _build_client(self) -> Any:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
            raise ProviderNotConfiguredError(
                "the google reference-image provider requires the 'google' extra -- "
                "install it with: uv sync --extra google"
            ) from exc

        if self._use_vertex:
            if not (self._project and self._location):
                raise ProviderNotConfiguredError(
                    "vertex mode requires REEL_HARNESS_GOOGLE_PROJECT and "
                    "REEL_HARNESS_GOOGLE_LOCATION"
                )
            return genai.Client(
                vertexai=True, project=self._project, location=self._location,
            )
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "the google reference-image provider requires REEL_HARNESS_GOOGLE_API_KEY "
                "(or vertex mode with a project and location)"
            )
        return genai.Client(api_key=self._api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- contract ---------------------------------------------------------

    def validate_request(self, request: ReferenceImageRequest) -> None:
        if request.resolution not in self.capabilities.supported_resolutions:
            raise ValueError(
                f"unsupported resolution {request.resolution!r} -- supported: "
                f"{sorted(self.capabilities.supported_resolutions)}"
            )
        if request.aspect_ratio not in self.capabilities.supported_aspect_ratios:
            raise ValueError(
                f"unsupported aspect ratio {request.aspect_ratio!r} -- supported: "
                f"{sorted(self.capabilities.supported_aspect_ratios)}"
            )
        if len(request.character_reference_paths) > self.capabilities.max_character_references:
            raise ValueError(
                f"{len(request.character_reference_paths)} character references exceeds the "
                f"documented maximum of {self.capabilities.max_character_references}"
            )
        for reference in request.character_reference_paths:
            if not Path(reference).exists():
                raise ValueError(f"character reference does not exist: {reference}")

    def estimate_cost(self, request: ReferenceImageRequest) -> CinematicCostEstimate:
        if self._price_per_image_usd is None:
            return CinematicCostEstimate(
                known=False,
                detail=(
                    f"no published price configured for {self.model_id} -- "
                    f"set one explicitly rather than trusting a stale default"
                ),
            )
        return CinematicCostEstimate(
            known=True, amount=self._price_per_image_usd, currency="USD",
            detail=f"{self.model_id} list price per image at {request.resolution}",
        )

    def generate_reference(
        self, request: ReferenceImageRequest, dest_dir: Path,
    ) -> ReferenceImageResult:
        self.validate_request(request)
        contents = self._build_contents(request)
        config = self._build_config(request)

        try:
            response = self._get_client().models.generate_content(
                model=self.model_id, contents=contents, config=config,
            )
        except Exception as exc:  # noqa: BLE001 - re-raised as a classified error below
            raise self._classify_exception(exc) from exc

        image_bytes, mime_type = self._extract_image(response)
        dest_dir.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(image_bytes).hexdigest()
        suffix = ".jpg" if "jpeg" in (mime_type or "") else ".png"
        image_path = dest_dir / f"reference-{checksum[:16]}{suffix}"
        image_path.write_bytes(image_bytes)

        return ReferenceImageResult(
            image_path=image_path,
            provider_id=self.provider_id,
            model_id=self.model_id,
            # Google's generated imagery is not licensed by this project;
            # the model id and watermark ARE the provenance, and the
            # publish-eligibility gate reads the license string, so it
            # names the source rather than claiming a grant.
            license=f"GOOGLE_GENERATED:{self.model_id}",
            checksum_sha256=checksum,
            watermark=SYNTHID_WATERMARK,
            seed=request.seed,
            request_id=getattr(response, "response_id", None),
            cost_amount=self._price_per_image_usd,
            cost_currency="USD" if self._price_per_image_usd is not None else None,
        )

    # -- request/response translation -------------------------------------

    def _build_contents(self, request: ReferenceImageRequest) -> list:
        """The prompt plus every character reference as raw bytes.

        `Part.from_bytes` rather than PIL: this project has no image
        library dependency, and the bytes on disk are exactly what the
        API wants."""
        from google.genai import types

        contents: list = [request.prompt]
        for reference in request.character_reference_paths:
            path = Path(reference)
            contents.append(types.Part.from_bytes(
                data=path.read_bytes(), mime_type=_mime_type_for(path),
            ))
        return contents

    def _build_config(self, request: ReferenceImageRequest):
        from google.genai import types

        return types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(
                aspect_ratio=request.aspect_ratio,
                image_size=_IMAGE_SIZE_BY_RESOLUTION[request.resolution],
                # Adults only, explicitly, at the API boundary too. The
                # adaptation schema already refuses minors and every
                # reference prompt says "adult", but this is the one
                # constraint worth stating three times.
                person_generation="ALLOW_ADULT",
            ),
        )

    def _extract_image(self, response: Any) -> tuple[bytes, str | None]:
        """Pulls the generated image out, converting every "no image"
        outcome into an explicit, classified error rather than an
        AttributeError three frames later."""
        feedback = getattr(response, "prompt_feedback", None)
        block_reason = _enum_name(getattr(feedback, "block_reason", None))
        if block_reason:
            # A blocked PROMPT never reached generation at all.
            raise ContentPolicyRefusedError(
                f"google refused the prompt (block_reason={block_reason})"
            )

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise TransientProviderError("google returned no candidates")
        candidate = candidates[0]
        finish_reason = _enum_name(getattr(candidate, "finish_reason", None))
        if finish_reason in _REFUSAL_FINISH_REASONS:
            detail = getattr(candidate, "finish_message", None) or ""
            raise ContentPolicyRefusedError(
                f"google refused to generate this reference "
                f"(finish_reason={finish_reason}) {detail}".strip()
            )

        content = getattr(candidate, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return bytes(data), getattr(inline, "mime_type", None)

        # NO_IMAGE (and any other non-refusal finish reason with no image)
        # is a transient-shaped failure: the same request may well succeed
        # on a retry, unlike a policy refusal.
        raise TransientProviderError(
            f"google returned no image data (finish_reason={finish_reason or 'unset'})"
        )

    def _classify_exception(self, exc: Exception) -> Exception:
        """Maps SDK errors onto this project's classification. Auth is
        never retried (a retry cannot fix a bad key) and the credential is
        never echoed into the message."""
        code = getattr(exc, "code", None)
        if code in (401, 403):
            return ProviderAuthError(
                f"google rejected the credential (HTTP {code}) -- check the API key or "
                f"the service account's permissions"
            )
        if code == 429 or (isinstance(code, int) and 500 <= code < 600):
            return TransientProviderError(f"google returned HTTP {code}")
        if isinstance(exc, ProviderNotConfiguredError | ContentPolicyRefusedError):
            return exc
        if code is not None:
            return TransientProviderError(f"google request failed (HTTP {code})")
        return TransientProviderError(f"google request failed: {type(exc).__name__}")


def _mime_type_for(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"


def _enum_name(value: Any) -> str | None:
    """The SDK returns real enums; tests and older payloads may carry
    plain strings. Both are read the same way rather than assuming one."""
    if value is None:
        return None
    return str(getattr(value, "name", value))
