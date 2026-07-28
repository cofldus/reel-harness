from __future__ import annotations

import hashlib
import time
import uuid
from pathlib import Path
from typing import Any

from reel_harness.core.errors import DependencyError, ProviderAuthError, TransientProviderError
from reel_harness.media import asset_video
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
from reel_harness.media.runner import run
from reel_harness.providers.base import LocalAssetResult, MediaCandidate

ADAPTER_VERSION = "pexels-videos-v1"

# Every candidate/result this adapter returns carries this license identifier.
# Pexels License (https://www.pexels.com/license/): free for commercial and
# non-commercial use, modification allowed, attribution appreciated but not
# legally required -- attribution_text is populated anyway so the manifest
# always carries full provenance. See docs/OPERATIONS.md for the full
# rationale behind choosing this provider.
PEXELS_LICENSE = "PEXELS_LICENSE"
PEXELS_LICENSE_URL = "https://www.pexels.com/license/"

# Hard ceiling on a single asset download -- a runaway/streamed response is a
# provider fault, not something to buffer without bounds.
MAX_ASSET_BYTES = 100 * 1024 * 1024
MAX_REDIRECTS = 3
# Target portrait height used to pick among a video's available renditions.
_TARGET_HEIGHT = 1920


class PexelsStockMediaProvider:
    """Adapter for the Pexels Video API
    (https://www.pexels.com/api/documentation/#videos-search).

    Vendor-neutral surface: pipeline/worker code depends only on the
    StockMediaProvider Protocol; the concrete vendor (Pexels) and its
    request/response shape are isolated to this class and the registry.

    A 2xx HTTP status is NOT success for a download: the received bytes must
    pass real ffprobe validation (video stream, resolution, duration) and
    survive normalization to canonical H.264/yuv420p through real ffmpeg
    before a result is returned. The API key lives only in the search
    request's Authorization header -- it is never sent to the (separately
    hosted) video-file download and never appears in exception messages,
    logs, results, or files. Response bodies are never logged or persisted.

    Cost note: a stage-level retry re-runs search AND re-downloads the
    selected file, which re-counts against Pexels' rate limit -- see
    docs/OPERATIONS.md.
    """

    provider_id = "pexels"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.pexels.com/videos",
        connect_timeout: float = 10.0,
        read_timeout: float = 60.0,
        max_retries: int = 3,
        retry_backoff_seconds: float = 2.0,
        max_asset_bytes: int = MAX_ASSET_BYTES,
        transport: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("asset_api_key is required for the pexels stock media provider")
        import httpx

        self._max_retries = max_retries
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_asset_bytes = max_asset_bytes
        self._httpx = httpx
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": api_key},
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
            follow_redirects=False,
        )
        # A separate, unauthenticated client for streaming the actual video
        # file (Pexels serves files from a different host than the search
        # API) -- the API key must never reach a download/redirect target.
        self._download_client = httpx.Client(
            timeout=httpx.Timeout(connect=connect_timeout, read=read_timeout, write=30.0, pool=30.0),
            transport=transport,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()
        self._download_client.close()

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
        params: dict[str, Any] = {
            "query": query, "orientation": orientation, "per_page": per_page, "page": page,
        }
        payload, request_id = self._get("/search", params)
        videos = payload.get("videos")
        if not isinstance(videos, list):
            raise TransientProviderError(
                f"asset search response missing a 'videos' array (request_id={request_id})"
            )
        candidates: list[MediaCandidate] = []
        for rank, video in enumerate(videos):
            candidate = self._to_candidate(video, rank, request_id)
            if candidate is None:
                continue
            if candidate.candidate_id in exclude_provider_asset_ids:
                continue
            if min_width is not None and (candidate.width or 0) < min_width:
                continue
            if min_height is not None and (candidate.height or 0) < min_height:
                continue
            if candidate.duration_sec is not None:
                if candidate.duration_sec < min_duration:
                    continue
                if max_duration is not None and candidate.duration_sec > max_duration:
                    continue
            candidates.append(candidate)
        return candidates

    def _to_candidate(self, video: dict, rank: int, request_id: str | None) -> MediaCandidate | None:
        try:
            video_id = str(video["id"])
            best = self._best_file(video.get("video_files") or [])
            if best is None:
                return None
            user = video.get("user") or {}
            name = user.get("name")
            return MediaCandidate(
                candidate_id=video_id,
                source_url=str(video.get("url") or ""),
                author=name,
                license_type=PEXELS_LICENSE,
                license_url=PEXELS_LICENSE_URL,
                provider_id=self.provider_id,
                download_url=str(best["link"]),
                creator_url=user.get("url"),
                commercial_use_allowed=True,
                modification_allowed=True,
                attribution_text=f"Video by {name} on Pexels" if name else "Video via Pexels",
                width=int(best.get("width") or video.get("width") or 0) or None,
                height=int(best.get("height") or video.get("height") or 0) or None,
                duration_sec=float(video["duration"]) if video.get("duration") is not None else None,
                fps=float(best["fps"]) if best.get("fps") is not None else None,
                content_type=str(best.get("file_type") or "video/mp4"),
                provider_rank=rank,
                provider_request_id=request_id,
            )
        except (KeyError, TypeError, ValueError):
            # A malformed entry in an otherwise-valid response is skipped, not
            # fatal -- the rest of the page may still be usable.
            return None

    @staticmethod
    def _best_file(files: list[dict]) -> dict | None:
        """Deterministic pick among a video's available mp4 renditions: the
        one closest to (without preferring under) the pipeline's portrait
        target height, tie-broken by the provider's own file id ascending so
        selection never depends on response ordering."""
        mp4_files = [f for f in files if f.get("file_type") == "video/mp4" and f.get("link")]
        if not mp4_files:
            return None

        def _key(f: dict) -> tuple[int, int, int]:
            height = f.get("height") or 0
            return (abs(height - _TARGET_HEIGHT), -height, f.get("id") or 0)

        return sorted(mp4_files, key=_key)[0]

    def download(self, candidate: MediaCandidate, dest_dir: Path) -> LocalAssetResult:
        deps = check_ffmpeg_available()
        if not deps.ffmpeg_available or not deps.ffprobe_available:
            raise DependencyError(
                "ffmpeg/ffprobe executable not found (required to validate/normalize stock video)"
            )
        assert deps.ffmpeg.path is not None
        assert deps.ffprobe.path is not None

        dest_dir.mkdir(parents=True, exist_ok=True)
        source_path = dest_dir / f"source-{uuid.uuid4().hex}.mp4"
        output_path = dest_dir / "asset.mp4"
        try:
            self._stream_download(candidate.download_url, source_path)

            probe = run(build_ffprobe_argv(deps.ffprobe.path, source_path), timeout=30)
            if probe.returncode != 0:
                raise TransientProviderError(
                    f"downloaded asset failed ffprobe (corrupt or undecodable video): "
                    f"ffprobe exit {probe.returncode}"
                )
            try:
                info = parse_ffprobe_output(probe.stdout)
            except (ValueError, KeyError) as exc:
                raise TransientProviderError(f"downloaded asset failed validation: {exc}") from exc
            try:
                asset_video.validate_asset_video(info)
            except ValueError as exc:
                raise TransientProviderError(f"downloaded asset failed validation: {exc}") from exc

            normalized = run(
                asset_video.normalize_asset_video_argv(deps.ffmpeg.path, source_path, output_path), timeout=60,
            )
            if normalized.returncode != 0:
                output_path.unlink(missing_ok=True)
                raise TransientProviderError(
                    f"stock video failed normalization: ffmpeg exit {normalized.returncode}"
                )
        finally:
            source_path.unlink(missing_ok=True)

        checksum = hashlib.sha256(output_path.read_bytes()).hexdigest()
        return LocalAssetResult(
            local_path=output_path,
            checksum_sha256=checksum,
            mime_type="video/mp4",
            source_url=candidate.download_url,
            author=candidate.author,
            license_type=candidate.license_type,
            provider_id=self.provider_id,
            provider_asset_id=candidate.candidate_id,
            source_page_url=candidate.source_url,
            creator_url=candidate.creator_url,
            commercial_use_allowed=candidate.commercial_use_allowed,
            modification_allowed=candidate.modification_allowed,
            attribution_text=candidate.attribution_text,
            width=info.width,
            height=info.height,
            duration_sec=info.duration_sec,
            fps=candidate.fps,
            request_id=candidate.provider_request_id,
        )

    def _stream_download(self, url: str, dest_path: Path) -> None:
        redirects = 0
        current_url = url
        while True:
            try:
                with self._download_client.stream("GET", current_url) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        redirects += 1
                        if redirects > MAX_REDIRECTS:
                            raise TransientProviderError("asset download exceeded the redirect limit")
                        location = response.headers.get("location")
                        if not location:
                            raise TransientProviderError(
                                "asset download redirected without a Location header"
                            )
                        target = self._httpx.URL(current_url).join(location)
                        if target.scheme != "https":
                            raise TransientProviderError(
                                f"asset download redirected to disallowed scheme {target.scheme!r}"
                            )
                        current_url = str(target)
                        continue
                    if response.status_code in (401, 403):
                        raise ProviderAuthError(
                            f"asset download rejected the configured credential (HTTP {response.status_code})"
                        )
                    if response.status_code != 200:
                        raise TransientProviderError(
                            f"asset download returned unexpected HTTP {response.status_code}"
                        )

                    content_type = response.headers.get("content-type", "").lower()
                    if "text/html" in content_type or "application/json" in content_type:
                        raise TransientProviderError(
                            f"asset download returned non-video content type {content_type!r}"
                        )

                    content_length = response.headers.get("content-length")
                    if content_length is not None:
                        try:
                            if int(content_length) > self._max_asset_bytes:
                                raise TransientProviderError(
                                    f"asset content-length {content_length} exceeds the "
                                    f"{self._max_asset_bytes} byte limit"
                                )
                        except ValueError:
                            pass

                    written = 0
                    with open(dest_path, "wb") as handle:
                        for chunk in response.iter_bytes():
                            written += len(chunk)
                            if written > self._max_asset_bytes:
                                raise TransientProviderError(
                                    f"asset download exceeded the {self._max_asset_bytes} "
                                    "byte limit while streaming"
                                )
                            handle.write(chunk)
                    if written == 0:
                        raise TransientProviderError("asset download returned an empty body")
                    return
            except (ProviderAuthError, TransientProviderError):
                dest_path.unlink(missing_ok=True)
                raise
            except self._httpx.TimeoutException as exc:
                dest_path.unlink(missing_ok=True)
                raise TransientProviderError(f"asset download timed out ({type(exc).__name__})") from exc
            except self._httpx.HTTPError as exc:
                dest_path.unlink(missing_ok=True)
                raise TransientProviderError(f"asset download transport error ({type(exc).__name__})") from exc

    def _get(self, path: str, params: dict) -> tuple[dict, str | None]:
        correlation_id = str(uuid.uuid4())
        attempts = self._max_retries + 1
        last_transient: Exception | None = None
        retry_after: float | None = None
        for attempt in range(attempts):
            if attempt > 0:
                delay = max(self._retry_backoff_seconds * attempt, retry_after or 0.0)
                retry_after = None
                time.sleep(min(delay, 30.0))
            try:
                response = self._client.get(path, params=params, headers={"X-Request-ID": correlation_id})
            except self._httpx.TimeoutException as exc:
                last_transient = TransientProviderError(f"asset search timed out ({type(exc).__name__})")
                continue
            except self._httpx.HTTPError as exc:
                last_transient = TransientProviderError(f"asset search transport error ({type(exc).__name__})")
                continue

            request_id = response.headers.get("x-request-id")
            if response.status_code in (401, 403):
                raise ProviderAuthError(
                    f"asset endpoint rejected the configured credential "
                    f"(HTTP {response.status_code}, request_id={request_id})"
                )
            if response.status_code == 429 or response.status_code >= 500:
                raw_retry_after = response.headers.get("retry-after")
                try:
                    retry_after = max(0.0, float(raw_retry_after)) if raw_retry_after else None
                except ValueError:
                    retry_after = None
                last_transient = TransientProviderError(
                    f"asset endpoint returned HTTP {response.status_code} (request_id={request_id})"
                )
                continue
            if response.status_code != 200:
                raise TransientProviderError(
                    f"asset endpoint returned unexpected HTTP {response.status_code} (request_id={request_id})"
                )

            content_type = response.headers.get("content-type", "").lower()
            if "application/json" not in content_type:
                last_transient = TransientProviderError(
                    f"asset endpoint returned non-JSON content type {content_type!r} (request_id={request_id})"
                )
                continue
            try:
                payload = response.json()
            except ValueError:
                last_transient = TransientProviderError(
                    f"asset endpoint returned malformed JSON (request_id={request_id})"
                )
                continue
            if not isinstance(payload, dict):
                last_transient = TransientProviderError(
                    f"asset endpoint returned a non-object JSON body (request_id={request_id})"
                )
                continue
            return payload, request_id

        assert last_transient is not None
        raise TransientProviderError(f"asset search failed after {attempts} attempts: {last_transient}")
