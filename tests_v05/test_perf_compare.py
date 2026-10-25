"""compare-grid: full byte comparison of a new grid against a reviewed reference."""
import gzip
import json
from pathlib import Path

import pytest

from leadtrend_v05 import perf_compare
from leadtrend_v05.perf_artifacts import logical_sha, record, write_gzip_csv, write_lean
from leadtrend_v05.perf_bench import dump, sha
from leadtrend_v05.perf_runner import safe_output

TABLES = {
    'benchmark': [dict(session_date='2020-01-02', benchmark_value=1.0)],
    'cash_flows': [dict(session_date='2020-01-02', event_id='E', security_id='A', cash_delta=1.0, kind='dividend_payment')],
    'corporate_actions': [dict(session_date='2020-01-02', event_id='E', security_id='A', event_type='cash_dividend',
                               handling='x', source='s')],
    'daily_contributions': [dict(session_date='2020-01-02', security_id='A', profit_dollars=1.0)],
    'daily_equity': [dict(session_date='2020-01-02', cash=1.0, total_equity=1.0)],
    'data_issues': [dict(session_date='2020-01-02', security_id='A', severity='info', message='m')],
    'decisions': [dict(signal_session_date='2020-01-03', security_id='A', action='retain', reason='buffer',
                       issuer_id='issuer', industry='industry', score=1.5, rank=1, buffer_score=1.0,
                       buffer_security_id='B', industry_counts_after='{}', target_count=1, frozen_count=0,
                       is_swap_week=1)],
    'fractional_cash': [dict(session_date='2020-01-02', event_id='E', security_id='A', amount=1.0)],
    'positions': [dict(session_date='2020-01-02', security_id='A', shares=1.0)],
    'receivables': [dict(session_date='2020-01-02', event_id='E', security_id='A', operation='created',
                         shares=1.0, amount=1.0, source='s')],
    'return_contributions': [dict(security_id='A', profit_dollars=1.0,
                                  return_contribution_percentage_points=1.0)],
    'rolling_relative': [dict(session_date='2020-01-02', rolling_52_week_relative_return=0.1)],
    'scores': [dict(signal_session_date='2020-01-03', security_id='A', score=1.5, unscorable_reason='')],
    'trades': [dict(session_date='2020-01-02', security_id='A', side='buy', shares=1.0)],
    'universe': [dict(signal_session_date='2020-01-03', security_id='A', eligible=1, class_selected=1)],
    'weekly_checks': [dict(signal_session_date='2020-01-03', is_swap_week=1)],
    'weekly_coverage': [dict(signal_session_date='2020-01-03', n_eligible=1, historical_no_price_ratio=0.0)],
}
SUMMARY = {'status': 'completed', 'total_return': 0.5}
ACCEPTANCE = {'status': 'PASS', 'failures': []}
POINTS = [dict(id='L020_N10_b2_R1', L=20, N=10, k=3, b=2, rebalance='R1'),
          dict(id='L010_N08_b2_R1', L=10, N=8, k=2, b=2, rebalance='R1')]


def shared_tables(root):
    """The two shared tables live outside the run folder, referenced by hash."""
    d = root / 'shared'; d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, rows in (('universe', TABLES['universe']), ('scores', TABLES['scores'])):
        p = d / (name + '.csv.gz')
        write_gzip_csv(p, rows, list(rows[0]))
        out[name] = dict(path=p, physical_sha256=sha(p), logical_sha256=logical_sha(p))
    return out


def make_run(folder, *, level='daily', tables=None, shared):
    folder.mkdir(parents=True)
    tables = TABLES if tables is None else tables
    write_lean(folder, tables, dict(SUMMARY), {'stage': 4}, {}, shared)
    dump(folder / 'acceptance.json', ACCEPTANCE)
    dump(folder / 'completion.json', dict(run_key='k', status='completed',
        acceptance={'status': 'NOT_RUN' if level == 'explore' else 'PASS'},
        verification_level=level, physical_sha256={}))
    return folder


