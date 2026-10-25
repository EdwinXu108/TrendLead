"""Explore verification level: same economics, smaller artifacts, no independent replay."""
import json
import sys
from pathlib import Path

import pytest

from leadtrend_v05 import perf_runner, stage3, stage4_acceptance, stage4_summary
from leadtrend_v05.perf_artifacts import expanded_bytes
from leadtrend_v05.perf_bench import Timer, dump

EXPLORE_TABLES = ('daily_equity', 'benchmark', 'trades', 'weekly_checks')
TABLES = {
    'daily_equity': [dict(session_date='2020-01-02', cash=1.0, total_equity=1.0, cash_reconciliation_error=0.0)],
    'benchmark': [dict(session_date='2020-01-02', benchmark_value=1.0)],
    'trades': [dict(session_date='2020-01-02', security_id='A', side='buy', shares=1.0)],
    'weekly_checks': [dict(signal_session_date='2020-01-03', is_swap_week=1)],
    'positions': [dict(session_date='2020-01-02', security_id='A', shares=1.0)],
    'decisions': [dict(signal_session_date='2020-01-03', security_id='A', action='retain', reason='buffer',
                       issuer_id='issuer', industry='industry', score=1.5, rank=1, buffer_score=1.0,
                       buffer_security_id='B', industry_counts_after='{}', target_count=1, frozen_count=0,
                       is_swap_week=1)],
}
SUMMARY = {'status': 'completed', 'max_asset_identity_error': 0.0}
MANIFEST = {'stage': 4, 'scope': 'development_only'}


