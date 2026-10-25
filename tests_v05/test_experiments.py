"""Experiment specs: registration checks, grid and k rules, neighbours and ranking, authorisation and the scorecard."""
import json
import math
import subprocess
from pathlib import Path

import pytest

from leadtrend_v05 import experiments
from leadtrend_v05.experiments import (ExperimentContext, expand_grid, load_spec, make_point, neighbors_of,
                                       null_band, percentile, rank_points, reference_exit_counts, registered_attempts,
                                       registration_commit, run_metrics)
from leadtrend_v05.perf_bench import dump
from leadtrend_v05.stage4_registry import points as registry_points
from leadtrend_v05.stage4_summary import interval

ROOT = Path(__file__).resolve().parent.parent

MOM = lambda L: {'kind': 'momentum', 'L': L, 'skip': 0}  # noqa: E731


def base_spec(**overrides):
    spec = {'id': 'T1', 'created_date': '2026-10-26', 'hypothesis': 'test', 'baseline': experiments.BASELINE,
            'grid': [{'signal': [MOM(60), MOM(90)], 'N': [8], 'b': [2], 'rebalance': ['R2']}], 'costs_bps': [5, 10]}
    spec.update(overrides)
    return spec


def write_spec(root, spec):
    path = root / 'research/v05/experiments' / f"{spec['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(spec), encoding='utf-8')
    return path


def test_k_is_computed_by_the_registry_rule_for_every_registered_size():
    for p in registry_points():
        assert make_point(MOM(p['L']), p['N'], p['b'], p['rebalance'])['k'] == p['k']
        assert make_point(MOM(p['L']), p['N'], p['b'], p['rebalance'])['id'] == p['id']


def test_grid_blocks_are_products_and_their_union():
    pts = expand_grid([
        {'signal': [MOM(60), MOM(90), MOM(126)], 'N': [8], 'b': [2], 'rebalance': ['R2']},
        {'signal': [MOM(60), MOM(90)], 'N': [10], 'b': [2], 'rebalance': ['R2']}])
    assert [p['id'] for p in pts] == ['L060_N08_b2_R2', 'L090_N08_b2_R2', 'L126_N08_b2_R2',
                                      'L060_N10_b2_R2', 'L090_N10_b2_R2']
    assert len(expand_grid([{'signal': [MOM(60)], 'N': [8, 8], 'b': [2], 'rebalance': ['R2']}])) == 1


def test_non_default_signals_get_keyed_ids():
    p = make_point({'kind': 'momentum', 'L': 60, 'skip': 5}, 8, 2, 'R2')
    assert p['id'].startswith('S') and p['id'].endswith('_L060_N08_b2_R2')


def test_neighbours_move_one_step_on_one_axis_only():
    pts = expand_grid([{'signal': [MOM(60), MOM(90), MOM(126)], 'N': [8], 'b': [2], 'rebalance': ['R2']},
                       {'signal': [MOM(90)], 'N': [10], 'b': [2], 'rebalance': ['R2']}])
    by_id = {p['id']: p for p in pts}
    near = {p['id'] for p in neighbors_of(by_id['L090_N08_b2_R2'], pts)}
    assert near == {'L060_N08_b2_R2', 'L126_N08_b2_R2', 'L090_N10_b2_R2'}
    assert 'L060_N10_b2_R2' not in near  # two steps away on two axes


def test_rank_uses_neighbourhood_means_then_id_and_incomplete_runs_rank_last():
    pts = expand_grid([{'signal': [MOM(60), MOM(90), MOM(126)], 'N': [8], 'b': [2], 'rebalance': ['R2']}])
    excess = {'L060_N08_b2_R2': 0.01, 'L090_N08_b2_R2': 0.02, 'L126_N08_b2_R2': 0.04}
    complete = {k: True for k in excess}
    order, means = rank_points(pts, excess, complete)
    assert means['L090_N08_b2_R2'] == pytest.approx((0.01 + 0.02 + 0.04) / 3)  # its own value and both neighbours
    assert means['L126_N08_b2_R2'] == pytest.approx((0.02 + 0.04) / 2)
    assert means['L060_N08_b2_R2'] == pytest.approx((0.01 + 0.02) / 2)
    assert [p['id'] for p in order] == ['L126_N08_b2_R2', 'L090_N08_b2_R2', 'L060_N08_b2_R2']
    complete['L126_N08_b2_R2'] = False
    _, means = rank_points(pts, excess, complete)
    assert means['L090_N08_b2_R2'] == -math.inf


def test_percentile_counts_values_at_or_below():
    assert percentile(5, [1, 2, 3, 4, 5, 6]) == 83.3
    assert percentile(None, [1]) == '' and percentile(1, []) == ''


def test_spec_with_holdout_or_later_end_or_wrong_baseline_is_refused(tmp_path):
    cases = [base_spec(hypothesis='probe holdout'), base_spec(end_date='2024-07-05'),
             base_spec(baseline=dict(experiments.BASELINE, N=10)), base_spec(costs_bps=[10]), base_spec(id='other')]
    for spec in cases:
        path = tmp_path / 'research/v05/experiments/T1.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec), encoding='utf-8')
        with pytest.raises(ValueError):
            load_spec(tmp_path, path)


