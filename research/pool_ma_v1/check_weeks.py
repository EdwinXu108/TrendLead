"""Manual week checks for pool_ma_v1 and the closest engine comparison.

    python -X utf8 research/pool_ma_v1/check_weeks.py

Three Friday-to-Friday weeks are recomputed by hand from api.tri_series and compared with the module. The
last block runs the engine's signal-layer pool with N=200 as the closest available proxy for all-equal.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT))

from leadtrend_bt import engine, ma_pool, runner, signal_layer, trend      # noqa: E402
from leadtrend_db import api                                                # noqa: E402

DB = str(ROOT / 'data' / 'db' / 'leadtrend_db_v1.sqlite')
WEEKS = (('2014-03-07', '2014-03-14'), ('2019-11-29', '2019-12-06'), ('2023-12-29', '2024-01-05'))


def week_return(market, day: str, nxt: str, kind: str) -> dict:
    i, j = market.index[day], market.index[nxt]
    pool = market.pools[day]
    if kind == 'P0':
        chosen = pool
        weight = 1.0 / len(pool)
    else:
        chosen = pool[market.long[i, pool]]
        weight = 1.0 / len(chosen) if len(chosen) else 0.0
    returns = [market.values[j, k] / market.values[i, k] - 1 for k in chosen]
    return dict(K=len(chosen), M=len(pool), value=float(weight * np.sum(returns)))


def main():
    market = ma_pool.load_market(DB)
    signal_days = api.friday_signal_days(ma_pool.START, ma_pool.LAST_ALLOWED_END, DB)
    result = ma_pool.run(market, signal_days, seeds=1)
    print('week            portfolio  hand        module      diff')
    worst = 0.0
    for day, nxt in WEEKS:
        i, j = market.index[day], market.index[nxt]
        for kind, name in (('P0', 'all_equal'), ('P2', 'long_full')):
            manual = week_return(market, day, nxt, kind)
            curve = result['portfolios'][name]['equity']
            offset = market.days.index(ma_pool.START)
            module = curve[j - offset] / curve[i - offset] - 1
            diff = abs(manual['value'] - module)
            worst = max(worst, diff)
            print(f'{day}  {kind}         {manual["value"]:+.8f}  {module:+.8f}  {diff:.2e}  '
                  f'(K={manual["K"]}, M={manual["M"]})')
    print(f'largest difference {worst:.2e}')

    config = signal_layer.signal_layer_config('V_P0_proxy', ranking='size', signal='momentum', L=1,
                                              N=200, rebalance='R1W')
    proxy = runner.run(config, out_root=str(ROOT / 'work' / 'pool_ma_v1' / 'proxy'), db_path=DB, write=False)
    summary = proxy['summary']
    p0 = result['portfolios']['all_equal']
    p0_excess = p0['cagr'] - result['portfolios']['voo']['cagr']
    print('\nengine proxy (signal layer, N=200, weekly equal weight) vs all-equal')
    print(f'  proxy cagr {summary["strategy"]["cagr"]:+.4%} dd {summary["strategy"]["max_drawdown"]:+.4%} '
          f'excess {summary["excess_cagr"]:+.4%}')
    print(f'  P0    cagr {p0["cagr"]:+.4%} dd {p0["max_drawdown"]:+.4%} excess {p0_excess:+.4%}')


if __name__ == '__main__':
    main()
