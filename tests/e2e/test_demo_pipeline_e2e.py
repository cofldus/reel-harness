from __future__ import annotations

import pytest

from reel_harness.core.state_machine import JobStatus
from reel_harness.manifest.schema import Manifest, is_publish_eligible
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run
from reel_harness.providers.demo_llm import DemoLLMProvider
from reel_harness.providers.demo_stock_media import DEMO_TEST_LICENSE, DemoStockMediaProvider
from reel_harness.providers.demo_tts import DemoTTSProvider, check_demo_tts_available
from reel_harness.worker.runner import ProviderBundle, run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available
DEMO_TTS_STATUS = check_demo_tts_available()
pytestmark = pytest.mark.skipif(
    not (FFMPEG_PRESENT and DEMO_TTS_STATUS.available),
    reason=f"requires real ffmpeg and a local TTS engine: {DEMO_TTS_STATUS.detail}",
)


def _demo_providers(render_burn_subtitles: bool = True) -> ProviderBundle:
    return ProviderBundle(
        llm=DemoLLMProvider(), tts=DemoTTSProvider(), stock_media=DemoStockMediaProvider(),
        render_burn_subtitles=render_burn_subtitles,
    )


def test_demo_pipeline_reaches_review_required_with_real_watchable_output(
    job_service, channel, session_factory, storage,
) -> None:
    """The exact scenario that motivated Demo Mode: a job driven end-to-end
    through demo llm/tts/asset providers must produce a real, audible,
    burned-caption video reaching REVIEW_REQUIRED -- not the Fake provider's
    silent solid-color placeholder."""
    job, _ = job_service.create_job(channel.id, idempotency_key="demo-e2e-1", topic="김치찌개 맛있게 끓이는 법")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, _demo_providers(), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
        assert db_job.reason_code == "USER_APPROVAL_REQUIRED"

    video_path = storage.job_dir(job.id) / "final" / "final.mp4"
    assert video_path.is_file()

    # Real, audible speech -- not FakeTTSProvider's silence. volumedetect's
    # mean_volume on true silence is -91.0dB (the numeric floor for 16-bit
    # PCM); anything meaningfully above that is real signal.
    deps = check_ffmpeg_available()
    result = run(
        [str(deps.ffmpeg.path), "-i", str(video_path), "-af", "volumedetect", "-f", "null", "-"], timeout=30,
    )
    mean_volume_line = next(line for line in result.stderr.splitlines() if "mean_volume" in line)
    mean_volume_db = float(mean_volume_line.split(":")[1].strip().rstrip("dB").strip())
    assert mean_volume_db > -60.0, f"audio track looks silent: {mean_volume_line}"

    # Every asset is stamped DEMO_TEST_LICENSE and the manifest is still
    # never publish-eligible, exactly like the Fake provider's
    # FAKE_TEST_LICENSE invariant (see CLAUDE.md).
    manifest = Manifest.model_validate_json(storage.read_bytes(job.id, "manifest.json"))
    assert manifest.assets
    assert all(asset.license_type == DEMO_TEST_LICENSE for asset in manifest.assets)
    assert is_publish_eligible(manifest) is False


def test_demo_pipeline_without_burn_subtitles_still_reaches_review_required(
    job_service, channel, session_factory, storage,
) -> None:
    """render_burn_subtitles is orthogonal to which providers are selected --
    demo providers work fine with it off too (just without captions)."""
    job, _ = job_service.create_job(channel.id, idempotency_key="demo-e2e-2", topic="quick pasta tips")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, _demo_providers(render_burn_subtitles=False), storage)
        assert db_job.status == JobStatus.REVIEW_REQUIRED.value
    assert (storage.job_dir(job.id) / "final" / "final.mp4").is_file()
