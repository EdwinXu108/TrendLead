"""Strategy experiments: a committed spec, one command, a fixed scorecard.

A spec is research/v05/experiments/<ID>.json. It must be committed with no pending changes before it runs, and the
commit that holds it is written into every output. Only the development segment (to 2024-06-28) is ever run.
"""
import csv
import json
import math
import multiprocessing
import subprocess
from collections import Counter
from pathlib import Path
from statistics import median

from . import paths
from .perf_artifacts import read_table
from .perf_bench import Timer, dump, sha
from .signals import canonical, describe, is_default_momentum, main_L, normalize, signal_key
from .stage4_registry import digest, points as stage4_points, verify
from .stage4_summary import interval
from .perf_cache import OptimizedContext

EXPERIMENTS = 'research/v05/experiments'
BASELINE = {'id': 'L090_N08_b2_R2', 'L': 90, 'N': 8, 'k': 2, 'b': 2, 'rebalance': 'R2'}
BASELINE_SIGNAL = {'kind': 'momentum', 'L': 90, 'skip': 0}
COSTS = [5, 10]
DEV_START, DEV_END = '2019-11-29', '2024-06-28'
DEFAULT_CONFIRM_TOP = 3
DEFAULT_NULL_RUNS = 200


def make_point(signal, N, b, rebalance):
    sig = normalize(signal)
    L = main_L(sig)
    base_id = f'L{L:03d}_N{N:02d}_b{b}_{rebalance}'
    pid = base_id if is_default_momentum(sig) else f'S{signal_key(sig)}_{base_id}'
    return {'id': pid, 'signal': sig, 'L': L, 'N': N, 'k': max(1, math.floor(.3 * N + .5)), 'b': b,
            'rebalance': rebalance}


def expand_grid(blocks):
    """Each block is a product of axes; the grid is the union of blocks (duplicates collapse)."""
    if not isinstance(blocks, list) or not blocks:
        raise ValueError('grid must be a non-empty list of axis blocks')
    points = {}
    for block in blocks:
        for axis in ('signal', 'N', 'b', 'rebalance'):
            if not isinstance(block.get(axis), list) or not block[axis]:
                raise ValueError(f'grid block needs a non-empty {axis} list')
        for signal in block['signal']:
            for N in block['N']:
                for b in block['b']:
                    for R in block['rebalance']:
                        p = make_point(signal, N, b, R)
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
    confirm_top = spec.get('confirm_top', DEFAULT_CONFIRM_TOP)
    null_runs = spec.get('null_runs', DEFAULT_NULL_RUNS)
    if isinstance(confirm_top, bool) or not isinstance(confirm_top, int) or confirm_top < 1:
        raise ValueError('confirm_top must be a positive integer')
    if isinstance(null_runs, bool) or not isinstance(null_runs, int) or null_runs < 0:
        raise ValueError('null_runs must be a non-negative integer')
    grid = expand_grid(spec['grid'])
    return dict(spec=spec, path=path, id=spec['id'], grid=grid, confirm_top=confirm_top, null_runs=null_runs,
                sha256=digest(path))


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
    """A configuration is one attempt: its normalised signal, N, k, b and rebalance."""
    return (canonical(point['signal']), point['N'], point['k'], point['b'], point['rebalance'])


def registered_attempts(root):
    """Distinct configurations tried: the stage 4 grid plus every registered experiment grid, de-duplicated."""
    seen = {attempt_key(make_point({'kind': 'momentum', 'L': p['L'], 'skip': 0}, p['N'], p['b'], p['rebalance']))
            for p in stage4_points()}
    for path in sorted((Path(root) / EXPERIMENTS).glob('*.json')):
        seen.update(attempt_key(p) for p in expand_grid(json.loads(path.read_text(encoding='utf-8'))['grid']))
    return len(seen)


