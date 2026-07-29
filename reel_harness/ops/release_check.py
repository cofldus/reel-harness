from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

_SEVERITY = {"PASS": 0, "WARN": 1, "SKIPPED": 1, "FAIL": 2}


@dataclass
class ReleaseCheckItem:
    name: str
    status: str  # PASS | WARN | FAIL | SKIPPED
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class ReleaseCheckReport:
    items: list[ReleaseCheckItem] = field(default_factory=list)

    @property
    def overall(self) -> str:
        worst = "PASS"
        for item in self.items:
            if _SEVERITY.get(item.status, 0) > _SEVERITY.get(worst, 0):
                worst = item.status
        return worst

    @property
    def ready_to_tag(self) -> bool:
        return self.overall != "FAIL"

    def to_dict(self) -> dict:
        return {
            "overall": self.overall, "ready_to_tag": self.ready_to_tag,
            "items": [i.to_dict() for i in self.items],
        }


def _run(argv: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user input interpolated
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, f"{type(exc).__name__}: {exc}"
    return result.returncode, (result.stdout + result.stderr)[-4000:]


def _check_git_clean(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["git", "status", "--porcelain"], repo_root, 30)
    if code != 0:
        return ReleaseCheckItem("git_clean", "FAIL", f"git status failed: {output}")
    if output.strip():
        return ReleaseCheckItem("git_clean", "FAIL", "working tree is not clean")
    return ReleaseCheckItem("git_clean", "PASS", "working tree clean")


def _check_git_branch(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["git", "branch", "--show-current"], repo_root, 10)
    if code != 0:
        return ReleaseCheckItem("git_branch", "FAIL", f"could not determine branch: {output}")
    branch = output.strip()
    if branch == "main" or branch.startswith("phase") or "release" in branch:
        return ReleaseCheckItem("git_branch", "PASS", f"on {branch!r}")
    return ReleaseCheckItem("git_branch", "WARN", f"on {branch!r} -- not 'main' or an obvious release branch")


def _check_git_synced(repo_root: Path) -> ReleaseCheckItem:
    code, _ = _run(["git", "fetch", "--quiet"], repo_root, 30)
    branch_code, branch_out = _run(["git", "branch", "--show-current"], repo_root, 10)
    if branch_code != 0:
        return ReleaseCheckItem("git_synced", "WARN", "could not determine branch")
    branch = branch_out.strip()
    ahead_code, ahead_out = _run(
        ["git", "rev-list", "--left-right", "--count", f"origin/{branch}...{branch}"], repo_root, 15,
    )
    if ahead_code != 0:
        return ReleaseCheckItem("git_synced", "WARN", f"no matching remote branch origin/{branch}")
    behind, ahead = (ahead_out.strip().split() + ["0", "0"])[:2]
    if behind != "0":
        return ReleaseCheckItem("git_synced", "FAIL", f"local branch is {behind} commit(s) behind origin/{branch}")
    if ahead != "0":
        return ReleaseCheckItem(
            "git_synced", "WARN", f"local branch is {ahead} commit(s) ahead of origin/{branch} (not yet pushed)",
        )
    return ReleaseCheckItem("git_synced", "PASS", f"in sync with origin/{branch}")


def _check_version_consistency() -> ReleaseCheckItem:
    import tomllib

    from reel_harness._version import __version__

    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject.is_file():
        return ReleaseCheckItem("version_consistency", "WARN", "pyproject.toml not found relative to package")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    if data["project"]["version"] != __version__:
        return ReleaseCheckItem(
            "version_consistency", "FAIL",
            f"pyproject.toml version {data['project']['version']!r} != reel_harness.__version__ {__version__!r}",
        )
    return ReleaseCheckItem("version_consistency", "PASS", __version__)


def _check_lockfile(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["uv", "lock", "--check"], repo_root, 60)
    if code != 0:
        return ReleaseCheckItem("lockfile", "FAIL", f"uv.lock does not match pyproject.toml: {output}")
    return ReleaseCheckItem("lockfile", "PASS", "uv.lock matches pyproject.toml")


def _last_line(output: str) -> str:
    return output.strip().splitlines()[-1] if output.strip() else ""


def _check_full_pytest(repo_root: Path, timeout: float) -> ReleaseCheckItem:
    code, output = _run(["uv", "run", "--no-sync", "python", "-m", "pytest", "-q"], repo_root, timeout)
    status = "PASS" if code == 0 else "FAIL"
    summary_line = next(
        (line for line in output.splitlines()[::-1] if "passed" in line or "failed" in line), output,
    )
    return ReleaseCheckItem("full_pytest", status, summary_line.strip())


def _check_mypy(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["uv", "run", "--no-sync", "python", "-m", "mypy", "reel_harness"], repo_root, 180)
    return ReleaseCheckItem("mypy", "PASS" if code == 0 else "FAIL", _last_line(output))


def _check_ruff(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["uv", "run", "--no-sync", "ruff", "check", "reel_harness", "tests"], repo_root, 120)
    return ReleaseCheckItem("ruff", "PASS" if code == 0 else "FAIL", _last_line(output))


def _check_secret_scan(repo_root: Path) -> ReleaseCheckItem:
    # tests/ is excluded: redaction/forbidden-substring tests deliberately
    # embed fake Google-OAuth-token-shaped fixture values (see
    # test_publish_journal.py) to prove rejection/redaction logic works --
    # the opposite of a real leak, not something to flag. Mirrors
    # .github/workflows/ci.yml's own Secret/token grep step exactly.
    # (Deliberately not spelling out the fixture's literal value here --
    # doing so would itself match the pattern below and self-trigger.)
    code, output = _run(
        ["git", "grep", "-nIE", r"AIza[0-9A-Za-z_-]{20,}|ya29\.[0-9A-Za-z_-]{10,}", "--", "*.py", ":!tests"],
        repo_root, 30,
    )
    # git grep exits 1 when nothing matches -- that is the SAFE outcome here.
    if code == 0 and output.strip():
        return ReleaseCheckItem("secret_scan", "FAIL", "possible real API key/token literal in tracked .py files")
    return ReleaseCheckItem("secret_scan", "PASS", "no real-looking secret literals found")


def _check_tracked_artifacts(repo_root: Path) -> ReleaseCheckItem:
    code, output = _run(["git", "status", "--porcelain"], repo_root, 30)
    if code != 0:
        return ReleaseCheckItem("tracked_artifacts", "WARN", "git status failed")
    suspicious = [
        line for line in output.splitlines()
        if line.startswith("??") and (line.endswith(".db") or "/jobs/" in line)
    ]
    if suspicious:
        return ReleaseCheckItem("tracked_artifacts", "FAIL", f"{len(suspicious)} suspicious untracked artifact(s)")
    return ReleaseCheckItem("tracked_artifacts", "PASS", "no stray job artifacts or database files")


def run_release_check(
    repo_root: Path, skip_slow: bool = False, pytest_timeout: float = 900.0,
) -> ReleaseCheckReport:
    """Everything that must pass before an RC tag is created -- never
    creates a commit or tag itself (see cli.main.cmd_release_check /
    docs/OPERATIONS.md). `skip_slow=True` skips full_pytest/mypy/ruff for
    a fast iterative check; the real pre-tag gate must always run with
    skip_slow=False."""
    items = [
        _check_git_clean(repo_root), _check_git_branch(repo_root), _check_git_synced(repo_root),
        _check_version_consistency(), _check_lockfile(repo_root),
    ]
    if not skip_slow:
        items.append(_check_full_pytest(repo_root, pytest_timeout))
        items.append(_check_mypy(repo_root))
        items.append(_check_ruff(repo_root))
    items.append(_check_secret_scan(repo_root))
    items.append(_check_tracked_artifacts(repo_root))
    return ReleaseCheckReport(items=items)
