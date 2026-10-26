"""Strategy experiments: a committed spec, one command, a fixed scorecard.

A spec is research/v05/experiments/<ID>.json. It must be committed with no pending changes before it runs, and the
commit that holds it is written into every output. Only the development segment (to 2024-06-28) is ever run.

Grid blocks are either axis products (signal, N, b, rebalance, selection, selection_params, all lists) or explicit
points (a list of dicts, each with its own anchor). A point's anchor is a configuration ID in the grid or a stage 4
configuration ID; the anchor is run too, and it is compared with paired differences.
"""
import csv
import hashlib
import json
import math
import multiprocessing
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

from . import paths
from .perf_artifacts import read_table
from .perf_bench import Timer, dump, sha
from .sectors import load_sector_map, sector_key
from .selection import SELECTION_RULES
from .selection_structured import RULE_PARAMS
from .signals import canonical, describe, is_default_momentum, main_L, normalize, signal_key
from .stage4_registry import digest, points as stage4_points, verify
from .stage4_summary import interval
from .perf_cache import OptimizedContext

EXPERIMENTS = 'research/v05/experiments'
BASELINE = {'id': 'L090_N08_b2_R2', 'L': 90, 'N': 8, 'k': 2, 'b': 2, 'rebalance': 'R2'}
BASELINE_SIGNAL = {'kind': 'momentum', 'L': 90, 'skip': 0}
A90_ID = 'L090_N10_b2_R2'  # the paired-difference reference of every configuration in a round-1 style experiment
PAIRS = [('ENT_NEG', 'ENT_POS'), ('SEC2', 'SEC3'), ('PEER', 'G'), ('SKP90', 'A90'), ('SKP60', 'A60')]
PAIR_ALIASES = {'A90': 'L090_N10_b2_R2', 'A60': 'L060_N10_b2_R2', 'A126': 'L126_N10_b2_R2', 'BASE_N08': 'L090_N08_b2_R2',
                'BASE_N12': 'L090_N12_b2_R2'}
REFERENCE_NAME = 'WGT'  # round 1's voo_weight reference; the risk columns report differences from it
TRADING_DAYS = 252
COSTS = [5, 10]
DEV_START, DEV_END = '2019-11-29', '2024-06-28'
DEFAULT_NULL_RUNS = 200
PERIODS = ('2019-11-29 to 2021-12-31', '2022-01-01 to 2024-06-28')
REGISTERED_SELECTIONS = set(RULE_PARAMS)


def selection_key(selection, params):
    """Eight hex digits naming a registered selection rule and its parameters; empty for the default rule."""
    if selection == 'default' and not params:
        return ''
    blob = json.dumps({'selection': selection, 'params': params or {}}, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()[:8]


def make_point(signal, N, b, rebalance, selection='default', selection_params=None, anchor=None):
    sig = normalize(signal)
    L = main_L(sig)
    base_id = f'L{L:03d}_N{N:02d}_b{b}_{rebalance}'
    pid = base_id if is_default_momentum(sig) else f'S{signal_key(sig)}_{base_id}'
    point = {'id': pid, 'signal': sig, 'L': L, 'N': N, 'k': max(1, math.floor(.3 * N + .5)), 'b': b, 'rebalance': rebalance}
    if selection != 'default' or selection_params:
        if selection not in SELECTION_RULES or selection == 'random_matched':
            raise ValueError('unknown or reserved selection rule: ' + selection)
        params = dict(selection_params or {})
        unknown = set(params) - set(RULE_PARAMS.get(selection, {}))
        if unknown:
            raise ValueError(f'{selection} does not take parameters {sorted(unknown)}')
        point.update(id=f'{pid}_{selection}_{selection_key(selection, params)}', selection=selection, selection_params=params)
    if anchor is not None:
        point['anchor'] = anchor
    return point


def _point_from_item(item):
    unknown = set(item) - {'signal', 'N', 'b', 'rebalance', 'selection', 'selection_params', 'anchor', 'name'}
    if unknown:
        raise ValueError('grid point has unknown keys: ' + ','.join(sorted(unknown)))
    p = make_point(item['signal'], item['N'], item['b'], item['rebalance'], item.get('selection', 'default'),
                   item.get('selection_params'), item.get('anchor'))
    if item.get('name') is not None:
        p['name'] = item['name']  # a label for reports only; not part of the identity
    return p


def expand_grid(blocks):
    """Each block is an axis product or an explicit point list; the grid is the union (duplicates collapse)."""
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('grid must be a non-empty list of blocks')
    points = {}
    for block in blocks:
        if 'points' in block:
            if not isinstance(block['points'], list) or not block['points']:
                raise ValueError('points block needs a non-empty list')
            for item in block['points']:
                p = _point_from_item(item)
                points.setdefault(p['id'], p)
            continue
        for axis in ('signal', 'N', 'b', 'rebalance'):
            if not isinstance(block.get(axis), list) or not block[axis]:
                raise ValueError(f'grid block needs a non-empty {axis} list')
        selections = block.get('selection', ['default'])
        params_list = block.get('selection_params', [None])
        for signal in block['signal']:
            for N in block['N']:
                for b in block['b']:
                    for R in block['rebalance']:
                        for selection in selections:
                            for params in params_list:
                                p = make_point(signal, N, b, R, selection, params, block.get('anchor'))
                                points.setdefault(p['id'], p)
    return list(points.values())


def load_spec(root, spec_path):
    path = Path(spec_path)
    text = path.read_text(encoding='utf-8')
    if 'holdout' in text.lower():
        raise ValueError('experiment specs may not mention the holdout')
    spec = json.loads(text)
    if spec.get('id') != path.stem:
        raise ValueError('spec id must equal the file name')
    for key in ('created_date', 'hypothesis', 'grid', 'costs_bps'):
        if key not in spec:
            raise ValueError(f'spec is missing {key}')
    if spec.get('baseline') != BASELINE:
        raise ValueError('the baseline is fixed to ' + BASELINE['id'])
    if spec['costs_bps'] != COSTS:
        raise ValueError('costs_bps must be [5, 10]')
    if spec.get('end_date', DEV_END) > DEV_END:
        raise ValueError('experiments may only use the development segment (to ' + DEV_END + ')')
    null_runs = spec.get('null_runs', DEFAULT_NULL_RUNS)
    if isinstance(null_runs, bool) or not isinstance(null_runs, int) or null_runs < 0:
        raise ValueError('null_runs must be a non-negative integer')
    grid = expand_grid(spec['grid'])
    confirm_top = spec.get('confirm_top', len(grid))
    if isinstance(confirm_top, bool) or not isinstance(confirm_top, int) or confirm_top < len(grid):
        raise ValueError('confirm_top must cover every configuration of the grid: daily confirmation for all')
    return dict(spec=spec, path=path, id=spec['id'], grid=grid, null_runs=null_runs, confirm_top=confirm_top, sha256=digest(path))


def registration_commit(root, spec_path):
    """Refuse unless the repository is clean and the spec is committed; return the commit that holds it."""
    root = Path(root)
    rel = Path(spec_path).resolve().relative_to(root.resolve()).as_posix()
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=root, capture_output=True, text=True, check=True).stdout
    if status.strip():
        raise ValueError('uncommitted changes: commit the registration file and everything else first')
    tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', rel], cwd=root, capture_output=True).returncode == 0
    if not tracked:
        raise ValueError('commit the registration file first (' + rel + ' is not tracked)')
    return subprocess.run(['git', 'log', '-1', '--format=%H', '--', rel], cwd=root, capture_output=True,
                          text=True, check=True).stdout.strip()


