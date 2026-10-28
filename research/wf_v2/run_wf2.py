"""Run the wf_v2 development segment and the frozen holdout over every eligible stock.

    python -X utf8 research/wf_v2/run_wf2.py --segment dev
    python -X utf8 research/wf_v2/run_wf2.py --segment holdout

The development run has two passes: the first collects each stock's yearly lookback ratios and picks, so
the common parameters (median across stocks) and the borrowed-parameter assignments can be built once;
the second rebuilds each stock's curves and scores the six lines. The holdout reuses the 2024-06-28 picks
and never selects again.
"""
from __future__ import annotations

import argparse
import csv
import json
import warnings
import math
import os
import sys
import time
from collections import Counter
from multiprocessing import Pool
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import ma_lab, ma_walk2     # noqa: E402

DEV_DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite')
FULL_DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_full_v1.sqlite')
START = '2014-02-28'
DEV_END = ma_lab.LAST_ALLOWED_END
HOLDOUT_END = ma_lab.HOLDOUT_END
ANCHOR = ma_walk2.HOLDOUT_ANCHOR
LINES = ma_walk2.LINES
CONTROLS = ma_walk2.CONTROLS
METRICS = ('cagr', 'max_drawdown', 'cagr_over_dd', 'sharpe')


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, np.ndarray):
        return [clean(v) for v in value.tolist()]
    if hasattr(value, 'item'):
        value = value.item()
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


# -- development ---------------------------------------------------------------------------------------------------

def phase1_job(args):
    ticker, db, end, allow = args
    try:
        walk = ma_walk2.load_walk(ticker, db, START, end, allow)
        analysis = walk.analyse()
    except ValueError as error:
        return dict(ticker=ticker, error=str(error))
    if not analysis['picks']:
        return dict(ticker=ticker, error=f'no selection day has {ma_walk2.MIN_HISTORY} sessions of history')
    return clean(dict(
        ticker=ticker, security_id=walk.sid,
        scoring_start=walk.days[walk.s_index], warmup_sessions=walk.s_index,
        evaluation_start=analysis['picks'][0]['date'],
        picks=analysis['picks'],
        picks_by_year={pick['year']: pick['combo'] for pick in analysis['picks']},
        ratios={year: item['ratio'] for year, item in analysis['ratios'].items()},
        eligible=walk.eligible,
        never_eligible=int(np.count_nonzero(~walk.eligible)),
    ))


def phase2_job(args):
    ticker, db, end, allow, b_map, c_map, mix_share = args
    try:
        walk = ma_walk2.load_walk(ticker, db, START, end, allow)
        b_assignment = {int(year): int(combo) for year, combo in b_map.items()}
        c_assignments = {int(seed): {int(year): (None if combo is None else int(combo))
                                     for year, combo in years.items()}
                         for seed, years in c_map.items()}
        out = ma_walk2.build_stock(walk, b_assignment, c_assignments, mix_share)
    except ValueError as error:
        return dict(ticker=ticker, error=str(error))
    if not out.get('computable'):
        return dict(ticker=ticker, error=out.get('reason', 'not computable'))
    out.pop('curves', None)
    out.update(ticker=ticker, security_id=walk.sid)
    return clean(out)


def dev_assignments(phase1: list[dict], common: dict) -> tuple[dict, dict]:
    b_map = {year: info['combo'] for year, info in common.items()}
    borrowed = ma_walk2.borrow_assignments(phase1)
    c_map = {}
    for stock in phase1:
        ticker = stock['ticker']
        own_years = {pick['year'] for pick in stock['picks']}
        c_map[ticker] = {seed: {year: borrowed[year][ticker][seed]
                                for year in borrowed if year in own_years}
                         for seed in range(ma_walk2.SEEDS)}
    return b_map, c_map


