"""Full Demo Mode lifecycle driven through the web UI's own HTTP routes
(not JobService/run_job called directly) -- the closest thing to a real
browser session this test suite can exercise without an actual browser.
Requires real ffmpeg + a local TTS engine (Demo Mode's whole point), same
skipif convention as tests/e2e/test_demo_pipeline_e2e.py."""
from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.db.schema import create_engine_from_url, make_session_factory
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.demo_tts import check_demo_tts_available
from reel_harness.worker.runner import run_job

FFMPEG_PRESENT = check_ffmpeg_available().all_available
DEMO_TTS_STATUS = check_demo_tts_available()
pytestmark = pytest.mark.skipif(
    not (FFMPEG_PRESENT and DEMO_TTS_STATUS.available),
    reason=f"requires real ffmpeg and a local TTS engine: {DEMO_TTS_STATUS.detail}",
)

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_IDEMPOTENCY_INPUT_RE = re.compile(r'name="idempotency_key" value="([^"]+)"')


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'web-lifecycle.db'}",
        jobs_dir=tmp_path / "jobs",
        app_api_key="a-real-non-placeholder-test-key",
    )
    return AppContext(settings=settings)


def _drive_job_to_completion(ctx: AppContext, job_id: str) -> None:
    """Manually runs the real pipeline for this job -- no background worker
    thread exists in this TestClient-based test, so this stands in for
    what `reel-harness serve`'s render-worker thread would otherwise do."""
    with ctx.session_factory() as session:
        from reel_harness.db.models import Channel, Job

        db_job = session.get(Job, job_id)
        channel = session.get(Channel, db_job.channel_id)
        run_job(session, db_job, channel, ctx.providers_for_job(db_job), ctx.storage)


def test_full_demo_lifecycle_through_web_routes(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        client = TestClient(app)

        # 1. Load the New Job form (sets the CSRF cookie, generates an
        #    idempotency key) and submit it with the Demo provider profile.
        form_page = client.get("/jobs/new")
        assert form_page.status_code == 200
        csrf_token = _CSRF_INPUT_RE.search(form_page.text).group(1)
        idempotency_key = _IDEMPOTENCY_INPUT_RE.search(form_page.text).group(1)

        create_response = client.post("/jobs", data={
            "topic": "김치찌개 맛있게 끓이는 법", "language": "ko", "duration_seconds": 30,
            "style": "cooking", "provider_profile": "demo", "burn_subtitles": "true",
            "idempotency_key": idempotency_key, "csrf_token": csrf_token,
        }, follow_redirects=False)
        assert create_response.status_code == 303
        job_url = create_response.headers["location"]
        job_id = job_url.rstrip("/").rsplit("/", 1)[-1]

        # 2. Before the pipeline runs, the status fragment must show the
        #    still-polling (non-terminal) markup.
        initial_status = client.get(f"/jobs/{job_id}/status")
        assert initial_status.status_code == 200
        assert "hx-trigger" in initial_status.text  # still actively polling

        # 3. Actually run the pipeline (stands in for the real worker thread).
        _drive_job_to_completion(ctx, job_id)

        # 4. After completion, the status fragment must show REVIEW_REQUIRED,
        #    a video element, and no more polling trigger (self-terminated).
        review_status = client.get(f"/jobs/{job_id}/status")
        assert review_status.status_code == 200
        assert "검수가 필요합니다" in review_status.text
        assert f"/jobs/{job_id}/video" in review_status.text
        assert "hx-trigger" not in review_status.text  # polling stopped

        # 5. The video actually streams and matches the real on-disk bytes.
        on_disk = ctx.storage.read_bytes(job_id, "final/final.mp4")
        video_response = client.get(f"/jobs/{job_id}/video")
        assert video_response.status_code == 200
        assert video_response.content == on_disk
        assert len(on_disk) > 0

        # 6. Approve via the web route, confirm the completed state.
        approve_response = client.post(
            f"/jobs/{job_id}/approve", headers={"X-CSRF-Token": csrf_token},
        )
        assert approve_response.status_code == 200
        assert "영상이 완성되었습니다" in approve_response.text
        assert "Demo Mode" in approve_response.text  # non-publishable banner

        # 7. State survives a fresh session_factory (restart-persistence).
        restarted_session_factory = make_session_factory(create_engine_from_url(ctx.settings.database_url))
        with restarted_session_factory() as session:
            from reel_harness.db.models import Job

            restored = session.get(Job, job_id)
            assert restored.status == "COMPLETED"

        # 8. The job now shows up in the job list page too.
        list_response = client.get("/jobs")
        assert "김치찌개" in list_response.text
    finally:
        app.dependency_overrides.clear()
