from __future__ import annotations

import json
import logging
import re

_LOGGER_NAME = "reel_harness"
_logger = logging.getLogger(_LOGGER_NAME)

# Generic patterns redacted unconditionally, regardless of what secret value is
# actually configured -- catches Authorization headers/bearer tokens even if
# nobody remembered to call register_secret() for that particular value.
_SECRET_PATTERNS = [
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)authorization\s*[:=]\s*\S+"),
]

# Specific known-secret values (e.g. the configured app_api_key) registered at
# runtime via register_secret() and redacted verbatim wherever they appear.
_registered_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    if value:
        _registered_secrets.add(value)


def _redact(text: str) -> str:
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("***REDACTED***", text)
    for secret in _registered_secrets:
        text = text.replace(secret, "***REDACTED***")
    return text


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
