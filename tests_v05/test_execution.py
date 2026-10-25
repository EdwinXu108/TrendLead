"""Execution: net differences, fees/slippage, no-trade weeks, budget."""

from __future__ import annotations

from datetime import date

import pytest

from leadtrend_v05.execution import adverse_fill, plan_equal_weight_trades
from leadtrend_v05.ledger import AccountState


def test_adverse_fill_directions():
    assert adverse_fill(100.0, "buy", 0.0005) == 100.05
    assert adverse_fill(100.0, "sell", 0.0005) == 99.95


def test_no_swap_week_zero_trades():
    acct = AccountState(cash=1000.0, positions={"A": 10})
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["A"],
        open_prices={"A": 100.0},
        fee_per_trade=1.0,
        slippage_rate=0.0005,
        session_date=date(2025, 3, 3),
        is_swap_week=False,
    )
    assert trades == []
    assert acct.shares_of("A") == 10
    assert acct.cash == 1000.0


def test_swap_week_net_difference_not_sell_and_buy():
    acct = AccountState(cash=0.0, positions={"A": 100})
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["A", "B"],
        open_prices={"A": 50.0, "B": 50.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=date(2025, 3, 3),
        is_swap_week=True,
    )
    sides_a = [t.side for t in trades if t.security_id == "A"]
    # only one side for A
    assert len(set(sides_a)) <= 1
    # never both buy and sell for same security in one plan
    for sid in {"A", "B"}:
        sides = {t.side for t in trades if t.security_id == sid}
        assert not ({"buy", "sell"} <= sides)


def test_exit_then_equal_weight_hand_check():
    """Cash 100k, targets A/B at $100, no prior positions.

    budget_each = 50000, ideal = floor((50000-1)/100) = 499 shares each.
    cost = 499*100+1 = 49901; total 99802; residual cash 198.
    """
    acct = AccountState(cash=100000.0)
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["A", "B"],
        open_prices={"A": 100.0, "B": 100.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=date(2025, 2, 18),
        is_swap_week=True,
    )
    by = {t.security_id: t for t in trades}
    assert by["A"].shares == 499
    assert by["B"].shares == 499
    assert by["A"].fee == 1.0
    assert by["A"].cash_delta == -(499 * 100.0 + 1.0)
    assert acct.cash == pytest.approx(100000.0 - 2 * (499 * 100.0 + 1.0))
    assert acct.cash >= -1e-6


def test_integer_floor_and_fee_visible():
    acct = AccountState(cash=2000.0)
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["A"],
        open_prices={"A": 300.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=date(2025, 2, 18),
        is_swap_week=True,
    )
    # budget=2000, ideal=floor(2000/300)=6, cost=6*300+1=1801
    assert len(trades) == 1
    assert trades[0].shares == 6
    assert trades[0].fee == 1.0
    assert trades[0].cash_delta == -(6 * 300.0 + 1.0)
    assert acct.cash == 2000.0 - 1801.0


def test_slippage_worsens_fill():
    acct = AccountState(cash=10000.0)
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["A"],
        open_prices={"A": 100.0},
        fee_per_trade=1.0,
        slippage_rate=0.0005,
        session_date=date(2025, 2, 18),
        is_swap_week=True,
    )
    t = trades[0]
    assert t.fill_price == 100.05
    assert t.reference_price == 100.0
    assert t.shares == int((10000 - 1) // 100)  # 99
    # cash check uses fill price and fee; never negative
    assert acct.cash >= -1e-6


def test_forced_exit_sells_all():
    acct = AccountState(cash=0.0, positions={"OLD": 10, "KEEP": 5})
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["KEEP", "NEW"],
        open_prices={"OLD": 20.0, "KEEP": 40.0, "NEW": 40.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=date(2025, 3, 3),
        is_swap_week=True,
    )
    sells = [t for t in trades if t.security_id == "OLD"]
    assert sells and sells[0].side == "sell" and sells[0].shares == 10
    assert acct.shares_of("OLD") == 0
