"""M2 grid: top-N momentum portfolio under the signal-layer protocol.

55 lookbacks x 6 holdings x 2 equal-weight modes = 660 runs. Registered in research/mom_v1/spec_m2.md.
Run from the repository root:

    python -X utf8 research/mom_v1/run_m2.py [--only RUN_ID[,RUN_ID...]] [--out DIR] [--results FILE]
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

DB = 'data/db/leadtrend_db_v1.sqlite'
LOOKBACKS = sorted(set(range(5, 251, 5)) | {21, 63, 126, 189, 252})
NS = (5, 10, 15, 20, 25, 30)
MODES = ('R1', 'R1W')
OUT = 'work/mom_v1/grid'
RESULTS = 'work/mom_v1/grid_results.json'


def tasks() -> list[tuple[int, int, str]]:
    return [(L, N, mode) for L in LOOKBACKS for N in NS for mode in MODES]


def run_id_for(L: int, N: int, mode: str) -> str:
    return f'M2_{mode}_L{L:03d}_N{N:02d}'


def config_for(L: int, N: int, mode: str) -> dict:
    return signal_layer_config(run_id_for(L, N, mode), ranking='momentum', L=L, N=N, rebalance=mode)


def task(args):
    L, N, mode, out_root = args
    cfg = config_for(L, N, mode)
    out = run(cfg, out_root=out_root, db_path=DB, write=True, files=False)
    s = out['summary']
    return dict(run_id=cfg['run_id'], L=L, N=N, mode=mode,
                cagr=s['strategy']['cagr'], excess=s['excess_cagr'], total=s['strategy']['total_return'],
                vol=s['strategy']['volatility'], drawdown=s['strategy']['max_drawdown'],
                turnover=s['turnover_one_way_annual'], positions=s['average_positions'],
                bench=s['benchmark']['cagr'], trades=s['trades'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', default='', help='comma separated run ids')
    parser.add_argument('--out', default=OUT)
    parser.add_argument('--results', default=RESULTS)
    args = parser.parse_args()
    only = {x for x in args.only.split(',') if x}
    jobs = [(L, N, mode, args.out) for L, N, mode in tasks() if not only or run_id_for(L, N, mode) in only]
    missing = only - {run_id_for(L, N, mode) for L, N, mode in tasks()}
    if missing:
        raise SystemExit(f'unknown run ids: {", ".join(sorted(missing))}')
    started = time.time()
    print('jobs', len(jobs), flush=True)
    with Pool(min(10, os.cpu_count() or 4)) as pool:
        rows = pool.map(task, jobs, chunksize=12)
    Path(args.results).parent.mkdir(parents=True, exist_ok=True)
    Path(args.results).write_text(json.dumps(rows, indent=1), encoding='utf8')
    print('done', len(rows), round(time.time() - started, 1), flush=True)


if __name__ == '__main__':
    main()
