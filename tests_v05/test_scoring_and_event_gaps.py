"""A3: insufficient_history vs price_gap_in_window; A5: unresolved event price gap."""

from __future__ import annotations

from datetime import date

from leadtrend_v05.dataio import EventRow
from leadtrend_v05.pipeline import run_pipeline
from leadtrend_v05.scoring import build_tri, momentum_score
from tests_v05.conftest import (
    cal_row,
    make_config,
    make_inputs,
    member,
    price,
    sec,
    snap,
    utc,
)


def _days(n: int, start: date = date(2025, 1, 2)) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_midwindow_start_is_insufficient_history_not_price_gap():
    """A name that only begins printing prices part-way through the window
    must be insufficient_history, not price_gap_in_window."""
    days = _days(12)
    L = 8
    # first 6 sessions have no S1 prices; then prices start
    first_valid = days[6]
    tri = {d: 100.0 + i for i, d in enumerate(days) if d >= first_valid}
    # Window of L+1=9 ends at days[-1] and includes days before first_valid
    sc, _, _, _, reason = momentum_score(
        tri,
        days[-1],
        L=L,
        session_dates=days,
        first_valid_price_date=first_valid,
    )
    assert sc is None
    assert reason == "insufficient_history"


def test_gap_after_start_still_price_gap():
    days = _days(20)
    L = 5
    tri = {d: 100.0 + i for i, d in enumerate(days)}
    del tri[days[-3]]
    sc, _, _, _, reason = momentum_score(
        tri,
        days[-1],
        L=L,
        session_dates=days,
        first_valid_price_date=days[0],
    )
    assert sc is None
    assert reason == "price_gap_in_window"


def test_unresolved_event_blocks_scoring_from_event_date():
    days = _days(20)
    prices = [price("S1", d, 20.0) for d in days if d != days[10]]
    events = [
        EventRow(
            event_id="EV_SPLIT",
            security_id="S1",
            event_type="split",
            effective_session=days[10],
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    tri_res = build_tri(prices, events, days)
    assert tri_res.unresolved_events
    assert tri_res.invalid_from == days[10]
    sc, _, _, _, reason = momentum_score(
        tri_res.tri,
        days[12],
        L=2,
        session_dates=days,
        unresolved_from=tri_res.invalid_from,
    )
    assert reason == "unresolved_event_price_gap"
    # Before the event date, scoring can still work if the window fits
    sc2, _, _, _, reason2 = momentum_score(
        tri_res.tri,
        days[9],
        L=2,
        session_dates=days,
        first_valid_price_date=days[0],
        unresolved_from=tri_res.invalid_from,
    )
    assert reason2 == ""


def test_unheld_event_price_gap_does_not_fail_run():
    """A5: unheld security with event on missing-price day → warning, not fatal."""
    days = _days(22)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("S3", issuer="I3", industry="HLTH"),
        sec("SYN_VOO", stype="etf", ticker="VOO"),
    ]
    prices = []
    gap_day = days[12]
    for j, d in enumerate(days):
        prices.append(price("S1", d, 50.0 + j * 0.2, turnover=2e6))
        prices.append(price("S2", d, 60.0 + j * 0.15, turnover=1e6))
        # S3 has a price gap on gap_day and a split there; never selected/held
        if d != gap_day:
            prices.append(price("S3", d, 30.0 + j * 0.05, turnover=1e5))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    events = [
        EventRow(
            event_id="EV_SPLIT_S3",
            security_id="S3",
            event_type="split",
            effective_session=gap_day,
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    snaps = [snap("SNAP_A", days[3], utc(days[4].year, days[4].month, days[4].day, 12, 0))]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2"), member("SNAP_A", "S3")]
    cfg = make_config(L=3, N=1, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )
    result = run_pipeline(inputs)
    # S3 never held (N=1 picks the stronger S1/S2) → run must succeed with a warning
    assert result.status == "ok", result.summary
    assert any(
        i.get("severity") == "warning" and "unresolved_event_price_gap" in i.get("message", "")
        for i in result.data_issues
    )
    # S3 must not appear as a fill candidate after the gap
    for d in result.decision_rows:
        if d["security_id"] == "S3" and d["action"] == "fill":
            assert d["signal_session_date"] < gap_day.isoformat() or d.get("score") == ""


def test_held_event_price_gap_fails():
    """A5: held security with event on missing-price day → explicit fail."""
    days = _days(22)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", stype="etf", ticker="VOO"),
    ]
    gap_day = days[12]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 50.0 + j * 0.5, turnover=2e6))
        prices.append(price("S2", d, 40.0 + j * 0.1, turnover=1e5))
        if d != gap_day:
            # S1 missing price only on gap_day — but then it can't be marked either.
            # To isolate the event path we also give S1 a price on other days only.
            pass
        if d != gap_day:
            prices.append(price("S1", d, 50.0 + j * 0.5, turnover=2e6))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    # rebuild clean S1 prices without duplicates
    prices = []
    for j, d in enumerate(days):
        if d != gap_day:
            prices.append(price("S1", d, 50.0 + j * 0.5, turnover=2e6))
        prices.append(price("S2", d, 40.0 + j * 0.1, turnover=1e5))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    events = [
        EventRow(
            event_id="EV_SPLIT_S1",
            security_id="S1",
            event_type="split",
            effective_session=gap_day,
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    snaps = [snap("SNAP_A", days[3], utc(days[4].year, days[4].month, days[4].day, 12, 0))]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=3, N=1, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )
    result = run_pipeline(inputs)
    # Either fails on held corporate-action gap, or fails on missing close mark
    # while S1 is held — both are explicit failures (never silent success).
    assert result.status == "failed"
    msg = result.summary.get("failure_message", "").lower()
    assert (
        "corporate action" in msg
        or "missing close" in msg
        or "missing-price" in msg
        or "unscorable" in msg
    )
