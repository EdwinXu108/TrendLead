"""Tests for amendment 1: random control book size, merged concentration, incremental pool rebalancing."""
from __future__ import annotations

import pytest

from leadtrend_bt import metrics, runner
from leadtrend_bt.engine import Backtest


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


def count_by_day(result: dict) -> dict[tuple[str, int], int]:
    counts: dict[tuple[str, int], int] = {}
    for row in result['positions']:
        key = (row['date'], row['tranche'])
        counts[key] = counts.get(key, 0) + 1
    return counts


def two_phase_series(n=24):
    """A/B/C rise for 12 sessions then flatten; D/E/F are flat then rise. Momentum rotates once, at week 3."""
    series = {}
    for sid in 'ABC':
        series[sid] = [1 + 0.01 * min(i, 12) for i in range(n)]
    for sid in 'DEF':
        series[sid] = [1 + 0.01 * max(0, i - 12) for i in range(n)]
    series['BENCH:VOO'] = [1.0] * n
    return series


def test_random_book_never_exceeds_n_and_is_refilled_on_every_check_day():
    mk = market(two_phase_series())
    cfg = base_cfg(N=2, b=2, L=2)
    base = Backtest(cfg, market=mk).run()
    plan = runner.sell_plan(base)
    assert sum(plan.values()) > 0, 'the synthetic market must make the original configuration sell'
    check_days = {t['date'] for t in base['trades']}
    for seed in range(5):
        out = Backtest(base_cfg(N=2, b=2, L=2, mode='random', seed=seed, plan=plan), market=mk).run()
        counts = count_by_day(out)
        for (day, tranche), n in counts.items():
            assert n <= 2, (seed, day, tranche, n)
        for day in check_days:
            for tranche in (0,):
                assert counts.get((day, tranche), 0) == 2, (seed, day, counts.get((day, tranche), 0))


def test_forced_exit_beyond_the_original_sell_count_does_not_sell_anything_else():
    """Pool {A,B,C} becomes {A,B}: C must leave even though the original configuration decided no sell."""
    series = {sid: [10.0] * 20 for sid in ('A', 'B', 'C')}
    series['BENCH:VOO'] = [1.0] * 20
    mk = market(series, signal_idx=(4, 9))
    cal = mk['calendar']
    p1 = [dict(security_id=s, issuer_id='I' + s) for s in ('A', 'B', 'C')]
    p2 = [dict(security_id=s, issuer_id='I' + s) for s in ('A', 'B')]
    mk['pools'] = {cal[4]: dict(version_id='v1', securities=p1), cal[9]: dict(version_id='v2', securities=p2)}
    cfg = base_cfg(N=3, b=2, L=1, mode='random', seed=0, plan={})
    out = Backtest(cfg, market=mk).run()
    last_signal = cal[9]
    sells = [d['security_id'] for d in out['decisions'] if d['signal_date'] == last_signal and d['action'] == 'sell']
    buys = [d['security_id'] for d in out['decisions'] if d['signal_date'] == last_signal and d['action'] == 'buy']
    assert sells == ['C']
    assert buys == []


def test_random_control_is_byte_identical_for_the_same_seed(tmp_path):
    mk = market(two_phase_series())
    cfg = base_cfg(N=2, b=2, L=2, mode='random', seed=7,
                   plan=runner.sell_plan(Backtest(base_cfg(N=2, b=2, L=2), market=mk).run()))
    paths = []
    for name in ('a', 'b'):
        result = Backtest(cfg, market=mk).run()
        out = tmp_path / name
        out.mkdir()
        runner.write_csv(out / 'trades.csv', ['date', 'tranche', 'security_id', 'side', 'shares', 'price', 'fee',
                                              'reason', 'signal_date'], result['trades'])
        runner.write_csv(out / 'positions.csv', ['date', 'tranche', 'security_id', 'shares', 'close', 'value'],
                         result['positions'])
        runner.write_csv(out / 'daily_equity.csv', ['date', 'cash', 'positions_value', 'equity', 'benchmark'],
                         result['daily'])
        runner.write_csv(out / 'weekly_decisions.csv', ['signal_date', 'tranche', 'security_id', 'action', 'rank',
                                                        'score'], result['decisions'])
        paths.append(out)
    for name in ('trades.csv', 'positions.csv', 'daily_equity.csv', 'weekly_decisions.csv'):
        assert (paths[0] / name).read_bytes() == (paths[1] / name).read_bytes(), name


def test_concentration_sums_one_security_across_tranches():
    """Two tranches each hold 10% of the merged equity in the same security: the single-name weight is 20%."""
    result = dict(
        daily=[dict(date='2024-01-02', cash=80.0, positions_value=20.0, equity=100.0, benchmark=100.0)],
        positions=[dict(date='2024-01-02', tranche=0, security_id='A', shares=10.0, close=1.0, value=10.0),
                   dict(date='2024-01-02', tranche=1, security_id='A', shares=10.0, close=1.0, value=10.0)],
        trades=[], decisions=[], pnl={'I': 0.0}, issuers={'A': 'I'})
    out = metrics.compute(result, {})
    assert out['max_single_weight'] == pytest.approx(0.20)
    assert out['max_top5_issuer_weight'] == pytest.approx(0.20)
    assert out['average_positions'] == pytest.approx(1.0)
    assert out['average_positions_sum'] == pytest.approx(2.0)


