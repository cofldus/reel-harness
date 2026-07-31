"""Full publish lifecycle driven through the web UI's own HTTP routes (not
PublicationService called directly) -- the closest thing to a real browser
session this test suite can exercise without an actual browser, mirroring
test_web_demo_job_lifecycle.py's shape.

Uses the real `fake` PUBLISHER provider (providers/fake_publisher.py, needs
no OAuth account, no network call) to drive an actual upload/processing
cycle -- distinct from the `fake` PIPELINE provider tier (permanently
publish-ineligible via NON_PUBLISHABLE_LICENSES). Since neither Demo nor
Fake pipeline output can ever become publish-eligible, and running the Real
pipeline would require real network calls forbidden in tests, the COMPLETED
job here is seeded directly (a publishable manifest+asset written straight
to disk) rather than produced by the actual generation pipeline -- the same
technique tests/unit/test_publication_service.py's own fixture uses."""
from __future__ import annotations

import hashlib
import re

import pytest
from fastapi.testclient import TestClient

from reel_harness.api.app import app, get_context
from reel_harness.bootstrap import AppContext
from reel_harness.config import Settings
from reel_harness.core.state_machine import JobStatus
from reel_harness.core.state_machine import apply_transition as apply_job_transition
from reel_harness.db.models import Asset, Job
from reel_harness.db.schema import create_engine_from_url, make_session_factory
from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
from reel_harness.manifest.writer import write_manifest
from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.media.runner import run

FFMPEG_PRESENT = check_ffmpeg_available().all_available
pytestmark = pytest.mark.skipif(not FFMPEG_PRESENT, reason="requires real ffmpeg to build a faststart mp4")

_CSRF_INPUT_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def _make_ctx(tmp_path) -> AppContext:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'publish-lifecycle.db'}",
        jobs_dir=tmp_path / "jobs",
        credential_dir=tmp_path / "credentials",
        app_api_key="a-real-non-placeholder-test-key",
    )
    return AppContext(settings=settings)


