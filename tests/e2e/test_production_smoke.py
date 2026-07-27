from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from reel_harness.media import ffprobe_validate
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.pipeline import stages
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider
from reel_harness.providers.fake_tts import FakeTTSProvider

FFMPEG_PRESENT = check_ffmpeg_available().all_available

SMOKE_WIDTH = 1080
SMOKE_HEIGHT = 1920
MIN_DURATION_SEC = 3 * 1.5  # 3 scenes, FakeTTSProvider's floor is 1.5s/scene
MAX_DURATION_SEC = 60.0


def _has_faststart(video_path) -> bool:
    """With `-movflags +faststart`, the `moov` atom is written before `mdat`
    so playback can start before the whole file downloads. Checking which
    4-byte atom marker appears first in the file is a real, simple check
    without needing a full MP4 box parser."""
    data = video_path.read_bytes()
    moov_pos = data.find(b"moov")
    mdat_pos = data.find(b"mdat")
    assert moov_pos != -1
    assert mdat_pos != -1
    return moov_pos < mdat_pos


@pytest.mark.skipif(not FFMPEG_PRESENT, reason="production-smoke requires a real ffmpeg/ffprobe binary")
def test_production_smoke_1080x1920(storage, tmp_path) -> None:
    job = SimpleNamespace(
        id=str(uuid.uuid4()),
        script={
            "scenes": [
                {
                    "voiceover": f"Scene {i} voiceover for the production smoke test.",
                    "subtitle": f"Scene {i}",
                    "visual_query": f"smoke test scene {i}",
                    "duration_hint_sec": 4.0,
                }
                for i in range(3)
            ],
        },
    )

    assets = stages.run_asset_fetching(job, FakeStockMediaProvider(), storage)
    tts_results = stages.run_tts_generating(job, FakeTTSProvider(), storage)

    render = stages.run_rendering(job, assets, tts_results, storage, width=SMOKE_WIDTH, height=SMOKE_HEIGHT)
    assert render.video_path.exists()
    assert (render.width, render.height) == (SMOKE_WIDTH, SMOKE_HEIGHT)
    assert render.ffmpeg_version is not None

    validation = stages.run_validating(
        job, render.video_path, expected_width=SMOKE_WIDTH, expected_height=SMOKE_HEIGHT,
    )
    assert (validation.width, validation.height) == (SMOKE_WIDTH, SMOKE_HEIGHT)
    assert validation.video_codec == "h264"
    assert validation.audio_codec == "aac"
    assert validation.has_audio_stream is True
    assert MIN_DURATION_SEC <= validation.duration_sec <= MAX_DURATION_SEC
    assert _has_faststart(render.video_path) is True

    # Persist the raw ffprobe JSON for the record.
    deps = check_ffmpeg_available()
    ffprobe_result = run(ffprobe_validate.build_ffprobe_argv(deps.ffprobe.path, render.video_path), timeout=30)
    report_path = tmp_path / "production_smoke_ffprobe.json"
    report_path.write_text(ffprobe_result.stdout, encoding="utf-8")
    assert json.loads(ffprobe_result.stdout)["streams"]
