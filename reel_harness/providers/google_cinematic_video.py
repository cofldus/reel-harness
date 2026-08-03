"""Real cinematic video generation via Vertex AI Veo (`google-genai`).

Chosen in F1's provider survey as the only API with first-class
multi-image character reference, and confirmed in F3's research as
sharing one SDK and one credential with the reference-image model -- so
casting and generation authenticate identically. See docs/STATUS.md.

Import discipline matches the reference-image adapter: `google.genai` is
an OPTIONAL dependency (the `google` extra), imported inside methods so a
machine without it still runs the entire fake/demo pipeline and gets an
install instruction instead of an ImportError traceback.

Three documented constraints are enforced LOCALLY rather than discovered
from a rejected request, because each one costs money to learn the hard
way:

1. **Reference-driven runs are 8s at 720p.** Not a preference -- the API
   fixes both when reference images are attached. Requesting anything
   else with references is refused here.
2. **At most 3 reference images**, of type `asset`.
3. **`person_generation` must be `allow_adult`.** Stated at the API
   boundary as well as in every prompt, because it is the one constraint
   worth repeating.

**Generated videos are deleted after 2 days.** `download_result` fetches
bytes immediately and writes them to the project's storage; nothing in
this codebase ever holds a provider URI as if it were durable.

**Verification status**: written against the INSTALLED SDK's real type
definitions (`GenerateVideosConfig`, `VideoGenerationReferenceImage`,
`GenerateVideosOperation`, `GenerateVideosResponse.rai_media_filtered_*`,
`Video.video_bytes`) -- introspected, not recalled. It has NEVER been run
against the live API on this machine; there are no credentials here. The
tests are contract tests against an injected fake client and prove
protocol conformance only, never live success.
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
    CinematicCapabilities,
    CinematicCostEstimate,
    CinematicGenerationHandle,
    CinematicGenerationRequest,
    CinematicGenerationStatus,
    CinematicVideoResult,
)

# What the chosen model actually supports, per the July 2026 research.
# `supported_durations_sec` is the reference-driven case: attaching
# character references fixes the clip at 8 seconds, and this provider
# exists to be used WITH references.
_VEO_CAPABILITIES = CinematicCapabilities(
    text_to_video=True,
    image_to_video=True,
    first_frame=True,
    last_frame=True,
    character_reference=True,
    multiple_references=True,
    video_reference=False,
    native_audio=True,
    lip_sync=False,
    supports_seed=True,
    supports_negative_prompt=True,
    supported_durations_sec=frozenset({8.0}),
    supported_aspect_ratios=frozenset({"16:9", "9:16"}),
    supported_resolutions=frozenset({"720p"}),
    max_concurrent_jobs=None,
)

# The API's own hard limits when reference images are attached.
MAX_REFERENCE_IMAGES = 3
REFERENCE_DURATION_SEC = 8.0
REFERENCE_RESOLUTION = "720p"

# The only region the GA endpoint serves (docs/STATUS.md's research).
# Enforced rather than defaulted: a project pinned to another region
# would fail at generation time, after the operator believed it was
# configured.
SUPPORTED_LOCATION = "us-central1"

_DEFAULT_MODEL = "veo-3.1-fast-generate-001"
# Published list price per second for the chosen model (veo-3.1-fast).
# Configurable for the same reason the image price is: a vendor's tariff
# is not this project's to promise, and `None` reports `known=False`
# rather than quoting a number that may have changed.
#
# UNSETTLED, and deliberately held at the higher figure. Google publishes
# 0.10/second for the Fast tier; this code has assumed 0.15. One real
# invoice is suggestive but not conclusive: an 8-second reference-driven
# clip plus two reference stills billed KRW 1,330, which is about USD
# 0.93 at any plausible rate -- matching 0.10/second (0.80 + 0.134) and
# not 0.15 (1.33). But GCP billing lags, so that total may be partial.
#
# It stays at 0.15 until a Vertex AI SKU line shows quantity x unit
# price, because the two directions are not symmetric: quoting too low
# lets a project overrun the ceiling its owner set, while quoting too
# high only makes the gate refuse work that was affordable. For a
# spending limit, over-estimating is the safe way to be wrong.
_DEFAULT_PRICE_PER_SECOND_USD = 0.15

# Operation error codes that mean "the model declined", as opposed to
# "something broke". gRPC status codes, which is what a long-running
# operation's `error` carries.
_PERMISSION_DENIED = 7
_UNAUTHENTICATED = 16
_RESOURCE_EXHAUSTED = 8
_INVALID_ARGUMENT = 3


class GoogleCinematicVideoProvider:
    provider_id = "google"
    capabilities = _VEO_CAPABILITIES

    def __init__(
        self, *, project: str = "", location: str = SUPPORTED_LOCATION,
        api_key: str = "", use_vertex: bool = True, model: str = _DEFAULT_MODEL,
        price_per_second_usd: float | None = _DEFAULT_PRICE_PER_SECOND_USD,
        generate_audio: bool = True, client: Any | None = None,
    ) -> None:
        self.model_id = model
        self._project = project
        self._location = location
        self._api_key = api_key
        self._use_vertex = use_vertex
        self._price_per_second_usd = price_per_second_usd
        self._generate_audio = generate_audio
        # An injected client is how the contract tests drive this adapter
        # without the SDK ever opening a socket.
        self._client = client
        # Requests are kept only so a poll/download can recover what the
        # generation was FOR -- never credentials, never a signed URL.
        self._requests: dict[str, CinematicGenerationRequest] = {}
        self._operations: dict[str, Any] = {}

    # -- client -----------------------------------------------------------

    def _build_client(self) -> Any:
        try:
            from google import genai
        except ImportError as exc:  # pragma: no cover - exercised by the extra being absent
            raise ProviderNotConfiguredError(
                "the google cinematic provider requires the 'google' extra -- "
                "install it with: uv sync --extra google"
            ) from exc

        if self._use_vertex:
            if not self._project:
                raise ProviderNotConfiguredError(
                    "vertex mode requires REEL_HARNESS_GOOGLE_PROJECT"
                )
            if self._location != SUPPORTED_LOCATION:
                raise ProviderNotConfiguredError(
                    f"{self.model_id} is only served from {SUPPORTED_LOCATION!r}, "
                    f"but REEL_HARNESS_GOOGLE_LOCATION is {self._location!r}"
                )
            return genai.Client(
                vertexai=True, project=self._project, location=self._location,
            )
        if not self._api_key:
            raise ProviderNotConfiguredError(
                "the google cinematic provider requires REEL_HARNESS_GOOGLE_API_KEY "
                "(or vertex mode with a project)"
            )
        return genai.Client(api_key=self._api_key)

    def _get_client(self) -> Any:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # -- contract ---------------------------------------------------------

    def validate_request(self, request: CinematicGenerationRequest) -> None:
        """Every documented limit checked BEFORE submission. A rejected
        request costs a round trip; a request that is accepted and then
        silently produces something other than what was asked for costs a
        generation."""
        references = request.reference_image_paths
        if len(references) > MAX_REFERENCE_IMAGES:
            raise ValueError(
                f"{len(references)} reference images exceeds the documented maximum of "
                f"{MAX_REFERENCE_IMAGES}"
            )
        for reference in references:
            if not Path(reference).exists():
                raise ValueError(f"reference image does not exist: {reference}")

        if references:
            # Not preferences: the API fixes both when references are
            # attached, so asking for anything else would silently get
            # something different.
            if request.duration_sec != REFERENCE_DURATION_SEC:
                raise ValueError(
                    f"reference-driven generation is fixed at {REFERENCE_DURATION_SEC}s, "
                    f"but {request.duration_sec}s was requested"
                )
            if request.resolution != REFERENCE_RESOLUTION:
                raise ValueError(
                    f"reference-driven generation is fixed at {REFERENCE_RESOLUTION}, "
                    f"but {request.resolution!r} was requested"
                )
        else:
            if request.duration_sec not in self.capabilities.supported_durations_sec:
                raise ValueError(
                    f"unsupported duration {request.duration_sec}s -- supported: "
                    f"{sorted(self.capabilities.supported_durations_sec)}"
                )
            if request.resolution not in self.capabilities.supported_resolutions:
                raise ValueError(f"unsupported resolution {request.resolution!r}")
        if request.aspect_ratio not in self.capabilities.supported_aspect_ratios:
            raise ValueError(
                f"unsupported aspect ratio {request.aspect_ratio!r} -- supported: "
                f"{sorted(self.capabilities.supported_aspect_ratios)}"
            )

    def estimate_cost(self, request: CinematicGenerationRequest) -> CinematicCostEstimate:
        if self._price_per_second_usd is None:
            return CinematicCostEstimate(
                known=False,
                detail=(
                    f"no published price configured for {self.model_id} -- set one "
                    f"explicitly rather than trusting a stale default"
                ),
            )
        amount = round(self._price_per_second_usd * request.duration_sec, 6)
        return CinematicCostEstimate(
            known=True, amount=amount, currency="USD",
            detail=f"{self.model_id} at {self._price_per_second_usd}/s x {request.duration_sec}s",
        )

    def create_generation(
        self, request: CinematicGenerationRequest,
    ) -> CinematicGenerationHandle:
        self.validate_request(request)
        config = self._build_config(request)
        try:
            operation = self._get_client().models.generate_videos(
                model=self.model_id, prompt=request.prompt, config=config,
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            raise self._classify_exception(exc) from exc

        reference = getattr(operation, "name", None)
        if not reference:
            raise TransientProviderError("veo returned an operation with no name")
        # An operation object is what the SDK polls with, so it is kept
        # alongside its name. The name alone is what gets PERSISTED (see
        # FableTake.provider_job_reference); a fresh process recovers by
        # asking for the operation by name, never by trusting this cache.
        self._operations[reference] = operation
        self._requests[reference] = request
        return CinematicGenerationHandle(
            provider_job_reference=reference, provider_id=self.provider_id,
            request_id=request.correlation_id or None,
        )

    def get_generation_status(
        self, handle: CinematicGenerationHandle,
    ) -> CinematicGenerationStatus:
        operation = self._refresh_operation(handle)
        if not getattr(operation, "done", False):
            return CinematicGenerationStatus(state="generating")

        error = getattr(operation, "error", None)
        if error:
            return self._status_for_error(error)

        response = getattr(operation, "response", None) or getattr(operation, "result", None)
        filtered = getattr(response, "rai_media_filtered_count", 0) or 0
        if filtered:
            reasons = getattr(response, "rai_media_filtered_reasons", None) or []
            # A safety filter removing the output is a human decision, not
            # a retry: the same prompt would be filtered again.
            return CinematicGenerationStatus(
                state="moderated",
                moderation_reason="; ".join(str(r) for r in reasons) or "filtered by safety review",
            )
        if not getattr(response, "generated_videos", None):
            return CinematicGenerationStatus(
                state="failed", failure_reason="operation completed with no generated video",
            )
        return CinematicGenerationStatus(state="succeeded")

    def cancel_generation(self, handle: CinematicGenerationHandle) -> None:
        """Best-effort local forget. The SDK exposes no cancel for a video
        operation, and pretending otherwise would be worse than saying so:
        an operator who believes a paid generation was cancelled would be
        billed anyway."""
        self._operations.pop(handle.provider_job_reference, None)
        self._requests.pop(handle.provider_job_reference, None)

    def download_result(
        self, handle: CinematicGenerationHandle, dest_dir: Path,
    ) -> CinematicVideoResult:
        """Fetches the bytes IMMEDIATELY. Generated videos are deleted
        after two days, so a provider URI is never treated as durable
        storage -- the local file is the artifact."""
        operation = self._refresh_operation(handle)
        response = getattr(operation, "response", None) or getattr(operation, "result", None)
        videos = getattr(response, "generated_videos", None) or []
        if not videos:
            raise TransientProviderError("veo operation has no generated video to download")
        video = videos[0].video

        data = getattr(video, "video_bytes", None)
        if not data:
            try:
                self._get_client().files.download(file=video)
            except Exception as exc:  # noqa: BLE001 - classified below
                raise self._classify_exception(exc) from exc
            data = getattr(video, "video_bytes", None)
        if not data:
            raise TransientProviderError(
                "veo returned a video with neither inline bytes nor a downloadable payload"
            )

        dest_dir.mkdir(parents=True, exist_ok=True)
        checksum = hashlib.sha256(data).hexdigest()
        video_path = dest_dir / f"take-{checksum[:16]}.mp4"
        video_path.write_bytes(data)

        request = self._requests.get(handle.provider_job_reference)
        duration = request.duration_sec if request is not None else REFERENCE_DURATION_SEC
        estimate = (
            self._price_per_second_usd * duration
            if self._price_per_second_usd is not None else None
        )
        return CinematicVideoResult(
            video_path=video_path,
            duration_sec=duration,
            provider_id=self.provider_id,
            model_id=self.model_id,
            # Names the source rather than claiming a grant, exactly as
            # the reference-image adapter does.
            license=f"GOOGLE_GENERATED:{self.model_id}",
            checksum_sha256=checksum,
            generation_seed=request.seed if request is not None else None,
            request_id=handle.request_id,
            cost_amount=round(estimate, 6) if estimate is not None else None,
            cost_currency="USD" if estimate is not None else None,
        )

    # -- request/response translation -------------------------------------

    def _build_config(self, request: CinematicGenerationRequest):
        from google.genai import types

        references = [
            types.VideoGenerationReferenceImage(
                image=types.Image(
                    image_bytes=Path(path).read_bytes(),
                    mime_type=_mime_type_for(Path(path)),
                ),
                # ASSET is the documented type for character/subject
                # consistency; STYLE would transfer look, not identity.
                reference_type=types.VideoGenerationReferenceType.ASSET,
            )
            for path in request.reference_image_paths
        ]
        return types.GenerateVideosConfig(
            number_of_videos=1,
            duration_seconds=int(request.duration_sec),
            aspect_ratio=request.aspect_ratio,
            resolution=request.resolution,
            # Adults only, at the API boundary as well as in the prompt.
            person_generation="allow_adult",
            negative_prompt=request.negative_prompt,
            seed=request.seed,
            generate_audio=self._generate_audio,
            reference_images=references or None,
        )

    def _refresh_operation(self, handle: CinematicGenerationHandle) -> Any:
        """Re-reads the operation from the provider. Always a fresh read:
        a cached `done=False` from a previous poll would strand a finished
        generation forever."""
        cached = self._operations.get(handle.provider_job_reference)
        try:
            operation = self._get_client().operations.get(
                cached if cached is not None else handle.provider_job_reference
            )
        except Exception as exc:  # noqa: BLE001 - classified below
            raise self._classify_exception(exc) from exc
        self._operations[handle.provider_job_reference] = operation
        return operation

    def _status_for_error(self, error: Any) -> CinematicGenerationStatus:
        code = error.get("code") if isinstance(error, dict) else getattr(error, "code", None)
        message = (
            error.get("message") if isinstance(error, dict) else getattr(error, "message", None)
        ) or "veo operation failed"
        if code in (_PERMISSION_DENIED, _UNAUTHENTICATED):
            # Surfaced as a failure rather than raised: the caller is a
            # poll loop, and the shot's own failure path already
            # classifies this correctly for a human.
            return CinematicGenerationStatus(state="failed", failure_reason=f"auth: {message}")
        if code == _INVALID_ARGUMENT:
            return CinematicGenerationStatus(state="failed", failure_reason=message)
        return CinematicGenerationStatus(state="failed", failure_reason=message)

    def _classify_exception(self, exc: Exception) -> Exception:
        code = getattr(exc, "code", None)
        if code in (401, 403, _PERMISSION_DENIED, _UNAUTHENTICATED):
            return ProviderAuthError(
                f"vertex rejected the credential -- check the service account's "
                f"permissions on project {self._project or '(unset)'}"
            )
        if code in (429, _RESOURCE_EXHAUSTED) or (isinstance(code, int) and 500 <= code < 600):
            return TransientProviderError(f"vertex returned an error (code {code})")
        if isinstance(exc, ProviderNotConfiguredError | ContentPolicyRefusedError):
            return exc
        return TransientProviderError(f"vertex request failed: {type(exc).__name__}")


def _mime_type_for(path: Path) -> str:
    return "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "image/png"