def _faststart_mp4_bytes(tmp_path, seed: str) -> bytes:
    deps = check_ffmpeg_available()
    out = tmp_path / f"eligible-{seed}.mp4"
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(out),
    ]
    result = run(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    return out.read_bytes()


def _seed_eligible_job(ctx: AppContext, tmp_path, key: str, topic: str) -> Job:
    from datetime import UTC, datetime

    channel = ctx.jobs.create_channel(name=f"c-{key}", niche="n", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key=key, topic=topic)
    video_bytes = _faststart_mp4_bytes(tmp_path, key)
    final_path = ctx.storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    final_path.write_bytes(video_bytes)
    checksum = hashlib.sha256(video_bytes).hexdigest()

    with ctx.session_factory() as session:
        db_job = session.get(Job, job.id)
        db_job.script = {"title": "T", "llm_provider_id": "fake", "llm_model_id": "m", "prompt_version": "v"}
        apply_job_transition(db_job, JobStatus.SCRIPT_GENERATING)
        apply_job_transition(db_job, JobStatus.POLICY_CHECKING)
        apply_job_transition(db_job, JobStatus.ASSET_FETCHING)
        apply_job_transition(db_job, JobStatus.TTS_GENERATING)
        apply_job_transition(db_job, JobStatus.RENDERING)
        apply_job_transition(db_job, JobStatus.VALIDATING)
        apply_job_transition(db_job, JobStatus.REVIEW_REQUIRED, reason_code="USER_APPROVAL_REQUIRED")
        apply_job_transition(db_job, JobStatus.READY)
        apply_job_transition(db_job, JobStatus.COMPLETED)
        session.add(Asset(
            job_id=job.id, scene_index=0, source_provider="pexels", local_path=str(final_path),
            checksum_sha256=checksum, mime_type="video/mp4", license_type="CC-BY-4.0",
            commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
        ))
        session.commit()

    manifest = Manifest(
        job_id=job.id, created_at=datetime.now(UTC), topic=topic, script_title="T",
        llm=LLMInfo(provider_id="fake", model_id="m", prompt_version="v"),
        tts=TTSInfo(provider_id="fake", voice_id="v1"),
        assets=[AssetInfo(
            scene_index=0, source_url="https://example.invalid/page", author="Creator",
            license_type="CC-BY-4.0", checksum_sha256=checksum,
            commercial_use_allowed=True, modification_allowed=True, attribution_text="Photo by Creator",
        )],
        validation=ValidationInfo(duration_sec=5.0, video_codec="h264", audio_codec="aac", has_audio_stream=True),
        final_video_checksum_sha256=checksum,
        approval=ApprovalInfo(decision="approve", decided_at=datetime.now(UTC)),
    )
    write_manifest(ctx.storage, job.id, manifest)
    return ctx.jobs.get_job(job.id)


def _drive_publication_forward(ctx: AppContext, max_cycles: int = 5) -> None:
    """Stands in for the real publisher-worker thread/`publisher-run`
    daemon -- no background worker exists in this TestClient-based test.
    Mirrors cmd_publisher_run_once's exact lease/run/release call shape,
    looped: a fresh READY_TO_UPLOAD publication needs one cycle to reach
    PROCESSING and a second to reach PUBLISHED (run_publication's own
    docstring: "a later publication-refresh call advances PROCESSING ->
    PUBLISHED")."""
    from reel_harness.worker.publish_lease import lease_next_via_lanes, release_publication_lease
    from reel_harness.worker.publish_runner import run_publication

    for _ in range(max_cycles):
        with ctx.session_factory() as session:
            from reel_harness.db.models import Job

            publication = lease_next_via_lanes(session, worker_id="test-publish-worker")
            if publication is None:
                return
            lease_token = publication.lease_token
            job = session.get(Job, publication.job_id)
            channel_niche = ctx.channel_niche_for_job(job)
            bundle = ctx.bundle_for_publication(publication)
            try:
                run_publication(
                    session, publication, ctx.storage, bundle,
                    channel_niche=channel_niche, lease_token=lease_token,
                )
            finally:
                release_publication_lease(session, publication, lease_token=lease_token)
                close = getattr(bundle.publisher, "close", None)
                if callable(close):
                    close()


def test_full_publish_lifecycle_through_web_routes(tmp_path) -> None:
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _seed_eligible_job(ctx, tmp_path, "lifecycle-1", topic="라이프사이클 테스트 영상")
        client = TestClient(app)

        # 1. The publish-setup page is reachable (job is COMPLETED+eligible)
        #    and carries a CSRF token.
        setup_page = client.get(f"/jobs/{job.id}/publish")
        assert setup_page.status_code == 200
        csrf_token = _CSRF_INPUT_RE.search(setup_page.text).group(1)

        # 2. Create the publication via the actual web route (not
        #    PublicationService.create_publication called directly).
        create_response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "fake", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        }, follow_redirects=False)
        assert create_response.status_code == 303
        publication_url = create_response.headers["location"]
        publication_id = publication_url.rstrip("/").rsplit("/", 1)[-1]

        # 3. Before the worker runs, the status fragment must show the
        #    still-polling (non-terminal) markup.
        initial_status = client.get(f"/publications/{publication_id}/status")
        assert initial_status.status_code == 200
        assert "hx-trigger" in initial_status.text

        # 4. Actually run the publish worker (stands in for the real
        #    publisher-worker thread) until it reaches a terminal status.
        _drive_publication_forward(ctx)

        # 5. After completion, the status fragment shows PUBLISHED, the
        #    published-video link, and no more polling trigger.
        final_status = client.get(f"/publications/{publication_id}/status")
        assert final_status.status_code == 200
        assert "게시 완료" in final_status.text
        assert "게시된 영상 보기" in final_status.text
        assert "hx-trigger" not in final_status.text

        refreshed = ctx.publications.get_publication(publication_id)
        assert refreshed.status == "PUBLISHED"
        assert refreshed.provider_video_id is not None
        assert refreshed.publication_url is not None

        # 6. The detail page and the list page both reflect this too.
        detail_response = client.get(f"/publications/{publication_id}")
        assert detail_response.status_code == 200
        assert "라이프사이클 테스트 영상" in detail_response.text

        list_response = client.get("/publications")
        assert "라이프사이클 테스트 영상" in list_response.text
        assert "게시 완료" in list_response.text

        # 7. The job detail page now lists this publication too.
        job_detail_response = client.get(f"/jobs/{job.id}")
        assert f"/publications/{publication_id}" in job_detail_response.text

        # 8. State survives a fresh session_factory (restart-persistence).
        restarted_session_factory = make_session_factory(create_engine_from_url(ctx.settings.database_url))
        with restarted_session_factory() as session:
            from reel_harness.db.models import Publication

            restored = session.get(Publication, publication_id)
            assert restored.status == "PUBLISHED"
    finally:
        app.dependency_overrides.clear()


def test_cancel_via_web_route_stops_the_lifecycle_before_upload(tmp_path) -> None:
    """A publication cancelled via the web route before the worker ever
    touches it must never reach PUBLISHED, even if the worker loop runs
    afterward (a cancelled publication is no longer leasable)."""
    ctx = _make_ctx(tmp_path)
    app.dependency_overrides[get_context] = lambda: ctx
    try:
        job = _seed_eligible_job(ctx, tmp_path, "lifecycle-cancel-1", topic="취소 테스트")
        client = TestClient(app)
        setup_page = client.get(f"/jobs/{job.id}/publish")
        csrf_token = _CSRF_INPUT_RE.search(setup_page.text).group(1)

        create_response = client.post(f"/jobs/{job.id}/publications", data={
            "provider": "fake", "account_reference": "default", "privacy_status": "private",
            "csrf_token": csrf_token,
        }, follow_redirects=False)
        publication_id = create_response.headers["location"].rstrip("/").rsplit("/", 1)[-1]

        cancel_response = client.post(
            f"/publications/{publication_id}/cancel", headers={"X-CSRF-Token": csrf_token},
        )
        assert cancel_response.status_code == 200
        assert "취소됨" in cancel_response.text

        _drive_publication_forward(ctx)  # must be a no-op -- nothing left to lease

        refreshed = ctx.publications.get_publication(publication_id)
        assert refreshed.status == "CANCELLED"
    finally:
        app.dependency_overrides.clear()
