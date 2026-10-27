"""Fill price: the opening price of the next session, or the close of the signal day."""
from __future__ import annotations

import pytest

from leadtrend_bt.engine import Backtest


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def market_with_spread(n=20, signal_idx=(4,)):
    """Every session has open = close + 1, so the two fill rules give different prices."""
    cal = dates(n)
    closes_a = {d: 10.0 + i for i, d in enumerate(cal)}
    opens_a = {d: 11.0 + i for i, d in enumerate(cal)}
    bench_tri = {d: 1.0 + 0.001 * i for i, d in enumerate(cal)}
    pools = {cal[i]: dict(version_id='v', securities=[dict(security_id='A', issuer_id='IA')]) for i in signal_idx}
    return dict(calendar=cal, signal_days=[cal[i] for i in signal_idx], pools=pools,
                tri={'A': dict(closes_a), 'BENCH:VOO': bench_tri},
                opens={'A': opens_a, 'BENCH:VOO': {d: 100.0 for d in cal}},
                closes={'A': closes_a, 'BENCH:VOO': {d: 100.0 + i for i, d in enumerate(cal)}},
                issuer={'A': 'IA'})


def cfg(**kw):
    base = dict(protocol='research_v1', run_id='unit', start=dates()[0], end=dates()[-1], mode='momentum',
                L=2, N=1, b=1, K=1, cost_bps=0, fee_per_order=0.0, capital=1000.0)
    base.update(kw)
    return base


def test_close_execution_fills_on_the_signal_day_close():
    mk = market_with_spread()
    cal = dates()
    out = Backtest(cfg(execution='close'), market=mk).run()
    buy = next(t for t in out['trades'] if t['side'] == 'buy')
    assert buy['date'] == cal[4]
    assert buy['price'] == pytest.approx(14.0)


def test_next_open_execution_is_unchanged_by_default():
    mk = market_with_spread()
    cal = dates()
    out = Backtest(cfg(), market=mk).run()
    buy = next(t for t in out['trades'] if t['side'] == 'buy')
    assert buy['date'] == cal[5]
    assert buy['price'] == pytest.approx(16.0)


def test_close_execution_sells_on_the_check_day_close():
    mk = market_with_spread(n=20, signal_idx=(4, 9))
    cal = dates()
    mk['pools'][cal[9]] = dict(version_id='v2', securities=[dict(security_id='B', issuer_id='IB')])
    mk['tri']['B'] = {d: 10.0 + i for i, d in enumerate(cal)}
    mk['opens']['B'] = {d: 11.0 + i for i, d in enumerate(cal)}
    mk['closes']['B'] = {d: 10.0 + i for i, d in enumerate(cal)}
    mk['issuer']['B'] = 'IB'
    out = Backtest(cfg(execution='close'), market=mk).run()
    sell = next(t for t in out['trades'] if t['side'] == 'sell')
    assert sell['date'] == cal[9]
    assert sell['price'] == pytest.approx(19.0)


def test_benchmark_enters_at_the_close_of_the_first_day():
    mk = market_with_spread()
    out = Backtest(cfg(execution='close'), market=mk).run()
    first = out['daily'][0]
    assert first['date'] == dates()[0]
    assert first['benchmark'] == pytest.approx(1000.0)


def test_close_execution_records_the_first_day():
    mk = market_with_spread()
    out = Backtest(cfg(execution='close'), market=mk).run()
    assert out['daily'][0]['date'] == dates()[0]
    assert out['daily'][-1]['equity'] > 0


def drift_market():
    """A rises, B is flat: weights drift, but the ranking never changes (no turnover)."""
    cal = dates(20)
    tri_a = {d: 1.0 + 0.01 * i for i, d in enumerate(cal)}
    tri_b = {d: 1.0 for d in cal}
    bench = {d: 1.0 + 0.001 * i for i, d in enumerate(cal)}
    pools = {cal[i]: dict(version_id='v', securities=[dict(security_id='A', issuer_id='IA'),
                                                     dict(security_id='B', issuer_id='IB')])
             for i in (4, 9, 14)}
    return dict(calendar=cal, signal_days=[cal[4], cal[9], cal[14]], pools=pools,
                tri={'A': tri_a, 'B': tri_b, 'BENCH:VOO': bench},
                opens={'A': dict(tri_a), 'B': dict(tri_b), 'BENCH:VOO': {d: 100.0 for d in cal}},
                closes={'A': dict(tri_a), 'B': dict(tri_b), 'BENCH:VOO': {d: 100.0 for d in cal}},
                issuer={'A': 'IA', 'B': 'IB'})


def test_r1w_rebalances_on_a_week_without_turnover():
    mk = drift_market()
    cal = dates()
    cfg_base = dict(protocol='research_v1', run_id='unit', start=cal[0], end=cal[-1], mode='momentum',
                    L=2, N=2, b=1, K=1, cost_bps=0, fee_per_order=0.0, capital=1000.0,
                    fractional=True, execution='close')
    out_r1 = Backtest(dict(cfg_base, rebalance='R1'), market=mk).run()
    out_r1w = Backtest(dict(cfg_base, rebalance='R1W'), market=mk).run()
    assert cal[9] not in {t['date'] for t in out_r1['trades']}
    assert cal[9] in {t['date'] for t in out_r1w['trades']}


def test_r1w_ends_each_check_day_at_equal_weight():
    mk = drift_market()
    cal = dates()
    out = Backtest(dict(protocol='research_v1', run_id='unit', start=cal[0], end=cal[-1], mode='momentum',
                        L=2, N=2, b=1, K=1, cost_bps=0, fee_per_order=0.0, capital=1000.0,
                        fractional=True, execution='close', rebalance='R1W'), market=mk).run()
    for day in (cal[4], cal[9], cal[14]):
        values = {r['security_id']: r['value'] for r in out['positions'] if r['date'] == day}
        assert values['A'] == pytest.approx(values['B'], rel=1e-9)