def attempt_key(point):
    """One configuration is one attempt: normalised signal, N, k, b, rebalance, selection rule and its parameters."""
    return (canonical(point['signal']), point['N'], point['k'], point['b'], point['rebalance'],
            point.get('selection', 'default'), json.dumps(point.get('selection_params') or {}, sort_keys=True))


def registered_attempts(root):
    """Distinct configurations tried: the stage 4 grid plus every registered experiment grid, de-duplicated."""
    seen = {attempt_key(make_point({'kind': 'momentum', 'L': p['L'], 'skip': 0}, p['N'], p['b'], p['rebalance']))
            for p in stage4_points()}
    for path in sorted((Path(root) / EXPERIMENTS).glob('*.json')):
        seen.update(attempt_key(p) for p in expand_grid(json.loads(path.read_text(encoding='utf-8'))['grid']))
    return len(seen)


def _zero_L(signal):
    n = normalize(signal)
    if n['kind'] == 'composite':
        return {'kind': 'composite', 'components': [_zero_L(c) for c in n['components']],
                'weights': n['weights'], 'combine': n['combine']}
    return dict(n, L=0)


def _rest_key(point):
    return json.dumps([_zero_L(point['signal']), point.get('selection', 'default'),
                       point.get('selection_params') or {}], sort_keys=True, separators=(',', ':'))


def neighbors_of(point, points):
    """Configurations one step along exactly one ordinal axis (momentum L, N, b, rebalance); the rest equal."""
    axes = ('L', 'N', 'b', 'rebalance')
    rest = _rest_key(point)
    found = {}
    for axis in axes:
        def same_rest(q):
            return _rest_key(q) == rest and all(q[a] == point[a] for a in axes if a != axis)
        candidates = [q for q in points if q['id'] != point['id'] and same_rest(q)]
        values = sorted({q[axis] for q in candidates} | {point[axis]}, key=lambda v: (isinstance(v, str), v))
        i = values.index(point[axis])
        adjacent = {values[j] for j in (i - 1, i + 1) if 0 <= j < len(values)}
        for q in candidates:
            if q[axis] in adjacent:
                found[q['id']] = q
    return [found[k] for k in sorted(found)]


def rank_points(points, excess10, complete):
    """Neighbourhood mean of 10 bp annualised excess (own value plus neighbours), best first, ties by id."""
    def value(q):
        v = excess10.get(q['id'])
        return v if complete.get(q['id']) and v is not None else -math.inf
    means = {}
    for p in points:
        group = [p] + neighbors_of(p, points)
        values = [value(q) for q in group]
        means[p['id']] = -math.inf if any(v == -math.inf for v in values) else sum(values) / len(values)
    order = sorted(points, key=lambda p: (-means[p['id']], p['id']))
    return order, means


def percentile(value, values):
    if value is None or not values:
        return ''
    return round(100.0 * sum(1 for v in values if v <= value) / len(values), 1)


