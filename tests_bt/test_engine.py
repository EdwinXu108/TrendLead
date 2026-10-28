"""Engine tests on hand-computed synthetic markets (no database)."""
from __future__ import annotations

import math

import pytest

from leadtrend_bt.engine import Backtest, HoldoutError


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def market(series: dict[str, list[float]], signal_idx=(4, 9, 14, 19), pools=None):
    cal = dates(len(next(iter(series.values()))))
    tri = {sid: {d: v for d, v in zip(cal, values)} for sid, values in series.items()}
    opens = {sid: dict(tri[sid]) for sid in series}
    closes = {sid: dict(tri[sid]) for sid in series}
    sig = [cal[i] for i in signal_idx]
    if pools is None:
        pools = {d: dict(version_id='v', securities=[dict(security_id=s, issuer_id='I' + s) for s in series
                                                     if not s.startswith('BENCH')]) for d in sig}
    return dict(calendar=cal, signal_days=sig, pools=pools, tri=tri, opens=opens, closes=closes,
                issuer={s: 'I' + s for s in series if not s.startswith('BENCH')})


def base_cfg(**kw):
    cfg = dict(protocol='legacy_v1', run_id='unit', start=dates()[0], end=dates()[-1], mode='momentum', L=2, N=2, b=1, K=1,
               cost_bps=0, fee_per_order=1.0, capital=1000.0)
    cfg.update(kw)
    return cfg


def three_series():
    return {'A': [1 + 0.01 * i for i in range(20)],
            'B': [1 + 0.005 * i for i in range(20)],
            'C': [1.0 if i <= 10 else 1.0 + 0.05 * (i - 10) for i in range(20)],
            'BENCH:VOO': [1 + 0.002 * i for i in range(20)]}


def test_initial_build_cash_and_share_rounding_are_hand_computable():
    bt = Backtest(base_cfg(), market=market(three_series()))
    out = bt.run()
    trades = [(t['date'], t['security_id'], t['side'], t['shares'], round(t['price'], 6)) for t in out['trades']]
    cal = dates()
    assert trades[0] == (cal[5], 'A', 'buy', 475.0, 1.05)
    assert trades[1] == (cal[5], 'B', 'buy', 486.0, 1.025)
    cash = 1000.0 - (475 * 1.05 + 1) - (486 * 1.025 + 1)
    assert cash == pytest.approx(1.10)


def test_buffer_sell_and_r2_reinvestment():
    bt = Backtest(base_cfg(), market=market(three_series()))
    out = bt.run()
    cal = dates()
    trades = [(t['date'], t['security_id'], t['side'], t['shares'], round(t['price'], 6)) for t in out['trades']]
    assert trades[2] == (cal[15], 'B', 'sell', 486.0, 1.075)
    proceeds = 486 * 1.075 - 1
    cash = 1.10 + proceeds
    shares_c = math.floor((cash - 1) / 1.25)
    assert trades[3] == (cal[15], 'C', 'buy', float(shares_c), 1.25)
    assert shares_c == 417
    final = 475 * 1.19 + 417 * 1.45 + (cash - (417 * 1.25 + 1))
    assert out['daily'][-1]['equity'] == pytest.approx(final)
    assert out['daily'][-1]['equity'] == pytest.approx(1170.20)


def test_equity_is_cash_plus_positions_and_cash_never_negative():
    bt = Backtest(base_cfg(), market=market(three_series()))
    out = bt.run()
    for row in out['daily']:
        assert row['cash'] >= -1e-9
        assert row['equity'] == pytest.approx(row['cash'] + row['positions_value'], abs=1e-9)


def test_no_security_is_bought_and_sold_on_the_same_signal():
    bt = Backtest(base_cfg(), market=market(three_series()))
    out = bt.run()
    by_signal = {}
    for d in out['decisions']:
        by_signal.setdefault(d['signal_date'], set()).add((d['security_id'], d['action']))
    for day, pairs in by_signal.items():
        sids = {sid for sid, action in pairs if action == 'sell'}
        buys = {sid for sid, action in pairs if action == 'buy'}
        assert not (sids & buys)