def test_explore_writes_only_the_small_subset_with_lean_bytes(tmp_path):
    lean = tmp_path / 'lean'; explore = tmp_path / 'explore'
    stage3.write_artifacts(lean, TABLES, dict(SUMMARY), dict(MANIFEST), artifact_mode='lean', shared={})
    stage3.write_artifacts(explore, TABLES, dict(SUMMARY), dict(MANIFEST), artifact_mode='explore')
    written = sorted(p.name for p in explore.iterdir())
    assert written == ['artifacts.json', 'benchmark.csv.gz', 'daily_equity.csv.gz', 'run_manifest.json',
                       'summary.json', 'trades.csv.gz', 'weekly_checks.csv.gz']
    for name in EXPLORE_TABLES:
        assert expanded_bytes(explore / (name + '.csv')) == expanded_bytes(lean / (name + '.csv'))
    assert (explore / 'summary.json').read_bytes() == (lean / 'summary.json').read_bytes()
    manifest = json.loads((explore / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['artifact_mode'] == 'explore'
    assert manifest['usage_note'] == '仅供探索筛选，不得作为正式结果或保留段依据'
    assert json.loads((lean / 'run_manifest.json').read_text(encoding='utf-8'))['artifact_mode'] == 'lean'


def test_explore_tolerances_are_the_acceptance_constants():
    # Guard: the shared constants keep the reviewed values; explore never loosens them.
    assert stage4_acceptance.TOLERANCE == 1e-7
    assert stage4_acceptance.CASH_FLOOR == -1e-8


def explore_folder(tmp_path, *, identity=0.0, recon=0.0, cash=1.0):
    folder = tmp_path / 'run'; folder.mkdir(parents=True)
    dump(folder / 'summary.json', {'status': 'completed', 'max_asset_identity_error': identity})
    (folder / 'daily_equity.csv').write_text(
        'session_date,cash,cash_reconciliation_error\n2020-01-02,%r,%r\n' % (cash, recon), encoding='utf-8')
    return folder


def test_explore_check_passes_within_tolerance_and_fails_beyond(tmp_path):
    ok = perf_runner.explore_check(explore_folder(tmp_path / 'ok', identity=1e-12, recon=-1e-12))
    assert ok['status'] == 'PASS' and ok['max_asset_identity_error'] == 1e-12
    bad_id = perf_runner.explore_check(explore_folder(tmp_path / 'id', identity=1e-3))
    assert bad_id['status'] == 'FAIL' and 'asset identity' in bad_id['failures']
    bad_recon = perf_runner.explore_check(explore_folder(tmp_path / 'recon', recon=-1e-3))
    assert bad_recon['status'] == 'FAIL' and 'daily cash reconciliation' in bad_recon['failures']
    assert bad_recon['max_daily_cash_reconciliation_error'] == 1e-3


def test_explore_check_applies_the_acceptance_cash_floor(tmp_path):
    from leadtrend_v05.stage4_acceptance import CASH_FLOOR
    # A cash shortfall inside the floor is tolerated exactly as in the formal acceptance.
    tolerated = perf_runner.explore_check(explore_folder(tmp_path / 'tol', cash=CASH_FLOOR / 2))
    assert tolerated['status'] == 'PASS' and tolerated['min_daily_cash'] == CASH_FLOOR / 2
    negative = perf_runner.explore_check(explore_folder(tmp_path / 'neg', cash=-1e-3))
    assert negative['status'] == 'FAIL' and negative['failures'] == ['negative cash']
    assert negative['tolerances']['cash_floor'] == CASH_FLOOR


def test_exploration_completion_follows_explore_check_not_acceptance():
    point = dict(id='L020_N10_b2_R1', L=20, N=10, k=3, b=2, rebalance='R1')
    registry = dict(configurations=[point], costs_bps=[5, 10])
    rows = [dict(configuration_id=point['id'], cost_bps=c, status='completed', acceptance_status='NOT_RUN',
                 explore_check_status='PASS', annualized_excess=0.01) for c in (5, 10)]
    criteria, _ = stage4_summary.evaluate_regions(registry, rows, exploration=True)
    assert criteria[0]['d_complete'] is True and criteria[0]['a_stress_beats_voo'] is True
    # Outside exploration the same NOT_RUN rows are not complete.
    criteria, _ = stage4_summary.evaluate_regions(registry, rows)
    assert criteria[0]['d_complete'] is False and criteria[0]['a_stress_beats_voo'] is False
    # A failed in-engine check never counts as a completed screening result.
    failed = [dict(r, explore_check_status='FAIL') for r in rows]
    criteria, _ = stage4_summary.evaluate_regions(registry, failed, exploration=True)
    assert criteria[0]['d_complete'] is False


def test_one_job_explore_skips_audit_and_marks_level(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / 'config').mkdir(); dump(tmp_path / 'config' / 'v05_stage3.json', {})
    ctx = SimpleNamespace(root=tmp_path, segment={'start': '2019-11-29', 'end': '2024-06-28'},
                          cache_key='key', artifact_mode='lean')
    monkeypatch.setattr(perf_runner, '_CTX', ctx); monkeypatch.setattr(perf_runner, '_TIMER', Timer())
    calls = []

    def run(root, config, out, **kwargs):
        stage3.write_artifacts(out, TABLES, dict(SUMMARY), dict(MANIFEST), artifact_mode='explore')
        return 0

    def audit(*args, **kwargs):
        calls.append('audit'); return {'status': 'PASS'}

    monkeypatch.setattr(stage3, 'run_dev', run)
    monkeypatch.setattr(stage4_acceptance, 'audit', audit)
    point = dict(id='test', L=20, N=10, k=3, b=2, rebalance='R1')
    folder = tmp_path / 'explore'
    result = perf_runner.one_job((point, 5, str(folder), 'explore', False, 'explore'))
    assert calls == []  # the independent replay acceptance never runs at explore level
    assert result['status'] == 'completed' and result['verification_level'] == 'explore'
    assert result['acceptance'] == {'status': 'NOT_RUN'}
    completion = json.loads((folder / 'completion.json').read_text(encoding='utf-8'))
    assert completion['acceptance'] == {'status': 'NOT_RUN'}
    assert completion['explore_check']['status'] == 'PASS'
    manifest = json.loads((folder / 'run_manifest.json').read_text(encoding='utf-8'))
    assert manifest['verification_level'] == 'explore'
    assert manifest['usage_note'] == '仅供探索筛选，不得作为正式结果或保留段依据'
    assert not (folder / 'acceptance.json').exists()
    assert not (folder / 'byte_comparison.json').exists()
    assert sorted(p.name for p in folder.iterdir()) == [
        'artifacts.json', 'benchmark.csv.gz', 'command.log', 'completion.json', 'config.json',
        'daily_equity.csv.gz', 'run_manifest.json', 'summary.json', 'trades.csv.gz', 'weekly_checks.csv.gz']


def test_one_job_explore_marks_failed_when_tolerance_exceeded(tmp_path, monkeypatch):
    from types import SimpleNamespace
    (tmp_path / 'config').mkdir(); dump(tmp_path / 'config' / 'v05_stage3.json', {})
    ctx = SimpleNamespace(root=tmp_path, segment={'start': '2019-11-29', 'end': '2024-06-28'},
                          cache_key='key', artifact_mode='lean')
    monkeypatch.setattr(perf_runner, '_CTX', ctx); monkeypatch.setattr(perf_runner, '_TIMER', Timer())

    def run(root, config, out, **kwargs):
        summary = dict(SUMMARY, max_asset_identity_error=1e-3)
        stage3.write_artifacts(out, TABLES, summary, dict(MANIFEST), artifact_mode='explore')
        return 0

    monkeypatch.setattr(stage3, 'run_dev', run)
    point = dict(id='test', L=20, N=10, k=3, b=2, rebalance='R1')
    result = perf_runner.one_job((point, 5, str(tmp_path / 'bad'), 'explore', False, 'explore'))
    assert result['status'] == 'failed'
    assert result['explore_check']['failures'] == ['asset identity']


def test_levels_never_share_a_completion_key():
    from leadtrend_v05.perf_runner import run_key
    point = {'id': 'p'}
    keys = {run_key('data', point, 5, mode, verify_level=level)
            for mode in ('lean', 'explore') for level in ('daily', 'regression', 'explore')}
    assert len(keys) == 6


def test_accepted_requires_the_matching_level_and_not_run_acceptance(tmp_path):
    from leadtrend_v05.perf_bench import sha
    p = tmp_path / 'summary.json'; p.write_text('{"value":1}')
    for level, acc in (('daily', 'PASS'), ('regression', 'PASS'), ('explore', 'NOT_RUN')):
        doc = dict(run_key=level + '_key', status='completed', verification_level=level,
                   acceptance={'status': acc}, physical_sha256={'summary.json': sha(p)})
        dump(tmp_path / 'completion.json', doc)
        assert perf_runner.accepted(tmp_path, level + '_key')
        assert not perf_runner.accepted(tmp_path, 'other_key')
    # a daily PASS result is never reusable as explore and vice versa
    dump(tmp_path / 'completion.json', dict(run_key='daily_key', status='completed', verification_level='daily',
        acceptance={'status': 'PASS'}, physical_sha256={'summary.json': sha(p)}))
    assert not perf_runner.accepted(tmp_path, 'explore_key')


def test_run_grid_explore_default_output_directory(monkeypatch):
    from leadtrend_v05 import stage4_runner
    captured = {}

    def fake(root, **kw):
        captured.update(kw); return {'status': 'PASS', 'executed': 0}

    monkeypatch.setattr(stage4_runner, 'run_grid', fake)
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    from leadtrend_v05.__main__ import main
    assert main(['run-grid', '--verify', 'explore']) == 0
    assert captured['output'] == 'work/runs/explore_grid'
    assert captured['verify_level'] == 'explore'
    assert main(['run-grid']) == 0
    assert captured['output'] == 'work/runs/grid'


def _explore_grid(root):
    """One registered point at two costs, explore level, with minimal summary fields."""
    grid = root / 'grid'
    for cost in (5, 10):
        folder = grid / 'L020_N10_b2_R1' / f'cost_{cost}bps'
        folder.mkdir(parents=True)
        (folder / 'daily_equity.csv').write_text(
            'session_date,total_equity,benchmark_value\n2019-12-02,100000.0,100000.0\n2024-06-28,110000.0,120000.0\n', encoding='utf-8')
        (folder / 'benchmark.csv').write_text(
            'session_date,benchmark_value\n2019-12-02,100000.0\n2024-06-28,120000.0\n', encoding='utf-8')
        dump(folder / 'summary.json', {
            'status': 'completed', 'comparison_start_session': '2019-12-02', 'total_return': 0.1,
            'annualized_return': 0.02, 'drawdown': {'max_drawdown': 0.05},
            'relative_nav_underwater': {'longest_underwater_calendar_days': 1, 'unrecovered_at_end': False},
            'longest_negative_rolling_52_week_relative_return_weeks': 1,
            'annual_turnover_one_way_half_two_sided': 1.0, 'n_trades': 2, 'fees_total': 2.0,
            'slippage_cost_dollars': 1.0, 'average_completed_holding_weeks': 1.0,
            'sub_500_dollar_trades': 0, 'top_five_return_contributions': []})
        dump(folder / 'completion.json', dict(run_key='k', status='completed', verification_level='explore',
            acceptance={'status': 'NOT_RUN'}, explore_check={'status': 'PASS'}, physical_sha256={}))
    return grid


def _registry_files(root):
    import shutil
    src = Path(stage4_summary.__file__).resolve().parent.parent / 'research' / 'v05' / 'registry'
    dst = root / 'research' / 'v05' / 'registry'
    dst.mkdir(parents=True)
    for p in src.glob('*registry*'):
        shutil.copy2(p, dst / p.name)


def _tiny_registry(monkeypatch):
    point = dict(id='L020_N10_b2_R1', L=20, N=10, k=3, b=2, rebalance='R1')
    registry = dict(configurations=[point], costs_bps=[5, 10],
                    development={'start': '2019-11-29', 'end': '2024-06-28'},
                    holdout={'start': '2024-07-05'})
    monkeypatch.setattr(stage4_summary, 'verify', lambda root: registry)


def _support_files(root):
    d = root / 'data' / 'v05' / 'normalized'; d.mkdir(parents=True)
    (d / 'securities.csv').write_text('security_id,ticker\nA,A\n', encoding='utf-8')
    c = root / 'data' / 'v05' / 'prep' / 'validate'; c.mkdir(parents=True)
    (c / 'weekly_coverage.csv').write_text(
        'signal_session_date,n_eligible,historical_no_price_ratio\n'
        '2019-12-06,10,0.01\n2020-01-03,10,0.01\n2022-06-03,10,0.0\n', encoding='utf-8')


def test_summarize_refuses_explore_runs_without_the_flag(tmp_path, monkeypatch):
    _registry_files(tmp_path); _tiny_registry(monkeypatch); _support_files(tmp_path)
    grid = _explore_grid(tmp_path)
    with pytest.raises(ValueError, match='exploration'):
        stage4_summary.aggregate(tmp_path, grid_dir=grid, output_dir=tmp_path / 'out',
                                 decision_path=tmp_path / 'out' / 'holdout_decision.json')


def test_summarize_exploration_marks_outputs_and_never_writes_a_decision(tmp_path, monkeypatch):
    _registry_files(tmp_path); _tiny_registry(monkeypatch); _support_files(tmp_path)
    grid = _explore_grid(tmp_path); out = tmp_path / 'out'
    rows, criteria, decision = stage4_summary.aggregate(tmp_path, grid_dir=grid, output_dir=out,
        decision_path=out / 'holdout_decision.json', exploration=True)
    assert all(r['result_class'] == 'explore' and r['explore_check_status'] == 'PASS' for r in rows)
    assert decision['exploration_only'] is True
    assert 'holdout_decision' in json.dumps(decision)
    assert sorted(p.name for p in out.iterdir()) == [
        'all_results_explore.csv', 'region_criteria_explore.csv',
        'subperiod_coverage_summary_explore.csv', 'summary_manifest_explore.json']
    assert not (out / 'holdout_decision.json').exists()
    manifest = json.loads((out / 'summary_manifest_explore.json').read_text(encoding='utf-8'))
    assert manifest['exploration_only'] is True


def test_summarize_grid_cli_refuses_explore_without_flag(tmp_path, monkeypatch):
    _registry_files(tmp_path); _tiny_registry(monkeypatch); _support_files(tmp_path)
    grid = _explore_grid(tmp_path)
    monkeypatch.setenv('PYTHONHASHSEED', '0')
    from leadtrend_v05.__main__ import main
    assert main(['summarize-grid', '--grid', str(grid), '--output', str(tmp_path / 'o'),
                 '--decision', str(tmp_path / 'o' / 'd.json')]) == 1
    assert not (tmp_path / 'o' / 'd.json').exists()


def test_heatmaps_mark_exploration_outputs(tmp_path, monkeypatch):
    # Same vendored matplotlib the production path uses; the base env has none.
    vendor = Path(stage4_summary.__file__).resolve().parent.parent / 'vendor/stage4_plot_deps'
    if vendor.is_dir():
        sys.path.insert(0, str(vendor))
    pytest.importorskip('matplotlib')
    # The real registry is read-only input; only an in-memory copy is used.
    registry = json.loads((Path(stage4_summary.__file__).resolve().parent.parent
                           / 'research/v05/registry/stage4_registry.json').read_text(encoding='utf-8'))
    monkeypatch.setattr(stage4_summary, 'verify', lambda root: registry)
    rows = [dict(configuration_id=p['id'], cost_bps=10, status='completed',
                 annualized_excess=0.01, annual_turnover_one_way=1.0, max_drawdown=0.05)
            for p in registry['configurations']]
    criteria = [dict(configuration_id=p['id'], L=p['L'], N=p['N'], k=p['k'], b=p['b'], rebalance=p['rebalance'],
                     neighborhood_mean_10bps=0.01) for p in registry['configurations']]
    out = tmp_path / 'out'
    stage4_summary.heatmaps(tmp_path, rows, criteria, output_dir=out, exploration=True)
    assert sorted(p.name for p in out.glob('heatmap*')) == [
        'heatmap_exploration_b2_R1.csv', 'heatmap_exploration_b2_R1.png',
        'heatmap_exploration_b2_R2.csv', 'heatmap_exploration_b2_R2.png',
        'heatmap_exploration_b3_R1.csv', 'heatmap_exploration_b3_R1.png',
        'heatmap_exploration_b3_R2.csv', 'heatmap_exploration_b3_R2.png']
