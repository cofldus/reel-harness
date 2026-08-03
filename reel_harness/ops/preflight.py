from __future__ import annotations

import importlib
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from reel_harness.config import Settings
from reel_harness.db.schema import SCHEMA_VERSION

PROFILES = ("fake", "production")

# Case-insensitive placeholder values that must never be the *real* value of
# a secret in a production profile -- catches copy-pasted example .env
# content left in place, not a judgment about what a real key looks like.
_PLACEHOLDER_SECRETS = {
    "changeme", "changeme-local-dev-key", "change-me", "change_me", "placeholder",
    "xxx", "todo", "test", "your-api-key-here", "your_api_key_here", "example", "secret",
}

# Below this, a render+upload cycle risks failing mid-write on a real
# machine; informational only (WARN), never a hard requirement, since a
# tiny scratch job needs far less than this.
_LOW_DISK_WARN_BYTES = 500 * 1024 * 1024

# The heartbeat must refresh well before the lease can be reclaimed as
# stale, or a healthy worker in a long stage risks losing its own lease.
_HEARTBEAT_RATIO_MAX = 1.0 / 3.0


@dataclass
class PreflightCheck:
    name: str
    status: str  # PASS | WARN | FAIL | NOT_CONFIGURED
    detail: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


_SEVERITY = {"PASS": 0, "NOT_CONFIGURED": 1, "WARN": 2, "FAIL": 3}


@dataclass
class PreflightReport:
    profile: str
    checks: list[PreflightCheck] = field(default_factory=list)

    @property
    def overall(self) -> str:
        worst = "PASS"
        for check in self.checks:
            if _SEVERITY.get(check.status, 0) > _SEVERITY.get(worst, 0):
                worst = check.status
        return worst

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "overall": self.overall,
            "checks": [c.to_dict() for c in self.checks],
        }


class _Checker:
    """Accumulates PreflightChecks, escalating WARN to FAIL for a fixed set
    of check names when running the "production" profile -- the same check
    logic runs for every profile; only the pass/fail bar for a handful of
    operationally-risky findings is stricter in production."""

    # Checks that, if they would otherwise WARN, are FAIL under
    # profile="production" -- these are exactly the ones the Phase 4A spec
    # calls out as production-blocking (placeholder secrets, repo-internal
    # credentials, an unwritable storage root, an unsupported schema, an
    # unsafe heartbeat/lease ratio, an unexplained public-upload flag, weak
    # API auth, risky credential file permissions).
    _STRICT_IN_PRODUCTION = frozenset({
        "api_authentication", "secret_placeholder", "repo_internal_credential",
        "storage_root_writable", "db_schema", "lease_heartbeat_ratio",
        "public_upload_feature_flag", "credential_file_permission", "public_bind_security",
    })

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.checks: list[PreflightCheck] = []

    def add(self, name: str, status: str, detail: str = "") -> None:
        if status == "WARN" and self.profile == "production" and name in self._STRICT_IN_PRODUCTION:
            status = "FAIL"
        self.checks.append(PreflightCheck(name=name, status=status, detail=detail))


def _check_config_parse(checker: _Checker, settings: Settings) -> None:
    # Reaching this point at all means Settings() parsed and
    # validate_provider_settings() already passed (see AppContext.__init__),
    # so this is a real, not vacuous, confirmation.
    checker.add("config_parse", "PASS", "settings loaded and passed startup validation")


def _check_db(checker: _Checker, session_factory) -> None:
    from sqlalchemy import text

    try:
        with session_factory() as session:
            session.execute(text("SELECT 1"))
            row = session.execute(text("SELECT version FROM schema_migrations")).scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must report, never crash
        checker.add("db_connectivity", "FAIL", f"{type(exc).__name__}: {exc}")
        checker.add("db_schema", "FAIL", "could not read schema_migrations")
        return
    checker.add("db_connectivity", "PASS", "SELECT 1 succeeded")
    if row is None:
        checker.add("db_schema", "FAIL", "schema_migrations table has no row")
    elif row == SCHEMA_VERSION:
        checker.add("db_schema", "PASS", f"v{row} (expected v{SCHEMA_VERSION})")
    else:
        checker.add("db_schema", "FAIL", f"v{row} (expected v{SCHEMA_VERSION}) -- run `reel-harness db-migrate`")


def _check_write_permission(checker: _Checker, name: str, root: Path) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / f".reel-harness-preflight-probe-{id(root)}"
        probe.write_bytes(b"probe")
        probe.unlink()
        checker.add(name, "PASS", f"{root} is writable")
    except OSError as exc:
        checker.add(name, "FAIL", f"{root} is not writable: {exc}")


