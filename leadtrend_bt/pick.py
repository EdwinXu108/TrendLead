"""Weekly stock-pick grid: take N names from the top K, hold H weeks, and compare the basket with VOO.

Each signal week the candidates are the representative securities of that week's pool version, ranked by the
company's fund-weight rank. A rule orders them — by weight, by beta, correlation or volatility over the last W
weeks, or by the sum of the weight and beta ranks — and the first N are bought equal weighted. The basket's
return over the next H weeks, minus VOO's over the same weeks, is one observation. Weeks are split by whether
the market itself was in a zone. Statistics only: no portfolio, no trading rules. Registered in
research/pick_v1/spec.md; development segment only.
"""
from __future__ import annotations

import itertools
from datetime import date

import numpy as np

from leadtrend_bt import boll_pf, boll_weekly, ma_pool
from leadtrend_db import api

SCOPE = 'top50_v1'
DEFAULT_DB = boll_weekly.DEFAULT_DB
START, END = ma_pool.START, ma_pool.LAST_ALLOWED_END
BENCH = ma_pool.BENCH

KS = (10, 20, 30, 50)
NS = (5, 10, 15, 20)
WS = (26, 52, 104)
HS = (13, 26, 52)
MEASURED = ('BETA_HI', 'BETA_LO', 'CORR_HI', 'CORR_LO', 'VOL_HI', 'VOL_LO', 'WB')
BASES = ('WGT',) + MEASURED
ALLS = (('ALL10', 10), ('ALL20', 20), ('ALL50', 50))
ZONE_LEVEL = 2


def configs() -> list[dict]:
    """The 259 registered rows, in configs.csv order."""
    rows = [dict(config_id=f'WGT_N{n}', rule='WGT', K=None, N=n, W=None) for n in NS]
    for base in MEASURED:
        for k, n in itertools.product(KS, NS):
            if n >= k:
                continue
            for w in WS:
                rows.append(dict(config_id=f'{base}_K{k}_N{n}_W{w}', rule=base, K=k, N=n, W=w))
    rows += [dict(config_id=name, rule=name, K=k, N=k, W=None) for name, k in ALLS]
    return rows


def _week_index(days: list[str]) -> tuple[list[str], list[int]]:
    """The last session of each ISO week and its row in the calendar."""
    weeks, rows, last = [], [], None
    for i, day in enumerate(days):
        key = date.fromisoformat(day).isocalendar()[:2]
        if key != last:
            weeks.append(day)
            rows.append(i)
            last = key
        else:
            weeks[-1] = day
            rows[-1] = i
    return weeks, rows


