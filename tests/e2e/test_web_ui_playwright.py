"""Real-browser E2E for the web UI: a real `reel-harness serve` subprocess
(same pattern as test_supervisor_subprocess_e2e.py) driven by a real
Chromium instance via Playwright. Requires `uv sync --extra e2e-browser`
plus `playwright install chromium` -- skipped entirely otherwise, mirroring
the ffmpeg/demo-tts skipif convention used throughout this suite."""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

from reel_harness.media.deps import check_ffmpeg_available
from reel_harness.providers.demo_tts import check_demo_tts_available

try:
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORTABLE = True
except ImportError:
    PLAYWRIGHT_IMPORTABLE = False


def _chromium_available() -> bool:
    if not PLAYWRIGHT_IMPORTABLE:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - any launch failure means "not available", not a test failure
        return False


FFMPEG_PRESENT = check_ffmpeg_available().all_available
DEMO_TTS_STATUS = check_demo_tts_available()
CHROMIUM_PRESENT = _chromium_available()
pytestmark = pytest.mark.skipif(
    not (FFMPEG_PRESENT and DEMO_TTS_STATUS.available and CHROMIUM_PRESENT),
    reason=(
        "requires real ffmpeg, a local TTS engine, and a Playwright chromium install "
        "(uv sync --extra e2e-browser && playwright install chromium): "
        f"ffmpeg={FFMPEG_PRESENT} demo_tts={DEMO_TTS_STATUS.available} chromium={CHROMIUM_PRESENT}"
    ),
)

_STARTUP_TIMEOUT = 20.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect((host, port))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def test_full_demo_job_flow_in_a_real_browser(tmp_path) -> None:
    db_path = tmp_path / "rh.db"
    jobs_dir = tmp_path / "jobs"
    creds_dir = tmp_path.parent / f"{tmp_path.name}-playwright-creds"
    database_url = f"sqlite:///{db_path}"

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["JOBS_DIR"] = str(jobs_dir)
    env["REEL_HARNESS_CREDENTIAL_DIR"] = str(creds_dir)
    env["APP_API_KEY"] = "a-real-non-placeholder-playwright-key"
    env["REEL_HARNESS_LLM_PROVIDER"] = "demo"
    env["REEL_HARNESS_TTS_PROVIDER"] = "demo"
    env["REEL_HARNESS_ASSET_PROVIDER"] = "demo"
    env["REEL_HARNESS_RENDER_BURN_SUBTITLES"] = "true"
    deps = check_ffmpeg_available()
    if deps.ffmpeg.path:
        env["REEL_HARNESS_FFMPEG_PATH"] = str(deps.ffmpeg.path)
    if deps.ffprobe.path:
        env["REEL_HARNESS_FFPROBE_PATH"] = str(deps.ffprobe.path)

    port = _free_port()
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "reel_harness.cli.main", "serve",
            "--host", "127.0.0.1", "--port", str(port), "--render-workers", "1", "--publisher-workers", "1",
        ],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **popen_kwargs,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        assert _wait_for_port("127.0.0.1", port, _STARTUP_TIMEOUT), "serve subprocess never opened its API port"

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                # 1. Dashboard loads, and offers the short-form queue.
                #    The home page now leads with Fable, so the job
                #    pipeline is reached through the nav rather than from
                #    a hero button.
                page.goto(base_url + "/")
                assert page.get_by_role("link", name="숏폼 작업").first.is_visible()

                # 2. Create a real Demo job via the form.
                page.goto(base_url + "/jobs/new")
                page.fill("#topic", "겨울철 실내 운동 루틴 추천")
                page.click('button[type="submit"]')
                page.wait_for_url(base_url + "/jobs/*")

                # 3/4. Observe status changes to completion (self-terminating poll).
                page.wait_for_selector("text=검수가 필요합니다", timeout=60_000)

                # 5. The video element is present and has a playable source.
                video = page.locator("video.job-video")
                assert video.is_visible()
                video_src = video.get_attribute("src")
                assert video_src and "/video" in video_src

                # 6. Download link works (HEAD-equivalent check via response status).
                download_response = page.request.get(base_url + video_src + "?download=1")
                assert download_response.status == 200
                assert "attachment" in download_response.headers.get("content-disposition", "")

                # 7. Job list shows the job.
                page.goto(base_url + "/jobs")
                assert page.get_by_text("겨울철 실내 운동 루틴 추천").first.is_visible()
            finally:
                browser.close()
    finally:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def _seed_eligible_job(database_url: str, jobs_dir, key: str, topic: str) -> str:
    """Seeds a COMPLETED job with a publishable (non-Demo/Fake-licensed)
    manifest+asset directly against the on-disk SQLite file the `serve`
    subprocess will read -- the same technique
    tests/integration/test_web_publish_lifecycle.py uses, necessary here
    too since neither Demo nor Real pipeline output is usable in an E2E
    test (Demo is permanently publish-ineligible; Real would need a
    genuine network call). Called BEFORE the server subprocess starts, so
    there is no concurrent-writer race with it."""
    import hashlib
    from datetime import UTC, datetime

    from reel_harness.bootstrap import AppContext
    from reel_harness.config import Settings
    from reel_harness.core.state_machine import JobStatus
    from reel_harness.core.state_machine import apply_transition as apply_job_transition
    from reel_harness.db.models import Asset, Job
    from reel_harness.manifest.schema import ApprovalInfo, AssetInfo, LLMInfo, Manifest, TTSInfo, ValidationInfo
    from reel_harness.manifest.writer import write_manifest

    ctx = AppContext(settings=Settings(database_url=database_url, jobs_dir=jobs_dir))
    channel = ctx.jobs.create_channel(name=f"c-{key}", niche="n", language="en")
    job, _ = ctx.jobs.create_job(channel.id, idempotency_key=key, topic=topic)

    deps = check_ffmpeg_available()
    final_path = ctx.storage.job_dir(job.id) / "final" / "final.mp4"
    final_path.parent.mkdir(parents=True, exist_ok=True)
    argv = [
        str(deps.ffmpeg.path), "-y",
        "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=25",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        "-movflags", "+faststart",
        str(final_path),
    ]
    from reel_harness.media.runner import run as run_ffmpeg

    result = run_ffmpeg(argv, timeout=30)
    assert result.returncode == 0, result.stderr
    checksum = hashlib.sha256(final_path.read_bytes()).hexdigest()

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
    return job.id


