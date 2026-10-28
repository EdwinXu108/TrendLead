"""Run the walk-forward experiment over every eligible stock and cache the numbers.

    python -X utf8 research/wf_v1/run_wf.py --freq daily,weekly --cash zero,dtb3

Writes research/wf_v1/wf_<frequency>_<cash>.json. Only summary numbers, each stock's line metrics,
annual returns and selection table are stored; the daily curves stay out of the cache.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import ma_lab, ma_walk     # noqa: E402

DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite')
START, END = '2014-02-28', ma_lab.LAST_ALLOWED_END
LINES = ('wf', 'hold', 'fixed', 'ex_post')
METRICS = ('cagr', 'max_drawdown', 'cagr_over_dd', 'sharpe')


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float):
        return value if value == value and abs(value) != float('inf') else None
    return value


def job(args):
    ticker, frequency, cash = args
    try:
        out = ma_walk.walk(ticker, start=START, end=END, cash_rate=cash, frequency=frequency, db_path=DB)
    except ValueError as error:                 # an unusable ticker must not stop the batch
        return dict(ticker=ticker, error=str(error))
    if not out.get('computable'):
        return dict(ticker=ticker, error=out.get('reason', 'not computable'))
    keep = dict(ticker=ticker, security_id=out['security'], complete=out['complete'],
                evaluation_start=out['evaluation_start'], evaluation_end=out['evaluation_end'],
                evaluation_sessions=out['evaluation_sessions'],
                lines={name: out['lines'][name] for name in LINES if name in out['lines']},
                selections=out['selections'], stability=out['stability'],
                ex_post=out['ex_post'])
    return clean(keep)


def medians(stocks: list[dict]) -> dict:
    out = {}
    for name in LINES:
        out[name] = {}
        for metric in METRICS:
            values = [s['lines'][name][metric] for s in stocks
                      if name in s['lines'] and s['lines'][name].get(metric) is not None]
            out[name][metric] = median(values) if values else None
    return out


def summarise(stocks: list[dict]) -> dict:
    beats_fixed = sum(1 for s in stocks
                      if s['lines']['wf']['cagr_over_dd'] is not None
                      and s['lines']['fixed']['cagr_over_dd'] is not None
                      and s['lines']['wf']['cagr_over_dd'] > s['lines']['fixed']['cagr_over_dd'])
    beats_hold_ratio = sum(1 for s in stocks
                           if s['lines']['wf']['cagr_over_dd'] is not None
                           and s['lines']['hold']['cagr_over_dd'] is not None
                           and s['lines']['wf']['cagr_over_dd'] > s['lines']['hold']['cagr_over_dd'])
    beats_hold_cagr = sum(1 for s in stocks
                          if s['lines']['wf']['cagr'] > s['lines']['hold']['cagr'])
    gaps = [s['lines']['ex_post']['cagr_over_dd'] - s['lines']['wf']['cagr_over_dd']
            for s in stocks
            if s['lines']['ex_post'].get('cagr_over_dd') is not None
            and s['lines']['wf']['cagr_over_dd'] is not None]
    types: dict[str, int] = {}
    changes: dict[int, int] = {}
    for stock in stocks:
        for row in stock['selections']:
            types[row['type']] = types.get(row['type'], 0) + 1
        count = stock['stability']['changes']
        changes[count] = changes.get(count, 0) + 1
    return dict(stocks=len(stocks), beats_fixed=beats_fixed,
                beats_hold_ratio=beats_hold_ratio, beats_hold_cagr=beats_hold_cagr,
                median_ex_post_gap=median(gaps) if gaps else None,
                medians=medians(stocks), type_counts=types,
                change_distribution={str(k): v for k, v in sorted(changes.items())})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--freq', default='daily,weekly')
    parser.add_argument('--cash', default='zero,dtb3')
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--out', default=str(HERE))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    stocks = ma_walk.stock_list(DB)
    if args.limit:
        stocks = stocks[:args.limit]
    tickers = [s['ticker'] for s in stocks]
    names = {s['ticker']: s['name'] for s in stocks}
    print(f'{len(tickers)} stocks x {len(ma_walk.COMBOS)} combinations', flush=True)
    for frequency in args.freq.split(','):
        for cash in args.cash.split(','):
            started = time.time()
            jobs = [(t, frequency, cash) for t in tickers]
            results, skipped = [], []
            with Pool(min(10, os.cpu_count() or 4)) as pool:
                for i, item in enumerate(pool.imap_unordered(job, jobs, chunksize=1), 1):
                    if 'error' in item:
                        skipped.append(item)
                    else:
                        item['name'] = names.get(item['ticker'], '')
                        results.append(item)
                    if i % 40 == 0:
                        print(f'  {frequency} {cash} {i}/{len(jobs)} {time.time() - started:.0f}s', flush=True)
            results.sort(key=lambda r: r['ticker'])
            skipped.sort(key=lambda r: r['ticker'])
            complete = [s for s in results if s['complete']]
            payload = dict(built=time.strftime('%Y-%m-%d %H:%M'), frequency=frequency, cash=cash,
                           start=START, end=END, combos=len(ma_walk.COMBOS),
                           selection_years=list(ma_walk.SELECTION_YEARS),
                           min_history=ma_walk.MIN_HISTORY, fixed=dict(type=ma_walk.FIXED[0],
                                                                       fast=ma_walk.FIXED[1],
                                                                       slow=ma_walk.FIXED[2]),
                           grid=dict(types=list(ma_walk.TYPES), fasts=list(ma_walk.FASTS),
                                     slows=list(ma_walk.SLOWS)),
                           summary=dict(complete=summarise(complete), all=summarise(results)),
                           stocks=results, skipped=skipped)
            path = out_dir / f'wf_{frequency}_{cash}.json'
            path.write_text(json.dumps(clean(payload), ensure_ascii=False), encoding='utf8')
            elapsed = time.time() - started
            print(f'  {frequency} {cash}: {len(results)} stocks ({len(complete)} complete), '
                  f'{len(skipped)} skipped -> {path.name} ({path.stat().st_size / 1024:.0f} KB) '
                  f'{elapsed:.0f}s', flush=True)


if __name__ == '__main__':
    main()
