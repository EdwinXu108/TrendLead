"""M3 battery: 13 signals x 6 holdings under the signal-layer protocol.

Registered in research/mom_v1/spec_m3.md. Zero cost, equal weight, close fills, b=1, K=1, one segment.
Run from the repository root:

    python -X utf8 research/mom_v1/run_m3.py [--only RUN_ID[,RUN_ID...]] [--out DIR] [--results FILE]
"""
import argparse
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

sys.path.insert(0, '.')
from leadtrend_bt.runner import run
from leadtrend_bt.signal_layer import signal_layer_config
from leadtrend_bt.signals import SIGNALS

DB = 'data/db/leadtrend_db_v1.sqlite'
NS = (5, 10, 15, 20, 25, 30)
OUT = 'work/mom_v1/m3'
RESULTS = 'work/mom_v1/m3_results.json'


def tasks() -> list[tuple[str, int]]:
    return [(name, N) for name in SIGNALS for N in NS]


def config_for(signal: str, N: int) -> dict:
    return signal_layer_config(f'M3_{signal}_N{N:02d}', ranking='signal', signal=signal, L=90, N=N)


def task(args):
    signal, N, out_root = args
    cfg = config_for(signal, N)
    out = run(cfg, out_root=out_root, db_path=DB, write=True, files=False)
    s = out['summary']
    return dict(run_id=cfg['run_id'], signal=signal, N=N, cagr=s['strategy']['cagr'], excess=s['excess_cagr'],
                total=s['strategy']['total_return'], vol=s['strategy']['volatility'],
                drawdown=s['strategy']['max_drawdown'], turnover=s['turnover_one_way_annual'],
                positions=s['average_positions'], bench=s['benchmark']['cagr'], trades=s['trades'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='', help='comma separated run ids')
    parser.add_argument('--out', default=OUT)
    parser.add_argument('--results', default=RESULTS)
    args = parser.parse_args()
    only = {x for x in args.only.split(',') if x}
    jobs = [(name, N, args.out) for name, N in tasks() if not only or f'M3_{name}_N{N:02d}' in only]
    missing = only - {f'M3_{name}_N{N:02d}' for name, N in tasks()}
    if missing:
        raise SystemExit(f'unknown run ids: {", ".join(sorted(missing))}')
    started = time.time()
    print('jobs', len(jobs), flush=True)
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        rows = pool.map(task, jobs, chunksize=3)
    Path(args.results).parent.mkdir(parents=True, exist_ok=True)
    Path(args.results).write_text(json.dumps(rows, indent=1), encoding='utf8')
    print('done', len(rows), round(time.time() - started, 1), flush=True)


if __name__ == '__main__':
    main()
