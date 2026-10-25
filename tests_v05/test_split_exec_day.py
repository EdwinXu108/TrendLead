"""Fix 4d: split on execution session with exact hand-calculated share/trade counts."""

from __future__ import annotations

from datetime import date

import pytest

from leadtrend_v05.calendar import Calendar
from leadtrend_v05.dataio import EventRow
from leadtrend_v05.pipeline import run_pipeline
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


def _weekdays(n: int, start: date) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_split_at_exec_day_exact_shares():
    """Week-1 build buys S1 only. Week-2 exec day applies 1->2 split and fills S2.

    Hand-calc (fee=1, slippage=0, capital=100_000, N=2):

    Week-1 exec (S1 open=100):
      budget_each = 100000/1 = 100000
      ideal_S1 = floor((100000-1)/100) = 999
      cash after = 100000 - (999*100+1) = 99

    Week-2 exec = split day:
      S1 shares 999 -> 1998 after 1->2 split
      open S1=50 (post-split), S2=50
      equity_at_open = 1998*50 + 99 = 99900 + 99 = 99_999
      budget_each = 49_999.5
      ideal_S1 = floor((49999.5-1)/50) = floor(999.97) = 999
      ideal_S2 = floor((49999.5-1)/50) = 999
      net_S1 = 999 - 1998 = -999  -> sell 999
      net_S2 = 999 - 0   = +999  -> buy 999
      (netting: never sell and buy S1 together)
    """
    days = _weekdays(20, date(2025, 1, 2))
    cal_rows = [cal_row(d) for d in days]
    cal = Calendar(cal_rows)
    sigs = cal.week_signal_sessions()

    # snapshot available from days[6] open-ish
    avail = utc(days[6].year, days[6].month, days[6].day, 12, 0)

    # Find first signal whose exec session is strictly after snapshot availability.
    # We will put the split on the SECOND such exec day so S1 is already held.
    exec_days = []
    for s in sigs:
        nxt = cal.next_session(s.session_date)
        if nxt:
            exec_days.append((s.session_date, nxt.session_date))
    usable = [(s, e) for s, e in exec_days if e > days[6]]
    assert len(usable) >= 2
    split_day = usable[1][1]

    # Fixed prices:
    # S1 = 100 before split_day, 50 on and after split_day
    # S2 = 50 always
    # S2 is not in SNAP_A (so week-1 target is S1 only with N=2 underfilled)
    # Actually N=2 with only S1 eligible -> target [S1] only. Then week-2 snapshot adds S2.
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    for d in days:
        s1 = 50.0 if d >= split_day else 100.0
        prices.append(price("S1", d, s1, s1))
        prices.append(price("S2", d, 50.0, 50.0))
        prices.append(price("SYN_VOO", d, 400.0, 400.0))

    events = [
        EventRow(
            event_id="EV_SPLIT",
            security_id="S1",
            event_type="split",
            effective_session=split_day,
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]

    # SNAP_A: only S1 (first build). SNAP_B: S1+S2 (later, so week-2 has a fill).
    snap_a_date = days[3]
    snap_b_date = days[10]
    snaps = [
        snap("SNAP_A", snap_a_date, avail),
        snap(
            "SNAP_B",
            snap_b_date,
            utc(split_day.year, split_day.month, split_day.day, 8, 0),
        ),
    ]
    # If snap_b available_at is at split_day 08:00 UTC which is before that session's
    # close, the signal BEFORE split_day (week-1 or a middle week) might already see SNAP_B.
    # We need SNAP_B to appear only for the signal that executes on split_day.
    # Safer: SNAP_B available after the prior signal close and before split_day close.
    # Use date precision on the calendar day before split_day.
    prior = days[days.index(split_day) - 1]
    snaps = [
        snap("SNAP_A", snap_a_date, avail, precision="timestamp"),
        snap("SNAP_B", snap_b_date, utc(prior.year, prior.month, prior.day, 12, 0)),
    ]

    members = [
        member("SNAP_A", "S1"),
        member("SNAP_B", "S1"),
        member("SNAP_B", "S2"),
    ]
    cfg = make_config(
        L=2,
        N=2,
        k=1,
        b=1.0,
        fee_per_trade=1.0,
        slippage_bps=0.0,
        benchmark_security_id="SYN_VOO",
    )
    inputs = make_inputs(
        cal_rows,
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )
    result = run_pipeline(inputs)
    assert result.status == "ok", result.summary

    # --- Week-1 exec: buy S1 only ---
    trades_w1 = [
        t
        for t in result.trade_rows
        if t["security_id"] == "S1" and t["session_date"] < split_day.isoformat()
    ]
    buys_w1 = [t for t in trades_w1 if t["side"] == "buy"]
    assert len(buys_w1) == 1
    assert int(buys_w1[0]["shares"]) == 999
    assert float(buys_w1[0]["fee"]) == 1.0

    # --- Week-2 exec (= split day): sell S1 999 and buy S2 999 ---
    trades_split = [
        t for t in result.trade_rows if t["session_date"] == split_day.isoformat()
    ]
    by_sid = {}
    for t in trades_split:
        by_sid.setdefault(t["security_id"], []).append(t)

    # S1: exactly one net sell of 999 (from 1998 down to 999)
    s1_trades = by_sid.get("S1", [])
    assert len(s1_trades) == 1
    assert s1_trades[0]["side"] == "sell"
    assert int(s1_trades[0]["shares"]) == 999
    # reference price is post-split open
    assert float(s1_trades[0]["reference_price"]) == pytest.approx(50.0)

    # S2: buy 999
    s2_trades = by_sid.get("S2", [])
    assert len(s2_trades) == 1
    assert s2_trades[0]["side"] == "buy"
    assert int(s2_trades[0]["shares"]) == 999

    # No double side on S1
    assert {t["side"] for t in s1_trades} == {"sell"}
