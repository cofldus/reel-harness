"""Contract tests for the Pexels stock-media adapter (MockTransport -- no
sockets, coexists with the network-block fixture; NOT a live provider E2E).
All keys are obviously-fake placeholders.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest

from reel_harness.core.errors import DependencyError, ProviderAuthError, TransientProviderError
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.ffprobe_validate import build_ffprobe_argv, parse_ffprobe_output
from reel_harness.media.runner import run
from reel_harness.providers.base import MediaCandidate
from reel_harness.providers.pexels_stock_media import PEXELS_LICENSE, PexelsStockMediaProvider

DEPS = check_ffmpeg_available()
FFMPEG_PRESENT = DEPS.all_available

FAKE_KEY = "FAKE-ASSET-ADAPTER-KEY-000000000000"

_VIDEOS_PAGE = {
    "page": 1,
    "per_page": 15,
    "total_results": 1,
    "videos": [
        {
            "id": 4242,
            "width": 1080,
            "height": 1920,
            "duration": 8,
            "url": "https://www.pexels.com/video/a-test-clip-4242/",
            "user": {"id": 7, "name": "Test Creator", "url": "https://www.pexels.com/@test-creator"},
            "video_files": [
                {"id": 1, "quality": "sd", "file_type": "video/mp4", "width": 640, "height": 360, "fps": 25.0,
                 "link": "https://videos.pexels.com/video-files/4242/4242-sd.mp4"},
                {"id": 2, "quality": "hd", "file_type": "video/mp4", "width": 1080, "height": 1920, "fps": 25.0,
                 "link": "https://videos.pexels.com/video-files/4242/4242-hd.mp4"},
            ],
        },
    ],
}


def _mp4_bytes(tmp_path: Path, *, width: int = 640, height: int = 480, duration: float = 1.0,
               with_audio: bool = False, video: bool = True) -> bytes:
    """A real, ffprobe-parseable MP4 built with the project's own ffmpeg via
    lavfi test sources -- there is no stdlib equivalent to Python's `wave`
    module for video, so this mirrors how tests/e2e/test_production_smoke.py
    proves the real toolchain rather than hand-rolling a container."""
    out = tmp_path / f"src-{width}x{height}-{duration}-{with_audio}-{video}.mp4"
    argv = [str(DEPS.ffmpeg.path), "-y"]
    if video:
        argv += ["-f", "lavfi", "-i", f"testsrc=duration={duration}:size={width}x{height}:rate=25"]
    if with_audio:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    elif not video:
        argv += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}"]
    if video:
        argv += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if with_audio or not video:
        argv += ["-c:a", "aac"]
    argv += [str(out)]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _provider(search_handler, download_handler, **overrides) -> PexelsStockMediaProvider:
    def router(request: httpx.Request) -> httpx.Response:
        if "api.pexels.com" in str(request.url) or request.url.path.startswith("/search"):
            return search_handler(request)
        return download_handler(request)

    defaults: dict = dict(api_key=FAKE_KEY, max_retries=2, retry_backoff_seconds=0.0)
    defaults.update(overrides)
    return PexelsStockMediaProvider(transport=httpx.MockTransport(router), **defaults)


def _search_ok(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=_VIDEOS_PAGE, headers={"x-request-id": "search-req-1"})


def test_search_picks_the_rendition_closest_to_portrait_target_and_maps_license() -> None:
    provider = _provider(_search_ok, download_handler=lambda r: httpx.Response(200))
    candidates = provider.search("cats", orientation="portrait", min_duration=1.0)
    assert len(candidates) == 1
    c = candidates[0]
    assert c.candidate_id == "4242"
    assert c.download_url.endswith("4242-hd.mp4"), "must pick the 1080x1920 rendition over the 640x360 one"
    assert c.license_type == PEXELS_LICENSE
    assert c.commercial_use_allowed is True
    assert c.modification_allowed is True
    assert c.author == "Test Creator"
    assert c.attribution_text == "Video by Test Creator on Pexels"
    assert c.duration_sec == 8.0
    assert c.provider_request_id == "search-req-1"


def test_search_applies_min_width_height_duration_and_dedup_filters() -> None:
    provider = _provider(_search_ok, download_handler=lambda r: httpx.Response(200))
    assert provider.search("cats", orientation="portrait", min_duration=100.0) == []
    assert provider.search("cats", orientation="portrait", min_duration=1.0, min_width=5000) == []
    assert provider.search(
        "cats", orientation="portrait", min_duration=1.0, exclude_provider_asset_ids=frozenset({"4242"}),
    ) == []


def test_empty_search_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"page": 1, "per_page": 15, "videos": []})

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    assert provider.search("no such thing", orientation="portrait", min_duration=1.0) == []


def test_search_401_raises_auth_error_non_retryable() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(401)

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    with pytest.raises(ProviderAuthError) as exc_info:
        provider.search("cats", orientation="portrait", min_duration=1.0)
    assert len(calls) == 1, "auth errors must not be retried"
    assert FAKE_KEY not in str(exc_info.value)


def test_search_403_raises_auth_error() -> None:
    provider = _provider(lambda r: httpx.Response(403), download_handler=lambda r: httpx.Response(200))
    with pytest.raises(ProviderAuthError):
        provider.search("cats", orientation="portrait", min_duration=1.0)


def test_search_429_honors_retry_after_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0"})
        return _search_ok(request)

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    candidates = provider.search("cats", orientation="portrait", min_duration=1.0)
    assert len(candidates) == 1
    assert len(calls) == 2


def test_search_500_then_success() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(500)
        return _search_ok(request)

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    assert len(provider.search("cats", orientation="portrait", min_duration=1.0)) == 1


def test_search_timeout_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout")

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    with pytest.raises(TransientProviderError):
        provider.search("cats", orientation="portrait", min_duration=1.0)


def test_search_malformed_json_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not json", headers={"content-type": "application/json"})

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    with pytest.raises(TransientProviderError):
        provider.search("cats", orientation="portrait", min_duration=1.0)


def test_search_html_error_page_with_200_is_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>oops</html>", headers={"content-type": "text/html"})

    provider = _provider(handler, download_handler=lambda r: httpx.Response(200))
    with pytest.raises(TransientProviderError):
        provider.search("cats", orientation="portrait", min_duration=1.0)


_CANDIDATE = MediaCandidate(
    candidate_id="4242", source_url="https://www.pexels.com/video/a-test-clip-4242/",
    author="Test Creator", license_type=PEXELS_LICENSE, license_url="https://www.pexels.com/license/",
    provider_id="pexels", download_url="https://videos.pexels.com/video-files/4242/4242-hd.mp4",
    creator_url="https://www.pexels.com/@test-creator", commercial_use_allowed=True,
    modification_allowed=True, attribution_text="Video by Test Creator on Pexels",
    width=1080, height=1920, duration_sec=8.0, fps=25.0, content_type="video/mp4",
    provider_rank=0, provider_request_id="search-req-1",
)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_validates_normalizes_and_checksums(tmp_path) -> None:
    body = _mp4_bytes(tmp_path, width=640, height=480, duration=1.0)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})

    provider = _provider(_search_ok, download_handler)
    result = provider.download(_CANDIDATE, tmp_path / "scene_0")

    assert result.local_path.name == "asset.mp4"
    assert result.mime_type == "video/mp4"
    assert result.provider_id == "pexels"
    assert result.provider_asset_id == "4242"
    assert result.commercial_use_allowed is True
    assert result.attribution_text == "Video by Test Creator on Pexels"
    assert result.checksum_sha256 == hashlib.sha256(result.local_path.read_bytes()).hexdigest()
    assert result.duration_sec and result.duration_sec > 0

    probe = run(build_ffprobe_argv(DEPS.ffprobe.path, result.local_path), timeout=30)
    info = parse_ffprobe_output(probe.stdout)
    assert info.video_codec == "h264"
    assert info.has_audio_stream is False, "original stock-clip audio must be stripped"
    # Only the normalized output remains -- the raw source download is cleaned up.
    leftovers = [p.name for p in result.local_path.parent.iterdir() if p.name != "asset.mp4"]
    assert leftovers == []


def test_download_missing_ffmpeg_is_blocked_dependency(tmp_path, monkeypatch) -> None:
    import reel_harness.providers.pexels_stock_media as mod

    monkeypatch.setattr(
        mod, "check_ffmpeg_available",
        lambda: type(DEPS)(ffmpeg=type(DEPS.ffmpeg)("ffmpeg", None, None, "not_found"), ffprobe=DEPS.ffprobe),
    )
    provider = _provider(_search_ok, download_handler=lambda r: httpx.Response(200))
    with pytest.raises(DependencyError):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_401_raises_auth_error(tmp_path) -> None:
    provider = _provider(_search_ok, download_handler=lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_empty_body_is_transient_and_cleans_up_temp(tmp_path) -> None:
    dest = tmp_path / "scene_0"
    provider = _provider(_search_ok, download_handler=lambda r: httpx.Response(200, content=b""))
    with pytest.raises(TransientProviderError):
        provider.download(_CANDIDATE, dest)
    assert list(dest.iterdir()) == [], "no partial download may be left behind"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_html_error_page_is_transient(tmp_path) -> None:
    provider = _provider(
        _search_ok,
        download_handler=lambda r: httpx.Response(200, content=b"<html>rate limited</html>",
                                                    headers={"content-type": "text/html"}),
    )
    with pytest.raises(TransientProviderError):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_corrupted_video_fails_ffprobe(tmp_path) -> None:
    provider = _provider(
        _search_ok,
        download_handler=lambda r: httpx.Response(200, content=b"not-a-real-mp4-container",
                                                    headers={"content-type": "video/mp4"}),
    )
    with pytest.raises(TransientProviderError):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_audio_only_file_rejected(tmp_path) -> None:
    body = _mp4_bytes(tmp_path, duration=1.0, video=False, with_audio=True)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})

    provider = _provider(_search_ok, download_handler)
    with pytest.raises(TransientProviderError):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_content_length_over_limit_rejected_before_streaming(tmp_path) -> None:
    body = _mp4_bytes(tmp_path, duration=1.0)

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body,
            headers={"content-type": "video/mp4", "content-length": str(10 * 1024 * 1024 * 1024)},
        )

    provider = _provider(_search_ok, download_handler)
    with pytest.raises(TransientProviderError, match="byte limit"):
        provider.download(_CANDIDATE, tmp_path)


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_streaming_over_limit_rejected_mid_stream(tmp_path) -> None:
    body = _mp4_bytes(tmp_path, width=640, height=480, duration=2.0)
    assert len(body) > 1024

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})

    provider = _provider(_search_ok, download_handler, max_asset_bytes=1024)
    dest = tmp_path / "scene_capped"
    with pytest.raises(TransientProviderError, match="byte limit"):
        provider.download(_CANDIDATE, dest)
    assert list(dest.iterdir()) == [], "no partial download may be left behind"


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg/ffprobe")
def test_download_follows_https_redirect_then_succeeds(tmp_path) -> None:
    body = _mp4_bytes(tmp_path, duration=1.0)
    calls: list[str] = []

    def download_handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url).endswith("4242-hd.mp4"):
            return httpx.Response(302, headers={"location": "https://cdn.example.invalid/final.mp4"})
        return httpx.Response(200, content=body, headers={"content-type": "video/mp4"})

    provider = _provider(_search_ok, download_handler)
    result = provider.download(_CANDIDATE, tmp_path)
    assert len(calls) == 2
    assert result.checksum_sha256 == hashlib.sha256(result.local_path.read_bytes()).hexdigest()


def test_download_redirect_to_non_https_scheme_rejected(tmp_path) -> None:
    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    provider = _provider(_search_ok, download_handler)
    with pytest.raises(TransientProviderError, match="scheme"):
        provider.download(_CANDIDATE, tmp_path)


def test_download_redirect_loop_hits_limit(tmp_path) -> None:
    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://cdn.example.invalid/still-redirecting"})

    provider = _provider(_search_ok, download_handler)
    with pytest.raises(TransientProviderError, match="redirect"):
        provider.download(_CANDIDATE, tmp_path)


def test_api_key_never_appears_in_any_raised_exception(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    provider = _provider(handler, download_handler=lambda r: httpx.Response(401))
    with pytest.raises(ProviderAuthError) as search_exc:
        provider.search("cats", orientation="portrait", min_duration=1.0)
    assert FAKE_KEY not in str(search_exc.value)
    with pytest.raises(ProviderAuthError) as dl_exc:
        provider.download(_CANDIDATE, tmp_path)
    assert FAKE_KEY not in str(dl_exc.value)