def _zero_L(signal):
    n = normalize(signal)
    if n['kind'] == 'momentum':
        return {'kind': 'momentum', 'L': 0, 'skip': n['skip']}
    return {'kind': 'composite', 'components': [_zero_L(c) for c in n['components']],
            'weights': n['weights'], 'combine': n['combine']}


def _rest_key(signal):
    return json.dumps(_zero_L(signal), sort_keys=True, separators=(',', ':'))


def neighbors_of(point, points):
    """Configurations one step along exactly one ordinal axis (momentum L, N, b, rebalance); the rest equal."""
    axes = ('L', 'N', 'b', 'rebalance')
    rest = _rest_key(point['signal'])
    found = {}
    for axis in axes:
        def same_rest(q):
            return _rest_key(q['signal']) == rest and all(q[a] == point[a] for a in axes if a != axis)
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


def combo_key(point, base_signal):
    return f"{point['N']}_{point['b']}_{point['rebalance']}_{signal_key(base_signal)}"


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
    years = {}
    for y in range(2020, 2024):
        x = interval(equity, benchmark, f'{y - 1}-12-31', f'{y}-12-31', first, initial)
        years[y] = x['strategy_return'] - x['benchmark_return'] if x else None
    high = interval(equity, benchmark, 'opening', '2021-12-31', first, initial)
    lower = interval(equity, benchmark, '2021-12-31', DEV_END, first, initial)
    return dict(
        annualized_return=s['annualized_return'], annualized_excess=full['annualized_excess'],
        benchmark_annualized=full['benchmark_annualized'], max_drawdown=s['drawdown']['max_drawdown'],
        turnover=s['annual_turnover_one_way_half_two_sided'],
        years=years, winning_years=sum(1 for v in years.values() if v is not None and v > 0),
        high_missing_excess=high['annualized_excess'] if high else None,
        lower_missing_excess=lower['annualized_excess'] if lower else None)


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


_WORKER = {}


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


def _null_cell(row):
    if row['null_percentile_low'] == '':
        return ''
    point = '无完成运行' if row['null_percentile'] == '' else f"{row['null_percentile']:.1f}%"
    return f"{point}（{row['null_percentile_low']:.1f}%–{row['null_percentile_high']:.1f}%）"