def _check_storage(checker: _Checker, settings: Settings) -> None:
    root = settings.jobs_dir.resolve()
    if root.is_dir():
        checker.add("storage_root", "PASS", str(root))
    else:
        checker.add("storage_root", "WARN", f"{root} does not exist yet -- will be created on first use")
    _check_write_permission(checker, "storage_root_writable", root)
    # LocalFilesystemStorage is the only StorageBackend; the reel jobs root
    # IS the storage root, and Fable cinematic projects use a second
    # instance rooted at settings.fable_projects_dir (checked below).
    checker.add("jobs_root", "PASS", f"same as storage_root ({root})")
    fable_root = settings.fable_projects_dir.resolve()
    if fable_root.is_dir():
        checker.add("fable_projects_root", "PASS", str(fable_root))
    else:
        checker.add(
            "fable_projects_root", "WARN",
            f"{fable_root} does not exist yet -- will be created on first use",
        )

    try:
        usage = shutil.disk_usage(root if root.is_dir() else root.parent)
    except OSError as exc:
        checker.add("free_disk_space", "WARN", f"could not stat disk usage: {exc}")
        return
    if usage.free < _LOW_DISK_WARN_BYTES:
        checker.add(
            "free_disk_space", "WARN",
            f"only {usage.free / (1024 * 1024):.0f} MiB free at {root}",
        )
    else:
        checker.add("free_disk_space", "PASS", f"{usage.free / (1024 * 1024 * 1024):.1f} GiB free at {root}")


def _check_credential_and_journal_roots(checker: _Checker, settings: Settings) -> None:
    from reel_harness.publisher.secret_store import SecretStoreError, resolve_secret_dir

    try:
        root = resolve_secret_dir(settings.credential_dir, Path.cwd())
    except SecretStoreError as exc:
        checker.add("repo_internal_credential", "FAIL", str(exc))
        checker.add("credential_root", "FAIL", "cannot resolve -- see repo_internal_credential")
        checker.add("journal_root", "FAIL", "cannot resolve -- see repo_internal_credential")
        checker.add("credential_file_permission", "FAIL", "cannot resolve -- see repo_internal_credential")
        return
    checker.add("repo_internal_credential", "PASS", "credential directory resolves outside the repository")
    checker.add("credential_root", "PASS", str(root))
    _check_write_permission(checker, "journal_root", root / "publish_journal")

    # Best-effort POSIX permission check -- Windows ACLs are a materially
    # different model this check does not attempt to evaluate; report that
    # honestly rather than pretending coverage that doesn't exist.
    import os

    if os.name != "posix":
        checker.add(
            "credential_file_permission", "PASS",
            "not checked on this platform (Windows ACLs, not POSIX mode bits)",
        )
        return
    if root.exists():
        mode = root.stat().st_mode & 0o777
        if mode & 0o077:
            checker.add(
                "credential_file_permission", "WARN",
                f"{root} is group/other-accessible (mode {oct(mode)}) -- consider chmod 700",
            )
        else:
            checker.add("credential_file_permission", "PASS", f"{root} is owner-only (mode {oct(mode)})")
    else:
        checker.add("credential_file_permission", "PASS", "credential directory does not exist yet")


def _check_env_file_permission(checker: _Checker) -> None:
    import os

    env_path = Path(".env")
    if not env_path.exists():
        checker.add("env_file_permission", "PASS", "no .env file present")
        return
    if os.name != "posix":
        checker.add("env_file_permission", "PASS", "not checked on this platform (Windows ACLs)")
        return
    mode = env_path.stat().st_mode & 0o777
    if mode & 0o077:
        checker.add("env_file_permission", "WARN", f".env is group/other-accessible (mode {oct(mode)})")
    else:
        checker.add("env_file_permission", "PASS", f".env is owner-only (mode {oct(mode)})")


def _check_ffmpeg(checker: _Checker) -> None:
    from reel_harness.media.deps import check_ffmpeg_available

    deps = check_ffmpeg_available()
    checker.add(
        "ffmpeg", "PASS" if deps.ffmpeg_available else "FAIL",
        f"{deps.ffmpeg.path} ({deps.ffmpeg.source})" if deps.ffmpeg_available else "not found",
    )
    checker.add(
        "ffprobe", "PASS" if deps.ffprobe_available else "FAIL",
        f"{deps.ffprobe.path} ({deps.ffprobe.source})" if deps.ffprobe_available else "not found",
    )


