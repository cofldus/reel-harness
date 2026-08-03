from __future__ import annotations

import subprocess

import pytest

from reel_harness.ops.release_check import run_release_check


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("hello")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=tmp_path, check=True)
    return tmp_path


def test_release_check_reports_clean_tree_for_a_fresh_commit(git_repo) -> None:
    report = run_release_check(git_repo, skip_slow=True)
    checks = {i.name: i for i in report.items}
    assert checks["git_clean"].status == "PASS"


def test_release_check_detects_dirty_working_tree(git_repo) -> None:
    (git_repo / "untracked.txt").write_text("x")
    report = run_release_check(git_repo, skip_slow=True)
    checks = {i.name: i for i in report.items}
    assert checks["git_clean"].status == "FAIL"
    assert report.overall == "FAIL"
    assert report.ready_to_tag is False


def test_release_check_skip_slow_omits_full_test_suite(git_repo) -> None:
    report = run_release_check(git_repo, skip_slow=True)
    names = {i.name for i in report.items}
    assert "full_pytest" not in names
    assert "mypy" not in names
    assert "ruff" not in names


def test_release_check_version_consistency_passes_for_real_project() -> None:
    """Run against the ACTUAL project (not a synthetic git_repo), where
    version consistency is meant to genuinely hold, proving the check
    exercises real pyproject.toml/__version__ reads."""
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[2]
    report = run_release_check(repo_root, skip_slow=True)
    checks = {i.name: i for i in report.items}
    assert checks["version_consistency"].status == "PASS"


def test_release_check_secret_scan_detects_a_real_looking_key(git_repo) -> None:
    (git_repo / "leaky.py").write_text('KEY = "AIzaSyAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
    subprocess.run(["git", "add", "leaky.py"], cwd=git_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "oops"], cwd=git_repo, check=True)
    report = run_release_check(git_repo, skip_slow=True)
    checks = {i.name: i for i in report.items}
    assert checks["secret_scan"].status == "FAIL"
    assert report.ready_to_tag is False


def test_release_check_secret_scan_passes_without_secrets(git_repo) -> None:
    report = run_release_check(git_repo, skip_slow=True)
    checks = {i.name: i for i in report.items}
    assert checks["secret_scan"].status == "PASS"


def test_release_check_report_to_dict_shape(git_repo) -> None:
    report = run_release_check(git_repo, skip_slow=True)
    payload = report.to_dict()
    assert "overall" in payload
    assert "ready_to_tag" in payload
    assert isinstance(payload["items"], list)
    assert all({"name", "status", "detail"} <= set(item.keys()) for item in payload["items"])


def test_release_check_full_pytest_failure_marks_overall_fail(git_repo, monkeypatch) -> None:
    """Exercises the slow-check-inclusion path via a monkeypatched
    subprocess result, without actually spawning a nested pytest run."""
    import reel_harness.ops.release_check as release_check_module

    original_run = release_check_module._run

    def _fake_run(argv, cwd, timeout):
        if argv[:2] == ["uv", "run"] and "pytest" in argv:
            return 1, "1 failed, 5 passed"
        if "mypy" in argv:
            return 0, "Success"
        if "ruff" in argv:
            return 0, "All checks passed!"
        return original_run(argv, cwd, timeout)

    monkeypatch.setattr(release_check_module, "_run", _fake_run)
    report = run_release_check(git_repo, skip_slow=False)
    checks = {i.name: i for i in report.items}
    assert checks["full_pytest"].status == "FAIL"
    assert report.ready_to_tag is False


def test_subprocess_output_is_decoded_as_utf8_not_the_locale_codec(tmp_path) -> None:
    """The gate died on a Korean Windows box before it checked anything.

    `text=True` decodes with the locale codec (cp949 here), so a single
    Korean byte anywhere in a subprocess's output raised inside
    subprocess's own reader thread, left stdout as None, and turned the
    concatenation into a TypeError. A release gate that crashes instead
    of reporting is worse than no gate.
    """
    import sys

    from reel_harness.ops.release_check import _run

    # The child writes real UTF-8 bytes rather than print()ing, because
    # a Python child on this box would itself encode stdout as cp949 and
    # fail before the parent ever got to decode anything. What is under
    # test is the PARENT's decoding.
    code, output = _run(
        [sys.executable, "-c",
         "import sys; sys.stdout.buffer.write('테스트 통과'.encode('utf-8'))"],
        tmp_path, 30,
    )
    assert code == 0
    assert "테스트 통과" in output


def test_a_subprocess_with_no_output_does_not_crash_the_gate(tmp_path) -> None:
    import sys

    from reel_harness.ops.release_check import _run

    code, output = _run([sys.executable, "-c", "pass"], tmp_path, 30)
    assert code == 0
    assert output == ""
