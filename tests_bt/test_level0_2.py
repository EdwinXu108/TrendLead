"""Level 0/2 engine tests: top_n/weighting parameters and R1 equal-weight rebalancing."""
from __future__ import annotations

import pytest

from leadtrend_bt.engine import Backtest


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def market(series: dict[str, list[float]], signal_idx=(4, 9, 14, 19)):
    cal = dates(len(next(iter(series.values()))))
    tri = {sid: {d: v for d, v in zip(cal, values)} for sid, values in series.items()}
    opens = {sid: dict(tri[sid]) for sid in series}
    closes = {sid: dict(tri[sid]) for sid in series}
    sig = [cal[i] for i in signal_idx]
    pools = {d: dict(version_id='v', securities=[dict(security_id=s, issuer_id='I' + s) for s in series
                                                 if not s.startswith('BENCH')]) for d in sig}
    return dict(calendar=cal, signal_days=sig, pools=pools, tri=tri, opens=opens, closes=closes,
                issuer={s: 'I' + s for s in series if not s.startswith('BENCH')})


def base_cfg(**kw):
    cfg = dict(protocol='legacy_v1', run_id='unit', start=dates()[0], end=dates()[-1], mode='momentum', L=2, N=2, b=1, K=1,
               cost_bps=0, fee_per_order=0.0, capital=1000.0)
    cfg.update(kw)
    return cfg


def flat_pool(amounts: dict[str, int], n=20):
    series = {sid: [10.0] * n for sid in amounts}
    series['BENCH:VOO'] = [1.0] * n
    mk = market(series)
    cal = mk['calendar']
    mk['pools'] = {cal[0]: dict(version_id='v1', amounts={
        sid: dict(issuer_id='I' + sid, amount_cents=amount, class_code='COM') for sid, amount in amounts.items()})}
    mk['pool_days'] = {cal[0]: 'v1'}
    return mk, cal


def test_top_n_selects_by_amount_and_capitalisation_weights_by_amount():
    mk, cal = flat_pool({'A': 4, 'B': 3, 'C': 2, 'D': 1})
    out = Backtest(base_cfg(mode='pool', top_n=2, weighting='cw', start=cal[0], fractional=True), market=mk).run()
    assert {t['security_id'] for t in out['trades']} == {'A', 'B'}
    last = {r['security_id']: r['value'] for r in out['positions'] if r['date'] == cal[-1]}
    assert last['A'] / last['B'] == pytest.approx(4 / 3)


def test_top_n_equal_weight_gives_each_selected_name_the_same_value():
    mk, cal = flat_pool({'A': 4, 'B': 3, 'C': 2, 'D': 1})
    out = Backtest(base_cfg(mode='pool', top_n=2, weighting='ew', start=cal[0], fractional=True), market=mk).run()
    last = {r['security_id']: r['value'] for r in out['positions'] if r['date'] == cal[-1]}
    assert last['A'] == pytest.approx(last['B'])
    assert set(last) == {'A', 'B'}


def test_top_n_above_the_pool_size_holds_every_company():
    mk, cal = flat_pool({'A': 4, 'B': 3, 'C': 2, 'D': 1})
    out = Backtest(base_cfg(mode='pool', top_n=200, weighting='cw', start=cal[0], fractional=True), market=mk).run()
    assert {t['security_id'] for t in out['trades']} == {'A', 'B', 'C', 'D'}


def rotation_market():
    """A and B rise at different speeds; C is flat; D takes off after day 10. N=3 sells C and buys D once."""
    series = {'A': [1 + 0.02 * i for i in range(20)],
              'B': [1 + 0.005 * i for i in range(20)],
              'C': [1.0] * 20,
              'D': [1.0 if i <= 10 else 1 + 0.05 * (i - 10) for i in range(20)],
              'BENCH:VOO': [1.0] * 20}
    return market(series)


def test_r1_restores_equal_weight_only_on_a_check_day_with_turnover():
    mk = rotation_market()
    cal = mk['calendar']
    bt = Backtest(base_cfg(mode='momentum', N=3, b=1, L=2, K=1, fractional=True, rebalance='R1'), market=mk)
    out = bt.run()
    day = cal[15]                                   # the session after the signal that sells C and buys D
    shares = {r['security_id']: r['shares'] for r in out['positions'] if r['date'] == day}
    assert set(shares) == {'A', 'B', 'D'}
    values = {sid: shares[sid] * bt.opens[sid][day] for sid in shares}   # equal at the execution price
    assert values['A'] == pytest.approx(values['B'])
    assert values['A'] == pytest.approx(values['D'])
    trades = [(t['security_id'], t['side']) for t in out['trades'] if t['date'] == day]
    assert ('C', 'sell') in trades and ('D', 'buy') in trades
    assert ('A', 'sell') in trades and ('B', 'buy') in trades       # A had run ahead, B had lagged
    later = [(t['security_id'], t['side']) for t in out['trades'] if t['date'] == cal[19]]
    assert later == []                                              # no turnover at the last check: no trade


def test_r2_does_not_resize_kept_names_on_the_same_market():
    mk = rotation_market()
    cal = mk['calendar']
    out = Backtest(base_cfg(mode='momentum', N=3, b=1, L=2, K=1, fractional=True, rebalance='R2'), market=mk).run()
    day = cal[15]
    trades = [(t['security_id'], t['side']) for t in out['trades'] if t['date'] == day]
    assert ('C', 'sell') in trades and ('D', 'buy') in trades
    assert ('A', 'sell') not in trades and ('B', 'buy') not in trades
