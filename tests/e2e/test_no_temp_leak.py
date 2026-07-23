from __future__ import annotations

from reel_harness.worker.runner import run_job


def _snapshot_files(path):
    return {p for p in path.rglob("*") if p.is_file()}


def test_full_run_writes_only_inside_job_storage_root(
    job_service, channel, session_factory, storage, fake_providers, tmp_path,
) -> None:
    outside_scratch = tmp_path / "not-storage"
    outside_scratch.mkdir()
    before = _snapshot_files(outside_scratch)

    job, _ = job_service.create_job(channel.id, idempotency_key="leak-1", topic="topic")
    with session_factory() as session:
        db_job = session.get(type(job), job.id)
        run_job(session, db_job, channel, fake_providers, storage)

    after = _snapshot_files(outside_scratch)
    assert before == after, "pipeline run must never write outside the job storage root"

    for path in storage.job_dir(job.id).rglob("*"):
        if path.is_file():
            assert path.is_relative_to(storage.root_dir)