def null_band(value, values, total):
    """(point, lower, upper) in percent for a configuration's value among its null controls.

    The point uses the completed runs only. Of the `total` registered runs, the lower bound assumes every stopped
    run was above `value`, the upper bound assumes every stopped run was below it. Blank when there is no value.
    """
    if value is None or total == 0:
        return '', '', ''
    at_or_below = sum(1 for v in values if v <= value)
    return percentile(value, values), round(100.0 * at_or_below / total, 1), round(100.0 * (at_or_below + total - len(values)) / total, 1)


def reference_exit_counts(folder):
    """Full exits per signal week in a reference run: sells that take the position to zero shares."""
    counts = {}
    for row in read_table(Path(folder) / 'trades.csv'):
        if row['side'] == 'sell' and float(row['shares_after']) == 0:
            counts[row['signal_session_date']] = counts.get(row['signal_session_date'], 0) + 1
    return counts


def run_metrics(folder, initial=100000.0):
    s = json.loads((Path(folder) / 'summary.json').read_text(encoding='utf-8'))
    equity = read_table(Path(folder) / 'daily_equity.csv')
    benchmark = read_table(Path(folder) / 'benchmark.csv')
    first = s['comparison_start_session']
    full = interval(equity, benchmark, 'opening', DEV_END, first, initial)
    years, year_returns = {}, {}
    for y in range(2020, 2024):
        x = interval(equity, benchmark, f'{y - 1}-12-31', f'{y}-12-31', first, initial)
        years[y] = x['strategy_return'] - x['benchmark_return'] if x else None
        year_returns[y] = (x['strategy_return'], x['benchmark_return']) if x else None
    high = interval(equity, benchmark, 'opening', '2021-12-31', first, initial)
    lower = interval(equity, benchmark, '2021-12-31', DEV_END, first, initial)
    return dict(
        annualized_return=s['annualized_return'], annualized_excess=full['annualized_excess'],
        benchmark_annualized=full['benchmark_annualized'], max_drawdown=s['drawdown']['max_drawdown'],
        turnover=s['annual_turnover_one_way_half_two_sided'],
        years=years, year_returns=year_returns, winning_years=sum(1 for v in years.values() if v is not None and v > 0),
        high_missing_excess=high['annualized_excess'] if high else None,
        lower_missing_excess=lower['annualized_excess'] if lower else None)


def _stdev(xs):
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _cov(xs, ys):
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (len(xs) - 1)


def _worst_drawdown(values):
    peak, worst = values[0], 0.0
    for v in values:
        peak = max(peak, v)
        worst = min(worst, v / peak - 1)
    return worst


def risk_from_series(days, equity, benchmark, annual_return, max_drawdown):
    """Daily risk of one run against VOO on the same sessions (ISO dates, values from the first comparison session).

    Volatility and tracking error are sample deviations of daily returns, annualised over 252 sessions; beta is the
    covariance with VOO's daily returns over VOO's variance. Return over volatility and over drawdown use the run's CAGR.
    The 2022 drawdown takes its peak inside calendar 2022 only.
    """
    rs = [equity[i] / equity[i - 1] - 1 for i in range(1, len(equity))]
    rb = [benchmark[i] / benchmark[i - 1] - 1 for i in range(1, len(benchmark))]
    vol = _stdev(rs) * math.sqrt(TRADING_DAYS)
    var_b = _cov(rb, rb)
    in_2022 = [e for d, e in zip(days, equity) if d.startswith('2022-')]
    return dict(vol=vol, beta=_cov(rs, rb) / var_b if var_b else None,
                te=_stdev([x - y for x, y in zip(rs, rb)]) * math.sqrt(TRADING_DAYS),
                ret_vol=annual_return / vol if vol else None,
                ret_dd=annual_return / abs(max_drawdown) if max_drawdown else None,
                dd_2022=_worst_drawdown(in_2022) if in_2022 else None)


def risk_metrics(folder, m):
    """risk_from_series for one daily-verified run folder; m is its run_metrics result."""
    folder = Path(folder)
    first = json.loads((folder / 'summary.json').read_text(encoding='utf-8'))['comparison_start_session']
    equity = {r['session_date']: float(r['total_equity']) for r in read_table(folder / 'daily_equity.csv')}
    bench = {r['session_date']: float(r['benchmark_value']) for r in read_table(folder / 'benchmark.csv')}
    days = sorted(d for d in equity if first <= d <= DEV_END and d in bench)
    return risk_from_series(days, [equity[d] for d in days], [bench[d] for d in days], m['annualized_return'], m['max_drawdown'])


def leave_one_year_out(year_returns):
    """Excess over VOO (annualised, the three remaining years compounded) when one of 2020-2023 is removed."""
    out = {}
    for y in year_returns:
        rest = [v for j, v in year_returns.items() if j != y]
        if any(v is None for v in rest):
            out[y] = None
            continue
        m = len(rest)
        strategy = math.prod(1 + s for s, _ in rest)
        voo = math.prod(1 + b for _, b in rest)
        out[y] = (strategy ** (1 / m) - 1) - (voo ** (1 / m) - 1)
    return out