def _check_demo_tts(checker: _Checker) -> None:
    """WARN, never FAIL -- unlike ffmpeg (required by every render regardless
    of provider), a local TTS engine is only needed if Demo Mode is actually
    selected (tts_provider=demo). Missing it is not a baseline readiness
    problem."""
    from reel_harness.providers.demo_tts import check_demo_tts_available

    status = check_demo_tts_available()
    checker.add("demo_tts", "PASS" if status.available else "WARN", status.detail)


def _check_runtime_dependencies(checker: _Checker) -> None:
    missing = []
    for module in ("httpx", "fastapi", "sqlalchemy", "pydantic", "pydantic_settings", "uvicorn"):
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(module)
    if missing:
        checker.add("runtime_dependencies", "FAIL", f"missing: {', '.join(missing)}")
    else:
        checker.add("runtime_dependencies", "PASS", "all required runtime packages import cleanly")


def _check_provider_registry(checker: _Checker) -> None:
    try:
        from reel_harness.providers import instagram_publisher, tiktok_publisher, youtube_publisher  # noqa: F401
        from reel_harness.providers.registry import provider_capabilities

        for provider in ("youtube", "tiktok", "instagram"):
            provider_capabilities(provider)
    except Exception as exc:  # noqa: BLE001
        checker.add("provider_registry", "FAIL", f"{type(exc).__name__}: {exc}")
        return
    checker.add("provider_registry", "PASS", "youtube/tiktok/instagram adapters + capabilities importable")


def _check_worker_settings(checker: _Checker, settings: Settings) -> None:
    if settings.lease_timeout_seconds <= 0:
        checker.add("worker_settings", "FAIL", "lease_timeout_seconds must be positive")
        return
    if settings.worker_poll_interval_seconds <= 0:
        checker.add("worker_settings", "FAIL", "worker_poll_interval_seconds must be positive")
        return
    checker.add("worker_settings", "PASS", "lease timeout and poll interval are positive")

    ratio = settings.lease_heartbeat_seconds / settings.lease_timeout_seconds
    if settings.lease_heartbeat_seconds <= 0:
        checker.add("lease_heartbeat_ratio", "FAIL", "lease_heartbeat_seconds must be positive")
    elif ratio > _HEARTBEAT_RATIO_MAX:
        checker.add(
            "lease_heartbeat_ratio", "WARN",
            f"heartbeat/timeout ratio {ratio:.2f} exceeds {_HEARTBEAT_RATIO_MAX:.2f} -- "
            "a healthy worker in a long stage risks a spurious stale-lease reclaim",
        )
    else:
        checker.add("lease_heartbeat_ratio", "PASS", f"heartbeat/timeout ratio {ratio:.2f}")


def _check_upload_chunk_settings(checker: _Checker, settings: Settings) -> None:
    problems = []
    if settings.youtube_upload_chunk_size <= 0 or settings.youtube_upload_chunk_size % 262144 != 0:
        problems.append("youtube_upload_chunk_size must be a positive multiple of 262144")
    if settings.tiktok_upload_chunk_size <= 0:
        problems.append("tiktok_upload_chunk_size must be positive")
    if problems:
        checker.add("upload_chunk_settings", "FAIL", "; ".join(problems))
    else:
        checker.add("upload_chunk_settings", "PASS", "youtube/tiktok chunk sizes valid")


def _check_public_upload_flag(checker: _Checker, settings: Settings) -> None:
    any_publisher_configured = any((
        settings.youtube_client_id and settings.youtube_client_secret.get_secret_value(),
        settings.tiktok_client_key and settings.tiktok_client_secret.get_secret_value(),
        settings.instagram_app_id and settings.instagram_app_secret.get_secret_value(),
    ))
    if not settings.allow_public_upload:
        checker.add("public_upload_feature_flag", "PASS", "disabled (default)")
        return
    if any_publisher_configured:
        checker.add(
            "public_upload_feature_flag", "PASS",
            "enabled, and at least one publisher OAuth client is configured to use it",
        )
    else:
        checker.add(
            "public_upload_feature_flag", "WARN",
            "REEL_HARNESS_ALLOW_PUBLIC_UPLOAD=true but no publisher OAuth client is configured -- "
            "enabled without anything that could use it",
        )


