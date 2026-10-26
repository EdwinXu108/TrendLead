"""Round 3 engineering (plan 10.3): the prior-close single-stock cap, merged tranche groups, registration fields.

The run-level tests use the synthetic 2019 segment from test_stage3, as test_p3 does.
"""
import json
import shutil
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from leadtrend_v05.execution import plan_exit_cash_trades
from leadtrend_v05.experiments import attempt_key, make_point
from leadtrend_v05.ledger import AccountState
from leadtrend_v05.stage3 import cap_reference_marks
from leadtrend_v05.tranche_merge import merge_folder, merge_group, point_jobs, result_folder
from test_p3 import _rerun, _rows, _write_quotes
from test_stage3 import _bounded_run_fixture

WEEKS = ['2019-11-29', '2019-12-06', '2019-12-13']
MOMENTUM = {'kind': 'momentum', 'L': 90, 'skip': 0}


def test_cap_marks_are_the_previous_close_with_the_split_ratio_applied():
    split = SimpleNamespace(event_type='split', security_id='A', shares_new_per_old=2.0)
    dividend = SimpleNamespace(event_type='cash_dividend', security_id='B', shares_new_per_old=None)
    assert cap_reference_marks({'A': 10.0, 'B': 20.0}, ['A', 'B'], [split, dividend]) == {'A': 5.0, 'B': 20.0}
    reverse = SimpleNamespace(event_type='reverse_split', security_id='A', shares_new_per_old=0.1)
    assert cap_reference_marks({'A': 10.0}, ['A'], [reverse]) == {'A': pytest.approx(100.0)}
    assert cap_reference_marks({}, ['A'], []) == {}  # a spin-off child: no previous close, no cap that session


def test_a_name_without_a_previous_close_is_not_trimmed():
    # C was distributed on the execution session (no previous close): it is held, but not retained, so no cap applies.
    held = AccountState(0.0, {'A': 80, 'C': 50})
    marks = cap_reference_marks({'A': 10.0}, ['A', 'C'], [])
    trades = plan_exit_cash_trades(held, ['A', 'C'], {'A': 10.0, 'C': 10.0}, 1.0, 0.0, date(2020, 1, 3), True,
                                   cap=0.5, marks=marks, total_pre=1000.0)
    assert [(t.security_id, t.reason, t.shares) for t in trades if t.reason == 'cap_trim'] == [('A', 'cap_trim', 30)]
    assert held.shares_of('C') == 50


def test_a_rally_on_the_execution_day_does_not_trim_under_the_prior_close_rule():
    # 40 shares, previous close 10, execution close 30, previous equity 1,000: 400 <= 0.5 x 1,000, so nothing is cut.
    marks = cap_reference_marks({'A': 10.0}, ['A'], [])
    held = AccountState(0.0, {'A': 40})
    assert plan_exit_cash_trades(held, ['A'], {'A': 30.0}, 1.0, 0.0, date(2020, 1, 3), True,
                                 cap=0.5, marks=marks, total_pre=1000.0) == []
    # The same holding priced at the execution close would be cut, which is the rule this change removed.
    today = AccountState(0.0, {'A': 40})
    cut = plan_exit_cash_trades(today, ['A'], {'A': 30.0}, 1.0, 0.0, date(2020, 1, 3), True,
                                cap=0.5, marks={'A': 30.0}, total_pre=1000.0)
    assert [(t.reason, t.shares) for t in cut] == [('cap_trim', 24)]


def test_a_previous_close_above_the_cap_is_trimmed():
    held = AccountState(0.0, {'A': 80})
    trades = plan_exit_cash_trades(held, ['A'], {'A': 10.0}, 1.0, 0.0, date(2020, 1, 3), True,
                                   cap=0.5, marks={'A': 10.0}, total_pre=1000.0)
    assert [(t.reason, t.shares) for t in trades] == [('cap_trim', 30)]
    assert held.shares_of('A') == 50


