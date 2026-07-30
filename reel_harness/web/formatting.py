from __future__ import annotations

from datetime import UTC, datetime


def format_duration(seconds: float) -> str:
    """1234.5 -> '20m 34s'; 45.2 -> '45s'; 7325 -> '2h 02m'. Never shows a
    fake sub-second precision a user-facing timer doesn't need."""
    total = int(round(seconds))
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_elapsed_since(moment: datetime, now: datetime | None = None) -> str:
    """Elapsed wall-clock time since `moment` (assumed timezone-aware, or
    naive-UTC as SQLite/SQLAlchemy commonly stores it)."""
    reference = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    return format_duration((reference - moment).total_seconds())
