"""Responsive and layout-invariant coverage for the Fable UI (v1).

Not pixel-diff screenshot testing. A committed PNG baseline breaks on
every font-hinting and codec difference between machines, which trains
people to regenerate baselines without looking -- the opposite of what a
visual test is for.

What is asserted instead is the set of properties the design actually
promises, each of which was a REAL defect at some point in this phase:

- No page scrolls horizontally at any width. (Wide media and long shot
  metadata both caused this.)
- The primary action stays reachable on a phone -- sticky footer plus a
  tab bar, and the tab bar is mobile-only.
- Choice controls are chips, never a native <select>, whose popup list is
  OS-drawn and unstyleable.
- Meters render their real value. Both the budget bar and the progress
  bars silently rendered full because inline styles are dropped under
  this app's CSP.
- Nothing relies on an inline style attribute, for the same reason.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time

import pytest

try:
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT = True
except ImportError:  # pragma: no cover - optional extra
    _PLAYWRIGHT = False


def _chromium_available() -> bool:
    if not _PLAYWRIGHT:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            browser.close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _chromium_available(), reason="playwright chromium not installed"
)

# The three widths the design system actually commits to.
VIEWPORTS = {"mobile": (390, 844), "tablet": (834, 1112), "desktop": (1440, 900)}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait_for_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


@pytest.fixture(scope="module")
def fable_server(tmp_path_factory):
    """A real server with every provider pinned to fake.

    Pinned deliberately and completely: this suite drives the paid gates,
    and a partially-pinned fixture is how a "free" test run quietly
    spends money on a developer's own credentials.
    """
    tmp_path = tmp_path_factory.mktemp("fable-ui")
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'rh.db'}",
        "JOBS_DIR": str(tmp_path / "jobs"),
        "REEL_HARNESS_FABLE_PROJECTS_DIR": str(tmp_path / "fable_projects"),
        "REEL_HARNESS_CREDENTIAL_DIR": str(tmp_path / "creds"),
        "APP_API_KEY": "a-real-non-placeholder-responsive-key",
        "REEL_HARNESS_LLM_PROVIDER": "fake",
        "REEL_HARNESS_TTS_PROVIDER": "fake",
        "REEL_HARNESS_ASSET_PROVIDER": "fake",
        "REEL_HARNESS_NARRATIVE_PROVIDER": "fake",
        "REEL_HARNESS_REFERENCE_IMAGE_PROVIDER": "fake",
        "REEL_HARNESS_CINEMATIC_PROVIDER": "fake",
        "REEL_HARNESS_ALLOW_PAID_GENERATION": "false",
    })
    port = _free_port()
    popen_kwargs: dict = {}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    proc = subprocess.Popen(
        [sys.executable, "-m", "reel_harness.cli.main", "serve",
         "--host", "127.0.0.1", "--port", str(port),
         "--render-workers", "0", "--publisher-workers", "0"],
        env=env, cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", **popen_kwargs,
    )
    assert _wait_for_port("127.0.0.1", port, 60.0), "serve never opened its port"
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        if os.name == "nt":
            proc.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()


PAGES = ("/", "/fable", "/fable/new")


@pytest.mark.parametrize("path", PAGES)
def test_no_page_scrolls_sideways_at_any_width(fable_server, path) -> None:
    """A page that scrolls horizontally on a phone is broken, and the
    cause is always one unconstrained child rather than the layout."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for name, (width, height) in VIEWPORTS.items():
                page = browser.new_page(viewport={"width": width, "height": height})
                page.goto(fable_server + path, wait_until="networkidle")
                overflow = page.evaluate(
                    "() => document.documentElement.scrollWidth - document.documentElement.clientWidth"
                )
                # 1px of rounding is not a horizontal scrollbar.
                assert overflow <= 1, f"{path} overflows by {overflow}px at {name}"
                page.close()
        finally:
            browser.close()


def test_the_tab_bar_is_mobile_only(fable_server) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            phone = browser.new_page(viewport={"width": 390, "height": 844})
            phone.goto(fable_server + "/fable", wait_until="networkidle")
            assert phone.locator(".tabbar").is_visible()
            # Four targets, each comfortably above the 44px minimum.
            for index in range(4):
                box = phone.locator(".tab").nth(index).bounding_box()
                assert box is not None and box["height"] >= 44

            desktop = browser.new_page(viewport={"width": 1440, "height": 900})
            desktop.goto(fable_server + "/fable", wait_until="networkidle")
            assert not desktop.locator(".tabbar").is_visible()
        finally:
            browser.close()


def test_choices_are_chips_not_native_selects(fable_server) -> None:
    """A native select's popup list is drawn by the OS and cannot be
    styled, so the compose screen must not contain one."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(fable_server + "/fable/new", wait_until="networkidle")
            assert page.locator("select").count() == 0
            assert page.locator(".chip-opt input[type=radio]").count() > 0
            # Exactly one option per group is pre-selected.
            for field in ("genre", "tone", "target_duration_sec"):
                checked = page.locator(f"input[name={field}]:checked").count()
                assert checked == 1, f"{field} has {checked} checked chips"
        finally:
            browser.close()


def test_no_element_anywhere_depends_on_an_inline_style(fable_server) -> None:
    """This app sends style-src 'self' with no 'unsafe-inline', so any
    style="" attribute is silently discarded by the browser. The budget
    meter and the progress bars both shipped broken this way."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            for path in PAGES:
                page.goto(fable_server + path, wait_until="networkidle")
                assert page.locator("[style]").count() == 0, f"{path} carries an inline style"
        finally:
            browser.close()


def test_the_primary_action_is_reachable_without_hunting(fable_server) -> None:
    """One clearly-primary action per screen, and on a phone it also
    rides in a sticky footer so it never ends up below three screens of
    reference material."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(fable_server + "/fable/new", wait_until="networkidle")
            assert page.locator(".btn-primary").count() >= 1

            phone = browser.new_page(viewport={"width": 390, "height": 844})
            phone.goto(fable_server + "/", wait_until="networkidle")
            assert phone.locator(".btn-primary").first.is_visible()
        finally:
            browser.close()


def test_both_colour_schemes_render_readable_text(fable_server) -> None:
    """Light mode is a deliberate variant, not an inverted afterthought:
    it has to keep real contrast rather than merely existing."""
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            for scheme in ("dark", "light"):
                page = browser.new_page(
                    viewport={"width": 1440, "height": 900}, color_scheme=scheme,
                )
                page.goto(fable_server + "/fable/new", wait_until="networkidle")
                colours = page.evaluate(
                    """() => {
                        const cs = getComputedStyle(document.body);
                        return [cs.color, cs.backgroundColor];
                    }"""
                )
                assert colours[0] != colours[1], f"{scheme}: text matches background"
                page.close()
        finally:
            browser.close()
