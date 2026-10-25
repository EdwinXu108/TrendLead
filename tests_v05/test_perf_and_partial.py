"""A4/A6: indexed pipeline scale test; outer exception preserves partial artifacts."""

from __future__ import annotations

import time
from datetime import date

import pytest

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


def _weekdays(n: int, start: date = date(2019, 1, 2)) -> list[date]:
    out = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d = date.fromordinal(d.toordinal() + 1)
    return out


def test_outer_exception_preserves_partial_artifacts():
    """A6: if an exception fires mid-run, universe/scores/trades/equity survive."""
    days = _weekdays(25)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", stype="etf", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 40.0 + j * 0.2, turnover=2e6))
        prices.append(price("S2", d, 50.0 + j * 0.1, turnover=1e6))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    # Force a KeyError path after some equity rows exist: held mark missing.
    # Easiest deterministic crash: a split that yields non-integer shares AFTER
    # the portfolio is built (so trades/equity already exist in state).
    from leadtrend_v05.calendar import Calendar

    cal = Calendar([cal_row(d) for d in days])
    sigs = cal.week_signal_sessions()
    first_exec = None
    for s in sigs:
        nxt = cal.next_session(s.session_date)
        if nxt and s.session_date > days[4]:
            first_exec = nxt.session_date
            break
    assert first_exec is not None
    # reverse split after first build. Only S1 in the universe so it MUST be bought.
    # price 7 → buy floor((100000-1)/7) = 14285 shares; 14285*(1/3) is non-integer.
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 7.0, turnover=2e6))
        prices.append(price("S2", d, 50.0 + j * 0.1, turnover=1e6))
        prices.append(price("SYN_VOO", d, 400.0 + j))
    split_day = days[days.index(first_exec) + 3]
    events = [
        EventRow(
            event_id="EV_RSPLIT",
            security_id="S1",
            event_type="reverse_split",
            effective_session=split_day,
            shares_new_per_old=1.0 / 3.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    snaps = [snap("SNAP_A", days[3], utc(days[4].year, days[4].month, days[4].day, 12, 0))]
    members = [member("SNAP_A", "S1")]
    cfg = make_config(
        L=3, N=1, k=1, b=1.0,
        fee_per_trade=1.0,
        slippage_bps=0.0,
        benchmark_security_id="SYN_VOO",
    )
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
    assert result.status == "failed"
    # Partial artifacts preserved (A6)
    assert result.trade_rows, "expected trades before the failure"
    assert result.equity_rows, "expected equity rows before the failure"
    assert result.universe_rows or result.score_rows or result.decision_rows
    # And the failure is structured
    assert result.failure_context is not None


def test_scale_500_securities_750_sessions():
    """A4: ~500 names × ~750 sessions end-to-end must finish in minutes, not hours."""
    n_stocks = 500
    n_sessions = 750
    days = _weekdays(n_sessions, start=date(2019, 1, 2))

    industries = [f"IND{i%12:02d}" for i in range(n_stocks)]
    securities = []
    for i in range(n_stocks):
        securities.append(
            sec(
                f"R{i:04d}",
                issuer=f"ISS{i:04d}",
                industry=industries[i],
                ticker=f"R{i:04d}",
            )
        )
    securities.append(
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO")
    )

    # Compact price generation (still deterministic)
    prices = []
    for i in range(n_stocks):
        sid = f"R{i:04d}"
        base = 20.0 + (i % 50) * 1.5
        drift = 0.0001 + (i % 17) * 0.00005
        px = base
        for j, d in enumerate(days):
            px = px * (1.0 + drift)
            prices.append(price(sid, d, px, turnover=1_000_000 + i * 100))
    for j, d in enumerate(days):
        prices.append(price("SYN_VOO", d, 300.0 + j * 0.05))

    snaps = [
        snap("SNAP_A", days[20], utc(days[21].year, days[21].month, days[21].day, 12, 0)),
        snap("SNAP_B", days[300], utc(days[301].year, days[301].month, days[301].day, 12, 0)),
    ]
    members = [member("SNAP_A", f"R{i:04d}") for i in range(0, n_stocks, 1)]
    members += [member("SNAP_B", f"R{i:04d}") for i in range(0, n_stocks, 1)]
    cfg = make_config(
        L=20, N=10, k=3, b=2.0,
        benchmark_security_id="SYN_VOO",
    )
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        config=cfg,
    )

    t0 = time.perf_counter()
    result = run_pipeline(inputs)
    elapsed = time.perf_counter() - t0

    assert result.status == "ok", result.summary
    assert result.equity_rows
    assert result.trade_rows
    # Report for the handover (visible in pytest -s or captured)
    print(f"\n[scale] securities={n_stocks} sessions={n_sessions} elapsed={elapsed:.2f}s")
    assert elapsed < 600, f"scale run too slow: {elapsed:.1f}s"
