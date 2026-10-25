"""A2: benchmark day-one is marked at close; open investment reported separately."""

from __future__ import annotations

from datetime import date

import pytest

from leadtrend_v05.benchmark import build_benchmark
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


def test_benchmark_open_purchase_marked_at_close_handcalc():
    """Day 0: open=100, close=110, invest 10_000 → 100 units → value = 100×110 = 11_000.

    Day 1: close=120, dividend $10/unit reinvested at close.
      units = 100 + (100×10)/120 = 108.333...
      value = units × 120 = 12_000 + 1000 = 13_000
    """
    days = _days(3, start=date(2025, 3, 3))
    prices = [
        price("SYN_VOO", days[0], 100.0, 110.0),
        price("SYN_VOO", days[1], 115.0, 120.0),
        price("SYN_VOO", days[2], 118.0, 120.0),
    ]
    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="SYN_VOO",
            event_type="cash_dividend",
            effective_session=days[1],
            shares_new_per_old=None,
            cash_amount_per_share=10.0,
            cash_basis="per_share_at_entitlement",
            pay_date=days[2],
            source="test",
        )
    ]
    bench = build_benchmark(
        "SYN_VOO",
        prices,
        events,
        days,
        start_value=10_000.0,
        start_session=days[0],
    )
    assert bench.start_value_at_open == 10_000.0
    assert bench.open_price == 100.0
    assert bench.close_price_first_day == 110.0
    # Day one is marked at CLOSE: units × close = 100 × 110 = 11_000
    assert bench.points[0].benchmark_value == pytest.approx(11_000.0)
    # Day two after dividend reinvestment at close 120:
    units = 100.0 + 1000.0 / 120.0
    assert bench.points[1].benchmark_value == pytest.approx(units * 120.0)


def test_benchmark_start_equals_first_trade_session():
    days = _days(22)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 40.0 + j * 0.2, turnover=2_000_000))
        prices.append(price("S2", d, 50.0 + j * 0.15, turnover=1_500_000))
        # open != close so A2 close-mark is visible
        prices.append(price("SYN_VOO", d, 400.0 + j * 0.5, 405.0 + j * 0.5))
    snaps = [snap("SNAP_A", days[3], utc(days[6].year, days[6].month, days[6].day, 12, 0))]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=3, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        config=cfg,
    )
    result = run_pipeline(inputs)
    assert result.status == "ok"
    assert result.trade_rows
    first_trade = min(t["session_date"] for t in result.trade_rows)
    s = result.summary
    assert s["comparison_available"] is True
    assert s["comparison_start_session"] == first_trade
    assert s["comparison_start_value_at_open"] == pytest.approx(cfg.initial_capital)
    assert result.benchmark_rows[0]["session_date"] == first_trade

    # Day-one value is units × close, NOT the open investment amount.
    open0 = None
    close0 = None
    for p in prices:
        if p.security_id == "SYN_VOO" and p.session_date.isoformat() == first_trade:
            open0, close0 = p.open, p.close
    assert open0 is not None
    assert float(result.benchmark_rows[0]["benchmark_value"]) == pytest.approx(
        cfg.initial_capital / open0 * close0
    )
    # With open != close this differs from initial_capital
    assert open0 != close0
    assert float(result.benchmark_rows[0]["benchmark_value"]) != pytest.approx(
        cfg.initial_capital
    )
    assert s["comparison_end_session"] == result.equity_rows[-1]["session_date"]


def test_no_trades_comparison_not_available():
    days = _days(22)
    securities = [
        sec("S1", issuer="I1", industry="TECH"),
        sec("S2", issuer="I2", industry="FIN"),
        sec("SYN_VOO", issuer="IV", stype="etf", industry="BENCH", ticker="VOO"),
    ]
    prices = []
    for j, d in enumerate(days):
        prices.append(price("S1", d, 40.0 + j * 0.2))
        prices.append(price("S2", d, 50.0 + j * 0.15))
        prices.append(price("SYN_VOO", d, 400.0 + j * 0.5))
    snaps = [snap("SNAP_A", days[-1], utc(days[-1].year, days[-1].month, days[-1].day, 12, 0))]
    members = [member("SNAP_A", "S1"), member("SNAP_A", "S2")]
    cfg = make_config(L=3, N=2, k=1, b=1.0, benchmark_security_id="SYN_VOO")
    inputs = make_inputs(
        [cal_row(d) for d in days],
        securities,
        snaps,
        members,
        prices,
        config=cfg,
    )
    result = run_pipeline(inputs)
    if not result.trade_rows:
        assert result.summary["comparison_available"] is False
        assert result.summary["comparison_start_session"] is None
        assert result.summary["comparison_start_value_at_open"] is None
        assert result.summary["benchmark_total_return"] is None
        assert "not available" in result.summary["comparison_note"]
    else:
        assert result.summary["comparison_available"] is True
