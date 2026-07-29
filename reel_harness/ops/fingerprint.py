from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from reel_harness._version import __version__
from reel_harness.config import Settings, normalize_provider_name
from reel_harness.db.schema import SCHEMA_VERSION


def _safe_host(url: str) -> str | None:
    """Returns only the host[:port] of a base URL, never the path or query
    string -- a base URL should never legitimately carry a credential, but
    this keeps the fingerprint safe even if one were misconfigured into
    one."""
    if not url:
        return None
    try:
        netloc = urlsplit(url).netloc
    except ValueError:
        return None
    return netloc or None


def config_fingerprint(settings: Settings) -> dict:
    """A safe, deterministic snapshot of *how* this process is configured --
    never *what secrets* it holds. Suitable for a startup log line, a job/
    Publication metadata field, a diagnostics report, or an incident
    bundle. Never includes an API key, OAuth token, client secret, signed
    URL, full credential path, or Authorization header -- only provider
    identifiers, model names, safe hosts (host[:port], never path/query),
    and non-secret policy settings.

    Deliberately excludes anything that changes between otherwise-identical
    runs (timestamps, PIDs, random ids) so the same configuration always
    produces the same fingerprint."""
    return {
        "app_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "storage_backend": "local_filesystem",
        "llm_provider": normalize_provider_name(settings.llm_provider),
        "llm_model": settings.llm_model or None,
        "llm_host": _safe_host(settings.llm_base_url),
        "tts_provider": normalize_provider_name(settings.tts_provider),
        "tts_model": settings.tts_model or None,
        "tts_host": _safe_host(settings.tts_base_url),
        "tts_format": settings.tts_format,
        "asset_provider": normalize_provider_name(settings.asset_provider),
        "asset_host": _safe_host(settings.asset_base_url),
        "publishers_registered": ["youtube", "tiktok", "instagram"],
        "instagram_media_url_mode": settings.instagram_media_url_mode,
        "worker_lease_timeout_seconds": settings.lease_timeout_seconds,
        "worker_heartbeat_seconds": settings.lease_heartbeat_seconds,
        "worker_poll_interval_seconds": settings.worker_poll_interval_seconds,
        "publisher_processing_poll_interval_seconds": settings.publisher_processing_poll_interval_seconds,
        "publisher_processing_max_duration_seconds": settings.publisher_processing_max_duration_seconds,
        "allow_public_upload": settings.allow_public_upload,
        "youtube_upload_chunk_size": settings.youtube_upload_chunk_size,
        "tiktok_upload_chunk_size": settings.tiktok_upload_chunk_size,
    }


def fingerprint_hash(fingerprint: dict) -> str:
    """A short, stable hash of a fingerprint dict -- used as a compact
    correlation id in logs/reports where the full dict would be verbose.
    Canonical JSON (sorted keys, no whitespace) so the same fingerprint
    always hashes the same way regardless of dict insertion order."""
    canonical = json.dumps(fingerprint, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
