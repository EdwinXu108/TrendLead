"""Run the pooled moving-average experiment and write the summary, curves and per-seed table."""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import ma_pool      # noqa: E402
from leadtrend_db import api          # noqa: E402

DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v2.sqlite')
WORK = ROOT / 'work' / 'scope_top50_v1'
KEYS = ('cagr', 'max_drawdown', 'longest_underwater_sessions', 'cagr_over_dd')


def clean(value):
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=DB)
    parser.add_argument('--out', default=str(HERE))
    parser.add_argument('--work', default=str(WORK))
    args = parser.parse_args()
    started = time.time()
    market = ma_pool.load_market(args.db, scope='top50_v1')
    signal_days = api.friday_signal_days(ma_pool.START, ma_pool.LAST_ALLOWED_END, args.db)
    print(f'{len(market.days)} sessions, {len(market.sids)} securities, {len(signal_days)} signal days '
          f'{time.time() - started:.0f}s', flush=True)

    versions = [api.pool_at(day, args.db, 'top50_v1')['version_id'] for day in signal_days]
    switches = sum(1 for a, b in zip(versions, versions[1:]) if a != b)
    dropped = 0
    for k in range(1, len(signal_days)):
        if versions[k] != versions[k - 1]:
            dropped += len(set(market.pools[signal_days[k - 1]].tolist()) -
                           set(market.pools[signal_days[k]].tolist()))

    counts_m, counts_k = [], []
    for day in signal_days:
        pool = market.pools[day]
        counts_m.append(len(pool))
        counts_k.append(int(market.long[market.index[day], pool].sum()) if len(pool) else 0)
    frozen = 0
    series = api.tri_series(market.sids, ma_pool.FIRST_DAY, ma_pool.LAST_ALLOWED_END, args.db)
    last_tri = np.array([series[sid][-1][0] if series[sid] else '' for sid in market.sids])
    for day in signal_days:
        i = market.index[day]
        held = market.pools[day][market.long[i, market.pools[day]]]
        frozen += int((last_tri[held] < day).sum())

    result = ma_pool.run(market, signal_days, seeds=ma_pool.SEEDS, scope='top50_v1')
    portfolios = {name: {key: value for key, value in row.items() if key != 'equity'}
                  for name, row in result['portfolios'].items()}
    curves_equity = {name: row['equity'] for name, row in result['portfolios'].items()}
    random_rows = result['random']
    voo_cagr = portfolios['voo']['cagr']
    for row in portfolios.values():
        row['excess_vs_voo'] = row['cagr'] - voo_cagr

    for row in random_rows:
        row['worst_year_return'] = row['worst_year']['return_']
    years = sorted({year for row in random_rows for year in row['annual_returns']})
    random_annual = {year: dict(median=float(np.median([row['annual_returns'][year] for row in random_rows
                                                        if year in row['annual_returns']]))) for year in years}
    beats = {}
    for key in KEYS:
        values = [row[key] for row in random_rows if row.get(key) is not None]
        if key == 'longest_underwater_sessions':
            beats[key] = sum(1 for value in values if portfolios['long_full'][key] < value)
        else:
            beats[key] = sum(1 for value in values if portfolios['long_full'][key] > value)

    summary = dict(
        config=dict(start=ma_pool.START, end=ma_pool.LAST_ALLOWED_END, rule='sma50_200',
                    fast=ma_pool.FAST, slow=ma_pool.SLOW, capital=ma_pool.CAPITAL,
                    seeds=ma_pool.SEEDS, database=args.db, built=time.strftime('%Y-%m-%d %H:%M')),
        diagnostics=dict(
            M=dict(mean=float(np.mean(counts_m)), min=int(np.min(counts_m)), max=int(np.max(counts_m))),
            K=dict(mean=float(np.mean(counts_k)), min=int(np.min(counts_k)), max=int(np.max(counts_k))),
            k_zero_weeks=int(sum(1 for k in counts_k if k == 0)),
            missing_tri_cells=market.diagnostics['missing_tri_cells'],
            missing_tri_securities=market.diagnostics['missing_tri_securities'],
            pool_versions=market.diagnostics['pool_versions'],
            version_switches=int(switches), dropped_names=int(dropped),
            frozen_holding_periods=int(frozen),
        ),
        portfolios={name: row for name, row in portfolios.items()},
        random=dict(seeds=len(random_rows),
                    metrics={key: ma_pool.quantiles(random_rows, key)
                             for key in KEYS + ('worst_year_return',)},
                    annual=random_annual,
                    beats=beats),
        elapsed_seconds=round(time.time() - started, 1),
    )
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / 'summary.json').write_text(json.dumps(clean(summary), ensure_ascii=False, indent=1),
                                          encoding='utf8', newline='\n')

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    days = [day for day in market.days if day >= ma_pool.START]
    curves = dict(dates=days,
                  equity={name: [round(float(v), 6) for v in values] for name, values in curves_equity.items()})
    stack = np.vstack(result['random_equity'])
    curves['random'] = {name: [round(float(v), 6) for v in np.percentile(stack, q, axis=0)]
                        for name, q in (('p05', 5), ('p50', 50), ('p95', 95))}
    curves['weekly'] = [dict(date=day, K=int(k), M=int(m)) for day, k, m in zip(signal_days, counts_k, counts_m)]
    (work / 'curves.json').write_text(json.dumps(curves, ensure_ascii=False), encoding='utf8', newline='\n')

    with (work / 'random_seeds.csv').open('w', encoding='utf8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['seed', 'cagr', 'max_drawdown', 'longest_underwater_sessions', 'cagr_over_dd',
                         'total_return', 'average_exposure', 'average_holdings', 'annual_turnover'])
        for row in random_rows:
            writer.writerow([row['seed']] + [f'{row[key]:.8f}' for key in
                                             ('cagr', 'max_drawdown', 'longest_underwater_sessions',
                                              'cagr_over_dd', 'total_return', 'average_exposure',
                                              'average_holdings', 'annual_turnover')])
    print(f'done {time.time() - started:.1f}s; P2 beats {beats}', flush=True)


if __name__ == '__main__':
    main()
