"""trend_v1 runner: 23 rules x 4 pool sizes x 2 slot rules x 2 cash rates, plus the two controls.

Registered in research/trend_v1/spec.md. Run from the repository root:

    python -X utf8 research/trend_v1/run_trend.py [--only RUN_ID[,RUN_ID...]] [--out DIR] [--stocks-only]

Full run outputs go to work/trend_v1/runs/<run_id>/; the summary files go to research/trend_v1/.
"""
import argparse
import csv
import json
import math
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

import numpy as np

sys.path.insert(0, '.')
from leadtrend_bt import metrics, trend
from leadtrend_bt.runner import run
from leadtrend_bt.signal_layer import trend_config

DB = 'data/db/leadtrend_db_v1.sqlite'
OUT = 'work/trend_v1'
RESULTS = 'research/trend_v1'
NS = (5, 10, 15, 20)
SLOTS = ('equal', 'amount')
CASH = ('zero', 'dtb3')
REASONS = ('signal', 'pool', 'class_switch')


def label(ma: str, fast: int, slow: int) -> str:
    return 'always' if ma == 'always' else trend.rule_label(ma, fast, slow)


def tasks() -> list[dict]:
    out = []
    for ma, fast, slow in trend.RULES:
        for n in NS:
            for slot in SLOTS:
                for cash in CASH:
                    out.append(dict(kind='trend', ma=ma, fast=fast, slow=slow, top_n=n, slot=slot,
                                    cash_rate=cash, universe='pool'))
    for n in NS:
        for slot in SLOTS:
            for cash in CASH:
                out.append(dict(kind='c1', ma='always', fast=0, slow=0, top_n=n, slot=slot,
                                cash_rate=cash, universe='pool'))
    for ma, fast, slow in trend.RULES:
        for cash in CASH:
            out.append(dict(kind='c3', ma=ma, fast=fast, slow=slow, top_n=1, slot='equal',
                            cash_rate=cash, universe='VOO'))
    return out


def run_id_for(task: dict) -> str:
    tag = label(task['ma'], task['fast'], task['slow'])
    if task['kind'] == 'c3':
        return f"C3_{tag}_{task['cash_rate']}"
    prefix = 'C1' if task['kind'] == 'c1' else 'T'
    return f"{prefix}_{tag}_N{task['top_n']}_{task['slot']}_{task['cash_rate']}"


def config_for(task: dict) -> dict:
    return trend_config(run_id_for(task), task['ma'], task['fast'], task['slow'], task['top_n'],
                        task['slot'], task['cash_rate'], universe=task['universe'])


def trade_counts(trades: list[dict]) -> dict:
    out: dict[str, dict[str, int]] = {}
    for trade in trades:
        if trade.get('side') == 'corporate_action_liquidation':
            continue
        year = trade['date'][:4]
        bucket = out.setdefault(year, {reason: 0 for reason in REASONS})
        if trade.get('reason') in bucket:
            bucket[trade['reason']] += 1
    return out


def row_from_summary(task: dict, run_id: str, summary: dict, trades: list[dict]) -> dict:
    st = summary['strategy']
    dd = st['max_drawdown']
    row = dict(run_id=run_id, kind=task['kind'], ma=task['ma'], fast=task['fast'], slow=task['slow'],
               top_n=task['top_n'], slot=task['slot'], cash_rate=task['cash_rate'], universe=task['universe'],
               cagr=st['cagr'], excess=summary['excess_cagr'], volatility=st['volatility'],
               cagr_over_dd=(st['cagr'] / abs(dd) if dd else float('nan')),
               max_drawdown=dd, peak_date=st['peak_date'], trough_date=st['trough_date'],
               recovery_date=st['recovery_date'], longest_underwater_sessions=st['longest_underwater_sessions'],
               dd_2020=st['max_drawdown_2020'], dd_2022=st['max_drawdown_2022'],
               worst_year=st['worst_year']['year'], worst_year_return=st['worst_year']['return_'],
               annual_returns=json.dumps(st['annual_returns'], sort_keys=True),
               average_invested=summary['average_invested_ratio'], average_positions=summary['average_positions'],
               max_single_weight=summary['max_single_weight'], turnover=summary['turnover_one_way_annual'],
               trades=summary['trades'], trades_by_reason_year=json.dumps(trade_counts(trades), sort_keys=True))
    return row


def task_run(args):
    task, out_root = args
    run_id = run_id_for(task)
    cfg = config_for(task)
    out = run(cfg, out_root=out_root, db_path=DB, write=True, files=True)
    return row_from_summary(task, run_id, out['summary'], out['result']['trades'])


