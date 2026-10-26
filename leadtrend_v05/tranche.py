"""Tranche sub-accounts and the single-stock cap as runs (plan 9.4), on committed configurations only.

A tranche group of K sub-accounts is K runs of the same configuration. Sub-account j (0 <= j < K) starts with 1/K of the
account, holds cash until its first signal week (the j-th), then checks every K signal weeks. Each sub-account
trades, pays fees and reports on its own; the group merges them into one account report and keeps every sub-account's
own folder. K = 1 is the ordinary run and must reproduce the reviewed engine byte for byte. The single-stock cap is a
run option (cap in (0, 1]); cap = 1 never triggers and must reproduce the reviewed engine byte for byte.

Usage (the gates use these; no registration, no attempt):
    python -X utf8 -m leadtrend_v05.tranche run --ids ID --K 1 --costs 10 --out work/runs/tranche/gate_K1
    python -X utf8 -m leadtrend_v05.tranche run --ids ID --cap 1.0 --costs 10 --out work/runs/tranche/gate_cap1
"""
import argparse
import csv
import json
from datetime import date
from pathlib import Path

from . import paths
from .perf_artifacts import read_table
from .perf_bench import dump
from .perf_cache import ensure_signal_tables, identity
from .perf_runner import safe_output
from .robustness import COSTS, EXPERIMENT_ID, _context, _run_jobs, configurations
from .stage3_metrics import longest_drawdown
from .stage4_acceptance import voo_values
from .stage4_summary import interval

INITIAL = 100000.0
MERGE_FIELDS_SKIP = {'session_date'}


def _write(path, rows):
    rows = list(rows)
    fields = list(dict.fromkeys(k for r in rows for k in r))
    with Path(path).open('w', encoding='utf-8', newline='') as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def merge_group(group, sub_names, signal_weeks, K, root, end):
    """Merge the sub-account folders of one group into group/merged and write group/group_manifest.json."""
    group = Path(group)
    subs = [group / s for s in sub_names]
    tables = [read_table(s / 'daily_equity.csv') for s in subs]
    sessions = [r['session_date'] for r in tables[0]]
    for t in tables[1:]:
        if [r['session_date'] for r in t] != sessions:
            raise ValueError('sub-account sessions differ; the group cannot be merged')
    common = set(tables[0][0])
    for t in tables[1:]:
        common &= set(t[0])
    numeric = sorted(k for k in common if k not in MERGE_FIELDS_SKIP)
    daily = []
    for i, d in enumerate(sessions):
        row = {'session_date': d}
        for k in numeric:
            row[k] = sum(float(t[i][k]) for t in tables)
        daily.append(row)
    trades, checks = [], []
    for j, s in enumerate(subs):
        for r in read_table(s / 'trades.csv'):
            trades.append(dict(r, account=j))
        for r in read_table(s / 'weekly_checks.csv'):
            checks.append(dict(r, account=j))
    trades.sort(key=lambda r: (r['session_date'], r['account'], r['security_id']))
    first = min(r['session_date'] for r in trades)
    days = [r['session_date'] for r in daily if r['session_date'] >= first and r['session_date'] <= end]
    values = voo_values(root, days, first, INITIAL)
    bench = [dict(session_date=d, benchmark_value=v) for d, v in zip(days, values)]
    merged = group / 'merged'
    merged.mkdir(parents=True, exist_ok=True)
    _write(merged / 'daily_equity.csv', daily)
    _write(merged / 'benchmark.csv', bench)
    _write(merged / 'trades.csv', trades)
    _write(merged / 'weekly_checks.csv', checks)
    equity_rows = [dict(session_date=r['session_date'], total_equity=r['total_equity']) for r in daily]
    full = interval(equity_rows, bench, 'opening', end, first, INITIAL)
    values_from_first = [float(r['total_equity']) for r in daily if r['session_date'] >= first]
    dates_from_first = [r['session_date'] for r in daily if r['session_date'] >= first]
    drawdown = longest_drawdown([INITIAL] + values_from_first,
                                [date.fromisoformat(d) for d in [first] + dates_from_first])
    sub_summaries = [json.loads((s / 'summary.json').read_text(encoding='utf-8')) for s in subs]
    turnover = sum(float(x['annual_turnover_one_way_half_two_sided']) for x in sub_summaries) / K
    summary = dict(status='completed', scope='development_only', tranche_K=K, comparison_start_session=first,
                   annualized_return=full['strategy_annualized'], drawdown=drawdown,
                   annual_turnover_one_way_half_two_sided=turnover,
                   n_trades=sum(int(x['n_trades']) for x in sub_summaries),
                   fees_total=sum(float(x['fees_total']) for x in sub_summaries),
                   note='merged account: K sub-accounts with 1/K capital each; turnover is the mean of the sub-accounts')
    dump(merged / 'summary.json', summary)
    dump(group / 'group_manifest.json', dict(K=K, sub_folders=list(sub_names), signal_weeks=list(signal_weeks),
                                             first_trade=first, merged_folder='merged',
                                             sub_annualized_return=[x['annualized_return'] for x in sub_summaries]))
    return merged