def make_grid(root, name, **kw):
    grid = root / name
    shared = shared_tables(root)
    for p in POINTS:
        for cost in (5, 10):
            make_run(grid / p['id'] / f'cost_{cost}bps', shared=shared, **kw)
    return grid


@pytest.fixture
def small_registry(monkeypatch):
    monkeypatch.setattr(perf_compare, 'verify', lambda root: dict(configurations=POINTS, costs_bps=[5, 10]))


def read_out(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def test_self_comparison_passes_with_full_item_count(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    out = tmp_path / 'work' / 'cmp.json'
    summary = perf_compare.compare_grid(tmp_path, grid, ref, out)
    assert summary['status'] == 'PASS'
    assert summary['pairs'] == 4 and summary['failures'] == 0
    # 17 logical tables + summary.json + acceptance.json per pair
    assert summary['comparison_items'] == 4 * 19
    doc = read_out(out)
    assert len(doc['comparisons']) == 4
    assert [ (c['configuration_id'], c['cost_bps']) for c in doc['comparisons'] ] == [
        ('L020_N10_b2_R1', 5), ('L020_N10_b2_R1', 10), ('L010_N08_b2_R1', 5), ('L010_N08_b2_R1', 10)]


def test_single_tampered_byte_in_one_table_fails_that_pair(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    victim = grid / 'L020_N10_b2_R1' / 'cost_5bps' / 'trades.csv.gz'
    text = gzip.decompress(victim.read_bytes()).replace(b'1.0', b'9.0', 1)
    victim.write_bytes(gzip.compress(text, mtime=0))
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json')
    assert summary['status'] == 'FAIL' and summary['failures'] == 1
    row = next(c for c in read_out(tmp_path / 'work' / 'cmp.json')['comparisons'] if c['status'] == 'FAIL')
    assert row['configuration_id'] == 'L020_N10_b2_R1' and row['cost_bps'] == 5
    assert row['files']['trades.csv']['equal'] is False
    assert 'trades.csv' in row['problems']


def test_summary_and_acceptance_are_compared_in_full(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    (grid / 'L010_N08_b2_R1' / 'cost_10bps' / 'summary.json').write_text('{"status": "completed", "total_return": 0.6}\n', encoding='utf-8')
    dump(grid / 'L020_N10_b2_R1' / 'cost_10bps' / 'acceptance.json', {'status': 'PASS', 'failures': ['x']})
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json')
    assert summary['status'] == 'FAIL' and summary['failures'] == 2
    rows = { (c['configuration_id'], c['cost_bps']): c for c in read_out(tmp_path / 'work' / 'cmp.json')['comparisons'] }
    assert rows['L010_N08_b2_R1', 10]['problems'] == ['summary.json']
    assert rows['L020_N10_b2_R1', 10]['acceptance_all_fields_equal'] is False
    assert 'acceptance.json' in rows['L020_N10_b2_R1', 10]['problems']


def test_missing_shared_file_fails_instead_of_skipping(tmp_path, small_registry):
    shared = shared_tables(tmp_path)
    grid = tmp_path / 'grid'; ref = tmp_path / 'ref'
    for p in POINTS:
        for cost in (5, 10):
            make_run(grid / p['id'] / f'cost_{cost}bps', shared=shared)
            make_run(ref / p['id'] / f'cost_{cost}bps', shared=shared)
    shared['universe']['path'].unlink()
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json')
    assert summary['status'] == 'FAIL' and summary['failures'] == 4
    row = read_out(tmp_path / 'work' / 'cmp.json')['comparisons'][0]
    assert 'universe.csv' in row['problems']
    assert 'error' in row['files']['universe.csv']


def test_explore_level_compares_only_the_written_subset(tmp_path, small_registry):
    explore_tables = {k: TABLES[k] for k in ('trades', 'daily_equity', 'benchmark', 'weekly_checks')}
    grid = make_grid(tmp_path, 'grid', level='explore', tables=explore_tables)
    ref = make_grid(tmp_path, 'ref', tables=TABLES)
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json', expect='explore')
    assert summary['status'] == 'PASS' and summary['failures'] == 0
    # 4 written tables + summary.json per pair; acceptance is not run at explore level
    assert summary['comparison_items'] == 4 * 5
    row = read_out(tmp_path / 'work' / 'cmp.json')['comparisons'][0]
    assert row['verification_level'] == 'explore'
    assert row['acceptance_all_fields_equal'] is None


def test_default_expectation_is_daily_so_explore_runs_fail(tmp_path, small_registry):
    explore_tables = {k: TABLES[k] for k in ('trades', 'daily_equity', 'benchmark', 'weekly_checks')}
    grid = make_grid(tmp_path, 'grid', level='explore', tables=explore_tables)
    ref = make_grid(tmp_path, 'ref', tables=TABLES)
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json')
    assert summary['expect'] == 'daily' and summary['status'] == 'FAIL' and summary['failures'] == 4
    rows = read_out(tmp_path / 'work' / 'cmp.json')['comparisons']
    assert all(c['problems'] == ['verification_level'] for c in rows)


def test_expect_explore_rejects_daily_runs(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json', expect='explore')
    assert summary['status'] == 'FAIL' and summary['failures'] == 4
    assert all('verification_level' in c['problems'] for c in read_out(tmp_path / 'work' / 'cmp.json')['comparisons'])


def test_records_without_a_level_field_count_as_daily(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    for p in POINTS:
        for cost in (5, 10):
            path = grid / p['id'] / f'cost_{cost}bps' / 'completion.json'
            doc = read_out(path); doc.pop('verification_level'); dump(path, doc)
    summary = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json')
    assert summary['status'] == 'PASS' and summary['failures'] == 0
    assert all(c['problems'] == [] for c in read_out(tmp_path / 'work' / 'cmp.json')['comparisons'])


def test_unknown_expectation_is_rejected(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    with pytest.raises(ValueError, match='expect'):
        perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'cmp.json', expect='regression')


def test_parallel_workers_match_single_process(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    serial = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'serial.json', workers=1)
    parallel = perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'work' / 'parallel.json', workers=2)
    for doc in (serial, parallel):
        doc.pop('wall_seconds'); doc.pop('workers')
    assert serial == parallel
    assert parallel['status'] == 'PASS'
    assert read_out(tmp_path / 'work' / 'serial.json')['comparisons'] == read_out(tmp_path / 'work' / 'parallel.json')['comparisons']
    # Registered order (configuration, then cost) regardless of which worker finishes first.
    assert [(c['configuration_id'], c['cost_bps']) for c in read_out(tmp_path / 'work' / 'parallel.json')['comparisons']] == [
        ('L020_N10_b2_R1', 5), ('L020_N10_b2_R1', 10), ('L010_N08_b2_R1', 5), ('L010_N08_b2_R1', 10)]


def test_output_must_be_inside_work(tmp_path, small_registry):
    grid = make_grid(tmp_path, 'grid'); ref = make_grid(tmp_path, 'ref')
    with pytest.raises(ValueError, match='protected'):
        perf_compare.compare_grid(tmp_path, grid, ref, tmp_path / 'research' / 'v05' / 'results' / 'x.json')


def test_cli_exposes_compare_grid():
    from leadtrend_v05.__main__ import build_parser
    args = build_parser().parse_args(['compare-grid', '--grid', 'work/runs/g', '--output', 'work/runs/c.json'])
    assert args.grid == 'work/runs/g' and args.workers == 1
    assert args.reference is None  # defaults to the reviewed unified reference inside the command
    assert args.expect == 'daily'
    assert build_parser().parse_args(['compare-grid', '--grid', 'g', '--output', 'o', '--workers', '3']).workers == 3
    explore = build_parser().parse_args(['compare-grid', '--grid', 'g', '--output', 'o', '--expect', 'explore'])
    assert explore.expect == 'explore'
