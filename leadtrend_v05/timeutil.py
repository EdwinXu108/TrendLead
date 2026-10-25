"""Timezone-aware time helpers. Internal comparisons use UTC; display uses America/New_York."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
NY = ZoneInfo("America/New_York")


def parse_date(value: str) -> date:
    value = value.strip()
    return date.fromisoformat(value)


def parse_datetime(value: str) -> datetime:
    """Parse ISO-8601 datetime; naive values are rejected (must carry tz)."""
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    dt = datetime.fromisoformat(raw)
    if dt.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware: {value!r}")
    return dt.astimezone(UTC)


def ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime is not allowed")
    return dt.astimezone(UTC)


def to_ny(dt: datetime) -> datetime:
    return ensure_utc(dt).astimezone(NY)


def ny_date(dt: datetime) -> date:
    return to_ny(dt).date()


def datetime_to_iso(dt: datetime) -> str:
    return ensure_utc(dt).isoformat().replace("+00:00", "Z")


def date_end_of_day_utc(d: date) -> datetime:
    """Treat a calendar date as known by the end of that UTC day (fallback only)."""
    return datetime.combine(d, time(23, 59, 59), tzinfo=UTC)


def date_precision_available_at(
    available_date: date,
    sessions: list[dict],
) -> datetime | None:
    """Date-only availability: open of the first session strictly after that date.

    sessions items must include session_date (date) and open_time_utc (datetime).
    """
    for row in sessions:
        if row["session_date"] > available_date:
            return ensure_utc(row["open_time_utc"])
    return None


def parse_optional_bool(value: str | None) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


def add_days(d: date, n: int) -> date:
    return d + timedelta(days=n)
