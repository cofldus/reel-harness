from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_PROVIDERS = ("youtube", "tiktok", "instagram")

# Forbidden-content guard, mirrored from publisher.journal -- a live
# verification record must never carry a credential or a signed URL.
_FORBIDDEN_SUBSTRINGS = ("access_token", "refresh_token", "authorization", "bearer ")


class LiveVerifyError(Exception):
    pass


@dataclass
class LiveVerificationRecord:
    """Deliberately separate from Publication (see docs/OPERATIONS.md) --
    a live-verify run is a diagnostic probe, not a real publish attempt,
    even when it uploads a real test clip. Never stores a credential,
    signed URL, upload session URI, or raw provider response -- only safe
    identifiers and outcome enums."""

    provider: str
    account_alias: str
    verification_type: str  # "read_only" | "upload_test"
    started_at: str
    completed_at: str | None
    outcome: str  # NOT_CONFIGURED | PASS | FAIL | APP_REVIEW_REQUIRED | ACCOUNT_NOT_ELIGIBLE | ...
    application_version: str
    config_fingerprint_hash: str
    safe_provider_object_id: str | None = None
    privacy_or_visibility: str | None = None
    processing_outcome: str | None = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider, "account_alias": self.account_alias,
            "verification_type": self.verification_type, "started_at": self.started_at,
            "completed_at": self.completed_at, "outcome": self.outcome,
            "application_version": self.application_version,
            "config_fingerprint_hash": self.config_fingerprint_hash,
            "safe_provider_object_id": self.safe_provider_object_id,
            "privacy_or_visibility": self.privacy_or_visibility,
            "processing_outcome": self.processing_outcome, "detail": self.detail,
        }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_only_check(ctx, provider: str, account: str) -> LiveVerificationRecord:
    from reel_harness._version import __version__
    from reel_harness.ops.fingerprint import fingerprint_hash
    from reel_harness.ops.preflight import run_remote_checks

    started_at = _now_iso()
    app_version = __version__
    fp_hash = fingerprint_hash(ctx.config_fingerprint())

    try:
        cred = ctx.credential_backend().get_credential(provider, account)
    except Exception as exc:  # noqa: BLE001
        return LiveVerificationRecord(
            provider=provider, account_alias=account, verification_type="read_only",
            started_at=started_at, completed_at=_now_iso(), outcome="FAIL",
            application_version=app_version, config_fingerprint_hash=fp_hash,
            detail=f"credential backend error: {type(exc).__name__}",
        )
    if cred is None:
        return LiveVerificationRecord(
            provider=provider, account_alias=account, verification_type="read_only",
            started_at=started_at, completed_at=_now_iso(), outcome="NOT_CONFIGURED",
            application_version=app_version, config_fingerprint_hash=fp_hash,
            detail="no saved credential",
        )

    checks = run_remote_checks(ctx.settings, ctx.credential_backend(), publishers=(provider,), account=account)
    check = checks[0] if checks else None
    outcome = check.status if check is not None else "FAIL"
    detail = check.detail if check is not None else "no result"

    extra_detail = ""
    privacy_or_visibility = None
    if outcome == "PASS" and provider in ("tiktok", "instagram"):
        try:
            from reel_harness.providers.registry import resolve_publisher

            publisher = resolve_publisher(
                provider, settings=ctx.settings, credential_backend=ctx.credential_backend(),
                account_reference=account,
            )
            try:
                info = publisher.get_creator_info()
            finally:
                close = getattr(publisher, "close", None)
                if callable(close):
                    close()
            if info is not None:
                privacy_or_visibility = ",".join(sorted(info.allowed_privacy_values)) or None
                if info.warnings:
                    if any("app_review" in w.lower() or "review" in w.lower() for w in info.warnings):
                        outcome = "APP_REVIEW_REQUIRED"
                    elif any(
                        "not a reels-eligible" in w.lower() or "not eligible" in w.lower() for w in info.warnings
                    ):
                        outcome = "ACCOUNT_NOT_ELIGIBLE"
                    extra_detail = "; ".join(info.warnings)
        except Exception as exc:  # noqa: BLE001 - read-only diagnostic must report, never crash
            extra_detail = f"creator_info enrichment failed: {type(exc).__name__}"

    return LiveVerificationRecord(
        provider=provider, account_alias=account, verification_type="read_only",
        started_at=started_at, completed_at=_now_iso(), outcome=outcome,
        application_version=app_version, config_fingerprint_hash=fp_hash,
        privacy_or_visibility=privacy_or_visibility,
        detail=(detail + ("; " + extra_detail if extra_detail else "")).strip("; "),
    )


def run_read_only_live_verify(
    ctx, providers: tuple[str, ...] = _PROVIDERS, account: str = "default",
) -> list[LiveVerificationRecord]:
    """Read-only across every requested provider -- a provider with no
    saved credential is reported NOT_CONFIGURED and the sweep continues to
    the next one, never aborting the whole run."""
    return [_read_only_check(ctx, provider, account) for provider in providers]


class LiveVerificationLog:
    """Append-only, fsync'd JSON-lines record of every live-verify run --
    rooted alongside the publish journal (repository-external, same
    rejection-of-repo-internal-paths guarantee). Distinct from
    Publication/PublicationAuditEvent: this records a diagnostic
    verification event, not a real publish attempt."""

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir
        self._root.mkdir(parents=True, exist_ok=True)
        self._path = self._root / "live_verification_records.jsonl"

    def append(self, record: LiveVerificationRecord) -> None:
        import os

        blob = json.dumps(record.to_dict(), sort_keys=True)
        for forbidden in _FORBIDDEN_SUBSTRINGS:
            if forbidden in blob.lower():
                raise LiveVerifyError(
                    f"refusing to write a live-verification record containing {forbidden!r}"
                )
        with open(self._path, "a", encoding="utf-8") as handle:
            handle.write(blob + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def read_all(self) -> list[dict]:
        if not self._path.is_file():
            return []
        records = []
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
        return records
