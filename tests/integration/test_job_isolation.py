from __future__ import annotations

from reel_harness.pipeline.stages import run_asset_fetching, run_script_generating
from reel_harness.providers.fake_llm import FakeLLMProvider
from reel_harness.providers.fake_stock_media import FakeStockMediaProvider


def _make_job(job_service, channel, key):
    job, _ = job_service.create_job(channel.id, idempotency_key=key, topic=f"topic-{key}")
    return job


def test_three_concurrent_jobs_get_fully_isolated_directories(job_service, channel, storage) -> None:
    llm = FakeLLMProvider()
    stock_media = FakeStockMediaProvider()
    jobs = [_make_job(job_service, channel, key=f"job-{i}") for i in range(3)]

    for job in jobs:
        run_script_generating(job, channel, llm)
        run_asset_fetching(job, stock_media, storage)

    job_dirs = {storage.job_dir(job.id) for job in jobs}
    assert len(job_dirs) == 3  # no two jobs share a directory

    for job in jobs:
        asset_dir = storage.job_dir(job.id) / "assets"
        assert asset_dir.exists()
        # every file under this job's asset dir must resolve back under this job's dir
        for path in asset_dir.rglob("*"):
            assert path.is_relative_to(storage.job_dir(job.id))
