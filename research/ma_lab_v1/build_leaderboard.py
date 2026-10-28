"""Precompute the per-stock leaderboard: every ticker, the whole grid, the best combination per metric.

Writes research/ma_lab_v1/leaderboard_<frequency>_<cash>.json, which the dashboard serves as-is.

    python -X utf8 research/ma_lab_v1/build_leaderboard.py [--freq daily] [--cash zero,dtb3] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

from leadtrend_bt import ma_lab      # noqa: E402
from leadtrend_db import api         # noqa: E402
import dashboard                     # noqa: E402

DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite')
START, END = '2014-02-28', '2024-06-28'
TYPES = ('sma', 'ema', 'wma', 'dema', 'tema', 'hma', 'kama')
FASTS = (3, 5, 8, 10, 13, 20, 30, 50)
SLOWS = (20, 30, 50, 80, 100, 150, 200, 250, 300)
METRICS = ('total_return', 'cagr', 'cagr_over_dd', 'sharpe', 'win_rate')
# a win rate from one or two trades says nothing; the win-rate board only looks at combinations with this many
WIN_MIN_SEGMENTS = 10
FIELDS = ('cagr', 'total_return', 'max_drawdown', 'cagr_over_dd', 'sharpe', 'invested_share', 'buys',
          'average_holding_weeks', 'volatility', 'segments', 'wins', 'win_rate', 'avg_win', 'avg_loss',
          'profit_factor')


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def job(args):
    ticker, cash, frequency = args
    try:
        lab = dashboard.Lab(ticker, START, END, cash, DB, frequency)
        rows = lab.rows_with_winrate()
    except ValueError as error:          # an ambiguous or unusable ticker must not stop the build
        return dict(ticker=ticker, error=str(error))
    hold = lab.hold
    best, best_full = {}, {}
    for metric in METRICS:
        pool = rows
        if metric == 'win_rate':         # a win rate from a couple of trades says nothing
            pool = [r for r in rows if (r.get('segments') or 0) >= WIN_MIN_SEGMENTS]
        pool = [r for r in pool if r.get(metric) is not None]
        if not pool:
            continue
        def item(row):
            entry = dict(type=row['type'], fast=row['fast'], slow=row['slow'],
                         first_valid_signal_day=row.get('first_valid_signal_day'),
                         warmup_short=row.get('warmup_short'))
            entry.update({key: row.get(key) for key in FIELDS})
            return entry
        best[metric] = item(max(pool, key=lambda r: r[metric]))
        # combinations whose averages only become valid after the window starts sit in cash for the warm-up
        ready = [r for r in pool if not r.get('warmup_short')]
        if ready:
            best_full[metric] = item(max(ready, key=lambda r: r[metric]))
    return clean(dict(ticker=ticker, security_id=lab.sid,
                      hold={key: hold.get(key) for key in FIELDS},
                      best=best, best_full=best_full, combos=len(rows)))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--freq', default='daily', choices=('daily', 'weekly'))
    parser.add_argument('--cash', default='zero,dtb3')
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    # the unambiguous ticker list comes from the same query the dashboard uses
    conn = api.connect(DB)
    rows = conn.execute("""select ss.current_ticker as ticker, ss.security_id as sid
                           from strategy_securities ss where ss.current_ticker <> ''""").fetchall()
    seen: dict[str, set] = {}
    for row in rows:
        seen.setdefault(row['ticker'], set()).add(row['sid'])
    tickers = sorted(t for t, ids in seen.items() if len(ids) == 1)
    if args.limit:
        tickers = tickers[:args.limit]
    combos = len(TYPES) * len([1 for f in FASTS for s in SLOWS if f < s])
    print(f'{len(tickers)} tickers x {combos} combos x {len(args.cash.split(","))} cash rates', flush=True)
    for cash in args.cash.split(','):
        started = time.time()
        jobs = [(t, cash, args.freq) for t in tickers]
        with Pool(min(10, os.cpu_count() or 4)) as pool:
            out = []
            for i, item in enumerate(pool.imap_unordered(job, jobs, chunksize=2), 1):
                if 'error' not in item:
                    out.append(item)
                if i % 40 == 0:
                    print(f'  {cash} {i}/{len(jobs)} {time.time() - started:.0f}s', flush=True)
        out.sort(key=lambda r: r['ticker'])
        payload = dict(built=time.strftime('%Y-%m-%d %H:%M'), frequency=args.freq, cash=cash,
                       start=START, end=END, combos=combos, win_rate_min_segments=WIN_MIN_SEGMENTS,
                       grid=dict(types=list(TYPES), fasts=list(FASTS), slows=list(SLOWS)),
                       stocks=out)
        path = HERE / f'leaderboard_{args.freq}_{cash}.json'
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf8')
        print(f'  {cash}: {len(out)} stocks -> {path.name} ({path.stat().st_size / 1024:.0f} KB) '
              f'{time.time() - started:.0f}s', flush=True)


if __name__ == '__main__':
    main()