def test_tranche_and_cap_enter_the_identity_only_when_given():
    plain = make_point(MOMENTUM, 10, 2, 'R2')
    t4 = make_point(MOMENTUM, 10, 2, 'R2', tranche=4)
    capped = make_point(MOMENTUM, 10, 2, 'R2', cap=0.15)
    assert plain['id'] == 'L090_N10_b2_R2'
    assert t4['id'] == 'L090_N10_b2_R2_K4' and t4['tranche'] == 4 and t4['k'] == 3
    assert capped['id'] == 'L090_N10_b2_R2_cap15' and capped['cap'] == 0.15
    assert len({attempt_key(plain), attempt_key(t4), attempt_key(capped)}) == 3
    assert len(attempt_key(plain)) == 7  # the key of every configuration registered before round 3
    with pytest.raises(ValueError):
        make_point(MOMENTUM, 10, 2, 'R2', tranche=1)
    with pytest.raises(ValueError):
        make_point(MOMENTUM, 10, 2, 'R2', cap=1.0)


def test_tranche_point_jobs_are_one_run_per_sub_account_with_its_own_index():
    point = make_point(MOMENTUM, 10, 2, 'R2', tranche=4)
    jobs = point_jobs(point, 10, Path('x') / 'cost_10bps', 'explore', 'explore', {'id': 'R3'}, start_date='2020-01-06')
    assert [job['tranche'] for job, *_ in jobs] == [dict(K=4, j=j) for j in range(4)]
    assert [folder for _, _, folder, *_ in jobs] == [str(Path('x') / 'cost_10bps' / f'sub_{j}') for j in range(4)]
    assert all(job['start_date'] == '2020-01-06' for job, *_ in jobs)
    plain = point_jobs(make_point(MOMENTUM, 10, 2, 'R2'), 10, Path('y'), 'explore', 'explore', {'id': 'R3'})
    assert len(plain) == 1 and 'tranche' not in plain[0][0]
    assert result_folder(Path('x'), point) == Path('x') / 'merged'
    assert result_folder(Path('x'), make_point(MOMENTUM, 10, 2, 'R2')) == Path('x')


def test_a_tranche_group_with_an_incomplete_sub_account_has_no_merged_result(tmp_path):
    group = tmp_path / 'group'
    (group / 'sub_0').mkdir(parents=True)
    (group / 'sub_0' / 'completion.json').write_text(json.dumps({'status': 'completed'}), encoding='utf-8')
    (group / 'sub_1').mkdir()
    assert merge_folder(group, 2, WEEKS, tmp_path, '2019-12-13', 'explore') is None
    assert not (group / 'merged').exists()


def test_merged_group_adds_positions_and_profit_by_security(tmp_path, monkeypatch):
    code, _ = _bounded_run_fixture(tmp_path, monkeypatch)
    assert code == 0
    _write_quotes(tmp_path, monkeypatch)
    group = tmp_path / 'group'
    for j in (0, 1):
        shutil.copytree(_rerun(tmp_path, f'k2_pos_j{j}', tranche={'K': 2, 'j': j}), group / f'sub_{j}')
    merge_group(group, ['sub_0', 'sub_1'], WEEKS, 2, tmp_path, '2019-12-13')
    subs = [group / f'sub_{j}' for j in (0, 1)]
    merged_value = sum(float(r['value']) for r in _rows(group / 'merged' / 'positions.csv'))
    assert merged_value == pytest.approx(sum(float(r['value']) for s in subs for r in _rows(s / 'positions.csv')))
    merged_profit = sum(float(r['profit_dollars']) for r in _rows(group / 'merged' / 'daily_contributions.csv'))
    assert merged_profit == pytest.approx(sum(float(r['profit_dollars']) for s in subs for r in _rows(s / 'daily_contributions.csv')))
    manifest = json.loads((group / 'merged' / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['tranche']['K'] == 2 and manifest['initial_capital_account'] == pytest.approx(100000.0)


def test_k1_and_cap_one_are_the_ordinary_run_for_the_authorization():
    from test_p3 import _ctx, _raw
    p = make_point(MOMENTUM, 10, 2, 'R2')
    ctx = _ctx([p])
    ctx.authorize(_raw(p, tranche={'K': 1, 'j': 0}), None)  # the K = 1 gate's run
    ctx.authorize(_raw(p, cap=1.0), None)  # the cap = 1 gate's run
    with pytest.raises(ValueError, match='cap'):
        ctx.authorize(_raw(p, cap=0.15), None)
