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
                # 1. Dashboard loads.
                page.goto(base_url + "/")
                assert page.get_by_text("새 영상 만들기").first.is_visible()

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
