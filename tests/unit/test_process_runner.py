from __future__ import annotations

import subprocess
import sys
import time

from reel_harness.media.runner import ProcessRunner, run


def test_run_captures_stdout_and_returncode() -> None:
    result = run([sys.executable, "-c", "print('hello')"], timeout=10)
    assert result.returncode == 0
    assert "hello" in result.stdout


def test_run_captures_nonzero_returncode_and_stderr() -> None:
    result = run([sys.executable, "-c", "import sys; sys.stderr.write('boom'); sys.exit(3)"], timeout=10)
    assert result.returncode == 3
    assert "boom" in result.stderr


def test_cancel_kills_a_long_running_process_promptly() -> None:
    runner = ProcessRunner([sys.executable, "-c", "import time; time.sleep(30)"])
    runner.start()
    time.sleep(0.3)  # let the child actually start
    started_at = time.monotonic()
    runner.cancel()
    elapsed = time.monotonic() - started_at
    assert elapsed < 10, "cancel() should terminate the process tree quickly, not wait out the sleep"


def test_timeout_triggers_cancel_and_raises() -> None:
    runner = ProcessRunner([sys.executable, "-c", "import time; time.sleep(30)"])
    runner.start()
    try:
        runner.wait(timeout=0.5)
        raise AssertionError("expected TimeoutExpired")
    except subprocess.TimeoutExpired:
        pass
