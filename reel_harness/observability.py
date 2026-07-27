from __future__ import annotations

import json
import logging
import re

_LOGGER_NAME = "reel_harness"
_logger = logging.getLogger(_LOGGER_NAME)

_MASK = "***REDACTED***"

# Generic patterns redacted unconditionally, regardless of what secret value is
# actually configured -- catches Authorization headers, bearer/basic tokens,
# api-key style headers/params/JSON fields, and OpenAI-style sk- keys even if
# nobody remembered to call register_secret() for that particular value.
# Each entry is (pattern, replacement); replacements keep the field name where
# possible so error messages stay readable without leaking the value.
_SECRET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}"), _MASK),
    (re.compile(r"(?i)\bbasic\s+[A-Za-z0-9+/=]{8,}"), _MASK),
    (re.compile(r"(?i)\bauthorization\b[\"']?\s*[:=]\s*\S+"), f"authorization={_MASK}"),
    # Header / query / JSON style key-value pairs. The value charset excludes
    # quotes and separators so only the secret itself is replaced; a minimum
    # value length avoids mauling short ordinary words.
    (
        re.compile(
            r"(?i)\b(api[_-]?key|apikey|x-api-key|access[_-]?token|refresh[_-]?token"
            r"|client[_-]?secret|secret[_-]?key)\b[\"']?\s*[:=]\s*[\"']?([^\s\"'&,;}{]{6,})"
        ),
        rf"\1={_MASK}",
    ),
    # URL query parameters named like credentials: keep ?name=, drop the value.
    (re.compile(r"(?i)([?&](?:key|token|access_token|api_key|apikey)=)[^&\s\"']+"), rf"\1{_MASK}"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"), _MASK),
]

# Specific known-secret values (e.g. the configured app_api_key or a provider
# API key) registered at runtime via register_secret() and redacted verbatim
# wherever they appear.
_registered_secrets: set[str] = set()

_MIN_SECRET_LENGTH = 8  # never register short common words -- replacement would maul ordinary text


def register_secret(value: str | None) -> None:
    if value and len(value) >= _MIN_SECRET_LENGTH:
        _registered_secrets.add(value)


def redact(text: str | None) -> str | None:
    """Single redaction rule set for BOTH log output and persisted fields
    (job.failure_summary, StageRun.error_detail, manifest/API error strings).
    Everything that stores or emits an error string must go through here so the
    two paths cannot drift. Never mutates the original exception/object --
    callers pass a rendered string. None passes through unchanged."""
    if text is None:
        return None
    for pattern, replacement in _SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    # Longest secrets first so overlapping registrations can't leave a suffix
    # of a longer secret behind after a shorter one is replaced.
    for secret in sorted(_registered_secrets, key=len, reverse=True):
        text = text.replace(secret, _MASK)
    return text


def _redact(text: str) -> str:
    redacted = redact(text)
    return redacted if redacted is not None else ""


class _RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = _redact(str(record.getMessage()))
        record.args = ()
        return True


def configure_logging(level: str = "INFO") -> None:
    """Idempotent: safe to call every time an AppContext is created."""
    _logger.setLevel(level)
    if not any(isinstance(f, _RedactingFilter) for f in _logger.filters):
        _logger.addFilter(_RedactingFilter())
    if not _logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        _logger.addHandler(handler)


def log_stage_event(
    *,
    job_id: str,
    stage: str,
    attempt: int,
    event: str,
    duration_ms: float | None = None,
    error_code: str | None = None,
) -> None:
    """Emits one structured JSON log line per pipeline stage transition. Never
    pass script/voiceover text or full request bodies here -- only identifiers
    and outcome fields. The `_RedactingFilter` scrubs Authorization/bearer
    values and any registered secret from the final message regardless, but
    the discipline of not passing secrets/PII in is the primary defense.
    """
    payload = {
        "job_id": job_id,
        "stage": stage,
        "attempt": attempt,
        "event": event,
        "duration_ms": duration_ms,
        "error_code": error_code,
    }
    _logger.info(json.dumps(payload))
