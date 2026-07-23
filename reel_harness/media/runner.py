from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass
class RunResult:
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner:
    """Runs an argv list (never a shell string) and can kill the whole process
    tree on cancel/timeout. POSIX and Windows tear down child process trees
    differently, so that difference is encapsulated here instead of leaking into
    every call site that spawns ffmpeg.
    """

    def __init__(self, argv: list[str], cwd: str | None = None) -> None:
        self._argv = argv
        self._cwd = cwd
        self._proc: subprocess.Popen[bytes] | None = None

    def start(self) -> None:
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        self._proc = subprocess.Popen(
            self._argv,
            cwd=self._cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **kwargs,
        )

    def wait(self, timeout: float | None = None) -> RunResult:
        if self._proc is None:
            raise RuntimeError("start() must be called before wait()")
        try:
            stdout, stderr = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.cancel()
            raise
        return RunResult(
            returncode=self._proc.returncode,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )

    def cancel(self) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        pid = self._proc.pid
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], shell=False, capture_output=True)
        else:
            os.killpg(os.getpgid(pid), 9)  # SIGKILL
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass


def run(argv: list[str], cwd: str | None = None, timeout: float | None = None) -> RunResult:
    runner = ProcessRunner(argv, cwd=cwd)
    runner.start()
    return runner.wait(timeout=timeout)
