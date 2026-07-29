"""cli.main._make_console_encoding_safe: found via a real live-smoke
attempt on this machine's cp949-codepage Windows console -- printing
"NOT RUN -- credentials not configured" (an em dash) crashed with
UnicodeEncodeError instead of reporting cleanly. No network."""
from __future__ import annotations

import io
import sys

from reel_harness.cli.main import _make_console_encoding_safe

_EM_DASH_MESSAGE = "NOT RUN — credentials not configured"


def test_a_restrictive_codepage_stream_really_does_crash_without_the_fix() -> None:
    """Negative control: proves this test setup reproduces the actual
    failure mode (not a false negative) before showing the fix avoids it."""
    buffer = io.BytesIO()
    restrictive_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
    try:
        print(_EM_DASH_MESSAGE, file=restrictive_stdout)
        restrictive_stdout.flush()
    except UnicodeEncodeError:
        return
    raise AssertionError("expected UnicodeEncodeError to reproduce the real bug -- test setup is wrong")


def test_console_encoding_safe_avoids_the_crash(monkeypatch) -> None:
    buffer = io.BytesIO()
    restrictive_stdout = io.TextIOWrapper(buffer, encoding="ascii", errors="strict")
    monkeypatch.setattr(sys, "stdout", restrictive_stdout)

    _make_console_encoding_safe()
    print(_EM_DASH_MESSAGE)  # must not raise
    sys.stdout.flush()

    assert b"NOT RUN" in buffer.getvalue()


def test_console_encoding_safe_is_a_no_op_on_a_stream_without_reconfigure() -> None:
    """capsys and similar test/harness stand-ins for sys.stdout may not be a
    real TextIOWrapper -- must never raise just because reconfigure() isn't
    available."""

    class _NoReconfigure:
        def write(self, text: str) -> int:
            return len(text)

    stream = _NoReconfigure()
    assert not hasattr(stream, "reconfigure")
    import reel_harness.cli.main as cli_main

    original_stdout, original_stderr = sys.stdout, sys.stderr
    try:
        sys.stdout = stream  # type: ignore[assignment]
        sys.stderr = stream  # type: ignore[assignment]
        cli_main._make_console_encoding_safe()  # must not raise
    finally:
        sys.stdout, sys.stderr = original_stdout, original_stderr