def write_csv(path: Path, rows: list[dict], fields: list[str] | None = None) -> None:
    if not rows:
        path.write_text('', encoding='utf8')
        return
    fields = fields or list(rows[0].keys())
    with path.open('w', encoding='utf8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, lineterminator='\n')
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, '') for k in fields})


def export_configs(path: Path) -> None:
    rows = [dict(run_id=run_id_for(t), kind=t['kind'], config=json.dumps(config_for(t), sort_keys=True))
            for t in tasks()]
    write_csv(path, rows)


def read_daily(out_root: Path, run_id: str) -> tuple[list[str], list[float], list[float]]:
    dates, equity, invested = [], [], []
    with (out_root / run_id / 'daily_equity.csv').open(encoding='utf8') as f:
        for row in csv.DictReader(f):
            dates.append(row['date'])
            eq = float(row['equity'])
            equity.append(eq)
            invested.append(float(row['positions_value']) / eq if eq else 0.0)
    return dates, equity, invested


def c2_rows(runs: list[dict], out_root: Path, rf: dict[str, float]) -> None:
    """Same average exposure, no timing: e * C1 return + (1 - e) * cash return, using the run's own cash rate."""
    by_id = {row['run_id']: row for row in runs}
    for row in runs:
        if row['kind'] != 'trend':
            continue
        c1 = by_id.get(f"C1_always_N{row['top_n']}_{row['slot']}_{row['cash_rate']}")
        if c1 is None:
            continue
        dates, eq_c1, _ = read_daily(out_root, c1['run_id'])
        _, eq_run, _ = read_daily(out_root, row['run_id'])
        e = float(row['average_invested'])
        values = [eq_run[0]]
        for i in range(1, len(dates)):
            r1 = eq_c1[i] / eq_c1[i - 1] - 1 if eq_c1[i - 1] else 0.0
            cash = rf.get(dates[i], 0.0) if row['cash_rate'] == 'dtb3' else 0.0
            values.append(values[-1] * (1 + e * r1 + (1 - e) * cash))
        dd = metrics._drawdown(dates, values)
        intra = {}
        for year in ('2020', '2022'):
            idx = [i for i, d in enumerate(dates) if d[:4] == year]
            intra[year] = metrics._drawdown([dates[i] for i in idx], [values[i] for i in idx])['max_drawdown'] if idx else float('nan')
        row.update(c2_cagr=metrics._cagr(values, len(values) - 1), c2_max_drawdown=dd['max_drawdown'],
                   c2_dd_2020=intra['2020'], c2_dd_2022=intra['2022'])


def link_controls(runs: list[dict]) -> None:
    by_id = {row['run_id']: row for row in runs}
    for row in runs:
        if row['kind'] != 'trend':
            continue
        c1 = by_id.get(f"C1_always_N{row['top_n']}_{row['slot']}_{row['cash_rate']}")
        if c1 is None:
            continue
        row.update(c1_run_id=c1['run_id'], c1_cagr=c1['cagr'], c1_excess=c1['excess'],
                   c1_max_drawdown=c1['max_drawdown'], c1_dd_2020=c1['dd_2020'], c1_dd_2022=c1['dd_2022'],
                   c1_average_invested=c1['average_invested'], c1_turnover=c1['turnover'])


def episodes(runs: list[dict], out_root: Path, bench: dict[str, float]) -> list[dict]:
    windows = (('2018Q4', '2018-09-01', '2019-03-31'), ('2020Q1', '2020-01-01', '2020-06-30'),
               ('2022', '2022-01-01', '2022-12-31'))
    dates = sorted(bench)
    series = {row['run_id']: read_daily(out_root, row['run_id']) for row in runs}
    rows = []
    for name, start, end in windows:
        idx = [i for i, d in enumerate(dates) if start <= d <= end]
        if not idx:
            continue
        peak_i = max(idx, key=lambda i: bench[dates[i]])
        after = [i for i in idx if i > peak_i]
        if not after:
            continue
        trough_i = min(after, key=lambda i: bench[dates[i]])
        peak, trough = bench[dates[peak_i]], bench[dates[trough_i]]
        recovery = next((d for d in dates[trough_i:] if bench[d] >= peak), '')
        for row in runs:
            run_dates, eq, invested = series[row['run_id']]
            pos = {d: i for i, d in enumerate(run_dates)}
            if dates[peak_i] not in pos or dates[trough_i] not in pos:
                continue
            p, t = pos[dates[peak_i]], pos[dates[trough_i]]
            r = pos.get(recovery) if recovery else None
            below = next((run_dates[i] for i in range(p, t + 1) if invested[i] < 0.5), '')
            back = ''
            if below:
                end_i = r if r is not None else len(run_dates) - 1
                back = next((run_dates[i] for i in range(pos[below], end_i + 1) if invested[i] >= 0.5), '')
            rows.append(dict(episode=name, run_id=row['run_id'], kind=row['kind'],
                             voo_peak_date=dates[peak_i], voo_trough_date=dates[trough_i],
                             voo_recovery_date=recovery, voo_drawdown=trough / peak - 1,
                             return_peak_to_trough=eq[t] / eq[p] - 1,
                             return_trough_to_recovery=(eq[r] / eq[t] - 1) if r is not None else '',
                             min_invested=min(invested[p:t + 1]), first_below_50=below, first_back_above_50=back))
    return rows


