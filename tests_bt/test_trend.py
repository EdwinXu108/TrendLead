"""Trend mode: moving-average signals, per-security exits and the trend accounting rules."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_bt import trend
from leadtrend_bt.engine import Backtest


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def series(values):
    return {day: float(value) for day, value in zip(dates(len(values)), values)}


def trend_market(prices: dict[str, list[float]], pools: dict[str, list[tuple[str, str, float]]],
                 signal_idx=(4, 9, 14, 19), actions=None):
    """pools: {signal day: [(security, issuer, company amount), ...]} in rank order."""
    cal = dates(len(next(iter(prices.values()))))
    prices = dict(prices)
    prices.setdefault('BENCH:VOO', [1.0 + 0.002 * i for i in range(len(cal))])
    tri = {sid: series(values) for sid, values in prices.items()}
    pool_map = {}
    for day, rows in pools.items():
        pool_map[day] = dict(
            version_id='v-' + day,
            securities=[dict(security_id=sid, issuer_id=issuer, company_amount_cents=amount,
                             amount_cents=amount) for sid, issuer, amount in rows],
            amounts={sid: dict(issuer_id=issuer, amount_cents=amount) for sid, issuer, amount in rows},
            company_amounts={issuer: amount for _, issuer, amount in rows})
    return dict(calendar=cal, signal_days=[cal[i] for i in signal_idx], pools=pool_map, tri=tri,
                opens={sid: dict(values) for sid, values in tri.items()},
                closes={sid: dict(values) for sid, values in tri.items()},
                issuer={sid: issuer for rows in pools.values() for sid, issuer, _ in rows},
                actions=actions or {})


def cfg(**kw):
    base = dict(protocol='research_v1', run_id='t', mode='trend', start=dates()[0], end=dates()[-1],
                top_n=1, slot='equal', cash_rate='zero', execution='close', fractional=True,
                cost_bps=0, fee_per_order=0.0, capital=1000.0)
    base.update(kw)
    return base


def flat_pool(day, rows):
    return {day: rows}


# --- moving averages -------------------------------------------------------------------------------------------

def test_sma_matches_the_hand_computed_mean():
    values = np.array([[1.0], [2.0], [3.0], [4.0]])
    out = trend.sma(values, 3)
    assert np.isnan(out[1, 0])
    assert out[2, 0] == pytest.approx(2.0)
    assert out[3, 0] == pytest.approx(3.0)


def test_ema_is_seeded_with_the_simple_mean_then_recurses():
    values = np.array([[1.0], [2.0], [3.0], [4.0], [5.0]])
    out = trend.ema(values, 3)                       # seed = mean(1,2,3) = 2 at index 2; alpha = 0.5
    assert out[2, 0] == pytest.approx(2.0)
    assert out[3, 0] == pytest.approx(0.5 * 4 + 0.5 * 2)
    assert out[4, 0] == pytest.approx(0.5 * 5 + 0.5 * 3.0)


def test_macd_signal_needs_twenty_six_plus_nine_minus_one_days():
    values = np.arange(1.0, 41.0)[:, None]
    line = trend.macd_line(values)
    assert np.isnan(line[24, 0]) and np.isfinite(line[25, 0])
    signal = trend.ema(line, 9)
    assert np.isnan(signal[32, 0]) and np.isfinite(signal[33, 0])
    long = trend.bullish(values, 'macd', 12, 26)
    assert not long[:34].any()


def test_insufficient_history_is_bearish():
    values = np.arange(1.0, 6.0)[:, None]
    assert not trend.bullish(values, 'sma', 2, 10).any()
    assert trend.bullish(values, 'sma', 2, 4)[-1, 0]


def test_a_missing_session_carries_the_previous_value_forward():
    values = np.array([[1.0], [np.nan], [3.0]])
    filled = trend.carry_forward(values)
    assert filled[1, 0] == 1.0
    assert trend.sma(filled, 3)[2, 0] == pytest.approx((1.0 + 1.0 + 3.0) / 3)


def test_fast_above_slow_is_long_and_equality_is_not():
    values = np.array([[1.0], [2.0], [3.0], [3.0], [3.0]])
    long = trend.bullish(values, 'sma', 1, 3)
    assert long[2, 0]
    assert not long[4, 0]                           # equal averages are not long


# --- engine rules ----------------------------------------------------------------------------------------------

def test_a_held_position_is_never_resized():
    prices = {'A': [1.0 + 0.05 * i for i in range(20)]}
    pools = {day: [('A', 'IA', 100.0)] for day in (dates()[4], dates()[9], dates()[14], dates()[19])}
    bt = Backtest(cfg(ma='always'), market=trend_market(prices, pools))
    out = bt.run()
    shares = [row['shares'] for row in out['positions'] if row['security_id'] == 'A']
    assert len(shares) > 5
    assert max(shares) - min(shares) < 1e-9
    assert len(out['trades']) == 1


def test_pool_exit_sells_the_name_that_left():
    prices = {'A': [1.0] * 20, 'B': [2.0] * 20}
    pools = {dates()[4]: [('A', 'IA', 100.0)], dates()[9]: [('B', 'IB', 100.0)],
             dates()[14]: [('B', 'IB', 100.0)], dates()[19]: [('B', 'IB', 100.0)]}
    bt = Backtest(cfg(ma='always'), market=trend_market(prices, pools))
    out = bt.run()
    sells = [t for t in out['trades'] if t['side'] == 'sell']
    assert [(t['security_id'], t['reason']) for t in sells] == [('A', 'pool')]


def test_class_switch_buys_the_new_class_with_the_proceeds():
    prices = {'OLD': [10.0] * 20, 'NEW': [20.0] * 20}
    pools = {dates()[4]: [('OLD', 'I1', 100.0)], dates()[9]: [('NEW', 'I1', 100.0)],
             dates()[14]: [('NEW', 'I1', 100.0)], dates()[19]: [('NEW', 'I1', 100.0)]}
    bt = Backtest(cfg(ma='always'), market=trend_market(prices, pools))
    out = bt.run()
    assert [(t['security_id'], t['side'], t['reason']) for t in out['trades']] == [
        ('OLD', 'buy', 'signal'), ('OLD', 'sell', 'class_switch'), ('NEW', 'buy', 'class_switch')]
    old_shares = out['trades'][0]['shares']
    new_shares = out['trades'][2]['shares']
    assert new_shares * 20.0 == pytest.approx(old_shares * 10.0, rel=1e-9)


def test_a_bearish_signal_sells_and_holds_cash():
    prices = {'A': [1.0 + 0.1 * i for i in range(10)] + [1.9 - 0.1 * i for i in range(10)]}
    pools = {day: [('A', 'IA', 100.0)] for day in (dates()[4], dates()[9], dates()[14], dates()[19])}
    bt = Backtest(cfg(ma='sma', fast=1, slow=5), market=trend_market(prices, pools))
    out = bt.run()
    reasons = [t['reason'] for t in out['trades'] if t['side'] == 'sell']
    assert 'signal' in reasons


def test_always_only_trades_when_the_pool_changes():
    prices = {'A': [1.0] * 20, 'B': [1.0] * 20}
    rows = [('A', 'IA', 200.0), ('B', 'IB', 100.0)]
    pools = {day: rows for day in (dates()[4], dates()[9], dates()[14], dates()[19])}
    bt = Backtest(cfg(ma='always', top_n=2), market=trend_market(prices, pools))
    out = bt.run()
    assert len(out['trades']) == 2
    assert all(t['side'] == 'buy' for t in out['trades'])


def test_insufficient_cash_scales_the_buys_and_never_goes_negative():
    prices = {'A': [10.0] * 8 + [20.0] * 12, 'B': [10.0] * 20, 'C': [10.0] * 20}
    pools = {dates()[4]: [('A', 'IA', 100.0)],
             dates()[9]: [('A', 'IA', 100.0), ('B', 'IB', 100.0), ('C', 'IC', 100.0)],
             dates()[14]: [('A', 'IA', 100.0), ('B', 'IB', 100.0), ('C', 'IC', 100.0)],
             dates()[19]: [('A', 'IA', 100.0), ('B', 'IB', 100.0), ('C', 'IC', 100.0)]}
    bt = Backtest(cfg(ma='always', top_n=3), market=trend_market(prices, pools))
    out = bt.run()
    buys_b = [t for t in out['trades'] if t['security_id'] == 'B' and t['side'] == 'buy']
    assert len(buys_b) == 1
    assert buys_b[0]['shares'] * 10.0 == pytest.approx(1000.0 / 3, rel=1e-6)   # scaled from 444 to the cash left
    assert all(row['cash'] >= -1e-9 for row in out['daily'])


def test_cash_interest_matches_the_daily_rate():
    prices = {'A': [1.0] * 20}
    pools = {day: [('A', 'IA', 100.0)] for day in (dates()[4], dates()[9], dates()[14], dates()[19])}
    rate = {day: 0.001 for day in dates()}
    bt = Backtest(cfg(ma='sma', fast=1, slow=19, cash_rate='dtb3', rf=rate), market=trend_market(prices, pools))
    out = bt.run()
    cash = [row['cash'] for row in out['daily']]
    assert cash[0] == pytest.approx(1000.0 * 1.001)   # never invested: slow line not available until day 19
    assert cash[5] == pytest.approx(1000.0 * 1.001 ** 6)


def test_zero_cash_rate_leaves_cash_untouched():
    prices = {'A': [1.0] * 20}
    pools = {day: [('A', 'IA', 100.0)] for day in (dates()[4], dates()[9], dates()[14], dates()[19])}
    bt = Backtest(cfg(ma='sma', fast=1, slow=19), market=trend_market(prices, pools))
    out = bt.run()
    assert all(row['cash'] == pytest.approx(1000.0) for row in out['daily'])
