"""Fix 2: continuous scoring window; event-without-price is fatal."""

from __future__ import annotations

from datetime import date

import pytest

from leadtrend_v05.scoring import TRIEventError, build_tri, momentum_score
from tests_v05.conftest import EventRow, price


def _sessions(n: int = 30, start: date = date(2025, 1, 2)) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_insufficient_history():
    days = _sessions(5)
    tri = {d: 100.0 + i for i, d in enumerate(days)}
    sc, _, _, win, reason = momentum_score(tri, days[-1], L=20, session_dates=days)
    assert sc is None
    assert reason == "insufficient_history"


def test_contiguous_window_scores():
    days = _sessions(30)
    tri = {d: 100.0 * (1.01**i) for i, d in enumerate(days)}
    sc, _, _, win, reason = momentum_score(tri, days[-1], L=5, session_dates=days)
    assert reason == ""
    assert win == 6
    assert sc == pytest.approx(tri[days[-1]] / tri[days[-6]] - 1.0)


def test_price_gap_in_window_not_skipped():
    days = _sessions(30)
    tri = {d: 100.0 + i for i, d in enumerate(days)}
    # drop one day in the middle of the last L+1=6 window
    gap_day = days[-3]
    del tri[gap_day]
    sc, _, _, win, reason = momentum_score(
        tri, days[-1], L=5, session_dates=days, first_valid_price_date=days[0]
    )
    assert sc is None
    assert reason == "price_gap_in_window"


def test_gap_outside_window_is_ok():
    days = _sessions(30)
    tri = {d: 100.0 + i for i, d in enumerate(days)}
    del tri[days[0]]  # far before the L+1 window
    sc, _, _, win, reason = momentum_score(tri, days[-1], L=5, session_dates=days)
    assert reason == ""
    assert sc is not None


def test_missing_signal_day():
    days = _sessions(30)
    tri = {d: 100.0 + i for i, d in enumerate(days)}
    del tri[days[-1]]
    sc, _, _, _, reason = momentum_score(tri, days[-1], L=5, session_dates=days)
    assert sc is None
    assert reason == "missing_price_at_signal"


def test_build_tri_event_on_missing_price_records_unresolved():
    days = _sessions(10)
    prices = [price("S1", d, 10.0 + i) for i, d in enumerate(days) if d != days[5]]
    events = [
        EventRow(
            event_id="EV_SPLIT",
            security_id="S1",
            event_type="split",
            effective_session=days[5],
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    tr = build_tri(prices, events, days)
    assert tr.unresolved_events
    assert tr.invalid_from == days[5]
    assert days[5] not in tr.tri  # TRI stops before the blocked date


def test_build_tri_dividend_on_missing_price_records_unresolved():
    days = _sessions(10)
    prices = [price("S1", d, 10.0 + i) for i, d in enumerate(days) if d != days[5]]
    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="S1",
            event_type="cash_dividend",
            effective_session=days[5],
            shares_new_per_old=None,
            cash_amount_per_share=0.5,
            cash_basis="per_share_at_entitlement",
            pay_date=days[-1],
            source="test",
        )
    ]
    tr = build_tri(prices, events, days)
    assert tr.unresolved_events
    assert tr.unresolved_events[0]["reason"] == "event_price_gap"