def measures(returns, bench_returns, k: int, window: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Beta, correlation and population volatility over the `window` weekly returns ending at week `k`."""
    size = returns.shape[1]
    beta = np.full(size, np.nan)
    corr = np.full(size, np.nan)
    vol = np.full(size, np.nan)
    if k < window:
        return beta, corr, vol
    x = np.asarray(bench_returns[k - window + 1:k + 1], dtype=float)
    if not np.all(np.isfinite(x)):
        return beta, corr, vol
    y = np.asarray(returns[k - window + 1:k + 1], dtype=float)
    ok = np.all(np.isfinite(y), axis=0)
    if not ok.any():
        return beta, corr, vol
    block = y[:, ok]
    xm = x - x.mean()
    xvar = float((xm * xm).mean())
    ym = block - block.mean(axis=0)
    if xvar > 0:
        beta[ok] = (xm[:, None] * ym).mean(axis=0) / xvar
        sx = float(np.sqrt(xvar))
        sy = np.sqrt((ym * ym).mean(axis=0))
        with np.errstate(invalid='ignore', divide='ignore'):
            corr[ok] = (xm[:, None] * ym).mean(axis=0) / (sx * sy)
    vol[ok] = np.sqrt((ym * ym).mean(axis=0))
    return beta, corr, vol


def load(db_path=None, scope: str = SCOPE, start: str = START, end: str = END) -> dict:
    """Weekly closes, candidates, the three measures for every W, and the forward returns for every H."""
    db_path = db_path or DEFAULT_DB
    market = ma_pool.load_market(db_path, start, end, scope)
    weeks_all, rows = _week_index(market.days)
    closes = market.values[rows]
    returns = np.full(closes.shape, np.nan)
    returns[1:] = closes[1:] / closes[:-1] - 1

    bench = api.tri_series([BENCH], market.days[0], end, db_path)[BENCH]
    by_day = {day: value for day, value, _ in bench}
    bench_closes, last = [], np.nan
    for day in weeks_all:
        last = by_day.get(day, last)
        bench_closes.append(last)
    bench_closes = np.asarray(bench_closes, dtype=float)
    bench_returns = np.full(bench_closes.size, np.nan)
    bench_returns[1:] = bench_closes[1:] / bench_closes[:-1] - 1

    signal = [day for day in api.friday_signal_days(start, end, db_path) if day in market.index]
    position = {day: k for k, day in enumerate(weeks_all)}
    index = [position[day] for day in signal]

    versions: dict[str, list[dict]] = {}
    cands: list[list[tuple[int, int]]] = []
    for day in signal:
        version = api.pool_at(day, db_path, scope)['version_id']
        if version not in versions:
            versions[version] = boll_pf.ranked_members(version, db_path, scope)
        i = market.index[day]
        rows_here = []
        for row in versions[version]:
            column = market.column.get(row['security_id'])
            if row['rank'] is None or column is None or not np.isfinite(market.values[i, column]):
                continue
            rows_here.append((column, int(row['rank'])))
        cands.append(sorted(rows_here, key=lambda item: item[1]))

    measure = {w: dict(beta=[], corr=[], vol=[]) for w in WS}
    for w in WS:
        for k in index:
            beta, corr, vol = measures(returns, bench_returns, k, w)
            measure[w]['beta'].append(beta)
            measure[w]['corr'].append(corr)
            measure[w]['vol'].append(vol)
        for key in ('beta', 'corr', 'vol'):
            measure[w][key] = np.vstack(measure[w][key])

    forward = {}
    for h in HS:
        block = np.full((len(signal), closes.shape[1]), np.nan)
        for j, k in enumerate(index):
            if k + h < len(weeks_all):
                block[j] = closes[k + h] / closes[k] - 1
        forward[h] = block
    voo_forward = {}
    for h in HS:
        block = np.full(len(signal), np.nan)
        for j, k in enumerate(index):
            if k + h < len(weeks_all):
                block[j] = bench_closes[k + h] / bench_closes[k] - 1
        voo_forward[h] = block

    zone = boll_weekly.market_zone(db_path, quarter_n=8, width=0.5, end=end)
    levels = [int(zone['level'].get(day, 0)) for day in signal]

    return dict(market=market, weeks=signal, cands=cands, measure=measure, forward=forward,
                voo_forward=voo_forward, levels=levels, versions=len(versions),
                columns=[[column for column, _ in rows_here] for rows_here in cands],
                ranks=[{column: rank for column, rank in rows_here} for rows_here in cands])


def order(book: dict, base: str, w: int | None, k: int | None = None) -> list[list[int]]:
    """Candidate columns in the rule's order for every week; empty when the measure is missing.

    `k` keeps only the names whose weight rank is at most K; WGT ignores it because its order is the rank itself.
    """
    out = []
    for j, columns in enumerate(book['columns']):
        ranks = book['ranks'][j]
        if k is not None and base != 'WGT':
            columns = [column for column in columns if ranks[column] <= k]
        if base == 'WGT':
            out.append(sorted(columns, key=lambda column: ranks[column]))
            continue
        if base == 'WB':
            beta = book['measure'][w]['beta'][j]
            known = [column for column in columns if np.isfinite(beta[column])]
            if len(known) < 2:
                out.append([])
                continue
            values = np.array([beta[column] for column in known])
            beta_rank = {column: int(1 + np.sum(values > beta[column])) for column in known}
            out.append(sorted(known, key=lambda column: (ranks[column] + beta_rank[column], ranks[column])))
            continue
        key, descending = base.split('_')
        field = {'BETA': 'beta', 'CORR': 'corr', 'VOL': 'vol'}[key]
        values = book['measure'][w][field][j]
        known = [column for column in columns if np.isfinite(values[column])]
        if descending == 'HI':
            out.append(sorted(known, key=lambda column: (-values[column], ranks[column])))
        else:
            out.append(sorted(known, key=lambda column: (values[column], ranks[column])))
    return out


def baskets(book: dict) -> dict:
    """{config_id: {H: excess array, 'valid': bool array}} for all 259 rows."""
    count = len(book['weeks'])
    out = {}
    cache: dict[tuple, list] = {}

    def cached(base: str, w: int | None, k: int | None) -> list:
        key = (base, w, k)
        if key not in cache:
            cache[key] = order(book, base, w, k)
        return cache[key]
    for row in configs():
        name, base, n = row['config_id'], row['rule'], row['N']
        values = {h: np.full(count, np.nan) for h in HS}
        valid = np.zeros(count, dtype=bool)
        if base in ('ALL10', 'ALL20', 'ALL50'):
            limit = row['K']
            for j, rows_here in enumerate(book['cands']):
                columns = [column for column, rank in rows_here if rank <= limit]
                if not columns:
                    continue
                valid[j] = True
                for h in HS:
                    values[h][j] = book['forward'][h][j, columns].mean()
        else:
            w = row['W']
            order_rows = cached(base, w, row['K'])
            for j, ordered in enumerate(order_rows):
                if len(ordered) < n:
                    continue
                chosen = ordered[:n]
                valid[j] = True
                for h in HS:
                    values[h][j] = book['forward'][h][j, chosen].mean()
        item = dict(valid=valid)
        for h in HS:
            item[h] = values[h] - book['voo_forward'][h]
        out[name] = item
    return out


def common_weeks(table: dict, h: int) -> np.ndarray:
    """Weeks on which every config is valid and the forward return exists, for one horizon."""
    mask = None
    for item in table.values():
        here = item['valid'] & np.isfinite(item[h])
        mask = here if mask is None else (mask & here)
    return mask


def groups(book: dict, mask: np.ndarray, state: str) -> list[dict]:
    """Episodes of zone weeks, or calendar years of normal weeks, restricted to the common weeks."""
    weeks = book['weeks']
    flags = [level >= ZONE_LEVEL for level in book['levels']]
    if state == 'zone':
        runs = boll_pf.runs([flag and keep for flag, keep in zip(flags, mask)], weeks)
        return [dict(name=f"{run['start']}~{run['end']}", weeks=run['weeks'],
                     index=[j for j, day in enumerate(weeks)
                            if mask[j] and flags[j] and run['start'] <= day <= run['end']])
                for run in runs]
    out = {}
    for j, day in enumerate(weeks):
        if mask[j] and not flags[j]:
            out.setdefault(day[:4], []).append(j)
    return [dict(name=year, weeks=len(index), index=index) for year, index in sorted(out.items())]


def summarise(values, mask, state_groups, baseline) -> dict:
    """Count, mean, median, share above zero, 10th percentile, and how many groups beat the baseline."""
    x = np.asarray([value for value, keep in zip(values, mask) if keep and np.isfinite(value)], dtype=float)
    row = dict(weeks=int(x.size))
    if not x.size:
        return dict(row, mean=np.nan, median=np.nan, win_rate=np.nan, p10=np.nan, beat=0, groups=0)
    row.update(mean=float(x.mean()), median=float(np.median(x)), win_rate=float(np.mean(x > 0)),
               p10=float(np.percentile(x, 10)))
    beat = total = 0
    for group in state_groups:
        index = [j for j in group['index'] if np.isfinite(values[j]) and np.isfinite(baseline[j])]
        if not index:
            continue
        total += 1
        if float(np.mean([values[j] for j in index])) > float(np.mean([baseline[j] for j in index])):
            beat += 1
    row.update(beat=beat, groups=total)
    return row
