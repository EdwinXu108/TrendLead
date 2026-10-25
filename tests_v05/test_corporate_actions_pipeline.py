"""Pipeline-level dividend tests with scenarios that MUST occur (A1)."""

from __future__ import annotations

from datetime import date

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


def _days(n: int, start: date = date(2025, 1, 2)) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def _build(days, prices_fn, events, N=1, L=2, members_ids=("S1", "S2")):
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        prices.extend(prices_fn(j, d))
        prices.append(price("SYN_VOO", d, 400.0 + j * 0.3))
    # Snapshot public after day 4, before the first scoreable week-end.
    snaps = [snap("SNAP_A", days[3], utc(days[4].year, days[4].month, days[4].day, 12, 0))]
    members = [member("SNAP_A", sid) for sid in members_ids]
    cfg = make_config(L=L, N=N, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    return make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        events=events,
        config=cfg,
    )


def test_held_through_exdate_receivable_then_cash():
    """(a) held before ex-date and still held: receivable then cash on pay_date."""
    days = _days(28)
    ex_day = days[16]
    pay_day = days[22]

    def prices_fn(j, d):
        px1 = 50.0 + j * 0.2
        px2 = 60.0 + j * 0.15
        if d == ex_day:
            px1 = 50.0 + j * 0.2 - 1.25
        return [price("S1", d, px1), price("S2", d, px2)]

    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="S1",
            event_type="cash_dividend",
            effective_session=ex_day,
            shares_new_per_old=None,
            cash_amount_per_share=1.25,
            cash_basis="per_share_at_entitlement",
            pay_date=pay_day,
            source="test",
        )
    ]
    result = run_pipeline(_build(days, prices_fn, events, N=1, L=3))
    assert result.status == "ok"

    eq = {r["session_date"]: r for r in result.equity_rows}
    prior = days[days.index(ex_day) - 1]
    assert float(eq[prior.isoformat()]["receivable_cash"]) == 0.0

    ex_row = eq[ex_day.isoformat()]
    rec_on_ex = float(ex_row["receivable_cash"])
    assert rec_on_ex > 0.0
    entitled_shares = rec_on_ex / 1.25
    assert entitled_shares == int(entitled_shares) > 0

    assert abs(
        float(ex_row["stock_value"]) + float(ex_row["cash"]) + rec_on_ex - float(ex_row["total_equity"])
    ) < 1e-6

    mid = days[days.index(ex_day) + 2]
    assert float(eq[mid.isoformat()]["receivable_cash"]) == rec_on_ex

    prior_pay = days[days.index(pay_day) - 1]
    cash_before = float(eq[prior_pay.isoformat()]["cash"])
    cash_after = float(eq[pay_day.isoformat()]["cash"])
    assert float(eq[pay_day.isoformat()]["receivable_cash"]) == 0.0
    assert abs((cash_after - cash_before) - rec_on_ex) < 1e-6


