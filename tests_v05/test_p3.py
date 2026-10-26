"""P3 engineering (plan 9.3-9.4): tranche sub-accounts, the single-stock cap, exposure-matched controls.

The run-level tests use the synthetic 2019 segment from test_stage3 (the ordinary engine path). The cap is an R2
option, so its trim arithmetic is tested on the planner directly.
"""
import csv
import json
import shutil
from datetime import date
from pathlib import Path

import pytest

from leadtrend_v05.execution import plan_exit_cash_trades
from leadtrend_v05.experiments import make_point
from leadtrend_v05.ledger import AccountState
from leadtrend_v05.robustness import RobustnessContext
from leadtrend_v05.selection import Candidate, select_random_matched
from leadtrend_v05.selection_matched import load_match_table, select_random_size_matched, select_random_sector_matched
from test_stage3 import _bounded_run_fixture


def _rows(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def _rerun(tmp_path, out_name, **extra):
    """Run the fixture configuration again with extra keys in its config (the fixture's monkeypatches stay active)."""
    from leadtrend_v05.stage3 import run_dev
    cfg = tmp_path / 'cfg.json'
    raw = json.loads(cfg.read_text(encoding='utf-8'))
    cfg.write_text(json.dumps(dict(raw, **extra)), encoding='utf-8')
    out = tmp_path / 'work' / 'runs' / out_name
    assert run_dev(tmp_path, cfg, out) == 0
    return out


def _write_quotes(tmp_path, monkeypatch):
    """The fixture keeps its prices in memory; the independent replay reads them as normalized prices.csv."""
    import leadtrend_v05.stage3 as stage3
    from leadtrend_v05 import paths
    inputs = stage3.load_inputs(None, None)
    norm = tmp_path / paths.NORMALIZED
    norm.mkdir(parents=True, exist_ok=True)
    with (norm / 'prices.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['security_id', 'session_date', 'open', 'close'])
        for r in inputs.prices:
            w.writerow([r.security_id, r.session_date.isoformat(), r.open, r.close])
    with (norm / 'securities.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['security_id', 'issuer_id', 'ticker'])
        for s in inputs.securities:
            w.writerow([s.security_id, s.issuer_id, s.ticker or ''])
    with (norm / 'calendar.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.writer(f)
        w.writerow(['session_date'])
        for c in inputs.calendar:
            w.writerow([c.session_date.isoformat()])
    with (norm / 'events.csv').open('w', encoding='utf-8', newline='') as f:
        csv.writer(f).writerow(['event_id', 'security_id', 'event_type', 'effective_session', 'cash_amount_per_share', 'shares_new_per_old', 'pay_date'])


def test_k1_reproduces_the_ordinary_run_byte_for_byte(tmp_path, monkeypatch):
    code, out = _bounded_run_fixture(tmp_path, monkeypatch)
    assert code == 0
    k1 = _rerun(tmp_path, 'k1', tranche={'K': 1, 'j': 0})
    for name in ('summary.json', 'daily_equity.csv', 'trades.csv', 'weekly_checks.csv', 'positions.csv'):
        assert (out / name).read_bytes() == (k1 / name).read_bytes(), name


def test_tranche_sub_accounts_check_on_their_own_weeks_with_one_kth_of_the_capital(tmp_path, monkeypatch):
    code, out = _bounded_run_fixture(tmp_path, monkeypatch)
    assert code == 0
    weeks = ['2019-11-29', '2019-12-06', '2019-12-13']
    subs = {j: _rerun(tmp_path, f'k2_j{j}', tranche={'K': 2, 'j': j}) for j in (0, 1)}
    for j, folder in subs.items():
        checked = sorted({r['signal_session_date'] for r in _rows(folder / 'weekly_checks.csv')})
        assert checked == weeks[j::2]
        assert float(_rows(folder / 'daily_equity.csv')[0]['total_equity']) == pytest.approx(50000.0)
        manifest = json.loads((folder / 'run_manifest.json').read_text(encoding='utf-8'))
        assert manifest['tranche'] == dict(K=2, j=j, initial_capital=50000.0, schedule_weeks=len(weeks[j::2]))


def test_merged_group_sums_the_sub_accounts_and_its_benchmark_starts_on_the_whole_account(tmp_path, monkeypatch):
    from leadtrend_v05.stage4_acceptance import audit_tranche_group
    from leadtrend_v05.tranche import merge_group
    code, out = _bounded_run_fixture(tmp_path, monkeypatch)
    assert code == 0
    _write_quotes(tmp_path, monkeypatch)
    weeks = ['2019-11-29', '2019-12-06', '2019-12-13']
    group = tmp_path / 'group'
    for j in (0, 1):
        folder = _rerun(tmp_path, f'k2_j{j}', tranche={'K': 2, 'j': j})
        shutil.copytree(folder, group / f'sub_{j}')
    merge_group(group, ['sub_0', 'sub_1'], weeks, 2, tmp_path, '2019-12-13')
    merged = _rows(group / 'merged' / 'daily_equity.csv')
    per_sub = [{r['session_date']: float(r['total_equity']) for r in _rows(group / f'sub_{j}' / 'daily_equity.csv')} for j in (0, 1)]
    for r in merged:
        assert float(r['total_equity']) == pytest.approx(per_sub[0][r['session_date']] + per_sub[1][r['session_date']])
    assert float(merged[0]['total_equity']) == pytest.approx(100000.0)
    trades = _rows(group / 'merged' / 'trades.csv')
    assert len(trades) == len(_rows(group / 'sub_0' / 'trades.csv')) + len(_rows(group / 'sub_1' / 'trades.csv'))
    first = min(t['session_date'] for t in trades)
    bench = _rows(group / 'merged' / 'benchmark.csv')
    assert bench[0]['session_date'] == first and float(bench[0]['benchmark_value']) == pytest.approx(100000.0)
    result = audit_tranche_group(group, root=tmp_path, end='2019-12-13')
    assert result['status'] == 'PASS', result['failures']


def test_cap_trims_a_retained_name_to_the_cap_and_the_proceeds_fund_the_additions():
    acct = AccountState(0.0, {'A': 100, 'B': 10})
    prices = {'A': 10.0, 'B': 10.0, 'C': 10.0}
    trades = plan_exit_cash_trades(acct, ['A', 'B', 'C'], prices, 1.0, 0.0, date(2020, 1, 3), True,
                                   cap=0.5, marks=prices, total_pre=1100.0)
    trims = [t for t in trades if t.reason == 'cap_trim']
    assert [(t.security_id, t.shares) for t in trims] == [('A', 45)]  # the cap keeps 550 / 10 = 55 shares of A
    assert acct.shares_of('A') == 55 and acct.shares_of('B') == 10
    buys = [t for t in trades if t.side == 'buy']
    assert [(t.security_id, t.shares) for t in buys] == [('C', 44)]  # funded by the 449 cash from the trim
    assert acct.cash == pytest.approx(449.0 - 441.0)  # cash conservation: trim proceeds less the buy and both fees


def test_cap_that_never_binds_leaves_the_trades_as_without_a_cap():
    base = AccountState(0.0, {'A': 100, 'B': 10})
    capped = AccountState(0.0, {'A': 100, 'B': 10})
    prices = {'A': 10.0, 'B': 10.0, 'C': 10.0}
    plain = plan_exit_cash_trades(base, ['A', 'B', 'C'], prices, 1.0, 0.0, date(2020, 1, 3), True)
    never = plan_exit_cash_trades(capped, ['A', 'B', 'C'], prices, 1.0, 0.0, date(2020, 1, 3), True,
                                  cap=1.0, marks=prices, total_pre=1100.0)
    assert [(t.security_id, t.side, t.shares, t.reason) for t in plain] == [(t.security_id, t.side, t.shares, t.reason) for t in never]
    assert base.cash == capped.cash


def test_the_cap_acts_only_in_trading_weeks():
    acct = AccountState(0.0, {'A': 100})
    prices = {'A': 10.0}
    assert plan_exit_cash_trades(acct, ['A'], prices, 1.0, 0.0, date(2020, 1, 10), False,
                                 cap=0.2, marks=prices, total_pre=1000.0) == []
    assert acct.shares_of('A') == 100


def test_matched_controls_keep_the_committed_exits_and_fill_each_buy_from_its_group():
    cands = [Candidate(f'S{i}', f'I{i}', 'ind', 1.0 - i * 0.05) for i in range(10)]
    groups = {f'S{i}': str(i // 2 + 1) for i in range(10)}
    aux = {'sector': {}, 'match': {'groups': groups, 'buys': ['S7', 'S8']}}
    held = ['S0', 'S1', 'S2']
    kwargs = dict(signal_date='2020-01-03', seed=3, exit_counts={'2020-01-03': 1},
                  held_meta={h: ('I' + h[1:], 'ind') for h in held}, aux=aux)
    ref = select_random_matched(cands, held, 5, 5, 2, None, sector_k=None, **kwargs)
    size = select_random_size_matched(cands, held, 5, 5, 2, None, sector_k=None, **kwargs)
    sector = select_random_sector_matched(cands, held, 5, 5, 2, None, sector_k=None, **kwargs)
    exits = lambda r: sorted(d.security_id for d in r.decisions if d.action == 'exit')  # noqa: E731
    assert exits(size) == exits(ref) == exits(sector)
    assert size.target_count == 5 and size.filled_all
    assert size.counts.get('match_hit', 0) + size.counts.get('match_adjacent', 0) == 2
    assert size.counts.get('match_uniform_fill', 0) == 1  # three slots after two retained names: two buys, one uniform


def test_match_table_reads_groups_and_reference_buys(tmp_path):
    path = tmp_path / 'match.csv'
    path.write_text('signal_session_date,security_id,group,buy\n'
                    '2020-01-03,S1,1,1\n2020-01-03,S2,3,0\n2020-01-10,S1,1,0\n', encoding='utf-8')
    table = load_match_table(str(path))
    assert table['2020-01-03'] == {'groups': {'S1': '1', 'S2': '3'}, 'buys': ['S1']}
    assert table['2020-01-10']['buys'] == []


def test_matched_and_reserved_rules_cannot_be_registered():
    with pytest.raises(ValueError, match='unknown or reserved'):
        make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'random_size_matched', {'seed': 0})
    with pytest.raises(ValueError, match='unknown or reserved'):
        make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2', 'random_matched', {'seed': 0})


def _ctx(points=()):
    ctx = RobustnessContext.__new__(RobustnessContext)
    ctx.segment = dict(start='2019-11-29', end='2024-06-28')
    ctx.start, ctx.end = date(2019, 11, 29), date(2024, 6, 28)

    class _Frames:
        def weeks(self):
            return ['2019-11-29', '2019-12-06', '2019-12-13', '2019-12-20', '2019-12-27', '2020-01-03']

    ctx.frames = _Frames()
    ctx.configure(points=list(points), costs=[5, 10], experiment={'id': 'T'})
    return ctx


def _raw(p, **changes):
    raw = dict(configuration_id=p['id'], L=p['L'], N=p['N'], k=p['k'], b=p['b'], rebalance=p['rebalance'],
               selection='default', slippage_bps=10, initial_capital=100000, fee_per_trade=1,
               start_date='2019-11-29', end_date='2024-06-28')
    raw.update(changes)
    return raw


def test_robustness_authorization_admits_only_valid_tranche_and_cap_values():
    p = make_point({'kind': 'momentum', 'L': 90, 'skip': 0}, 10, 2, 'R2')
    ctx = _ctx([p])
    ctx.authorize(_raw(p, tranche={'K': 3, 'j': 2}, cap=1.0), None)
    ctx.authorize(_raw(p, tranche={'K': 1, 'j': 0}), None)
    for bad in ({'K': 0, 'j': 0}, {'K': 13, 'j': 0}, {'K': 2, 'j': 2}, {'K': 2}):
        with pytest.raises(ValueError, match='tranche'):
            ctx.authorize(_raw(p, tranche=bad), None)
    for bad in (0.0, 1.5, -0.2):
        with pytest.raises(ValueError, match='cap'):
            ctx.authorize(_raw(p, cap=bad), None)


def test_drawdown_record_gives_the_largest_drawdown_its_dates_and_the_longest_spell():
    from leadtrend_v05.drawdown import drawdown_record
    days = ['2020-01-02', '2020-01-03', '2020-01-06', '2020-01-07', '2020-01-08', '2020-01-09']
    values = [100.0, 120.0, 90.0, 110.0, 130.0, 95.0]
    rec = drawdown_record(days, values)
    assert rec['max_drawdown'] == pytest.approx(95 / 130 - 1)  # 95/130 is deeper than 90/120
    assert (rec['dd_peak_date'], rec['dd_trough_date'], rec['dd_recovery_date']) == ('2020-01-08', '2020-01-09', '')
    assert rec['unrecovered_at_end'] == 1
    # The 120 peak is regained at 130 on 01-08 (5 days underwater); the 130 peak is never regained (1 day to the end).
    assert rec['longest_underwater_days'] == (date(2020, 1, 8) - date(2020, 1, 3)).days


def test_worst_calendar_year_and_sharpe_net_of_dtb3():
    from leadtrend_v05.drawdown import sharpe_net_of_dtb3, worst_calendar_year
    days = ['2021-12-30', '2021-12-31', '2022-01-03', '2022-06-30', '2023-01-03']
    values = [100.0, 100.0, 90.0, 80.0, 88.0]
    ret, label = worst_calendar_year(days, values)
    assert ret == pytest.approx(-0.2) and label == '2022'  # 80 over the 2021 close of 100, full year
    assert label != '2023'
    rates = {d: 4.0 for d in days}
    sh = sharpe_net_of_dtb3(days, values, rates)
    assert sh is not None and sh < 0  # losing path, positive rate: a negative ratio


def test_newey_west_errors_equal_the_hand_computed_sandwich():
    import numpy as np
    from leadtrend_v05.p3_diagnostics import ols_newey_west
    rng = np.random.default_rng(7)
    x = rng.normal(size=12)
    y = 0.5 + 1.5 * x + rng.normal(scale=0.3, size=12)
    res = ols_newey_west(y, x.reshape(-1, 1), lag=2)
    X = np.column_stack([np.ones(12), x])
    beta = np.linalg.solve(X.T @ X, X.T @ y)
    e = y - X @ beta
    S = np.zeros((2, 2))
    for t in range(12):
        S += np.outer(X[t], X[t]) * e[t] ** 2
    for lag in (1, 2):
        w = 1 - lag / 3
        for t in range(lag, 12):
            G = np.outer(X[t] * e[t], X[t - lag] * e[t - lag])
            S += w * (G + G.T)
    V = np.linalg.inv(X.T @ X) @ S @ np.linalg.inv(X.T @ X)
    assert np.allclose(res['coef'], beta)
    assert np.allclose(res['t'], beta / np.sqrt(np.diag(V)))


def test_match_files_flag_the_reference_buys_and_control_points_carry_their_parameters(tmp_path):
    from leadtrend_v05.p3_diagnostics import MATCH_OBJECTS, control_point, match_files
    pools = [dict(signal_session_date='2020-01-03', security_id=f'S{i}', quintile=str(i // 2 + 1), sector='信息技术' if i % 2 else '金融',
                  issuer_weight=0.1) for i in range(6)]
    buys = {obj: {('2020-01-03', 'S5')} for obj in MATCH_OBJECTS}
    paths_by = match_files(pools, buys, tmp_path)
    rows = _rows(paths_by[('A90', 'size')])
    assert [r['buy'] for r in rows] == ['0', '0', '0', '0', '0', '1']
    assert _rows(paths_by[('A90', 'sector')])[1]['group'] == '信息技术'
    point = dict(id='P', signal={'kind': 'momentum', 'L': 90, 'skip': 0}, L=90, N=10, k=3, b=2, rebalance='R2')
    cp = control_point(point, 'sector', 4, {'2020-01-03': 1}, paths_by[('SEC3', 'sector')], sector_k=3)
    assert cp['selection'] == 'random_sector_matched'
    assert cp['selection_params']['seed'] == 4 and cp['selection_params']['sector_k'] == 3
    assert cp['id'].endswith('__random_sector_matched_s004')


def test_dtb3_lookup_uses_the_latest_observation_on_or_before_the_date():
    from leadtrend_v05.p3_diagnostics import dtb3_on
    rates = {'2020-01-02': 1.5, '2020-01-06': 1.4}
    assert dtb3_on(rates, '2020-01-05') == 1.5
    assert dtb3_on(rates, '2020-01-06') == 1.4
    assert dtb3_on(rates, '2019-12-31') is None