def test_split_dividend_and_spinoff_reach_the_portfolio():
    series = {'A': [10.0] * 20, 'BENCH:VOO': [1.0] * 20}
    mk = market(series, signal_idx=(4, 19))
    cal = mk['calendar']
    mk['actions'] = {'A': [dict(action_date=cal[7], action_type='split', ratio=2.0, cash_amount=None,
                                spin_value=None, vendor_factor=None),
                           dict(action_date=cal[8], action_type='cash_dividend', ratio=None, cash_amount=0.5,
                                spin_value=None, vendor_factor=None),
                           dict(action_date=cal[9], action_type='spinoff', ratio=None, cash_amount=None,
                                spin_value=1.0, vendor_factor=None)]}
    cfg = base_cfg(N=1, L=1, b=1)
    bt = Backtest(cfg, market=mk)
    out = bt.run()
    first_buy = out['trades'][0]
    assert first_buy['shares'] == 99.0                      # floor((1000-1)/10)
    cash = 1000.0 - (99 * 10 + 1)
    after_split = out['daily'][7]['positions_value']
    assert after_split == pytest.approx(99 * 2 * 10.0)
    assert out['daily'][8]['cash'] == pytest.approx(cash + 99 * 2 * 0.5)
    assert out['daily'][9]['cash'] == pytest.approx(cash + 99 * 2 * 0.5 + 99 * 2 * 1.0)


def test_tranches_start_and_check_on_their_own_weeks():
    series = {'A': [1 + 0.01 * i for i in range(20)], 'B': [1 + 0.002 * i for i in range(20)],
              'BENCH:VOO': [1.0] * 20}
    cfg = base_cfg(K=2, N=1, b=1, L=1)
    bt = Backtest(cfg, market=market(series))
    out = bt.run()
    cal = dates()
    first = {}
    for trade in out['trades']:
        first.setdefault(trade['tranche'], trade['date'])
    assert first[0] == cal[5]          # tranche 0 builds on signal week 0
    assert first[1] == cal[10]         # tranche 1 builds on signal week 1
    checks = {(d['tranche'], d['signal_date']) for d in out['decisions']}
    assert checks == {(0, cal[4]), (1, cal[9]), (0, cal[14]), (1, cal[19])}


def test_benchmark_ratio_equals_the_total_return_index_ratio():
    bt = Backtest(base_cfg(), market=market(three_series()))
    out = bt.run()
    daily = [r for r in out['daily'] if r['benchmark']]
    entry = daily[0]
    tri = bt.tri['BENCH:VOO']
    for row in daily:
        assert row['benchmark'] / entry['benchmark'] == pytest.approx(
            tri[row['date']] / tri[entry['date']], rel=1e-12)


def test_holdout_guard_rejects_a_late_end():
    cfg = base_cfg(end='2024-07-05')
    with pytest.raises(HoldoutError):
        Backtest(cfg, market=market(three_series()))


def test_pool_equal_weight_matches_an_independent_calculation():
    """Zero cost, fractional shares, weekly full rebalance: compare with a direct weights calculation."""
    series = {'A': [1 + 0.01 * i for i in range(20)], 'B': [1 + 0.02 * i for i in range(20)],
              'C': [1 + 0.005 * i for i in range(20)], 'BENCH:VOO': [1.0] * 20}
    mk = market(series, signal_idx=(4, 9, 14, 19))
    cal = mk['calendar']
    mk['pools'] = {cal[0]: dict(version_id='v1', amounts={
        'A': dict(issuer_id='IA', amount_cents=3, class_code='COM'),
        'B': dict(issuer_id='IB', amount_cents=2, class_code='COM'),
        'C': dict(issuer_id='IC', amount_cents=1, class_code='COM')})}
    mk['pool_days'] = {cal[0]: 'v1'}
    cfg = base_cfg(mode='pool_ew', start=cal[0], fractional=True, fee_per_order=0.0, N=3)
    bt = Backtest(cfg, market=mk)
    out = bt.run()
    budget = 1000.0 / 3
    expected = [budget * (series['A'][i] + series['B'][i] + series['C'][i]) for i in range(20)]
    for row, want in zip(out['daily'], expected):
        assert row['equity'] == pytest.approx(want, rel=1e-9)


def test_r1_rebalance_buys_the_held_names_back_to_equal_weight():
    """7aabb2e: before the fix, an R1 rebalance only sold; held names were never topped up, so cash piled up."""
    mkt = market(three_series())
    cfg = base_cfg(protocol='research_v1', rebalance='R1', N=2, b=1, K=1, execution='next_open',
                   capital=100000.0)
    out = Backtest(cfg, market=mkt).run()
    trades = {(t['date'], t['security_id'], t['side']) for t in out['trades']}
    assert ('2024-01-16', 'B', 'sell') in trades            # the switch happens on this check day
    assert ('2024-01-16', 'C', 'buy') in trades
    assert ('2024-01-16', 'A', 'sell') in trades            # A is trimmed, not left overweight
    rebalance_days = {t['date'] for t in out['trades'] if t.get('reason') == 'rebalance'}
    for day in sorted(rebalance_days):
        rows = [p for p in out['positions'] if p['date'] == day]
        if len(rows) < 2:
            continue
        mean = sum(r['value'] for r in rows) / len(rows)
        tolerance = max(r['close'] for r in rows) + cfg['fee_per_order']
        assert all(abs(r['value'] - mean) <= tolerance for r in rows), day
