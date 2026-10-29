"""boll_pf_v3: the pick_v1 rules as weekly-rebalanced portfolios.

Normal weeks hold the weight top 5 (WGT5), the top 10 ordered by weight rank plus beta rank (WB5), or the top 10
(EW10); in TILT_B's concentrated state a line can switch to the ten most volatile names of the top 20 (VOL10).
Same accounting as boll_pf_v1 (`ma_pool.simulate`). Registered in research/boll_pf_v3/spec.md; development only.
"""
from __future__ import annotations

import numpy as np

from leadtrend_bt import boll_pf, ma_pool, pick

W = 52
# line: (normal rule, concentrated rule)
LINES = {'EW10': ('EW10', 'EW10'), 'TILT_B*': ('EW10', 'BETA5'), 'WGT5': ('WGT5', 'WGT5'), 'WB5': ('WB5', 'WB5'),
         'EW10_VOL': ('EW10', 'VOL10'), 'WGT5_VOL': ('WGT5', 'VOL10'), 'WB5_VOL': ('WB5', 'VOL10')}


def picks(book: dict) -> dict[str, list[list[int]]]:
    """Every rule's chosen columns for every week, with the registered fallbacks."""
    ew10 = [[column for column in columns if book['ranks'][j][column] <= 10]
            for j, columns in enumerate(book['columns'])]
    wgt5 = [ordered[:5] for ordered in pick.order(book, 'WGT', None)]
    wb = pick.order(book, 'WB', W, 10)
    vol = pick.order(book, 'VOL_HI', W, 20)
    beta = pick.order(book, 'BETA_HI', W, 10)
    return dict(EW10=ew10, WGT5=wgt5,
                WB5=[ordered[:5] if len(ordered) >= 5 else wgt5[j] for j, ordered in enumerate(wb)],
                VOL10=[ordered[:10] if len(ordered) >= 10 else ew10[j] for j, ordered in enumerate(vol)],
                BETA5=[ordered[:5] if len(ordered) >= 5 else ew10[j] for j, ordered in enumerate(beta)])


def run(db_path=None, end: str = pick.END, allow_holdout: bool = False, batch: str = 'boll_pf_v3',
        sim_start: str | None = None) -> dict:
    """Every line's metrics; `sim_start` runs the portfolio from that close while the states keep their history."""
    book = pick.load(db_path, end=end, allow_holdout=allow_holdout, batch=batch)
    market = book['market']
    state = boll_pf.concentrated(book['levels'])
    chosen = picks(book)
    size = len(market.sids)
    sims = {}
    for name, (normal, tilt) in LINES.items():
        targets = {}
        for j, day in enumerate(book['weeks']):
            columns = chosen[tilt if state[j] else normal][j]
            weights = np.zeros(size)
            weights[columns] = 1.0 / len(columns)
            targets[market.index[day]] = weights
        sims[name] = ma_pool.simulate(market, sim_start or ma_pool.START, targets, ma_pool.CAPITAL)
    sims['VOO'] = boll_pf.voo_line(market, sim_start or ma_pool.START, ma_pool.CAPITAL)
    rows = {name: ma_pool.portfolio_metrics(sim) for name, sim in sims.items()}
    for row in rows.values():
        row['excess_cagr'] = row['cagr'] - rows['VOO']['cagr']
    return dict(book=book, state=state, picks=chosen, simulations=sims, metrics=rows,
                yearly=boll_pf.yearly(rows))