def pair_summary(group: list[dict], key: str) -> dict:
    out = {}
    for field in ('cagr', 'max_drawdown', 'cagr_over_dd'):
        values = [stock['diffs'][key][field] for stock in group
                  if stock['diffs'].get(key) and stock['diffs'][key].get(field) is not None]
        entry = dict(n=len(values), incomparable=len(group) - len(values),
                     median=float(np.median(values)) if values else None,
                     q25=float(np.percentile(values, 25)) if values else None,
                     q75=float(np.percentile(values, 75)) if values else None,
                     win=sum(1 for value in values if value > 1e-9),
                     loss=sum(1 for value in values if value < -1e-9),
                     tie=sum(1 for value in values if abs(value) <= 1e-9))
        out[field] = entry
    return out


def summarise(stocks: list[dict]) -> dict:
    groups = {'from2016': [s for s in stocks if s['from2016']], 'all': stocks}
    out = {}
    for name, group in groups.items():
        entry = dict(stocks=len(group), lines={}, pairs={})
        for key in LINES:
            entry['lines'][key] = {}
            for metric in METRICS:
                values = [s['lines'][key][metric] for s in group
                          if s['lines'].get(key) and s['lines'][key].get(metric) is not None]
                entry['lines'][key][metric] = float(np.median(values)) if values else None
        for key in CONTROLS:
            entry['pairs'][key] = pair_summary(group, key)
        out[name] = entry
    picks = [pick for stock in stocks for pick in stock['picks']]
    out['type_counts'] = dict(sorted(Counter(ma_walk2.COMBOS[pick['combo']][0] for pick in picks).items()))
    changes = Counter(len({pick['combo'] for pick in stock['picks']}) - 1 for stock in stocks)
    out['change_distribution'] = {str(k): v for k, v in sorted(changes.items())}
    return out


def run_dev(limit: int = 0) -> dict:
    stocks = ma_walk2.stock_list(DEV_DB)
    if limit:
        stocks = stocks[:limit]
    tickers = [stock['ticker'] for stock in stocks]
    started = time.time()
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        phase1, skipped = [], []
        for i, item in enumerate(pool.imap_unordered(phase1_job, [(t, DEV_DB, DEV_END, False) for t in tickers],
                                                     chunksize=2), 1):
            (skipped if 'error' in item else phase1).append(item)
            if i % 60 == 0:
                print(f'  dev pass 1 {i}/{len(tickers)} {time.time() - started:.0f}s', flush=True)
    phase1.sort(key=lambda item: item['ticker'])
    skipped.sort(key=lambda item: item['ticker'])
    print(f'  dev pass 1: {len(phase1)} stocks, {len(skipped)} skipped {time.time() - started:.0f}s', flush=True)

    common = ma_walk2.common_combos(phase1)
    b_map, c_map = dev_assignments(phase1, common)
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        jobs = [(stock['ticker'], DEV_DB, DEV_END, False, b_map, c_map[stock['ticker']], None)
                for stock in phase1]
        results, failed = [], []
        for i, item in enumerate(pool.imap_unordered(phase2_job, jobs, chunksize=2), 1):
            (failed if 'error' in item else results).append(item)
            if i % 60 == 0:
                print(f'  dev pass 2 {i}/{len(jobs)} {time.time() - started:.0f}s', flush=True)
    results.sort(key=lambda item: item['ticker'])
    for item in failed:
        skipped.append(item)
    payload = dict(segment='dev', built=time.strftime('%Y-%m-%d %H:%M'), start=START, end=DEV_END,
                   database=DEV_DB, combos=len(ma_walk2.COMBOS), seeds=ma_walk2.SEEDS,
                   coverage=ma_walk2.COVERAGE, warmup=ma_walk2.WARMUP,
                   grid=dict(types=list(ma_walk2.TYPES), fasts=list(ma_walk2.FASTS),
                             slows=list(ma_walk2.SLOWS)),
                   common={str(year): dict(combo=ma_walk2.COMBOS[info['combo']][0],
                                           fast=ma_walk2.COMBOS[info['combo']][1],
                                           slow=ma_walk2.COMBOS[info['combo']][2],
                                           median=info['median'], coverage=info['coverage'],
                                           stocks=info['stocks'])
                           for year, info in common.items()},
                   never_eligible=dict(
                       counts=sorted({stock['never_eligible'] for stock in phase1}),
                       combos=[dict(type=ma_walk2.COMBOS[i][0], fast=ma_walk2.COMBOS[i][1],
                                    slow=ma_walk2.COMBOS[i][2])
                               for i in range(len(ma_walk2.COMBOS))
                               if all(not stock['eligible'][i] for stock in phase1)]),
                   summary=summarise(results), stocks=results, skipped=skipped,
                   elapsed_seconds=round(time.time() - started, 1))
    return payload


