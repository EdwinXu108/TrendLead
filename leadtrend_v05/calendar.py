"""Session calendar utilities. Sessions come only from explicit input, never Mon-Fri rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from .dataio import CalendarRow
from .timeutil import NY, ensure_utc, to_ny


@dataclass(frozen=True)
class Session:
    session_date: date
    open_time_utc: datetime
    close_time_utc: datetime
    is_early_close: bool
    note: str

    @property
    def open_time_ny(self) -> datetime:
        return to_ny(self.open_time_utc)

    @property
    def close_time_ny(self) -> datetime:
        return to_ny(self.close_time_utc)


class Calendar:
    def __init__(self, rows: list[CalendarRow]):
        self.sessions: list[Session] = [
            Session(
                session_date=r.session_date,
                open_time_utc=ensure_utc(r.open_time_utc),
                close_time_utc=ensure_utc(r.close_time_utc),
                is_early_close=r.is_early_close,
                note=r.note,
            )
            for r in rows
        ]
        self.sessions.sort(key=lambda s: s.session_date)
        self._by_date = {s.session_date: s for s in self.sessions}

    def __len__(self) -> int:
        return len(self.sessions)

    def get(self, session_date: date) -> Session | None:
        return self._by_date.get(session_date)

    def index(self, session_date: date) -> int:
        for i, s in enumerate(self.sessions):
            if s.session_date == session_date:
                return i
        raise KeyError(session_date)

    def next_session(self, session_date: date) -> Session | None:
        for s in self.sessions:
            if s.session_date > session_date:
                return s
        return None

    def week_signal_sessions(self) -> list[Session]:
        """Last session of each America/New_York ISO week present in the calendar."""
        if not self.sessions:
            return []
        groups: list[list[Session]] = []
        current_key = None
        current: list[Session] = []
        for s in self.sessions:
            ny = to_ny(s.close_time_utc)
            iso = ny.isocalendar()
            key = (iso[0], iso[1])
            if current_key is None or key != current_key:
                if current:
                    groups.append(current)
                current_key = key
                current = [s]
            else:
                current.append(s)
        if current:
            groups.append(current)
        return [g[-1] for g in groups]

    def date_precision_available_instant(self, available_date: date) -> datetime | None:
        for s in self.sessions:
            if s.session_date > available_date:
                return s.open_time_utc
        return None
