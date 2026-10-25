"""Time rules: snapshot availability, week signals, holidays, no next session."""

from __future__ import annotations

from datetime import date, datetime, timezone

from leadtrend_v05.calendar import Calendar
from leadtrend_v05.dataio import CalendarRow
from leadtrend_v05.universe import choose_snapshot
from tests_v05.conftest import (
    cal_row,
    make_inputs,
    member,
    price,
    sec,
    snap,
    utc,
)


def _cal_days(days: list[date]) -> list[CalendarRow]:
    return [cal_row(d) for d in days]


def test_snapshot_not_available_before_public_time():
    d1, d2, d3 = date(2025, 2, 10), date(2025, 2, 11), date(2025, 2, 12)
    inputs = make_inputs(
        calendar=_cal_days([d1, d2, d3]),
        securities=[sec("A")],
        snapshots=[
            snap("S1", date(2025, 1, 31), utc(2025, 2, 11, 14, 30))  # available at d2 open
        ],
        members=[member("S1", "A")],
        prices=[price("A", d1, 10.0), price("A", d2, 10.0), price("A", d3, 10.0)],
    )
    # signal at d1 close: not yet public
    choice = choose_snapshot(inputs, utc(2025, 2, 10, 21, 0))
    assert choice.snapshot is None
    # signal at d2 close: available
    choice = choose_snapshot(inputs, utc(2025, 2, 11, 21, 0))
    assert choice.snapshot is not None
    assert choice.snapshot.snapshot_id == "S1"


def test_date_precision_available_after_next_session_open():
    d1, d2, d3 = date(2025, 2, 10), date(2025, 2, 11), date(2025, 2, 12)
    inputs = make_inputs(
        calendar=_cal_days([d1, d2, d3]),
        securities=[sec("A")],
        snapshots=[
            snap(
                "S1",
                date(2025, 1, 31),
                utc(2025, 2, 11, 14, 30),  # after available_at date 2025-02-10 -> first session 2/11 open
                precision="date",
            )
        ],
        members=[member("S1", "A")],
        prices=[price("A", d1, 10.0), price("A", d2, 10.0), price("A", d3, 10.0)],
    )
    # On d1 close, date-precision snapshot for 2025-02-10 is not yet public
    # (available only from d2 open). Rebuild with available date = d1.
    inputs.snapshots[0].available_at = utc(2025, 2, 11, 14, 30)
    choice = choose_snapshot(inputs, utc(2025, 2, 10, 21, 0))
    assert choice.snapshot is None
    choice = choose_snapshot(inputs, utc(2025, 2, 11, 21, 0))
    assert choice.snapshot is not None


def test_later_revision_does_not_affect_earlier_signal():
    d1, d2, d3, d4 = (
        date(2025, 2, 10),
        date(2025, 2, 11),
        date(2025, 2, 12),
        date(2025, 2, 13),
    )
    inputs = make_inputs(
        calendar=_cal_days([d1, d2, d3, d4]),
        securities=[sec("A"), sec("B")],
        snapshots=[
            snap("S_ORIG", date(2025, 1, 31), utc(2025, 2, 10, 12, 0), revision="orig"),
            snap("S_REV", date(2025, 1, 31), utc(2025, 2, 13, 12, 0), revision="rev"),
        ],
        members=[member("S_ORIG", "A"), member("S_REV", "B")],
        prices=[
            price("A", d1, 10.0),
            price("B", d1, 10.0),
            price("A", d2, 10.0),
            price("B", d2, 10.0),
            price("A", d3, 10.0),
            price("B", d3, 10.0),
            price("A", d4, 10.0),
            price("B", d4, 10.0),
        ],
    )
    early = choose_snapshot(inputs, utc(2025, 2, 12, 21, 0))
    assert early.snapshot.snapshot_id == "S_ORIG"
    assert {m.security_id for m in early.members} == {"A"}

    later = choose_snapshot(inputs, utc(2025, 2, 13, 21, 0))
    assert later.snapshot.snapshot_id == "S_REV"
    assert {m.security_id for m in later.members} == {"B"}


def test_future_snapshot_ignored():
    d1, d2 = date(2025, 2, 10), date(2025, 2, 11)
    inputs = make_inputs(
        calendar=_cal_days([d1, d2]),
        securities=[sec("A"), sec("Z")],
        snapshots=[
            snap("S_NOW", date(2025, 1, 31), utc(2025, 2, 10, 12, 0)),
            snap("S_FUT", date(2025, 2, 11), utc(2025, 7, 1, 12, 0)),
        ],
        members=[member("S_NOW", "A"), member("S_FUT", "Z")],
        prices=[price("A", d1, 1.0), price("A", d2, 1.0), price("Z", d1, 1.0), price("Z", d2, 1.0)],
    )
    choice = choose_snapshot(inputs, utc(2025, 2, 11, 21, 0))
    assert choice.snapshot.snapshot_id == "S_NOW"
    assert "Z" not in {m.security_id for m in choice.members}


def test_week_signal_sessions_skip_friday_holiday():
    days = [
        date(2025, 4, 14),
        date(2025, 4, 15),
        date(2025, 4, 16),
        date(2025, 4, 17),  # Thursday early close
        # 2025-04-18 Friday holiday
        date(2025, 4, 21),
        date(2025, 4, 22),
    ]
    cal = Calendar(_cal_days(days))
    sigs = cal.week_signal_sessions()
    sig_dates = [s.session_date for s in sigs]
    assert date(2025, 4, 17) in sig_dates  # week ends Thursday
    assert date(2025, 4, 18) not in sig_dates
    # next session after 4/17 is 4/21
    nxt = cal.next_session(date(2025, 4, 17))
    assert nxt.session_date == date(2025, 4, 21)


def test_last_signal_without_next_session():
    days = [date(2025, 5, 28), date(2025, 5, 29), date(2025, 5, 30)]
    cal = Calendar(_cal_days(days))
    last = cal.week_signal_sessions()[-1]
    assert last.session_date == date(2025, 5, 30)
    assert cal.next_session(last.session_date) is None