def test_valid_spec_loads_with_defaults(tmp_path):
    path = write_spec(tmp_path, base_spec())
    loaded = load_spec(tmp_path, path)
    assert loaded['null_runs'] == 200
    assert [p['id'] for p in loaded['grid']] == ['L060_N08_b2_R2', 'L090_N08_b2_R2']


def git(root, *args):
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', *args], cwd=root,
                          capture_output=True, text=True, check=True).stdout.strip()


def test_registration_needs_a_clean_committed_spec(tmp_path):
    git(tmp_path, 'init', '-q', '-b', 'main')
    write_spec(tmp_path, base_spec())
    path = tmp_path / 'research/v05/experiments/T1.json'
    with pytest.raises(ValueError, match='uncommitted changes'):
        registration_commit(tmp_path, path)  # untracked files count as uncommitted
    git(tmp_path, 'add', '-A')
    git(tmp_path, 'commit', '-q', '-m', 'register')
    commit = registration_commit(tmp_path, path)
    assert commit == git(tmp_path, 'rev-parse', 'HEAD')
    path.write_text(path.read_text(encoding='utf-8') + ' ', encoding='utf-8')
    with pytest.raises(ValueError, match='uncommitted changes'):
        registration_commit(tmp_path, path)  # modified after registration


class _Ctx(ExperimentContext):
    def __init__(self):  # bypass the cache-only constructor; authorize needs only these attributes
        self.segment = {'start': '2019-11-29', 'end': '2024-06-28'}
        self.start, self.end = None, None
        self.configure(points=[make_point(MOM(60), 8, 2, 'R2'), make_point(MOM(90), 10, 2, 'R2')], costs=[5, 10],
                       experiment={'id': 'T1'})


def raw_for(p, **over):
    raw = dict(configuration_id=p['id'], L=p['L'], N=p['N'], k=p['k'], b=p['b'], rebalance=p['rebalance'],
               signal=p['signal'], slippage_bps=10, initial_capital=100000, fee_per_trade=1,
               start_date='2019-11-29', end_date='2024-06-28')
    raw.update(over)
    return raw


def test_authorisation_admits_only_registered_points_costs_and_the_development_segment():
    ctx = _Ctx()
    p = ctx.points['L060_N08_b2_R2']
    assert ctx.authorize(raw_for(p), None) == (None, None)
    with pytest.raises(ValueError, match='not in the registered'):
        ctx.authorize(raw_for(p, configuration_id='L010_N08_b2_R2'), None)
    with pytest.raises(ValueError, match='unregistered cost'):
        ctx.authorize(raw_for(p, slippage_bps=7), None)
    with pytest.raises(ValueError, match='segment'):
        ctx.authorize(raw_for(p, end_date='2024-07-05'), None)
    with pytest.raises(ValueError, match='segment'):
        ctx.authorize(raw_for(p), '2024-07-05')
    with pytest.raises(ValueError, match='does not match'):
        ctx.authorize(raw_for(p, k=3), None)


def fixture_run(folder, excess_pattern=(100000.0, 110000.0), bench=(100000.0, 120000.0)):
    folder.mkdir(parents=True)
    dump(folder / 'summary.json', {'status': 'completed', 'comparison_start_session': '2019-12-02', 'total_return': 0.1,
        'annualized_return': 0.02, 'drawdown': {'max_drawdown': 0.05},
        'relative_nav_underwater': {'longest_underwater_calendar_days': 1, 'unrecovered_at_end': False},
        'longest_negative_rolling_52_week_relative_return_weeks': 1, 'annual_turnover_one_way_half_two_sided': 1.0,
        'n_trades': 2, 'fees_total': 2.0, 'slippage_cost_dollars': 1.0, 'average_completed_holding_weeks': 1.0,
        'sub_500_dollar_trades': 0, 'top_five_return_contributions': []})
    (folder / 'daily_equity.csv').write_text(
        f'session_date,total_equity,benchmark_value\n2019-12-02,{excess_pattern[0]},{bench[0]}\n'
        f'2024-06-28,{excess_pattern[1]},{bench[1]}\n', encoding='utf-8')
    (folder / 'benchmark.csv').write_text(
        f'session_date,benchmark_value\n2019-12-02,{bench[0]}\n2024-06-28,{bench[1]}\n', encoding='utf-8')


def test_run_metrics_reuses_the_interval_arithmetic(tmp_path):
    fixture_run(tmp_path / 'run')
    m = run_metrics(tmp_path / 'run')
    from leadtrend_v05.perf_artifacts import read_table
    equity = read_table(tmp_path / 'run' / 'daily_equity.csv')
    bench = read_table(tmp_path / 'run' / 'benchmark.csv')
    full = interval(equity, bench, 'opening', '2024-06-28', '2019-12-02', 100000.0)
    assert m['annualized_excess'] == full['annualized_excess']
    assert m['benchmark_annualized'] == full['benchmark_annualized']
    assert m['annualized_return'] == 0.02 and m['max_drawdown'] == 0.05 and m['turnover'] == 1.0