def run_variant(root, base, ids, costs=COSTS, *, K=None, cap=None, workers=1):
    """Runs of the registered configurations ids at the given costs.

    K None and cap None: the ordinary run (explore). K > 1: K sub-account runs per configuration, merged. cap: the
    single-stock cap run. Outputs go under base/<label>/<configuration>/ only.
    """
    root, base = Path(root), Path(base)
    doc = identity(root, 'uniform')
    points = configurations(root, ids)
    ensure_signal_tables(root, doc, [p['signal'] for p in points])
    label = (f'K{K}' if K else 'plain') + ('' if cap is None else f'_cap{cap}')
    tranche_list = [None] if not K or K == 1 else list(range(K))
    payload = dict(points=points, costs=list(COSTS), experiment=dict(id=EXPERIMENT_ID, kind='tranche_or_cap', label=label))
    ctx = _context(root, doc, payload)
    weeks = ctx.signal_weeks()
    jobs, layout = [], {}
    for p in points:
        for cost in costs:
            if K and K > 1:
                group = base / label / p['id'] / f'cost_{cost}bps'
                names = []
                for j in tranche_list:
                    job = dict(p, tranche=dict(K=K, j=j), experiment=dict(id=EXPERIMENT_ID, kind='tranche', K=K, j=j))
                    if cap is not None:
                        job['cap'] = cap
                    folder = group / f'sub_{j}'
                    names.append(folder.name)
                    jobs.append((job, cost, str(folder), 'explore', False, 'explore'))
                layout[(p['id'], cost)] = (group, names)
            else:
                job = dict(p, experiment=dict(id=EXPERIMENT_ID, kind='cap' if cap is not None else 'plain', label=label))
                if K == 1:
                    job['tranche'] = dict(K=1, j=0)
                if cap is not None:
                    job['cap'] = cap
                folder = base / label / p['id'] / f'cost_{cost}bps'
                jobs.append((job, cost, str(folder), 'explore', False, 'explore'))
    executed = _run_jobs(jobs, ctx, workers, payload, None)
    merged = []
    for (pid, cost), (group, names) in layout.items():
        merge_group(group, names, weeks, K, root, ctx.segment['end'])
        merged.append(str(group / 'merged'))
    return dict(label=label, weeks=len(weeks), executed=executed, merged=merged)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Tranche sub-accounts and the single-stock cap (runs only)')
    sub = ap.add_subparsers(dest='mode', required=True)
    run = sub.add_parser('run')
    run.add_argument('--ids', required=True)
    run.add_argument('--K', type=int, default=None)
    run.add_argument('--cap', type=float, default=None)
    run.add_argument('--costs', default='10')
    run.add_argument('--out', required=True)
    run.add_argument('--workers', default='1')
    args = ap.parse_args(argv)
    root = Path('.').resolve()
    base = safe_output(root, args.out)
    workers = 1 if args.workers == '1' else int(args.workers)
    costs = tuple(int(x) for x in args.costs.split(','))
    result = run_variant(root, base, args.ids.split(','), costs, K=args.K, cap=args.cap, workers=workers)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