# -- holdout -------------------------------------------------------------------------------------------------------

def holdout_phase1_job(args):
    """The 2024-06-28 pick, from development data only."""
    ticker, db, end, allow = args
    try:
        walk = ma_walk2.load_walk(ticker, db, START, end, allow)
        item = walk.ratios_at(anchor=ANCHOR)
    except ValueError as error:
        return dict(ticker=ticker, error=str(error))
    if item is None or not np.isfinite(item['ratio']).any():
        return dict(ticker=ticker, error=f'fewer than {ma_walk2.MIN_HISTORY} sessions from S to {ANCHOR}')
    best = int(np.nanargmax(item['ratio']))
    kind, fast, slow = ma_walk2.COMBOS[best]
    return clean(dict(ticker=ticker, security_id=walk.sid, combo=best, type=kind, fast=fast, slow=slow,
                      ratio=float(item['ratio'][best]), ratios=item['ratio'], date=item['date'],
                      eligible=walk.eligible, never_eligible=int(np.count_nonzero(~walk.eligible))))


def freeze(out_dir: Path) -> dict:
    """Pick once at the anchor on development data and write the frozen table, before the holdout runs."""
    stocks = ma_walk2.stock_list(DEV_DB)
    tickers = [stock['ticker'] for stock in stocks]
    started = time.time()
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        phase1, skipped = [], []
        for i, item in enumerate(pool.imap_unordered(holdout_phase1_job,
                                                     [(t, DEV_DB, DEV_END, False) for t in tickers],
                                                     chunksize=2), 1):
            (skipped if 'error' in item else phase1).append(item)
            if i % 60 == 0:
                print(f'  freeze {i}/{len(tickers)} {time.time() - started:.0f}s', flush=True)
    phase1.sort(key=lambda item: item['ticker'])
    skipped.sort(key=lambda item: item['ticker'])
    print(f'  freeze: {len(phase1)} stocks, {len(skipped)} skipped {time.time() - started:.0f}s', flush=True)

    eligible = np.array([item['eligible'] for item in phase1], dtype=bool)
    ratios = np.array([item['ratios'] for item in phase1], dtype=float)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', category=RuntimeWarning)
        medians = np.nanmedian(ratios, axis=0)
    coverage = eligible.sum(axis=0) / len(phase1)
    usable = (coverage >= ma_walk2.COVERAGE) & np.isfinite(medians)
    best_common = int(np.argmax(np.where(usable, medians, -np.inf)))
    kind, fast, slow = ma_walk2.COMBOS[best_common]

    stocks_for_borrow = [dict(ticker=item['ticker'], picks_by_year={ANCHOR: item['combo']},
                              eligible=item['eligible']) for item in phase1]
    per_seed = {seed: ma_walk2.borrow_year(stocks_for_borrow, ANCHOR, seed)
                for seed in range(ma_walk2.SEEDS)}
    borrow = {item['ticker']: {seed: per_seed[seed].get(item['ticker']) for seed in per_seed}
              for item in phase1}

    payload = dict(anchor=ANCHOR, built=time.strftime('%Y-%m-%d %H:%M'), database=DEV_DB,
                   common=dict(combo=best_common, type=kind, fast=fast, slow=slow,
                               median=float(medians[best_common]), coverage=float(coverage[best_common]),
                               stocks=len(phase1)),
                   stocks=[dict(ticker=item['ticker'], security_id=item['security_id'], combo=item['combo'],
                                type=item['type'], fast=item['fast'], slow=item['slow'],
                                ratio=item['ratio'], date=item['date'],
                                never_eligible=item['never_eligible']) for item in phase1],
                   borrow=borrow, skipped=skipped, elapsed_seconds=round(time.time() - started, 1))
    path = out_dir / 'frozen_2024-06-28.json'
    path.write_text(json.dumps(clean(payload), ensure_ascii=False), encoding='utf8', newline='\n')
    with (out_dir / 'frozen_2024-06-28.csv').open('w', encoding='utf8', newline='') as handle:
        writer = csv.writer(handle, lineterminator='\n')
        writer.writerow(['ticker', 'security_id', 'own_type', 'own_fast', 'own_slow', 'own_ratio',
                         'common_type', 'common_fast', 'common_slow', 'common_ratio'])
        for item in payload['stocks']:
            writer.writerow([item['ticker'], item['security_id'], item['type'], item['fast'], item['slow'],
                             f"{item['ratio']:.8f}", kind, fast, slow, f"{payload['common']['median']:.8f}"])
    print(f"  common at {ANCHOR}: {kind} {fast}/{slow} coverage {coverage[best_common]:.3f} "
          f"median {medians[best_common]:.3f}", flush=True)
    return payload


