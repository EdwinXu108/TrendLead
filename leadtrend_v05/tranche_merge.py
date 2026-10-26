"""Tranche groups as one account (plan 9.4(1)): K sub-account runs of one configuration merged into one result folder.

A tranche point runs as K sub-account runs (folders sub_0 .. sub_{K-1} under its cost folder). Their merge is written to
merged/ with the tables the rest of the pipeline reads (daily equity, benchmark, trades, positions, daily contributions,
summary, run manifest, completion). Sub-account j checks at signal weeks j, j+K, ... of the schedule that starts at the
run's first signal week. Concentration and profit are added over the sub-accounts; the group audit (plan 9.4(1)) is
run for daily groups. This module imports neither experiments nor robustness, so both may use it.
"""
import csv
import json
import shutil
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from .perf_artifacts import read_table
from .perf_bench import dump
from .stage3_metrics import longest_drawdown
from .stage4_acceptance import audit_tranche_group, voo_values
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


def _table(path):
    """Rows of one run table (plain or gzipped, or shared by the run's manifest), None when the run did not write it."""
    try:
        return read_table(path)
    except FileNotFoundError:
        return None


def _iso(value):
    return value.isoformat() if isinstance(value, date) else str(value)


def result_folder(folder, point):
    """Where a point's result is read: the merged folder of a tranche point, otherwise its single run folder."""
    K = point.get('tranche')
    return Path(folder) / 'merged' if K and K > 1 else Path(folder)


def point_jobs(point, cost, cost_folder, mode, level, experiment, start_date=None):
    """Run jobs of one point at one cost: one run, or one sub-account run per tranche index j (folder sub_j).

    A tranche point carries its K as an integer; the job carries the sub-account's {K, j}. A start date moves the whole
    schedule (sub-account j then checks at the j-th signal week from that start).
    """
    base = dict(point, experiment=experiment)
    if start_date is not None:
        base['start_date'] = start_date
    K = point.get('tranche')
    if not K:
        return [(base, cost, str(cost_folder), mode, False, level)]
    jobs = []
    for j in range(K):
        jobs.append((dict(base, tranche=dict(K=K, j=j)), cost, str(Path(cost_folder) / f'sub_{j}'), mode, False, level))
    return jobs


def _completed(folder):
    path = Path(folder) / 'completion.json'
    return path.exists() and json.loads(path.read_text(encoding='utf-8')).get('status') == 'completed'


def _merge_positions(subs):
    tables = [_table(s / 'positions.csv') for s in subs]
    if any(t is None for t in tables):
        return None
    acc = {}
    for t in tables:
        for r in t:
            key = (r['session_date'], r['security_id'], r.get('asset_type', ''))
            if key not in acc:
                acc[key] = dict(r, shares=float(r['shares']), value=float(r['value']))
            else:
                acc[key]['shares'] += float(r['shares'])
                acc[key]['value'] += float(r['value'])
    return [acc[k] for k in sorted(acc)]


def _merge_contributions(subs):
    tables = [_table(s / 'daily_contributions.csv') for s in subs]
    if any(t is None for t in tables):
        return None
    acc = defaultdict(float)
    for t in tables:
        for r in t:
            acc[(r['session_date'], r['security_id'])] += float(r['profit_dollars'])
    return [dict(session_date=d, security_id=s, profit_dollars=v) for (d, s), v in sorted(acc.items())]


def _merge_manifest(subs, K, sub_names):
    docs = [json.loads((s / 'run_manifest.json').read_text(encoding='utf-8')) for s in subs]
    doc = dict(docs[0])
    doc['tranche'] = dict(K=K, merged_from=list(sub_names), initial_capital=INITIAL)
    doc['initial_capital_account'] = INITIAL
    if any('selection_counts' in d for d in docs):
        counts = Counter()
        for d in docs:
            for k, v in (d.get('selection_counts') or {}).items():
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    counts[k] += v
        doc['selection_counts'] = dict(counts)
    return doc


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
    if merged.exists():
        shutil.rmtree(merged)
    merged.mkdir(parents=True, exist_ok=True)
    _write(merged / 'daily_equity.csv', daily)
    _write(merged / 'benchmark.csv', bench)
    _write(merged / 'trades.csv', trades)
    _write(merged / 'weekly_checks.csv', checks)
    positions = _merge_positions(subs)
    if positions is not None:
        _write(merged / 'positions.csv', positions)
    contributions = _merge_contributions(subs)
    if contributions is not None:
        _write(merged / 'daily_contributions.csv', contributions)
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
    if any('single_stock_cap_trims' in x for x in sub_summaries):
        summary['single_stock_cap_trims'] = sum(int(x.get('single_stock_cap_trims', 0)) for x in sub_summaries)
    dump(merged / 'summary.json', summary)
    dump(merged / 'run_manifest.json', _merge_manifest(subs, K, sub_names))
    dump(group / 'group_manifest.json', dict(K=K, sub_folders=list(sub_names), signal_weeks=[_iso(w) for w in signal_weeks],
                                             first_trade=first, merged_folder='merged',
                                             sub_annualized_return=[x['annualized_return'] for x in sub_summaries]))
    return merged


def merge_folder(group, K, weeks, root, end, level):
    """Merge one tranche point's sub-accounts at one cost, if all of them completed; the merged folder carries completion.

    Daily groups also get the group audit (audit_tranche_group); a failed audit is written as a failed completion, so the
    group reads as incomplete. A group with an incomplete sub-account has no merged folder. Returns the merged folder or None.
    """
    group = Path(group)
    names = [f'sub_{j}' for j in range(K)]
    if not all(_completed(group / n) for n in names):
        shutil.rmtree(group / 'merged', ignore_errors=True)
        return None
    merge_group(group, names, [_iso(w) for w in weeks], K, root, _iso(end))
    status, acceptance = 'completed', {'status': 'NOT_RUN'}
    if level == 'daily':
        result = audit_tranche_group(group, root=root, end=_iso(end))
        dump(group / 'group_acceptance.json', result)
        status = 'completed' if result['status'] == 'PASS' else 'failed'
        acceptance = {'status': result['status']}
    explore = {'status': 'PASS' if status == 'completed' else 'FAIL'} if level == 'explore' else None
    dump(group / 'merged' / 'completion.json', dict(status=status, verification_level=level, acceptance=acceptance,
                                                    explore_check=explore, tranche_K=K))
    return group / 'merged'


def merge_points(points, folder_of, costs, weeks, root, end, level):
    """Merge every tranche point at each cost. folder_of(point, cost) gives the cost folder of the point's runs."""
    for p in points:
        K = p.get('tranche')
        if not K or K < 2:
            continue
        for cost in costs:
            merge_folder(folder_of(p, cost), K, weeks, root, end, level)