def top20_universe(db: str, signal_days: list[str]) -> dict[str, str]:
    """Every security that reached the top 20 by company amount on any signal day; largest class per company."""
    from leadtrend_db import api
    members: dict[str, str] = {}
    cache: dict[str, list[tuple[str, str, float]]] = {}
    for day in signal_days:
        pool = api.pool_at(day, db)
        version = pool['version_id']
        if version not in cache:
            amounts = api.pool_amounts(version, db)
            company = api.pool_company_amounts(version, db)
            best: dict[str, tuple[float, str]] = {}
            for sid, row in amounts.items():
                issuer = row['issuer_id']
                if issuer not in best or row['amount_cents'] > best[issuer][0]:
                    best[issuer] = (float(row['amount_cents']), sid)
            ranked = sorted(best.items(), key=lambda kv: (-company.get(kv[0], 0.0), kv[1][1]))[:20]
            cache[version] = [(sid, issuer, company.get(issuer, 0.0)) for issuer, (_, sid) in ranked]
        for sid, issuer, _ in cache[version]:
            members[sid] = issuer
    return members


def per_stock(db: str, out_dir: Path) -> None:
    """Section 10: every company that ever reached the top 20, traded on its own signal, plus forward returns."""
    from leadtrend_db import api
    days = api.trading_days('2012-12-01', '2024-06-28', db)
    signal_days = api.friday_signal_days('2014-02-28', '2024-06-28', db)
    members = top20_universe(db, signal_days)
    sids = sorted(members)
    index = {d: i for i, d in enumerate(days)}
    raw = api.tri_series(sids, days[0], '2024-06-28', db)
    series = {sid: {d: tri for d, tri, _ in rows} for sid, rows in raw.items()}
    values = trend.carry_forward(trend.matrix(series, days, sids))
    start_i = index['2014-02-28']
    sig_i = [index[d] for d in signal_days]
    spans = [index[d] for d in days[start_i:]]
    rows, forward, summary = [], [], []
    for ma, fast, slow in trend.RULES:
        long = trend.bullish(values, ma, fast, slow)
        tag = trend.rule_label(ma, fast, slow)
        better_ratio = lower_dd = 0
        differences = {1: [], 4: [], 13: []}
        pooled = {1: ([], []), 4: ([], []), 13: ([], [])}
        for j, sid in enumerate(sids):
            state = long[sig_i, j]
            daily_state = np.zeros(len(days), dtype=bool)
            for k, i in enumerate(sig_i):
                daily_state[i:] = state[k]
            daily_state[:sig_i[0]] = False
            prices = values[start_i:, j]
            held = daily_state[start_i:]
            steps = np.where(np.isfinite(prices[1:] / prices[:-1]), prices[1:] / prices[:-1], 1.0)
            equity_rule = np.concatenate([[1.0], np.cumprod(np.where(held[:-1], steps, 1.0))])
            equity_hold = np.concatenate([[1.0], np.cumprod(steps)])
            years = (len(prices) - 1) / 252
            cagr_rule = equity_rule[-1] ** (1 / years) - 1
            cagr_hold = equity_hold[-1] ** (1 / years) - 1
            dd_rule = metrics._drawdown(days[start_i:], list(equity_rule))['max_drawdown']
            dd_hold = metrics._drawdown(days[start_i:], list(equity_hold))['max_drawdown']
            ratio_rule = cagr_rule / abs(dd_rule) if dd_rule else float('nan')
            ratio_hold = cagr_hold / abs(dd_hold) if dd_hold else float('nan')
            better_ratio += ratio_rule > ratio_hold
            lower_dd += abs(dd_rule) < abs(dd_hold)
            switches = int(np.abs(np.diff(daily_state[start_i:].astype(int))).sum())
            rows.append(dict(signal=tag, ma=ma, fast=fast, slow=slow, security_id=sid, issuer=members[sid],
                             cagr_rule=cagr_rule, cagr_hold=cagr_hold, dd_rule=dd_rule, dd_hold=dd_hold,
                             ratio_rule=ratio_rule, ratio_hold=ratio_hold,
                             invested_share=float(held.mean()), switches_per_year=switches / years))
            for h in (1, 4, 13):
                horizon = h * 5
                on, off = [], []
                for k, i in enumerate(sig_i):
                    if i + horizon >= len(days) or days[i + horizon] > '2024-06-28':
                        continue
                    if not values[i, j]:
                        continue
                    ret = values[i + horizon, j] / values[i, j] - 1
                    if not np.isfinite(ret):
                        continue
                    (on if state[k] else off).append(ret)
                differences[h].append(float(np.mean(on) - np.mean(off)) if on and off else float('nan'))
                pooled[h][0].extend(on)
                pooled[h][1].extend(off)
                forward.append(dict(signal=tag, security_id=sid, horizon=h,
                                    mean_long=float(np.mean(on)) if on else float('nan'), n_long=len(on),
                                    mean_flat=float(np.mean(off)) if off else float('nan'), n_flat=len(off),
                                    difference=(float(np.mean(on)) - float(np.mean(off))) if on and off else float('nan')))
        for h in (1, 4, 13):
            clean = [v for v in differences[h] if np.isfinite(v)]
            on, off = pooled[h]
            summary.append(dict(signal=tag, ma=ma, fast=fast, slow=slow, horizon=h,
                                mean_long=float(np.mean(on)) if on else float('nan'), n_long=len(on),
                                mean_flat=float(np.mean(off)) if off else float('nan'), n_flat=len(off),
                                difference=(float(np.mean(on)) - float(np.mean(off))) if on and off else float('nan'),
                                mean_difference_across_stocks=float(np.mean(clean)) if clean else float('nan'),
                                median_difference_across_stocks=float(np.median(clean)) if clean else float('nan'),
                                share_ratio_above_hold=better_ratio / len(sids), share_dd_lower=lower_dd / len(sids)))
    write_csv(out_dir / 'per_stock.csv', rows)
    write_csv(out_dir / 'per_stock_forward.csv', forward)
    write_csv(out_dir / 'per_stock_summary.csv', summary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='')
    parser.add_argument('--out', default=OUT)
    parser.add_argument('--results', default=RESULTS)
    parser.add_argument('--stocks-only', action='store_true')
    args = parser.parse_args()
    out_root = Path(args.out) / 'runs'
    out_root.mkdir(parents=True, exist_ok=True)
    results = Path(args.results)
    results.mkdir(parents=True, exist_ok=True)
    export_configs(results / 'configs.csv')
    if not args.stocks_only:
        only = {x for x in args.only.split(',') if x}
        jobs = [(t, str(out_root)) for t in tasks() if not only or run_id_for(t) in only]
        missing = only - {run_id_for(t) for t in tasks()}
        if missing:
            raise SystemExit(f'unknown run ids: {", ".join(sorted(missing))}')
        started = time.time()
        print('jobs', len(jobs), flush=True)
        with Pool(min(10, os.cpu_count() or 4)) as pool:
            runs = pool.map(task_run, jobs, chunksize=1)
        print('runs done', len(runs), round(time.time() - started, 1), flush=True)
        from leadtrend_db import api
        rf = api.rf_daily('2014-02-28', '2024-06-28', DB)
        link_controls(runs)
        c2_rows(runs, out_root, rf)
        write_csv(results / 'runs.csv', runs)
        bench = {row[0]: row[1] for row in api.tri_series(['BENCH:VOO'], '2014-02-28', '2024-06-28', DB)['BENCH:VOO']}
        write_csv(results / 'episodes.csv', episodes(runs, out_root, bench))
        wide: dict[str, dict] = {}
        for row in runs:
            dates, equity, _ = read_daily(out_root, row['run_id'])
            for day, value in zip(dates, equity):
                wide.setdefault(day, {})[row['run_id']] = value
        ids = [row['run_id'] for row in runs]
        with (results / 'equity_daily.csv').open('w', encoding='utf8', newline='') as f:
            writer = csv.writer(f, lineterminator='\n')
            writer.writerow(['date'] + ids)
            for day in sorted(wide):
                writer.writerow([day] + [f'{wide[day][i]:.2f}' if i in wide[day] else '' for i in ids])
    per_stock(DB, results)
    print('done', flush=True)


if __name__ == '__main__':
    main()
