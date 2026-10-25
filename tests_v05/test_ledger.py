"""Ledger: splits, dividends, event idempotence, cash identity."""

from __future__ import annotations

from datetime import date

import pytest

from leadtrend_v05.ledger import (
    AccountState,
    apply_split,
    confirm_dividend,
    pay_receivables,
    record_trade_cash,
)
from leadtrend_v05.scoring import build_tri
from tests_v05.conftest import EventRow, price


def test_split_doubles_shares_without_fake_pnl():
    acct = AccountState(cash=1000.0, positions={"S1": 100})
    apply_split(acct, "S1", 2.0, "EV1")
    assert acct.shares_of("S1") == 200
    # value unchanged at half price
    stock, cash, rec, total = acct.equity_components({"S1": 50.0})
    assert stock == 200 * 50.0
    assert total == 1000.0 + 10000.0


def test_split_event_id_not_reprocessed():
    acct = AccountState(cash=0.0, positions={"S1": 100})
    apply_split(acct, "S1", 2.0, "EV1")
    apply_split(acct, "S1", 2.0, "EV1")  # replay
    assert acct.shares_of("S1") == 200


def test_split_non_integer_result_rejected():
    acct = AccountState(cash=0.0, positions={"S1": 101})
    with pytest.raises(ValueError):
        apply_split(acct, "S1", 1.5, "EV1")


def test_dividend_entitlement_then_payment():
    acct = AccountState(cash=100.0, positions={"S1": 10})
    confirm_dividend(
        acct,
        "S1",
        "EV_DIV",
        cash_per_share=0.5,
        ex_date=date(2025, 4, 10),
        pay_date=date(2025, 4, 25),
        entitled_shares=10,
    )
    stock, cash, rec, total = acct.equity_components({"S1": 20.0})
    assert rec == 5.0
    assert cash == 100.0
    assert total == 100.0 + 200.0 + 5.0

    # before pay_date: still receivable
    paid = pay_receivables(acct, date(2025, 4, 20))
    assert paid == []
    assert acct.cash == 100.0

    paid = pay_receivables(acct, date(2025, 4, 25))
    assert len(paid) == 1
    assert acct.cash == 105.0
    assert acct.receivable_cash == 0.0
    # no double count in total
    stock, cash, rec, total = acct.equity_components({"S1": 20.0})
    assert rec == 0.0
    assert total == 105.0 + 200.0


def test_dividend_event_id_not_reprocessed():
    acct = AccountState(cash=0.0, positions={"S1": 10})
    confirm_dividend(acct, "S1", "EV_DIV", 0.5, date(2025, 4, 10), date(2025, 4, 25), 10)
    confirm_dividend(acct, "S1", "EV_DIV", 0.5, date(2025, 4, 10), date(2025, 4, 25), 10)
    assert acct.receivable_cash == 5.0


def test_zero_shares_no_dividend():
    acct = AccountState(cash=0.0)
    confirm_dividend(acct, "S1", "EV_DIV", 0.5, date(2025, 4, 10), date(2025, 4, 25), 0)
    assert acct.receivable_cash == 0.0


def test_trade_cash_buy_and_sell():
    acct = AccountState(cash=1000.0)
    delta = record_trade_cash(acct, "S1", 10, "buy", 50.0, 1.0)
    assert delta == -(10 * 50.0 + 1.0)
    assert acct.cash == 1000.0 - 501.0
    assert acct.shares_of("S1") == 10
    delta = record_trade_cash(acct, "S1", 10, "sell", 55.0, 1.0)
    assert delta == 10 * 55.0 - 1.0
    assert acct.cash == pytest.approx(1000.0 - 501.0 + 549.0)
    assert acct.shares_of("S1") == 0


def test_negative_cash_rejected():
    acct = AccountState(cash=10.0)
    with pytest.raises(ValueError):
        record_trade_cash(acct, "S1", 10, "buy", 50.0, 1.0)


def test_zero_qty_no_fee_and_no_cash_change():
    acct = AccountState(cash=100.0, positions={"S1": 5})
    delta = record_trade_cash(acct, "S1", 0, "buy", 50.0, 1.0)
    assert delta == 0.0
    assert acct.cash == 100.0


def test_tri_split_no_jump_and_dividend_reinvested():
    from datetime import date as D

    sessions = [D(2025, 3, 18), D(2025, 3, 19), D(2025, 3, 20), D(2025, 3, 21)]
    prices = [
        price("S1", sessions[0], 100.0),
        price("S1", sessions[1], 100.0),
        price("S1", sessions[2], 50.0),  # 1->2 split, price halves
        price("S1", sessions[3], 50.0),
    ]
    events = [
        EventRow(
            event_id="EV_SPLIT",
            security_id="S1",
            event_type="split",
            effective_session=sessions[2],
            shares_new_per_old=2.0,
            cash_amount_per_share=None,
            cash_basis="",
            pay_date=None,
            source="test",
        )
    ]
    tri = build_tri(prices, events, sessions)
    # value continuous: 1*100=100 -> 2*50=100
    assert tri.tri[sessions[1]] == pytest.approx(100.0)
    assert tri.tri[sessions[2]] == pytest.approx(100.0)


def test_tri_dividend_reinvest():
    from datetime import date as D

    sessions = [D(2025, 4, 9), D(2025, 4, 10), D(2025, 4, 11)]
    prices = [
        price("S1", sessions[0], 100.0),
        price("S1", sessions[1], 99.5),  # ex-date mechanical drop
        price("S1", sessions[2], 99.5),
    ]
    events = [
        EventRow(
            event_id="EV_DIV",
            security_id="S1",
            event_type="cash_dividend",
            effective_session=sessions[1],
            shares_new_per_old=None,
            cash_amount_per_share=0.5,
            cash_basis="per_share_at_entitlement",
            pay_date=D(2025, 4, 25),
            source="test",
        )
    ]
    tri = build_tri(prices, events, sessions)
    # At close of 4/10: shares = 1 + 0.5/99.5, value = shares*99.5 = 99.5 + 0.5 = 100
    assert tri.tri[sessions[0]] == pytest.approx(100.0)
    assert tri.tri[sessions[1]] == pytest.approx(100.0)