def _write_scorecard(out, rows, header):
    fields = list(rows[0].keys())
    with (out / 'scorecard.csv').open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    lines = ['# 成绩单：' + header['id'], '']
    lines += [f"- 累计试验次数：{header['attempts']}（按（规范化信号、N、k、b、再平衡）去重，计入第四阶段 72 个配置与 research/v05/experiments/ 下全部已登记配置）",
              f"- 登记文件提交号：{header['registration_commit']}",
              f"- 登记假设：{header['hypothesis']}",
              f"- 基准逐字节检查：{header['baseline_check']}",
              '- 开发段结果，存在幸存者偏差，使用当前行业分类；不构成策略有效性的结论。',
              '- 随机对照为换手匹配：每个确认配置一组，种子从 0 起。随机只决定持仓取舍，每周退出只数与该配置自己的探索档运行相同。',
              '- 随机对照百分位格式为“点估计（下界–上界）”：该配置 10 bp 年化超额不高于随机结果的比例。点估计只用已完成的运行；下界假设停止的运行全部高于该配置，上界假设全部低于。',
              '- 收益、超额、回撤、邻域均值以百分比显示，保留 1 位小数；年单边换手为比值，保留 1 位小数。', '']
    if header.get('null_counts'):
        lines.append('- 随机对照完成数（完成/登记）：' + '；'.join(f'{k} {v}' for k, v in header['null_counts'].items()))
        lines.append('')
    cols = ['配置', '信号', 'N', 'b', '再平衡', '10bp年化收益', '10bp年化超额', '5bp年化收益', '5bp年化超额',
            '相对基准超额差(10bp)', '邻域均值(10bp)', '最大回撤', '年单边换手(x)', '2020超额', '2021超额', '2022超额',
            '2023超额', '胜年数', '缺价较多段超额', '缺价较少段超额', '随机对照百分位', '随机完成数',
            '随机换手中位数(x)', '是否daily确认']
    lines.append('| ' + ' | '.join(cols) + ' |')
    lines.append('|' + '---|' * len(cols))
    for r in rows:
        vals = [r['id'], r['signal'], r['N'], r['b'], r['rebalance'], _pct(r['annualized_return_10bps']),
                _pct(r['annualized_excess_10bps']), _pct(r['annualized_return_5bps']), _pct(r['annualized_excess_5bps']),
                _pct(r['excess_vs_baseline_10bps']), _pct(r['neighborhood_mean_10bps']), _pct(r['max_drawdown']),
                _ratio(r['turnover_one_way']), _pct(r['excess_2020']), _pct(r['excess_2021']), _pct(r['excess_2022']),
                _pct(r['excess_2023']), r['winning_years'], _pct(r['high_missing_excess']),
                _pct(r['lower_missing_excess']), _null_cell(r), r['null_completed'],
                _ratio(r['null_turnover_median']), r['daily_confirmed']]
        lines.append('| ' + ' | '.join('' if v is None else str(v) for v in vals) + ' |')
    (out / 'scorecard.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')


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
    ensure_signal_tables(root, doc, [p['signal'] for p in loaded['grid']])
    points = loaded['grid'] + ([p for p in [make_point(BASELINE_SIGNAL, BASELINE['N'], BASELINE['b'], BASELINE['rebalance'])]
                               if p['id'] not in {q['id'] for q in loaded['grid']}])
    payload = dict(points=points, costs=COSTS, experiment=experiment)
    ctx = _experiment_ctx(root, doc, payload)
    jobs = _jobs(points, base, 'explore', 'explore', 'explore', COSTS, experiment)
    _run_jobs(jobs, ctx, workers, payload)
    check = check_baseline(root, base)
    excess10, complete = {}, {}
    for p in points:
        m = _metrics_or_none(_run_folder(base, p, 10, 'explore'))
        complete[p['id']] = m is not None
        excess10[p['id']] = m['annualized_excess'] if m else None
    order, means = rank_points(points, excess10, complete)
    confirmed = [p for p in order][:loaded['confirm_top']]
    if BASELINE['id'] not in {p['id'] for p in confirmed}:
        confirmed.append(next(p for p in points if p['id'] == BASELINE['id']))
    daily_jobs = _jobs(confirmed, base, 'daily', 'lean', 'daily', COSTS, experiment)
    _run_jobs(daily_jobs, ctx, workers, payload)
    # Null controls: per confirmed configuration, random picks that make the same full exits each week as that
    # configuration's own explore run (10 bp). Each configuration has its own group; groups share no runs.
    combos = {}
    for p in confirmed:
        combos.setdefault(combo_key(p, p['signal']), p)
    null_points = []
    for key, p in combos.items():
        if _metrics_or_none(_run_folder(base, p, 10, 'explore')) is None:
            raise ValueError('reference explore run incomplete; its null controls cannot be matched: ' + p['id'])
        exits = reference_exit_counts(_run_folder(base, p, 10, 'explore'))
        for seed in range(n_null):
            null_points.append(dict(id=f'NULL_{p["id"]}_s{seed:03d}', signal=p['signal'], L=p['L'], N=p['N'], k=p['k'],
                                    b=p['b'], rebalance=p['rebalance'], selection='random_matched',
                                    selection_params=dict(seed=seed, exit_counts=exits), _combo=key))
    null_jobs = []
    for q in null_points:
        q2 = {k: v for k, v in q.items() if k != '_combo'}
        null_jobs.append((dict(q2, experiment=experiment), 10, str(base / 'null' / q['_combo'] / q['id'] / 'cost_10bps'),
                          'explore', False, 'explore'))
    payload = dict(points=points + [{k: v for k, v in q.items() if k != '_combo'} for q in null_points],
                   costs=COSTS, experiment=experiment)
    ctx = _experiment_ctx(root, doc, payload)
    _run_jobs(null_jobs, ctx, workers, payload)
    null_values, null_turnover = {}, {}
    for q in null_points:
        m = _metrics_or_none(base / 'null' / q['_combo'] / q['id'] / 'cost_10bps')
        if m is not None:
            null_values.setdefault(q['_combo'], []).append(m['annualized_excess'])
            null_turnover.setdefault(q['_combo'], []).append(m['turnover'])
    totals = Counter(q['_combo'] for q in null_points)
    null_counts = {key: f"{len(null_values.get(key, []))}/{totals[key]}" for key in combos}
    confirmed_ids = {p['id'] for p in confirmed}
    base_metrics = _metrics_or_none(_run_folder(base, next(p for p in points if p['id'] == BASELINE['id']), 10, 'explore'))
    rows = []
    for p in [next(q for q in points if q['id'] == BASELINE['id'])] + [q for q in order if q['id'] != BASELINE['id']]:
        m10 = _metrics_or_none(_run_folder(base, p, 10, 'explore'))
        m5 = _metrics_or_none(_run_folder(base, p, 5, 'explore'))
        nulls = dict(null_percentile='', null_percentile_low='', null_percentile_high='', null_completed='',
                     null_turnover_median='')
        if p['id'] in confirmed_ids:
            key = combo_key(p, p['signal'])
            point, low, high = null_band(m10['annualized_excess'] if m10 else None, null_values.get(key, []), totals[key])
            nulls = dict(null_percentile=point, null_percentile_low=low, null_percentile_high=high,
                         null_completed=null_counts[key],
                         null_turnover_median=median(null_turnover[key]) if null_turnover.get(key) else '')
        rows.append(dict(
            id=p['id'], signal=describe(p['signal']), N=p['N'], b=p['b'], rebalance=p['rebalance'],
            annualized_return_10bps=m10['annualized_return'] if m10 else '',
            annualized_excess_10bps=m10['annualized_excess'] if m10 else '',
            annualized_return_5bps=m5['annualized_return'] if m5 else '',
            annualized_excess_5bps=m5['annualized_excess'] if m5 else '',
            excess_vs_baseline_10bps=(m10['annualized_excess'] - base_metrics['annualized_excess'])
            if (m10 and base_metrics) else '',
            neighborhood_mean_10bps=means[p['id']] if means[p['id']] != -math.inf else '',
            max_drawdown=m10['max_drawdown'] if m10 else '', turnover_one_way=m10['turnover'] if m10 else '',
            excess_2020=(m10['years'][2020] if m10 else ''), excess_2021=(m10['years'][2021] if m10 else ''),
            excess_2022=(m10['years'][2022] if m10 else ''), excess_2023=(m10['years'][2023] if m10 else ''),
            winning_years=m10['winning_years'] if m10 else '',
            high_missing_excess=m10['high_missing_excess'] if m10 else '',
            lower_missing_excess=m10['lower_missing_excess'] if m10 else '',
            **nulls, daily_confirmed='yes' if p['id'] in confirmed_ids else 'no'))
    base.mkdir(parents=True, exist_ok=True)
    by_id = {p['id']: null_counts[combo_key(p, p['signal'])] for p in confirmed}
    header = dict(id=spec_id, attempts=registered_attempts(root), registration_commit=commit,
                  hypothesis=loaded['spec']['hypothesis'],
                  baseline_check=', '.join(f'{k} {v}' for k, v in check.items()), null_counts=by_id)
    _write_scorecard(base, rows, header)
    dump(base / 'experiment_manifest.json', dict(
        experiment=experiment, spec=loaded['spec'], attempts=header['attempts'], baseline_check=check,
        confirmed=[p['id'] for p in confirmed], null_runs=n_null, null_combos=sorted(combos),
        grid_points=len(loaded['grid']), points=len(points), null_points=len(null_points), null_counts=by_id))
    return base
