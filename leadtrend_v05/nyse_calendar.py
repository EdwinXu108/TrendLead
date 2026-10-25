"""NYSE session calendar with official early closes (local file + source citation)."""

from __future__ import annotations

import csv
from datetime import date, datetime, time
from pathlib import Path

from .timeutil import NY

# NYSE early closes (13:00 America/New_York) 2019–2026, from NYSE holiday schedule.
# Sources (verified 2026-10-25):
#   https://www.nyse.com/markets/hours-calendars
#   https://www.nasdaq.com/market-activity/stock-market-holiday-schedule
#   https://s2.q4cdn.com/154085107/files/doc_news/NYSE-Group-Announces-2022-2023-and-2024-Holiday-and-Early-Closings-Calendar-2021.pdf
#   https://www.nyse.com/trade/hours-calendars (2026-2028 footnotes)
# July 1 2022, Dec 23 2022, Dec 22 2023 and July 2 2026 are normal
# sessions; weekend-adjacent holidays do not imply an early equity close.
EARLY_CLOSE_DATES: dict[date, str] = {
    date(2019, 7, 3): "day_before_independence_day",
    date(2019, 11, 29): "day_after_thanksgiving",
    date(2019, 12, 24): "christmas_eve",
    date(2020, 11, 27): "day_after_thanksgiving",
    date(2020, 12, 24): "christmas_eve",
    date(2021, 11, 26): "day_after_thanksgiving",
    date(2022, 11, 25): "day_after_thanksgiving",
    date(2023, 7, 3): "day_before_independence_day",
    date(2023, 11, 24): "day_after_thanksgiving",
    date(2024, 7, 3): "day_before_independence_day",
    date(2024, 11, 29): "day_after_thanksgiving",
    date(2024, 12, 24): "christmas_eve",
    date(2025, 7, 3): "day_before_independence_day",
    date(2025, 11, 28): "day_after_thanksgiving",
    date(2025, 12, 24): "christmas_eve",
    date(2026, 11, 27): "day_after_thanksgiving",
    date(2026, 12, 24): "christmas_eve",
}

EARLY_CLOSE_TIME = time(13, 0)  # 13:00 America/New_York
NORMAL_CLOSE_TIME = time(16, 0)
NORMAL_OPEN_TIME = time(9, 30)


def is_early_close(d: date) -> tuple[bool, str]:
    if d in EARLY_CLOSE_DATES:
        return True, EARLY_CLOSE_DATES[d]
    return False, ""


def session_times_utc(d: date) -> tuple[str, str, bool, str]:
    """Open/close as UTC ISO strings using America/New_York (DST-aware)."""
    from datetime import timezone as tz

    early, note = is_early_close(d)
    close_t = EARLY_CLOSE_TIME if early else NORMAL_CLOSE_TIME
    open_dt = datetime.combine(d, NORMAL_OPEN_TIME, tzinfo=NY).astimezone(tz.utc)
    close_dt = datetime.combine(d, close_t, tzinfo=NY).astimezone(tz.utc)
    return (
        open_dt.isoformat().replace("+00:00", "Z"),
        close_dt.isoformat().replace("+00:00", "Z"),
        early,
        note or ("early_close" if early else ""),
    )


def write_calendar_csv(path: Path, session_dates: list[date]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["session_date", "open_time_utc", "close_time_utc", "is_early_close", "note"],
        )
        w.writeheader()
        for d in session_dates:
            o, c, early, note = session_times_utc(d)
            w.writerow(
                {
                    "session_date": d.isoformat(),
                    "open_time_utc": o,
                    "close_time_utc": c,
                    "is_early_close": "1" if early else "0",
                    "note": f"{note}; nyse_hours_calendars_2026-10-25",
                }
            )