def test_publish_setup_flow_in_a_real_browser(tmp_path) -> None:
    """Real-browser confirmation that a publish-eligible job's Job Detail
    page shows a working "게시하기" link, that the publish-setup page
    correctly renders each platform's real readiness state (all three show
    disabled/"연결 필요" here, since no OAuth client is configured in this
    test environment), and that its link to /publisher-accounts works.
    Does not exercise a real OAuth redirect (would need a fake OAuth
    provider server, disproportionate effort for this test) or drive a
    publication all the way to PUBLISHED in-browser (already covered,
    including the fake-publisher full upload/processing cycle, by
    tests/integration/test_web_publish_lifecycle.py) -- this test's value
    is confirming the actual rendered HTML/navigation, not re-testing
    business logic a lower layer already covers."""
    db_path = tmp_path / "rh.db"
    jobs_dir = tmp_path / "jobs"
    creds_dir = tmp_path.parent / f"{tmp_path.name}-publish-playwright-creds"
    database_url = f"sqlite:///{db_path}"

    job_id = _seed_eligible_job(database_url, jobs_dir, "publish-e2e-1", "실전 게시 테스트 영상")

    env = os.environ.copy()
    env["DATABASE_URL"] = database_url
    env["JOBS_DIR"] = str(jobs_dir)
    env["REEL_HARNESS_CREDENTIAL_DIR"] = str(creds_dir)
    env["APP_API_KEY"] = "a-real-non-placeholder-playwright-key"
    deps = check_ffmpeg_available()
    if deps.ffmpeg.path:
        env["REEL_HARNESS_FFMPEG_PATH"] = str(deps.ffmpeg.path)
    if deps.ffprobe.path:
        env["REEL_HARNESS_FFPROBE_PATH"] = str(deps.ffprobe.path)

    port = _free_port()
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        [
            sys.executable, "-m", "reel_harness.cli.main", "serve",
            "--host", "127.0.0.1", "--port", str(port), "--render-workers", "1", "--publisher-workers", "1",
        ],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **popen_kwargs,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        assert _wait_for_port("127.0.0.1", port, _STARTUP_TIMEOUT), "serve subprocess never opened its API port"

        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            try:
                # 1. Job Detail shows a working publish link.
                page.goto(base_url + f"/jobs/{job_id}")
                publish_link = page.get_by_role("link", name="게시하기")
                assert publish_link.is_visible()
                publish_link.click()
                page.wait_for_url(base_url + f"/jobs/{job_id}/publish")

                # 2. Every platform renders its real (unconfigured) state.
                assert page.get_by_text("YouTube").first.is_visible()
                assert page.get_by_text("TikTok").first.is_visible()
                assert page.get_by_text("Instagram Reels").first.is_visible()
                assert page.get_by_text("연결 필요").first.is_visible()

                # 3. Its link to the accounts screen actually works.
                page.get_by_role("link", name="계정 연결").first.click()
                page.wait_for_url(base_url + "/publisher-accounts")
                assert page.get_by_text("미설정").first.is_visible()
            finally:
                browser.close()
    finally:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.send_signal(signal.SIGINT)
        try:
            proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