def position_shares(folder, sectors, equity_by_day):
    """Largest single-name weight and daily largest sector weight, from a daily-verified run's positions."""
    folder = Path(folder)
    per_day_max_name = defaultdict(float)
    per_day_sector = defaultdict(lambda: defaultdict(float))
    for r in read_table(folder / 'positions.csv'):
        day = r['session_date']
        if day not in equity_by_day:
            continue
        value = float(r['value'])
        share = value / equity_by_day[day]
        per_day_max_name[day] = max(per_day_max_name[day], share)
        if r.get('asset_type', 'actual_shares') == 'actual_shares':
            per_day_sector[day][sectors.get(r['industry'], r['industry'])] += share
    days = sorted(equity_by_day)
    daily_name = [per_day_max_name.get(d, 0.0) for d in days]
    daily_sector = [max(per_day_sector[d].values()) if per_day_sector.get(d) else 0.0 for d in days]
    n = len(days) or 1
    return dict(max_single_weight=max(daily_name, default=0.0), median_daily_max_single_weight=median(daily_name) if daily_name else 0.0,
                sector_over_40_share=sum(1 for v in daily_sector if v > 0.40) / n,
                sector_over_50_share=sum(1 for v in daily_sector if v > 0.50) / n)


def _run_folder(base, point, cost, phase):
    return Path(base) / phase / point['id'] / f'cost_{cost}bps'


def _jobs(ctx_points, base, phase, mode, level, costs, experiment):
    jobs = []
    for p in ctx_points:
        p = dict(p, experiment=experiment)
        for cost in costs:
            jobs.append((p, cost, str(_run_folder(base, p, cost, phase)), mode, False, level))
    return jobs


class ExperimentContext(OptimizedContext):
    """Cached context whose authorisation admits only registered experiment points (the sealed grid is untouched)."""

    def configure(self, points, costs, experiment):
        self.points = {p['id']: p for p in points}
        self.costs = list(costs)
        self.experiment = experiment

    def authorize(self, raw, end_override):
        if end_override is not None:
            raise ValueError('unregistered segment / holdout boundary')
        p = self.points.get(raw.get('configuration_id'))
        if p is None:
            raise ValueError('configuration is not in the registered experiment')
        for key in ('L', 'N', 'k', 'b', 'rebalance'):
            if raw.get(key) != p[key]:
                raise ValueError('configuration does not match the registration: ' + key)
        if normalize(raw.get('signal', BASELINE_SIGNAL)) != p['signal']:
            raise ValueError('signal does not match the registration')
        if raw.get('selection', 'default') != p.get('selection', 'default') or raw.get('selection_params') != p.get('selection_params'):
            raise ValueError('selection does not match the registration')
        if raw.get('slippage_bps') not in self.costs:
            raise ValueError('unregistered cost')
        if raw.get('initial_capital') != 100000 or raw.get('fee_per_trade') != 1:
            raise ValueError('unregistered account assumption')
        if raw['start_date'] != self.segment['start'] or raw['end_date'] != self.segment['end']:
            raise ValueError('unregistered segment / holdout boundary')
        return self.start, self.end


def _init_worker(cache_path, cache_key, payload):
    from . import perf_runner
    from .perf_cache import worker_cache
    ctx = worker_cache(cache_path, cache_key)
    ctx.__class__ = ExperimentContext
    ctx.configure(**payload)
    perf_runner._TIMER = Timer()
    perf_runner._TIMER.instrument()
    perf_runner._CTX = ctx


def _run_jobs(jobs, ctx, workers, payload):
    """Run every job whose outcome is not already accepted under the same key; return the number executed."""
    from . import perf_runner
    from .perf_runner import accepted, run_key
    todo = []
    for job in jobs:
        p, cost, folder, mode, legacy, level = job
        if not accepted(folder, run_key(ctx.cache_key, p, cost, mode, legacy, level)):
            todo.append(job)
    if not todo:
        return 0
    if workers == 1:
        perf_runner._CTX = ctx
        perf_runner._TIMER = Timer()
        perf_runner._TIMER.instrument()
        ctx.configure(**payload)
        for job in todo:
            perf_runner.one_job(job)
        return len(todo)
    spawn = multiprocessing.get_context('spawn')
    cache_path = str(ctx.cache_folder / 'prepared.pkl')
    with spawn.Pool(workers, initializer=_init_worker, initargs=(cache_path, ctx.cache_key, payload)) as pool:
        list(pool.imap_unordered(perf_runner.one_job, todo))
    return len(todo)


def _experiment_ctx(root, doc, payload):
    from .perf_cache import load_cache
    ctx, _ = load_cache(root, doc)
    ctx.__class__ = ExperimentContext
    ctx.configure(**payload)
    return ctx


def _metrics_or_none(folder):
    folder = Path(folder)
    if not (folder / 'completion.json').exists():
        return None
    done = json.loads((folder / 'completion.json').read_text(encoding='utf-8'))
    if done.get('status') != 'completed':
        return None
    if done.get('verification_level') == 'explore' and (done.get('explore_check') or {}).get('status') != 'PASS':
        return None
    return run_metrics(folder)


