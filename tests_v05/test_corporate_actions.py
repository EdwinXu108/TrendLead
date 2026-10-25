"""Dividend entitlement vs open trades on ex-date; split between signal and execution."""

from __future__ import annotations

from datetime import date

from leadtrend_v05.execution import plan_equal_weight_trades
from leadtrend_v05.ledger import AccountState, confirm_dividend, pay_receivables


def test_exdate_open_sell_still_gets_dividend():
    """Entitlement uses previous-close holdings; sell at open of ex-date keeps dividend."""
    acct = AccountState(cash=0.0, positions={"S1": 100})
    prev_close_shares = dict(acct.positions)  # close of day before ex

    ex_day = date(2025, 4, 10)
    pay_day = date(2025, 4, 25)

    # start of ex_day: confirm dividend from prior close BEFORE open trades
    entitled = prev_close_shares.get("S1", 0)
    confirm_dividend(acct, "S1", "EV_DIV", 1.0, ex_day, pay_day, entitled)
    assert acct.receivable_cash == 100.0

    # open sell entire position
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=[],  # exit all
        open_prices={"S1": 50.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=ex_day,
        is_swap_week=True,
    )
    assert any(t.security_id == "S1" and t.side == "sell" for t in trades)
    assert acct.shares_of("S1") == 0
    assert acct.receivable_cash == 100.0  # still entitled

    pay_receivables(acct, pay_day)
    assert acct.cash == 100.0 * 50.0 - 1.0 + 100.0


def test_exdate_open_buy_does_not_get_dividend():
    acct = AccountState(cash=10_000.0)
    prev_close_shares = dict(acct.positions)  # empty

    ex_day = date(2025, 4, 10)
    pay_day = date(2025, 4, 25)

    entitled = prev_close_shares.get("S1", 0)
    confirm_dividend(acct, "S1", "EV_DIV", 1.0, ex_day, pay_day, entitled)
    assert acct.receivable_cash == 0.0

    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["S1"],
        open_prices={"S1": 50.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=ex_day,
        is_swap_week=True,
    )
    assert any(t.security_id == "S1" and t.side == "buy" for t in trades)
    pay_receivables(acct, pay_day)
    assert acct.receivable_cash == 0.0
    assert acct.cash == 10_000.0 - (trades[0].shares * 50.0 + 1.0)


def test_split_applied_before_execution_netting():
    """Split on exec session: convert existing shares first, then net."""
    acct = AccountState(cash=0.0, positions={"S1": 100})
    # 1 -> 2 split at open of exec session
    from leadtrend_v05.ledger import apply_split

    apply_split(acct, "S1", 2.0, "EV_SPLIT")
    assert acct.shares_of("S1") == 200

    # post-split open price is half
    trades = plan_equal_weight_trades(
        account=acct,
        target_ids=["S1", "S2"],
        open_prices={"S1": 50.0, "S2": 50.0},
        fee_per_trade=1.0,
        slippage_rate=0.0,
        session_date=date(2025, 3, 20),
        is_swap_week=True,
    )
    # equity at open = 200*50 = 10000; budget_each=5000; ideal=floor(4999/50)=99
    # S1 currently 200 -> sell 101 to get to 99
    t_s1 = [t for t in trades if t.security_id == "S1"]
    assert t_s1 and t_s1[0].side == "sell"
    assert acct.shares_of("S1") == 99
    # If we had mixed pre-split 100 shares with post-split prices we would target ~99
    # from a phantom 5000 value; instead we correctly started from 200 shares.