def _check_paid_generation_flag(checker: _Checker, settings: Settings) -> None:
    """Reports the Fable spend switch, mirroring the public-upload flag
    check above. Enabled with only the free tiers selected is a WARN, not
    a FAIL: it is harmless today, but it means a switch about money is on
    with nothing behind it -- the state an operator most easily forgets
    about before selecting a real adapter."""
    from reel_harness.providers.registry import provider_charges_money

    paid_selected = provider_charges_money(settings.cinematic_provider) or provider_charges_money(
        settings.reference_image_provider
    )
    if not settings.allow_paid_generation:
        checker.add(
            "paid_generation_feature_flag", "PASS",
            "disabled (default) -- only the free fable tiers can generate",
        )
        return
    if paid_selected:
        checker.add(
            "paid_generation_feature_flag", "PASS",
            "enabled, and a cost-incurring fable provider is selected "
            "(each project still needs its own budget limit)",
        )
    else:
        checker.add(
            "paid_generation_feature_flag", "WARN",
            "REEL_HARNESS_ALLOW_PAID_GENERATION=true but every selected fable provider is a "
            "free tier -- enabled without anything that could spend",
        )


def _check_api_authentication(checker: _Checker, settings: Settings) -> None:
    key = settings.app_api_key
    if not key:
        checker.add("api_authentication", "FAIL", "app_api_key is empty")
    elif key.strip().lower() in _PLACEHOLDER_SECRETS:
        checker.add("api_authentication", "WARN", "app_api_key is still the default/placeholder value")
    elif len(key) < 16:
        checker.add(
            "api_authentication", "WARN",
            f"app_api_key is only {len(key)} characters -- consider a longer key",
        )
    else:
        checker.add("api_authentication", "PASS", "app_api_key is set and not a known placeholder")


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def _check_public_bind_security(checker: _Checker, settings: Settings) -> None:
    """The web UI (see reel_harness.web) has no login system -- only CSRF
    protection for browser-originated requests, the same trust boundary
    /healthz//status already use. Binding `serve` beyond loopback without
    that being a deliberate, informed choice is a real exposure: FAIL under
    `--profile production` whenever app_api_key is still a placeholder (the
    one credential /v1/* actually checks), and always at least WARN on any
    non-loopback bind so a plain `preflight` run surfaces it too -- never
    assume "bound to localhost" is the default just because it usually is."""
    host = settings.api_host
    if host in _LOOPBACK_HOSTS:
        checker.add("public_bind_security", "PASS", f"api_host={host!r} (loopback only)")
        return
    if settings.app_api_key.strip().lower() in _PLACEHOLDER_SECRETS:
        checker.add(
            "public_bind_security", "WARN",
            f"api_host={host!r} but app_api_key is a placeholder -- /v1/* would be effectively "
            "open to the network",
        )
        return
    checker.add(
        "public_bind_security", "WARN",
        f"api_host={host!r}: the web UI has no login, only CSRF protection for browser-originated "
        "requests -- expose only on a trusted network, ideally behind a reverse proxy performing "
        "real authentication",
    )


def _check_secret_placeholders(checker: _Checker, settings: Settings) -> None:
    candidates = {
        "REEL_HARNESS_LLM_API_KEY": settings.llm_api_key.get_secret_value(),
        "REEL_HARNESS_TTS_API_KEY": settings.tts_api_key.get_secret_value(),
        "REEL_HARNESS_ASSET_API_KEY": settings.asset_api_key.get_secret_value(),
        "REEL_HARNESS_YOUTUBE_CLIENT_SECRET": settings.youtube_client_secret.get_secret_value(),
        "REEL_HARNESS_TIKTOK_CLIENT_SECRET": settings.tiktok_client_secret.get_secret_value(),
        "REEL_HARNESS_INSTAGRAM_APP_SECRET": settings.instagram_app_secret.get_secret_value(),
    }
    flagged = [
        var for var, value in candidates.items()
        if value and value.strip().lower() in _PLACEHOLDER_SECRETS
    ]
    if flagged:
        checker.add("secret_placeholder", "WARN", f"placeholder-looking value(s): {', '.join(flagged)}")
    else:
        checker.add("secret_placeholder", "PASS", "no configured secret matches a known placeholder value")