def check_baseline(root, base):
    """The baseline's explore summaries must match the reviewed stage 4 runs byte for byte (skipped if absent)."""
    results = {}
    for cost in COSTS:
        ref = Path(root) / paths.STAGE4_RUNS / BASELINE['id'] / f'cost_{cost}bps' / 'summary.json'
        new = Path(base) / 'explore' / BASELINE['id'] / f'cost_{cost}bps' / 'summary.json'
        if not ref.exists():
            results[f'{cost}bps'] = 'skipped: reference not present in this checkout'
        else:
            equal = ref.read_bytes() == new.read_bytes()
            results[f'{cost}bps'] = 'PASS' if equal else 'FAIL'
            if not equal:
                raise ValueError(f'baseline summary differs from the reviewed run at {cost} bps; experiment withdrawn')
    return results


def _pct(v):
    return '' if v in (None, '') else f'{100 * v:.1f}%'


def _ratio(v):
    return '' if v in (None, '') else f'{v:.1f}'


def _ratio2(v):
    return '' if v in (None, '') else f'{v:.2f}'


def _null_cell(row):
    if row['null_percentile_low'] == '':
        return ''
    point = '无完成运行' if row['null_percentile'] == '' else f"{row['null_percentile']:.1f}%"
    return f"{point}（{row['null_percentile_low']:.1f}%–{row['null_percentile_high']:.1f}%）"


SCORECARD_COLUMNS = [
    ('配置', 'id', str), ('信号', 'signal', str), ('选股规则', 'selection', str), ('N', 'N', str), ('b', 'b', str),
    ('再平衡', 'rebalance', str), ('锚点', 'anchor', str),
    ('10bp年化收益', 'annualized_return_10bps', _pct), ('10bp年化超额', 'annualized_excess_10bps', _pct),
    ('5bp年化超额', 'annualized_excess_5bps', _pct), ('相对锚点超额差(10bp)', 'paired_vs_anchor_10bps', _pct),
    ('相对锚点超额差(5bp)', 'paired_vs_anchor_5bps', _pct), ('相对A90超额差(10bp)', 'paired_vs_A90_10bps', _pct),
    ('相对A90超额差(5bp)', 'paired_vs_A90_5bps', _pct), ('时段A配对差', 'paired_period_a', _pct),
    ('时段B配对差', 'paired_period_b', _pct), ('逐年剔除一年后超额最小值', 'leave_one_year_out_min', _pct),
    ('剔除后仍为正的次数(/4)', 'leave_one_year_out_positive', str), ('最大回撤', 'max_drawdown', _pct),
    ('年单边换手(x)', 'turnover_one_way', _ratio), ('2020超额', 'excess_2020', _pct), ('2021超额', 'excess_2021', _pct),
    ('2022超额', 'excess_2022', _pct), ('2023超额', 'excess_2023', _pct), ('胜年数', 'winning_years', str),
    ('随机对照百分位', 'null_cell', str), ('随机完成数', 'null_completed', str), ('随机换手中位数(x)', 'null_turnover_median', _ratio),
    ('最大单股权重', 'max_single_weight', _pct), ('单板块超40%天数占比', 'sector_over_40_share', _pct),
    ('单板块超50%天数占比', 'sector_over_50_share', _pct), ('ENT一遍买入', 'pass1_buys', str), ('ENT二遍买入', 'pass2_buys', str),
    ('CAP触发周数', 'cap_trigger_weeks', str), ('CAP顺延次数', 'cap_deferred', str),
    ('年化波动', 'vol_annual', _pct), ('β(对VOO)', 'beta', _ratio2), ('跟踪误差', 'tracking_error', _pct),
    ('收益/波动', 'ret_over_vol', _ratio2), ('收益/回撤', 'ret_over_dd', _ratio2), ('2022年最大回撤', 'dd_2022', _pct),
    ('相对WGT超额差(10bp)', 'paired_vs_WGT_10bps', _pct), ('相对WGT年化波动差', 'vol_vs_WGT', _pct),
    ('相对WGT最大回撤差', 'dd_vs_WGT', _pct), ('相对WGT收益/波动差', 'ret_over_vol_vs_WGT', _ratio2),
]