def test_exdate_exec_day_open_sell_still_gets_dividend():
    """(b) MUST sell S1 at open of ex-date and still receive the dividend.

    Deterministic path:
      week-1 signal buys S1 (N=1); week-2 signal replaces S1 with S2;
      week-2 exec day is the dividend ex-date for S1.
    """
    days = _days(30)
    # After snapshot (day 4), first week-end with history is around day 8-9.
    # We discover exec days from the same calendar rules as the pipeline.
    from leadtrend_v05.calendar import Calendar

    cal = Calendar([cal_row(d) for d in days])
    sigs = cal.week_signal_sessions()
    # first two signals that can trade after snapshot visibility (day 4)
    usable = []
    for s in sigs:
        nxt = cal.next_session(s.session_date)
        if nxt and s.session_date > days[4]:
            usable.append((s.session_date, nxt.session_date))
    assert len(usable) >= 2
    # week-2 exec is the sell day = ex-date
    ex_day = usable[1][1]
    pay_day = days[days.index(ex_day) + 5]

    def prices_fn(j, d):
        # S1 dominates until the first exec, then collapses; S2 takes over.
        first_exec = usable[0][1]
        if d < first_exec:
            px1 = 100.0 + j * 2.0
            px2 = 50.0
        else:
            px1 = max(20.0, 100.0 - j * 1.5)
            px2 = 40.0 + j * 3.0
        return [price("S1", d, px1), price("S2", d, px2)]

    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="S1",
            event_type="cash_dividend",
            effective_session=ex_day,
            shares_new_per_old=None,
            cash_amount_per_share=0.75,
            cash_basis="per_share_at_entitlement",
            pay_date=pay_day,
            source="test",
        )
    ]
    result = run_pipeline(_build(days, prices_fn, events, N=1, L=2))
    assert result.status == "ok", result.summary

    sells_on_ex = [
        t
        for t in result.trade_rows
        if t["session_date"] == ex_day.isoformat()
        and t["security_id"] == "S1"
        and t["side"] == "sell"
    ]
    # Scenario must have happened:
    assert sells_on_ex, "expected S1 to be sold at open of ex-date"

    eq = {r["session_date"]: r for r in result.equity_rows}
    rec = float(eq[ex_day.isoformat()]["receivable_cash"])
    assert rec > 0.0, "sold at open of ex-date but no dividend receivable"
    entitled = rec / 0.75
    assert entitled == int(entitled) > 0
    # S1 fully sold that day
    assert int(sells_on_ex[0]["shares"]) == int(entitled)


def test_exdate_exec_day_open_buy_gets_no_dividend():
    """(c) MUST buy S2 at open of ex-date and NOT receive the dividend.

    week-1 buys S1; week-2 replaces S1 with S2; S2 dividend ex = week-2 exec day.
    S2 was not held at prior close, so receivable must be 0 on ex-day.
    """
    days = _days(30)
    from leadtrend_v05.calendar import Calendar

    cal = Calendar([cal_row(d) for d in days])
    sigs = cal.week_signal_sessions()
    usable = []
    for s in sigs:
        nxt = cal.next_session(s.session_date)
        if nxt and s.session_date > days[4]:
            usable.append((s.session_date, nxt.session_date))
    assert len(usable) >= 2
    ex_day = usable[1][1]
    pay_day = days[days.index(ex_day) + 5]

    def prices_fn(j, d):
        first_exec = usable[0][1]
        if d < first_exec:
            px1 = 100.0 + j * 2.0
            px2 = 50.0
        else:
            px1 = max(20.0, 100.0 - j * 1.5)
            px2 = 40.0 + j * 3.0
        return [price("S1", d, px1), price("S2", d, px2)]

    events = [
        EventRow(
            event_id="EV_DIV_S2",
            security_id="S2",
            event_type="cash_dividend",
            effective_session=ex_day,
            shares_new_per_old=None,
            cash_amount_per_share=0.50,
            cash_basis="per_share_at_entitlement",
            pay_date=pay_day,
            source="test",
        )
    ]
    result = run_pipeline(_build(days, prices_fn, events, N=1, L=2))
    assert result.status == "ok", result.summary

    buys_on_ex = [
        t
        for t in result.trade_rows
        if t["session_date"] == ex_day.isoformat()
        and t["security_id"] == "S2"
        and t["side"] == "buy"
    ]
    assert buys_on_ex, "expected S2 to be bought at open of ex-date"

    # S2 was not held before this buy (first S2 purchase is on ex-day)
    prior_s2_buys = [
        t
        for t in result.trade_rows
        if t["security_id"] == "S2" and t["session_date"] < ex_day.isoformat()
    ]
    assert not prior_s2_buys, "S2 must not have been held before the ex-date buy"

    eq = {r["session_date"]: r for r in result.equity_rows}
    rec_on_ex = float(eq[ex_day.isoformat()]["receivable_cash"])
    assert rec_on_ex == 0.0, "open buy on ex-date must not receive the dividend"

    # Even after pay date, no S2 dividend cash appears from this event
    # (cash may change from trades; receivable stays 0)
    if pay_day.isoformat() in eq:
        assert float(eq[pay_day.isoformat()]["receivable_cash"]) == 0.0
