"""ma_pool: hand-computed equity on a synthetic pool, the boundary cases, and a repeatable random draw."""
from __future__ import annotations

import numpy as np
import pytest

from leadtrend_bt import ma_pool, trend


def make_market(days, values, long, pools, sids=None):
    values = trend.carry_forward(np.array(values, dtype=float))
    sids = sids or [f'S{i}' for i in range(values.shape[1])]
    return ma_pool.Market(days, sids, values, np.array(long, dtype=bool), pools, {})


def base_market(values=None):
    days = ['d1', 'd2', 'd3', 'd4', 'd5', 'd6']
    values = values or [[10, 20, 5], [10, 20, 5], [11, 20, 5], [12, 20, 5], [12, 22, 5], [13, 22, 6]]
    long = [[False, False, False], [True, False, False], [True, False, False], [True, False, False],
            [False, True, True], [False, True, True]]
    pools = {'d2': np.array([0, 1]), 'd5': np.array([1, 2])}
    return make_market(days, values, long, pools)


def test_hand_computed_equity_for_the_three_deterministic_portfolios():
    market = base_market()
    days = ['d2', 'd5']
    p0 = ma_pool.simulate(market, 'd1', ma_pool.equal_targets(market, days), capital=100)
    assert np.allclose(p0['equity'], [100, 100, 105, 110, 115, 126.5], rtol=0, atol=1e-12)
    p1 = ma_pool.simulate(market, 'd1', ma_pool.long_targets(market, days, full=False), capital=100)
    assert np.allclose(p1['equity'], [100, 100, 105, 110, 110, 121], rtol=0, atol=1e-12)
    p2 = ma_pool.simulate(market, 'd1', ma_pool.long_targets(market, days, full=True), capital=100)
    assert np.allclose(p2['equity'], [100, 100, 110, 120, 120, 132], rtol=0, atol=1e-12)


def test_no_long_name_keeps_the_second_portfolio_in_cash():
    market = base_market()
    market.long[4, :] = False
    market.long[5, :] = False
    p2 = ma_pool.simulate(market, 'd1', ma_pool.long_targets(market, ['d2', 'd5'], full=True), capital=100)
    assert np.allclose(p2['equity'], [100, 100, 110, 120, 120, 120], rtol=0, atol=1e-12)
    assert p2['equity'][5] == p2['equity'][4]


def test_a_name_that_leaves_the_pool_is_sold_at_the_switch():
    market = base_market()
    p0 = ma_pool.simulate(market, 'd1', ma_pool.equal_targets(market, ['d2', 'd5']), capital=100)
    # the first pool holds S0 and S1; the second holds S1 and S2, so S0 is sold on d5
    assert p0['equity'][4] == pytest.approx(115)
    assert p0['equity'][5] == pytest.approx(126.5)


def test_a_total_return_series_that_ends_is_frozen_at_its_last_value():
    values = [[10, 20, 5], [10, 20, 5], [11, 20, 5], [12, 20, 5], [12, 22, 5], [13, 22, np.nan]]
    market = base_market(values)
    p2 = ma_pool.simulate(market, 'd1', ma_pool.long_targets(market, ['d2', 'd5'], full=True), capital=100)
    # S2 keeps its d5 value of 5 on d6, so the d6 return is 0 for it
    assert p2['equity'][5] == pytest.approx(60 + 12 * 5)


def test_a_pool_name_without_any_price_is_not_investable():
    values = [[10, 20, np.nan], [10, 20, np.nan], [11, 20, 5], [12, 20, 5], [12, 22, 5], [13, 22, 6]]
    market = base_market(values)
    assert list(ma_pool.priced([0, 1, 2], market.values, 1)) == [0, 1]
    assert list(ma_pool.priced([0, 1, 2], market.values, 3)) == [0, 1, 2]


def test_the_random_draw_is_repeatable_and_seed_dependent():
    market = base_market()
    first = ma_pool.random_targets(market, ['d2', 'd5'], seed=0)
    again = ma_pool.random_targets(market, ['d2', 'd5'], seed=0)
    other = ma_pool.random_targets(market, ['d2', 'd5'], seed=1)
    for key in first:
        assert np.array_equal(first[key], again[key])
    assert any(not np.array_equal(first[key], other[key]) for key in first)
    # K = 1 on d2 and K = 2 on d5: exactly one and two names are held
    assert int(np.count_nonzero(first[1])) == 1
    assert int(np.count_nonzero(first[4])) == 2


def test_the_end_guard_refuses_the_reserved_segment():
    with pytest.raises(ValueError):
        ma_pool._check_end('2024-07-01')
    assert ma_pool._check_end('2024-06-28') == '2024-06-28'
