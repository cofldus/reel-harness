"""Deterministic stand-in for a real cinematic video generation API.
Zero network, unit/integration tests only. Never imitates real video
quality: the "generated clip" is a tiny still-image mp4 rendered with the
real local ffmpeg (so downstream ffprobe validation and editing run
against a genuine video file), or the submission fails loudly when ffmpeg
is absent -- the fake provider never bypasses the BLOCKED_DEPENDENCY
discipline (CLAUDE.md). Every result is stamped FAKE_TEST_LICENSE so it
can never pass a real publish gate."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from reel_harness.core.errors import DependencyError, TransientProviderError
from reel_harness.providers.base import (
    CinematicCapabilities,
    CinematicCostEstimate,
    CinematicGenerationHandle,
    CinematicGenerationRequest,
    CinematicGenerationStatus,
    CinematicVideoResult,
)
from reel_harness.providers.fake_stock_media import FAKE_TEST_LICENSE, make_minimal_png

FakeCinematicMode = Literal["ok", "generating_once", "failed", "moderated", "timeout"]

_FAKE_CAPABILITIES = CinematicCapabilities(
    text_to_video=True,
    image_to_video=True,
    first_frame=True,
    last_frame=False,
    character_reference=True,
    multiple_references=True,
    video_reference=False,
    native_audio=False,
    lip_sync=False,
    supports_seed=True,
    supports_negative_prompt=True,
    supported_durations_sec=frozenset({2.0, 4.0, 6.0, 8.0}),
    supported_aspect_ratios=frozenset({"9:16", "16:9"}),
    supported_resolutions=frozenset({"360p", "720p"}),
    max_concurrent_jobs=None,
)


class FakeCinematicVideoProvider:
    """In-memory job table keyed by provider_job_reference; a job's
    reference is derived deterministically from the request (prompt +
    correlation_id), so identical submissions produce identical
    references -- letting idempotency tests observe duplicate-submission
    behavior without any provider state surviving the process."""

    provider_id = "fake"
    capabilities = _FAKE_CAPABILITIES

    def __init__(self, mode: FakeCinematicMode = "ok") -> None:
        self.mode = mode
        self._submitted: dict[str, CinematicGenerationRequest] = {}
        self._polled_once: set[str] = set()

    def validate_request(self, request: CinematicGenerationRequest) -> None:
        if request.duration_sec not in self.capabilities.supported_durations_sec:
            raise ValueError(
                f"unsupported duration {request.duration_sec}s -- fake provider supports "
                f"{sorted(self.capabilities.supported_durations_sec)}"
            )
        if request.aspect_ratio not in self.capabilities.supported_aspect_ratios:
            raise ValueError(f"unsupported aspect ratio {request.aspect_ratio!r}")
        if request.resolution not in self.capabilities.supported_resolutions:
            raise ValueError(f"unsupported resolution {request.resolution!r}")
        for ref in request.reference_image_paths:
            if not Path(ref).exists():
                raise ValueError(f"reference image does not exist: {ref}")

    def estimate_cost(self, request: CinematicGenerationRequest) -> CinematicCostEstimate:
        # Deterministic, obviously-fake pricing so budget-gate tests have a
        # real number to sum without imitating any vendor's price list.
        return CinematicCostEstimate(
            known=True, amount=round(request.duration_sec * 0.01, 4), currency="FAKE",
            detail="fake provider: 0.01 FAKE per second",
        )

    def create_generation(self, request: CinematicGenerationRequest) -> CinematicGenerationHandle:
        if self.mode == "timeout":
            raise TransientProviderError("fake cinematic generation submit timed out")
        self.validate_request(request)
        reference = hashlib.sha256(
            f"{request.prompt}:{request.correlation_id}".encode()
        ).hexdigest()[:16]
        self._submitted[reference] = request
        return CinematicGenerationHandle(
            provider_job_reference=reference, provider_id=self.provider_id,
            request_id=f"fake-cine-req-{reference}",
        )

    def get_generation_status(self, handle: CinematicGenerationHandle) -> CinematicGenerationStatus:
        if handle.provider_job_reference not in self._submitted:
            return CinematicGenerationStatus(state="failed", failure_reason="unknown generation reference")
        if self.mode == "failed":
            return CinematicGenerationStatus(state="failed", failure_reason="fake provider forced failure")
        if self.mode == "moderated":
            return CinematicGenerationStatus(
                state="moderated", moderation_reason="fake provider forced moderation block",
            )
        if self.mode == "generating_once" and handle.provider_job_reference not in self._polled_once:
            self._polled_once.add(handle.provider_job_reference)
            return CinematicGenerationStatus(state="generating")
        return CinematicGenerationStatus(state="succeeded")

    def cancel_generation(self, handle: CinematicGenerationHandle) -> None:
        self._submitted.pop(handle.provider_job_reference, None)

    def download_result(
        self, handle: CinematicGenerationHandle, dest_dir: Path,
    ) -> CinematicVideoResult:
        request = self._submitted.get(handle.provider_job_reference)
        if request is None:
            raise TransientProviderError(
                f"unknown generation reference {handle.provider_job_reference!r}"
            )
        from reel_harness.media import ffmpeg_render
        from reel_harness.media.deps import check_ffmpeg_available
        from reel_harness.media.runner import run

        deps = check_ffmpeg_available()
        if not deps.all_available:
            # Never pretend a video exists without ffmpeg -- same
            # BLOCKED_DEPENDENCY discipline as the render stage.
            raise DependencyError("ffmpeg is required to materialize a fake cinematic clip")

        dest_dir.mkdir(parents=True, exist_ok=True)
        seed_bytes = hashlib.sha256(handle.provider_job_reference.encode()).digest()
        color = (seed_bytes[0], seed_bytes[1], seed_bytes[2])
        still_path = dest_dir / f".fake-cine-{handle.provider_job_reference}.png"
        still_path.write_bytes(make_minimal_png(64, 64, color))

        if request.aspect_ratio == "16:9":
            width, height = 640, 360
        else:
            width, height = 360, 640
        video_path = dest_dir / f"take-{handle.provider_job_reference}.mp4"
        silent_wav = dest_dir / f".fake-cine-{handle.provider_job_reference}.wav"
        _write_silent_wav(silent_wav, request.duration_sec)
        assert deps.ffmpeg.path is not None  # guarded by all_available above
        argv = ffmpeg_render.render_scene_clip(
            deps.ffmpeg.path, still_path, silent_wav, video_path, width, height,
        )
        result = run(argv, timeout=60)
        still_path.unlink(missing_ok=True)
        silent_wav.unlink(missing_ok=True)
        if result.returncode != 0:
            raise TransientProviderError(
                f"fake cinematic clip render failed: {result.stderr[-300:]}"
            )
        data = video_path.read_bytes()
        return CinematicVideoResult(
            video_path=video_path,
            duration_sec=request.duration_sec,
            provider_id=self.provider_id,
            model_id="fake-cinematic-v1",
            license=FAKE_TEST_LICENSE,
            checksum_sha256=hashlib.sha256(data).hexdigest(),
            generation_seed=request.seed,
            request_id=f"fake-cine-req-{handle.provider_job_reference}",
            cost_amount=round(request.duration_sec * 0.01, 4),
            cost_currency="FAKE",
        )


def _write_silent_wav(path: Path, duration_sec: float) -> None:
    """Minimal valid mono 16-bit PCM WAV of silence -- stdlib only, so the
    fake clip has a real audio stream for the existing ffmpeg scene-clip
    path (which maps an audio input and uses -shortest for duration)."""
    import struct
    import wave

    sample_rate = 44100
    frame_count = int(sample_rate * duration_sec)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(struct.pack(f"<{frame_count}h", *([0] * frame_count)))