def pool_market(series: dict[str, list[float]], first: dict[str, int], second: dict[str, int],
                second_day: int = 10):
    mk = market(series)
    cal = mk['calendar']
    mk['pools'] = {cal[0]: dict(version_id='v1', amounts={
        sid: dict(issuer_id='I' + sid, amount_cents=amount, class_code='COM') for sid, amount in first.items()}),
        cal[second_day]: dict(version_id='v2', amounts={
            sid: dict(issuer_id='I' + sid, amount_cents=amount, class_code='COM')
            for sid, amount in second.items()})}
    mk['pool_days'] = {cal[0]: 'v1', cal[second_day]: 'v2'}
    return mk, cal


def test_unchanged_pool_target_produces_no_trade_on_the_version_day():
    series = {'A': [10.0] * 20, 'B': [20.0] * 20, 'C': [5.0] * 20, 'BENCH:VOO': [1.0] * 20}
    mk, cal = pool_market(series, {'A': 1, 'B': 1, 'C': 1}, {'A': 1, 'B': 1, 'C': 1})
    out = Backtest(base_cfg(mode='pool_ew', start=cal[0], fractional=True), market=mk).run()
    days = {t['date'] for t in out['trades']}
    assert days == {cal[0]}
    assert all(t['reason'] == 'version' for t in out['trades'])


def test_replacing_one_name_trades_only_the_exit_and_the_entry():
    series = {'A': [10.0] * 20, 'B': [20.0] * 20, 'C': [5.0] * 20, 'D': [8.0] * 20, 'BENCH:VOO': [1.0] * 20}
    mk, cal = pool_market(series, {'A': 1, 'B': 1, 'C': 1}, {'A': 1, 'B': 1, 'D': 1})
    out = Backtest(base_cfg(mode='pool_ew', start=cal[0], fractional=True), market=mk).run()
    on_day = [(t['security_id'], t['side']) for t in out['trades'] if t['date'] == cal[10]]
    assert sorted(on_day) == [('C', 'sell'), ('D', 'buy')]
    assert out['daily'][-1]['cash'] == pytest.approx(0.0, abs=1e-6)


def test_a_price_move_is_absorbed_by_difference_trades_on_the_version_day():
    series = {'A': [10.0] * 10 + [11.0] * 10, 'B': [20.0] * 20, 'C': [5.0] * 20, 'D': [8.0] * 20,
              'BENCH:VOO': [1.0] * 20}
    mk, cal = pool_market(series, {'A': 1, 'B': 1, 'C': 1}, {'A': 1, 'B': 1, 'D': 1})
    out = Backtest(base_cfg(mode='pool_ew', start=cal[0], fractional=True), market=mk).run()
    before = {row['security_id']: row['shares'] for row in out['positions'] if row['date'] == cal[9]}
    on_day = {t['security_id']: t for t in out['trades'] if t['date'] == cal[10]}
    assert set(on_day) == {'A', 'B', 'C', 'D'}
    assert on_day['A']['side'] == 'sell' and on_day['A']['shares'] < before['A']
    assert on_day['C']['side'] == 'sell' and on_day['C']['shares'] == pytest.approx(before['C'])
    assert on_day['B']['side'] == 'buy' and on_day['D']['side'] == 'buy'


def test_pool_rebalancing_keeps_cash_non_negative_with_costs():
    series = {'A': [10.0 + 0.1 * i for i in range(20)], 'B': [20.0 + 0.2 * i for i in range(20)],
              'C': [5.0 - 0.05 * i for i in range(20)], 'D': [8.0 + 0.3 * i for i in range(20)],
              'BENCH:VOO': [1.0] * 20}
    mk, cal = pool_market(series, {'A': 1, 'B': 1, 'C': 1}, {'A': 1, 'B': 1, 'D': 1})
    out = Backtest(base_cfg(mode='pool_ew', start=cal[0], fractional=True, cost_bps=10), market=mk).run()
    for row in out['daily']:
        assert row['cash'] >= -1e-9
        assert row['equity'] == pytest.approx(row['cash'] + row['positions_value'], abs=1e-9)


def test_a_deferred_exit_does_not_push_the_random_book_above_n():
    """The exit has no opening price on the fill day, so it is deferred; the vacant slot must not be filled twice."""
    series = {sid: [10.0] * 20 for sid in ('A', 'B', 'C', 'D')}
    series['BENCH:VOO'] = [1.0] * 20
    mk = market(series, signal_idx=(4, 9))
    cal = mk['calendar']
    p1 = [dict(security_id=s, issuer_id='I' + s) for s in ('A', 'B', 'C')]
    p2 = [dict(security_id=s, issuer_id='I' + s) for s in ('A', 'B', 'D')]
    mk['pools'] = {cal[4]: dict(version_id='v1', securities=p1), cal[9]: dict(version_id='v2', securities=p2)}
    mk['opens']['C'].pop(cal[10])                       # the exit cannot fill on the day after the signal
    cfg = base_cfg(N=3, b=2, L=1, mode='random', seed=0, plan={f'{cal[9]}|0': 1})
    out = Backtest(cfg, market=mk).run()
    on_day = [row['security_id'] for row in out['positions'] if row['date'] == cal[10]]
    assert sorted(on_day) == ['A', 'B', 'C']
    assert not [t for t in out['trades'] if t['date'] == cal[10] and t['security_id'] == 'D']
    deferred = [t for t in out['trades'] if t['security_id'] == 'C' and t['side'] == 'sell']
    assert deferred and deferred[0]['date'] == cal[11]