def build_holdout(walk, combo_a: int, combo_b: int, c_combos: dict[int, int | None],
                  mix_share: float) -> dict:
    state_a, sig = walk.holdout_state(combo_a)
    lines, empty = {}, {}
    lines['a'], curve_a, days = walk.line_metrics(state_a, sig)
    empty['a'] = walk.empty_for(combo_a, sig)

    state_b, _ = walk.holdout_state(combo_b)
    lines['b'], curve_b, _ = walk.line_metrics(state_b, sig)
    empty['b'] = walk.empty_for(combo_b, sig)

    seed_rows, seed_ratios, seed_curves = [], [], []
    for seed in sorted(c_combos):
        combo = c_combos[seed]
        if combo is None:
            continue
        state, _ = walk.holdout_state(combo)
        row, curve, _ = walk.line_metrics(state, sig)
        seed_rows.append(row)
        seed_ratios.append(row['cagr_over_dd'])
        seed_curves.append(curve)
    if seed_rows:
        finite = [(abs(value), i) for i, value in enumerate(seed_ratios)
                  if value is not None and math.isfinite(value)]
        representative = min(finite)[1] if finite else 0
        lines['c'] = ma_walk2.median_row(seed_rows)
        for field in ('annual', 'peak_date', 'trough_date', 'recovery_date'):
            lines['c'][field] = seed_rows[representative][field]
        curve_c = seed_curves[representative]
        dispersion = dict(seeds=len(seed_rows), min_ratio=min(seed_ratios), max_ratio=max(seed_ratios),
                          representative_seed=sorted(c_combos)[representative])
    else:
        lines['c'] = None
        dispersion = dict(seeds=0, min_ratio=None, max_ratio=None, representative_seed=None)

    state_d, _ = walk.holdout_state(ma_walk2.FIXED_INDEX)
    lines['d'], curve_d, _ = walk.line_metrics(state_d, sig)
    empty['d'] = walk.empty_for(ma_walk2.FIXED_INDEX, sig)

    state_e = np.ones(len(walk.signal_days) - sig, dtype=bool)
    lines['e'], curve_e, _ = walk.line_metrics(state_e, sig)

    lines['f'], curve_f = walk.mix_metrics(mix_share, sig)
    diffs = {key: ma_walk2.pair_diff(lines['a'], lines.get(key)) for key in CONTROLS}
    return dict(computable=True, evaluation_start=days[0], evaluation_end=days[-1],
                evaluation_sessions=len(days) - 1, lines=lines, empty=empty, diffs=diffs,
                c_dispersion=dispersion, mix_share=float(mix_share),
                splits={key: ma_walk2.split_returns(days, curve)
                        for key, curve in (('a', curve_a), ('b', curve_b), ('c', curve_c),
                                           ('d', curve_d), ('e', curve_e), ('f', curve_f))})