def test_scorecard_files_carry_the_registration_header(tmp_path):
    rows = [dict(id='L090_N08_b2_R2', signal='动量 L=90', N=8, b=2, rebalance='R2', annualized_return_10bps=0.02,
                 annualized_return_5bps=0.021, annualized_excess_10bps=0.01, annualized_excess_5bps=0.011,
                 excess_vs_baseline_10bps='', neighborhood_mean_10bps=0.01, max_drawdown=0.05, turnover_one_way=1.0,
                 excess_2020=0.0, excess_2021=0.0, excess_2022=0.0, excess_2023=0.0, winning_years=0,
                 high_missing_excess=0.0, lower_missing_excess=0.0, null_percentile=60.0, null_percentile_low=45.0,
                 null_percentile_high=75.0, null_completed='15/20', null_turnover_median=1.25, daily_confirmed='yes')]
    experiments._write_scorecard(tmp_path, rows, dict(id='T1', attempts=72, registration_commit='abc', hypothesis='自检，与第四阶段重复',
                                                       baseline_check='5bps PASS, 10bps PASS',
                                                       null_counts={'L090_N08_b2_R2': '15/20'}))
    md = (tmp_path / 'scorecard.md').read_text(encoding='utf-8')
    assert '累计试验次数：72' in md and '登记文件提交号：abc' in md and '幸存者偏差' in md
    assert '随机对照完成数（完成/登记）：L090_N08_b2_R2 15/20' in md and '登记假设：自检，与第四阶段重复' in md
    assert '60.0%（45.0%–75.0%）' in md and '| 2.0% | 1.0% |' in md  # percentages to one decimal
    assert '| 5.0% | 1.0 |' in md  # drawdown is a percentage, turnover is a ratio, both to one decimal
    header_line = (tmp_path / 'scorecard.csv').read_text(encoding='utf-8').splitlines()[0]
    assert header_line.startswith('id,signal') and 'null_percentile_low' in header_line and 'null_turnover_median' in header_line
    assert (tmp_path / 'scorecard.csv').read_text(encoding='utf-8').splitlines()[1].split(',')[-2] == '1.25'  # raw values


def test_null_band_reports_the_completed_point_and_bounds_for_stopped_runs():
    values = [0.01, 0.02, 0.03, 0.04]  # completed null excess values
    assert null_band(0.025, values, 4) == (50.0, 50.0, 50.0)
    assert null_band(0.025, values, 8) == (50.0, 25.0, 75.0)  # four stopped: all above (lower) or all below (upper)
    assert null_band(None, values, 8) == ('', '', '')
    assert null_band(0.025, [], 4) == ('', 0.0, 100.0)


def test_reference_exit_counts_only_full_sells_per_signal_week(tmp_path):
    folder = tmp_path / 'ref'
    folder.mkdir()
    (folder / 'trades.csv').write_text(
        'session_date,security_id,side,shares,signal_session_date,shares_after\n'
        '2020-01-02,A,sell,10,2019-12-27,0\n'
        '2020-01-02,B,sell,5,2019-12-27,3\n'  # a trim is not a full exit
        '2020-01-02,C,buy,7,2019-12-27,7\n'
        '2020-01-09,D,sell,4,2020-01-03,0\n', encoding='utf-8')
    assert reference_exit_counts(folder) == {'2019-12-27': 1, '2020-01-03': 1}


def test_registered_attempts_count_distinct_configurations_once(tmp_path):
    # The repository now registers E0 (no new attempts) and R1_mechanisms (twelve new configurations): 72 + 12.
    assert registered_attempts(ROOT) == 84
    write_spec(tmp_path, base_spec())  # L060 and L090 at N8: both already in the stage 4 grid
    assert registered_attempts(tmp_path) == 72
    write_spec(tmp_path, base_spec(id='T2', grid=[{'signal': [MOM(60)], 'N': [9], 'b': [2], 'rebalance': ['R2']}]))
    assert registered_attempts(tmp_path) == 73


def test_selection_must_match_the_registration_for_every_point():
    ctx = _Ctx()
    null = dict(make_point(MOM(60), 8, 2, 'R2'), id='NULL_L060_N08_b2_R2_s000', selection='random_matched',
                selection_params=dict(seed=0, exit_counts={}))
    ctx.configure(points=[null], costs=[5, 10], experiment={'id': 'T1'})
    assert ctx.authorize(dict(raw_for(null), configuration_id=null['id'], selection='random_matched',
                              selection_params=dict(seed=0, exit_counts={})), None) == (None, None)
    with pytest.raises(ValueError, match='selection does not match'):
        ctx.authorize(dict(raw_for(null), configuration_id=null['id']), None)  # default selection requested