def _write_scorecard(out, rows, header):
    fields = list(rows[0].keys())
    with (out / 'scorecard.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    lines = ['# 成绩单：' + header['id'], '']
    lines += [f"- 累计试验次数：{header['attempts']}（按（规范化信号、选股规则、参数、N、k、b、再平衡）去重，计入第四阶段 72 个配置与 research/v05/experiments/ 下全部已登记配置）",
              f"- 登记文件提交号：{header['registration_commit']}",
              f"- 登记假设：{header['hypothesis']}",
              f"- 基准逐字节检查：{header['baseline_check']}",
              '- 开发段结果，存在幸存者偏差，使用当前行业分类；不构成策略有效性的结论。成绩单不设门槛。',
              '- 随机对照为换手匹配：每个配置一组，种子从 0 起，退出只数取自该配置自己的 10 bp 探索档运行，并遵守该配置的细分行业 k 与大板块上限（如有）。',
              '- 随机对照百分位格式为“点估计（下界–上界）”：该配置 10 bp 年化超额不高于随机结果的比例。点估计只用已完成的运行；下界假设停止的运行全部高于该配置，上界假设全部低于。',
              '- 配对差 = 本配置减锚点（同一成本、同一时段）；逐年剔除一年后，用其余三年复利求得的相对 VOO 年化超额。',
              '- 风险列来自 daily 运行（10 bp）：相对 VOO 的日收益，252 个交易日年化；收益/波动、收益/回撤用年化收益（CAGR）；'
              '2022 年最大回撤的峰值取 2022 年内的最高点；相对 WGT 的差为本配置减 WGT。',
              '- 收益、超额、回撤、占比以百分比显示，保留 1 位小数；年单边换手为比值，保留 1 位小数。', '']
    if header.get('null_counts'):
        lines.append('- 随机对照完成数（完成/登记）：' + '；'.join(f'{k} {v}' for k, v in header['null_counts'].items()))
        lines.append('')
    lines.append('| ' + ' | '.join(c[0] for c in SCORECARD_COLUMNS) + ' |')
    lines.append('|' + '---|' * len(SCORECARD_COLUMNS))
    for r in rows:
        cells = {key: (_null_cell(r) if key == 'null_cell' else r.get(key)) for _, key, _ in SCORECARD_COLUMNS}
        vals = [fn(cells[key]) if cells[key] not in (None, '') else '' for _, key, fn in SCORECARD_COLUMNS]
        lines.append('| ' + ' | '.join('' if v is None else str(v) for v in vals) + ' |')
    (out / 'scorecard.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


def registered_points(root):
    """Every configuration of every committed registration, by ID (the first registration that holds it wins)."""
    found = {}
    for path in sorted((Path(root) / EXPERIMENTS).glob('*.json')):
        for p in expand_grid(json.loads(path.read_text(encoding='utf-8'))['grid']):
            found.setdefault(p['id'], p)
    return found


def _anchor_point(root, anchor, points):
    for p in points:
        if p['id'] == anchor:
            return p
    for p in stage4_points():
        if p['id'] == anchor:
            return make_point({'kind': 'momentum', 'L': p['L'], 'skip': 0}, p['N'], p['b'], p['rebalance'])
    p = registered_points(root).get(anchor)
    if p is not None:
        return p
    raise ValueError('anchor is not a configuration of this experiment, of stage 4 or of a committed registration: ' + str(anchor))


def write_csv_rows(path, rows):
    if not rows:
        path.write_text('', encoding='utf-8')
        return
    with path.open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def _diff(a, b):
    return '' if a in (None, '') or b in (None, '') else a - b


def pair_rows(points, metrics10, metrics5, pairs=None):
    """Named paired differences (a minus b): annualised 10 bp and 5 bp excess, and the two sub-periods at 10 bp.

    pairs defaults to round 1's PAIRS; a registration may name its own pairs.
    """
    ids = {p['name']: p['id'] for p in points if p.get('name')}
    ids.update(PAIR_ALIASES)
    rows = []
    for a, b in (PAIRS if pairs is None else pairs):
        ia, ib = ids.get(a), ids.get(b)
        ma, mb = (metrics10.get(ia) if ia else None), (metrics10.get(ib) if ib else None)
        fa, fb = (metrics5.get(ia) if ia else None), (metrics5.get(ib) if ib else None)
        ok = bool(ma and mb and fa and fb)
        rows.append(dict(pair=f'{a}-{b}', a=a, b=b, a_id=ia or '', b_id=ib or '',
                         diff_10bps=(ma['annualized_excess'] - mb['annualized_excess']) if ok else '',
                         diff_5bps=(fa['annualized_excess'] - fb['annualized_excess']) if ok else '',
                         diff_period_a=(ma['high_missing_excess'] - mb['high_missing_excess']) if ok else '',
                         diff_period_b=(ma['lower_missing_excess'] - mb['lower_missing_excess']) if ok else ''))
    return rows


def write_pairs_md(scorecard_path, pairs):
    lines = ['', '## 配对表（a 减 b，年化超额之差；探索档）', '',
             '| 配对 | 10bp 差 | 5bp 差 | 时段A差(10bp) | 时段B差(10bp) |', '|---|---|---|---|---|']
    for r in pairs:
        def cell(v):
            return '' if v in ('', None) else f'{100 * v:.1f}%'
        lines.append(f"| {r['pair']} | {cell(r['diff_10bps'])} | {cell(r['diff_5bps'])} | {cell(r['diff_period_a'])} | {cell(r['diff_period_b'])} |")
    with scorecard_path.open('a', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def run_experiment(root, spec_path, workers=1, null_runs=None):
    root = Path(root)
    loaded = load_spec(root, spec_path)
    spec_id = loaded['id']
    commit = registration_commit(root, spec_path)
    verify(root)  # sealed stage 4 inputs: registry seal, normalised data hashes
    from .perf_cache import ensure_signal_tables, identity, load_cache
    n_null = loaded['null_runs'] if null_runs is None else null_runs
    base = root / paths.RUNS / 'experiments' / spec_id
    experiment = {'id': spec_id, 'registration_commit': commit, 'spec_sha256': loaded['sha256']}
    doc = identity(root, 'uniform')
    load_cache(root, doc)  # builds the cache for this code on first use, otherwise loads it
    grid = loaded['grid']
    anchors = {}
    for p in grid:
        if p.get('anchor') is not None:
            anchors[p['anchor']] = None
    ids = {p['id'] for p in grid}
    extra = []
    for p in [make_point(BASELINE_SIGNAL, BASELINE['N'], BASELINE['b'], BASELINE['rebalance'])]:
        if p['id'] not in ids:
            extra.append(p)
            ids.add(p['id'])
    for anchor in sorted(anchors):
        a = _anchor_point(root, anchor, grid + extra)
        if a['id'] not in ids:
            extra.append(a)
            ids.add(a['id'])
    # Reference configurations: run and reported, never ranked, never counted; each is a committed configuration.
    for rid in loaded['spec'].get('references', []):
        r = _anchor_point(root, rid, grid + extra)
        if r['id'] not in ids:
            extra.append(r)
            ids.add(r['id'])
    points = grid + extra
    ensure_signal_tables(root, doc, [p['signal'] for p in points])
    payload = dict(points=points, costs=COSTS, experiment=experiment)
    ctx = _experiment_ctx(root, doc, payload)
    _run_jobs(_jobs(points, base, 'explore', 'explore', 'explore', COSTS, experiment), ctx, workers, payload)
    check = check_baseline(root, base)
    excess10, complete = {}, {}
    for p in points:
        m = _metrics_or_none(_run_folder(base, p, 10, 'explore'))
        complete[p['id']] = m is not None
        excess10[p['id']] = m['annualized_excess'] if m else None
    order, means = rank_points(points, excess10, complete)
    # Every configuration gets a daily verified run at both costs: positions and decisions come from it.
    _run_jobs(_jobs(points, base, 'daily', 'lean', 'daily', COSTS, experiment), ctx, workers, payload)
    # Null controls: per configuration, random picks that make the same full exits each week as its own explore run
    # (10 bp). Each configuration has its own group; groups share no runs.
    null_points = []
    for p in points:
        if _metrics_or_none(_run_folder(base, p, 10, 'explore')) is None:
            raise ValueError('reference explore run incomplete; its null controls cannot be matched: ' + p['id'])
        exits = reference_exit_counts(_run_folder(base, p, 10, 'explore'))
        sector_k = (p.get('selection_params') or {}).get('sector_k') if p.get('selection') == 'sector_cap' else None
        for seed in range(n_null):
            params = dict(seed=seed, exit_counts=exits)
            if sector_k is not None:
                params['sector_k'] = sector_k
            null_points.append(dict(id=f'NULL_{p["id"]}_s{seed:03d}', signal=p['signal'], L=p['L'], N=p['N'], k=p['k'],
                                    b=p['b'], rebalance=p['rebalance'], selection='random_matched', selection_params=params,
                                    _group=p['id']))
    null_jobs = []
    for q in null_points:
        q2 = {k: v for k, v in q.items() if k != '_group'}
        null_jobs.append((dict(q2, experiment=experiment), 10,
                          str(base / 'null' / q['_group'] / q['id'] / 'cost_10bps'), 'explore', False, 'explore'))
    payload = dict(points=points + [{k: v for k, v in q.items() if k != '_group'} for q in null_points],
                   costs=COSTS, experiment=experiment)
    ctx = _experiment_ctx(root, doc, payload)
    _run_jobs(null_jobs, ctx, workers, payload)
    null_values, null_turnover = defaultdict(list), defaultdict(list)
    totals = Counter(q['_group'] for q in null_points)
    for q in null_points:
        m = _metrics_or_none(base / 'null' / q['_group'] / q['id'] / 'cost_10bps')
        if m is not None:
            null_values[q['_group']].append(m['annualized_excess'])
            null_turnover[q['_group']].append(m['turnover'])
    base_metrics = {p['id']: _metrics_or_none(_run_folder(base, p, 10, 'explore')) for p in points}
    base_metrics5 = {p['id']: _metrics_or_none(_run_folder(base, p, 5, 'explore')) for p in points}
    sector_map = load_sector_map(root)
    sectors = {}
    rows = []
    risks = {p['id']: risk_metrics(_run_folder(base, p, 10, 'daily'), base_metrics[p['id']]) for p in points if base_metrics[p['id']]}
    wgt = next((p for p in points if p.get('name') == REFERENCE_NAME), None)
    anchor_of = {p['id']: (p.get('anchor') or '') for p in points}
    for p in [q for q in points if q['id'] == BASELINE['id']] + [q for q in order if q['id'] != BASELINE['id']]:
        m10, m5 = base_metrics[p['id']], base_metrics5[p['id']]
        anchor_id = anchor_of[p['id']] or (BASELINE['id'] if p['id'] == BASELINE['id'] else '')
        anchor = next((q for q in points if q['id'] == anchor_id), None)
        a10 = base_metrics.get(anchor['id']) if anchor else None
        a5 = base_metrics5.get(anchor['id']) if anchor else None
        loyo = leave_one_year_out(m10['year_returns']) if m10 else {}
        loyo_values = [v for v in loyo.values() if v is not None]
        equity_by_day = {}
        if m10:
            for r in read_table(_run_folder(base, p, 10, 'daily') / 'daily_equity.csv'):
                equity_by_day[r['session_date']] = float(r['total_equity'])
            positions = position_shares(_run_folder(base, p, 10, 'daily'), {k: sector_key(k, sector_map)[0] for k in set(
                r['industry'] for r in read_table(_run_folder(base, p, 10, 'daily') / 'positions.csv'))}, equity_by_day)
        else:
            positions = {}
        manifest = json.loads((_run_folder(base, p, 10, 'explore') / 'run_manifest.json').read_text(encoding='utf-8'))
        counts = manifest.get('selection_counts', {})
        m_null = null_values.get(p['id'], [])
        point, low, high = null_band(m10['annualized_excess'] if m10 else None, m_null, totals[p['id']])
        nulls = dict(null_percentile=point, null_percentile_low=low, null_percentile_high=high,
                     null_completed=f"{len(m_null)}/{totals[p['id']]}",
                     null_turnover_median=median(null_turnover[p['id']]) if null_turnover.get(p['id']) else '')
        risk = risks.get(p['id'], {})
        wgt_risk = risks.get(wgt['id'], {}) if wgt else {}
        wgt_m = base_metrics.get(wgt['id']) if wgt else None
        rows.append(dict(
            id=p['id'], signal=describe(p['signal']), selection=p.get('selection', 'default') + (
                '' if not p.get('selection_params') else ' ' + json.dumps(p['selection_params'], ensure_ascii=False, sort_keys=True)),
            N=p['N'], b=p['b'], rebalance=p['rebalance'], anchor=anchor_id,
            annualized_return_10bps=m10['annualized_return'] if m10 else '',
            annualized_excess_10bps=m10['annualized_excess'] if m10 else '',
            annualized_return_5bps=m5['annualized_return'] if m5 else '',
            annualized_excess_5bps=m5['annualized_excess'] if m5 else '',
            paired_vs_anchor_10bps=(m10['annualized_excess'] - a10['annualized_excess']) if (m10 and a10) else '',
            paired_vs_anchor_5bps=(m5['annualized_excess'] - a5['annualized_excess']) if (m5 and a5) else '',
            paired_vs_A90_10bps=(m10['annualized_excess'] - base_metrics[A90_ID]['annualized_excess']) if (m10 and base_metrics.get(A90_ID)) else '',
            paired_vs_A90_5bps=(m5['annualized_excess'] - base_metrics5[A90_ID]['annualized_excess']) if (m5 and base_metrics5.get(A90_ID)) else '',
            paired_period_a=(m10['high_missing_excess'] - a10['high_missing_excess']) if (m10 and a10) else '',
            paired_period_b=(m10['lower_missing_excess'] - a10['lower_missing_excess']) if (m10 and a10) else '',
            leave_one_year_out_min=min(loyo_values) if loyo_values else '',
            leave_one_year_out_positive=sum(1 for v in loyo_values if v > 0) if loyo_values else '',
            max_drawdown=m10['max_drawdown'] if m10 else '', turnover_one_way=m10['turnover'] if m10 else '',
            excess_2020=(m10['years'][2020] if m10 else ''), excess_2021=(m10['years'][2021] if m10 else ''),
            excess_2022=(m10['years'][2022] if m10 else ''), excess_2023=(m10['years'][2023] if m10 else ''),
            winning_years=m10['winning_years'] if m10 else '',
            max_single_weight=positions.get('max_single_weight', ''),
            sector_over_40_share=positions.get('sector_over_40_share', ''),
            sector_over_50_share=positions.get('sector_over_50_share', ''),
            pass1_buys=counts.get('pass1_buys', ''), pass2_buys=counts.get('pass2_buys', ''),
            cap_trigger_weeks=counts.get('cap_trigger_weeks', ''), cap_deferred=counts.get('cap_deferred', ''),
            vol_annual=risk.get('vol', ''), beta=risk.get('beta', ''), tracking_error=risk.get('te', ''),
            ret_over_vol=risk.get('ret_vol', ''), ret_over_dd=risk.get('ret_dd', ''), dd_2022=risk.get('dd_2022', ''),
            paired_vs_WGT_10bps=_diff(m10 and m10['annualized_excess'], wgt_m and wgt_m['annualized_excess']),
            vol_vs_WGT=_diff(risk.get('vol'), wgt_risk.get('vol')),
            dd_vs_WGT=_diff(m10 and m10['max_drawdown'], wgt_m and wgt_m['max_drawdown']),
            ret_over_vol_vs_WGT=_diff(risk.get('ret_vol'), wgt_risk.get('ret_vol')),
            null_cell='', **nulls, daily_confirmed='yes'))
    base.mkdir(parents=True, exist_ok=True)
    by_id = {p['id']: f"{len(null_values.get(p['id'], []))}/{totals[p['id']]}" for p in points}
    header = dict(id=spec_id, attempts=registered_attempts(root), registration_commit=commit,
                  hypothesis=loaded['spec']['hypothesis'],
                  baseline_check=', '.join(f'{k} {v}' for k, v in check.items()), null_counts=by_id)
    _write_scorecard(base, rows, header)
    pairs = pair_rows(points, base_metrics, base_metrics5, loaded['spec'].get('pairs'))
    write_csv_rows(base / 'pairs.csv', pairs)
    write_pairs_md(base / 'scorecard.md', pairs)
    dump(base / 'experiment_manifest.json', dict(
        experiment=experiment, spec=loaded['spec'], attempts=header['attempts'], baseline_check=check,
        grid_points=len(grid), points=len(points), anchors_added=[p['id'] for p in extra], null_runs=n_null,
        confirm_top=loaded['confirm_top'], daily_confirmed_points=len(points),
        null_points=len(null_points), null_counts=by_id))
    return base