def run_preflight(
    settings: Settings, session_factory, profile: str = "fake",
) -> PreflightReport:
    """Local-only checks (never touches a provider network) covering
    everything an operator needs to know before running this process for
    real: config, DB, storage, credentials, ffmpeg, dependencies, the
    provider/publisher registry, and worker/upload/public-upload policy
    sanity. `profile="production"` escalates a fixed set of WARN-worthy
    findings to FAIL -- see _Checker._STRICT_IN_PRODUCTION. Remote checks
    are a separate, explicit step (see ops.preflight.run_remote_checks),
    never folded in here so a plain `preflight` call is always
    network-free."""
    if profile not in PROFILES:
        raise ValueError(f"unknown preflight profile {profile!r} (supported: {', '.join(PROFILES)})")
    checker = _Checker(profile)
    _check_config_parse(checker, settings)
    _check_db(checker, session_factory)
    _check_storage(checker, settings)
    _check_credential_and_journal_roots(checker, settings)
    _check_env_file_permission(checker)
    _check_ffmpeg(checker)
    _check_demo_tts(checker)
    _check_runtime_dependencies(checker)
    _check_provider_registry(checker)
    _check_worker_settings(checker, settings)
    _check_upload_chunk_settings(checker, settings)
    _check_public_upload_flag(checker, settings)
    _check_paid_generation_flag(checker, settings)
    _check_api_authentication(checker, settings)
    _check_secret_placeholders(checker, settings)
    _check_public_bind_security(checker, settings)
    return PreflightReport(profile=profile, checks=checker.checks)


def _remote_check_youtube(settings: Settings, credential_backend, account: str) -> PreflightCheck:
    """YouTube's `Publisher.get_creator_info()` is intentionally a no-op
    (see providers.youtube_publisher -- YouTube's capabilities set
    requires_creator_info=False, so the shared publish path never calls
    it), so a real remote check needs the same token-refresh +
    channel-identity call `publisher-doctor youtube --check-remote` makes,
    not the generic get_creator_info() path used for TikTok/Instagram."""
    from reel_harness.core.errors import ProviderAuthError, TransientProviderError
    from reel_harness.providers.registry import _resolve_fresh_youtube_access_token
    from reel_harness.publisher.oauth_youtube import YouTubeOAuthClient

    try:
        token = _resolve_fresh_youtube_access_token(settings, credential_backend, account)
    except (ProviderAuthError, TransientProviderError) as exc:
        return PreflightCheck("remote_youtube", "FAIL", f"{type(exc).__name__}")
    client = YouTubeOAuthClient(settings.youtube_client_id, settings.youtube_client_secret.get_secret_value())
    try:
        identity = client.fetch_channel_identity(token)
        return PreflightCheck("remote_youtube", "PASS", f"channel_id={identity.channel_id}")
    except (ProviderAuthError, TransientProviderError) as exc:
        return PreflightCheck("remote_youtube", "FAIL", f"{type(exc).__name__}")
    finally:
        client.close()


def _remote_check_creator_info(
    provider: str, settings: Settings, credential_backend, account: str,
) -> PreflightCheck:
    from reel_harness.providers.registry import resolve_publisher

    publisher = resolve_publisher(
        provider, settings=settings, credential_backend=credential_backend, account_reference=account,
    )
    try:
        info = publisher.get_creator_info()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must report, never crash
        return PreflightCheck(f"remote_{provider}", "FAIL", f"{type(exc).__name__}")
    finally:
        close = getattr(publisher, "close", None)
        if callable(close):
            close()
    detail = f"account={info.account_identifier!r}" if info is not None else "account reachable"
    return PreflightCheck(f"remote_{provider}", "PASS", detail)


def run_remote_checks(
    settings: Settings, credential_backend, publishers: tuple[str, ...] = ("youtube", "tiktok", "instagram"),
    account: str = "default",
) -> list[PreflightCheck]:
    """Opt-in (`--check-remote`) read-only external calls, kept entirely
    separate from run_preflight() so the default command never touches a
    network. For each requested publisher with a saved credential, performs
    exactly the same real, read-only account-identity call
    `publisher-doctor <provider> --check-remote` does -- no duplicate
    logic, no new write. A provider with no saved credential is reported
    NOT_CONFIGURED, never attempted."""
    checks: list[PreflightCheck] = []
    for provider in publishers:
        try:
            cred = credential_backend.get_credential(provider, account)
        except Exception as exc:  # noqa: BLE001
            checks.append(PreflightCheck(f"remote_{provider}", "FAIL", f"credential lookup failed: {exc}"))
            continue
        if cred is None:
            checks.append(PreflightCheck(f"remote_{provider}", "NOT_CONFIGURED", "no saved credential"))
            continue
        if provider == "youtube":
            checks.append(_remote_check_youtube(settings, credential_backend, account))
        else:
            checks.append(_remote_check_creator_info(provider, settings, credential_backend, account))
    return checks