def holdout_phase2_job(args):
    ticker, db, end, allow, combo_a, combo_b, c_combos, mix_share = args
    try:
        walk = ma_walk2.load_walk(ticker, db, START, end, allow)
        out = build_holdout(walk, int(combo_a), int(combo_b),
                            {int(seed): (None if combo is None else int(combo))
                             for seed, combo in c_combos.items()}, float(mix_share))
    except ValueError as error:
        return dict(ticker=ticker, error=str(error))
    out.update(ticker=ticker, security_id=walk.sid)
    return clean(out)


def summarise_holdout(stocks: list[dict]) -> dict:
    entry = dict(stocks=len(stocks), lines={}, pairs={})
    for key in LINES:
        entry['lines'][key] = {}
        for metric in METRICS:
            values = [s['lines'][key][metric] for s in stocks
                      if s['lines'].get(key) and s['lines'][key].get(metric) is not None]
            entry['lines'][key][metric] = float(np.median(values)) if values else None
    for key in CONTROLS:
        entry['pairs'][key] = pair_summary(stocks, key)
    return entry


def run_holdout(frozen_path: Path, dev_path: Path) -> dict:
    frozen = json.loads(frozen_path.read_text(encoding='utf8'))
    dev = json.loads(dev_path.read_text(encoding='utf8'))
    dev_by_ticker = {stock['ticker']: stock for stock in dev['stocks']}
    stocks = [stock for stock in frozen['stocks'] if stock['ticker'] in dev_by_ticker]
    common = frozen['common']['combo']
    started = time.time()
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        jobs = []
        for stock in stocks:
            ticker = stock['ticker']
            jobs.append((ticker, FULL_DB, HOLDOUT_END, True, stock['combo'], common,
                         frozen['borrow'][ticker], dev_by_ticker[ticker]['mix_share']))
        results, failed = [], []
        for i, item in enumerate(pool.imap_unordered(holdout_phase2_job, jobs, chunksize=2), 1):
            (failed if 'error' in item else results).append(item)
            if i % 60 == 0:
                print(f'  holdout {i}/{len(jobs)} {time.time() - started:.0f}s', flush=True)
    results.sort(key=lambda item: item['ticker'])
    failed.sort(key=lambda item: item['ticker'])
    payload = dict(segment='holdout', built=time.strftime('%Y-%m-%d %H:%M'), start=START, end=HOLDOUT_END,
                   anchor=ANCHOR, database=FULL_DB, combos=len(ma_walk2.COMBOS), seeds=ma_walk2.SEEDS,
                   common=frozen['common'], summary=summarise_holdout(results), stocks=results,
                   skipped=frozen['skipped'] + failed, elapsed_seconds=round(time.time() - started, 1))
    return payload


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--segment', default='dev', choices=('dev', 'freeze', 'holdout'))
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--out', default=str(HERE))
    parser.add_argument('--dev', default=str(HERE / 'wf2_dev.json'))
    parser.add_argument('--frozen', default=str(HERE / 'frozen_2024-06-28.json'))
    args = parser.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    if args.segment == 'dev':
        payload = run_dev(args.limit)
        path = out_dir / 'wf2_dev.json'
    elif args.segment == 'freeze':
        freeze(out_dir)
        return
    else:
        payload = run_holdout(Path(args.frozen), Path(args.dev))
        path = out_dir / 'wf2_holdout.json'
    path.write_text(json.dumps(clean(payload), ensure_ascii=False), encoding='utf8', newline='\n')
    summary = payload['summary']
    print(f"{args.segment}: {len(payload['stocks'])} stocks, {len(payload['skipped'])} skipped -> {path.name} "
          f"({path.stat().st_size / 1024:.0f} KB) {payload['elapsed_seconds']}s", flush=True)
    if args.segment == 'dev':
        group = summary['from2016']
        print(f"  from2016 {group['stocks']}: A beats B in {group['pairs']['b']['cagr_over_dd']['win']}, "
              f"beats D in {group['pairs']['d']['cagr_over_dd']['win']}, "
              f"beats E in {group['pairs']['e']['cagr_over_dd']['win']}", flush=True)
    else:
        print(f"  holdout {summary['stocks']}: A beats D in {summary['pairs']['d']['cagr_over_dd']['win']}",
              flush=True)


if __name__ == '__main__':
    main()
