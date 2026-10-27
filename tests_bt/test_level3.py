"""Level-3 tests: range truncation, composite percentile score, amount-weighted buys, core-satellite merge."""
from __future__ import annotations

import pytest

from leadtrend_bt.engine import Backtest, _percentiles
from leadtrend_bt.runner import merge_results


def dates(n=20):
    from datetime import date, timedelta
    day = date(2024, 1, 1)
    return [(day + timedelta(days=i)).isoformat() for i in range(n)]


def market(series, pools):
    cal = dates(len(next(iter(series.values()))))
    tri = {sid: {d: v for d, v in zip(cal, values)} for sid, values in series.items()}
    return dict(calendar=cal, signal_days=sorted(pools), pools=pools, tri=tri,
                opens={sid: dict(tri[sid]) for sid in series}, closes={sid: dict(tri[sid]) for sid in series},
                issuer={s: 'I' + s for s in series if not s.startswith('BENCH')})


def cfg(**kw):
    base = dict(run_id='unit', start=dates()[0], end=dates()[-1], mode='momentum', L=1, N=1, b=1, K=1,
                cost_bps=0, fee_per_order=0.0, capital=1000.0)
    base.update(kw)
    return base


def pool(amounts, day):
    return dict(version_id='v' + day, securities=[dict(security_id=s, issuer_id='I' + s, amount_cents=a)
                                                  for s, a in amounts.items()])


def test_percentiles_are_average_rank_in_unit_interval():
    assert _percentiles([1.0, 2.0, 2.0, 3.0]) == pytest.approx([0.0, 0.5, 0.5, 1.0])
    assert _percentiles([5.0]) == pytest.approx([0.5])


def test_top_m_truncates_the_scoreable_universe_and_sells_a_name_that_leaves_it():
    series = {'A': [1 + 0.02 * i for i in range(20)], 'B': [1 + 0.01 * i for i in range(20)],
              'C': [1 + 0.005 * i for i in range(20)], 'D': [1.0] * 20, 'BENCH:VOO': [1.0] * 20}
    cal = dates()
    pools = {cal[4]: pool({'A': 4, 'B': 3, 'C': 2, 'D': 1}, 'd1'),
             cal[9]: pool({'A': 1, 'B': 3, 'C': 4, 'D': 2}, 'd2')}
    mk = market(series, pools)
    out = Backtest(cfg(top_m=2), market=mk).run()
    first = [t['security_id'] for t in out['trades'] if t['date'] == cal[5]]
    assert first == ['A']                                   # only the top-2 range is scoreable, A ranks first
    second = [(t['security_id'], t['side']) for t in out['trades'] if t['date'] == cal[10]]
    assert ('A', 'sell') in second                          # A fell to amount rank 4 and left the range
    assert not any(sid == 'D' for sid, _ in second)         # D was never in range


def test_composite_score_uses_only_size_when_w_is_one():
    series = {'A': [1.1] * 20, 'B': [1.2] * 20, 'C': [1.3] * 20, 'D': [1.4] * 20, 'BENCH:VOO': [1.0] * 20}
    cal = dates()
    mk = market(series, {cal[4]: pool({'A': 4, 'B': 3, 'C': 2, 'D': 1}, 'd1')})
    out = Backtest(cfg(N=2, w=1.0), market=mk).run()
    assert [t['security_id'] for t in out['trades']] == ['A', 'B']


def test_composite_score_with_ties_falls_back_to_security_id():
    series = {s: [1.0] * 20 for s in ('A', 'B', 'C')}
    series['BENCH:VOO'] = [1.0] * 20
    cal = dates()
    mk = market(series, {cal[4]: pool({'A': 2, 'B': 2, 'C': 2}, 'd1')})
    out = Backtest(cfg(N=2, w=1.0), market=mk).run()
    assert [t['security_id'] for t in out['trades']] == ['A', 'B']


def test_amount_weighting_splits_the_first_build_by_fund_amount():
    series = {'A': [10.0] * 20, 'B': [10.0] * 20, 'BENCH:VOO': [1.0] * 20}
    cal = dates()
    mk = market(series, {cal[4]: pool({'A': 3, 'B': 1}, 'd1')})
    out = Backtest(cfg(N=2, fractional=True, buy_weighting='amount'), market=mk).run()
    values = {r['security_id']: r['value'] for r in out['positions'] if r['date'] == cal[-1]}
    assert values['A'] / values['B'] == pytest.approx(3.0)


def test_core_satellite_merge_conserves_equity_and_positions():
    core = dict(daily=[dict(date='2024-01-02', cash=10.0, positions_value=90.0, equity=100.0, benchmark=0.0)],
                positions=[dict(date='2024-01-02', tranche=0, security_id='A', shares=9.0, close=10.0, value=90.0)],
                trades=[], decisions=[], pnl={'IA': 1.0}, issuers={'A': 'IA'})
    sat = dict(daily=[dict(date='2024-01-02', cash=5.0, positions_value=45.0, equity=50.0, benchmark=0.0)],
               positions=[dict(date='2024-01-02', tranche=0, security_id='A', shares=4.5, close=10.0, value=45.0)],
               trades=[], decisions=[], pnl={'IA': 2.0}, issuers={'A': 'IA'})
    merged = merge_results(core, sat, 100.0, 50.0)
    assert merged['daily'][0]['equity'] == pytest.approx(150.0)
    assert merged['daily'][0]['cash'] == pytest.approx(15.0)
    assert len(merged['positions']) == 1
    assert merged['positions'][0]['shares'] == pytest.approx(13.5)
    assert merged['pnl']['IA'] == pytest.approx(3.0)


def test_core_satellite_holds_the_later_sleeve_as_cash_before_it_starts():
    core = dict(daily=[dict(date='2024-01-02', cash=0.0, positions_value=100.0, equity=100.0, benchmark=0.0),
                       dict(date='2024-01-03', cash=0.0, positions_value=101.0, equity=101.0, benchmark=0.0)],
                positions=[], trades=[], decisions=[], pnl={}, issuers={})
    sat = dict(daily=[dict(date='2024-01-03', cash=50.0, positions_value=0.0, equity=50.0, benchmark=0.0)],
               positions=[], trades=[], decisions=[], pnl={}, issuers={})
    merged = merge_results(core, sat, 100.0, 50.0)
    assert merged['daily'][0]['equity'] == pytest.approx(150.0)   # 100 core + 50 satellite cash
    assert merged['daily'][1]['equity'] == pytest.approx(151.0)
