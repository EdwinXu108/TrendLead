"""Signal-level diagnostics for the Top 200 pool (descriptive only, never a trading input).

Every statistic is computed from the weekly signal days: the signal uses total-return data up to and including the
signal day, and the future return runs from that close to the close of the h-th later signal day. A future window
that would end after 2024-06-28 is refused, so the reserved segment can never enter a statistic.
"""
from __future__ import annotations

import bisect
import math
from collections import defaultdict

from leadtrend_db import api

LAST_ALLOWED_END = '2024-06-28'
PERIOD_A = ('2014-02-28', '2019-11-29')
PERIOD_B = ('2019-11-29', '2024-06-28')
SIGNALS = {'MOM_5': 5, 'MOM_20': 20, 'MOM_60': 60, 'MOM_90': 90, 'MOM_126': 126, 'MOM_252': 252}
HORIZONS = (1, 4, 13)
GROUPS = (5, 10)


def assert_future_end(end: str) -> str:
    """Refuse any future-return window that ends in the reserved segment."""
    if end > LAST_ALLOWED_END:
        raise ValueError(f'future window ends {end}, after {LAST_ALLOWED_END}')
    return end


def momentum(tri: dict[str, float], day: str, base_day: str) -> float | None:
    """tri_day / tri_base - 1; None when either end is missing or non-positive."""
    a, b = tri.get(day), tri.get(base_day)
    if a is None or b is None or b <= 0:
        return None
    return a / b - 1.0


def future_return(tri: dict[str, float], day: str, end_day: str) -> float | None:
    """Total return from the close of `day` to the close of `end_day`, with the reserved-segment guard."""
    assert_future_end(end_day)
    a, b = tri.get(day), tri.get(end_day)
    if a is None or b is None or a <= 0:
        return None
    return b / a - 1.0


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = rank
        i = j + 1
    return ranks


def spearman(x: list[float], y: list[float]) -> float | None:
    """Rank correlation with average ranks for ties; None when either side has no variation."""
    if len(x) < 3 or len(x) != len(y):
        return None
    rx, ry = _ranks(x), _ranks(y)
    n = len(rx)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    if vx <= 0 or vy <= 0:
        return None
    return cov / math.sqrt(vx * vy)


def newey_west_t(series: list[float], lag: int) -> float | None:
    """t value of the mean with a Newey-West variance (Bartlett weights, `lag` autocovariances)."""
    n = len(series)
    if n < 3:
        return None
    mean = sum(series) / n
    e = [v - mean for v in series]
    var = sum(v * v for v in e) / n
    for l in range(1, min(lag, n - 1) + 1):
        cov = sum(e[i] * e[i - l] for i in range(l, n)) / n
        var += 2.0 * (1.0 - l / (lag + 1.0)) * cov
    if var <= 0:
        return None
    return mean / math.sqrt(var / n)


def _bucket(values: list[float], returns: list[float], k: int) -> list[float]:
    """Equal-count buckets after sorting by value; returns the mean return of each bucket (low to high)."""
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    n = len(order)
    out = []
    for g in range(k):
        lo, hi = g * n // k, (g + 1) * n // k
        members = [returns[order[i]] for i in range(lo, hi)]
        out.append(sum(members) / len(members) if members else float('nan'))
    return out


def _period(day: str) -> str:
    if PERIOD_A[0] <= day <= PERIOD_A[1]:
        return 'A'
    if PERIOD_B[0] <= day <= PERIOD_B[1]:
        return 'B'
    return ''


def build_panel(db_path=None) -> dict:
    """Signal days, per-day company universe (largest eligible class per issuer) and total-return series."""
    signal_days = api.friday_signal_days('2014-02-28', LAST_ALLOWED_END, db_path)
    calendar = api.trading_days('2012-12-01', LAST_ALLOWED_END, db_path)
    first = calendar.index(signal_days[0])
    lookback = calendar[max(0, first - 252)]
    universe: dict[str, dict[str, float]] = {}
    ids: set[str] = set()
    for day in signal_days:
        pool = api.pool_at(day, db_path)
        amounts = api.pool_amounts(pool['version_id'], db_path) if pool['version_id'] else {}
        best: dict[str, tuple[float, str]] = {}
        for sid, row in amounts.items():
            issuer = row['issuer_id']
            amount = float(row['amount_cents'])
            if issuer not in best or amount > best[issuer][0]:
                best[issuer] = (amount, sid)
        universe[day] = {sid: amount for amount, sid in best.values()}
        ids.update(universe[day])
    series = api.tri_series(sorted(ids), lookback, LAST_ALLOWED_END, db_path)
    tri = {sid: {d: v for d, v, _ in rows} for sid, rows in series.items()}
    return dict(signal_days=signal_days, calendar=calendar, universe=universe, tri=tri, lookback=lookback)


def build_rows(panel: dict) -> dict[str, list[dict]]:
    """Per signal day: one row per company with the signals and future returns that exist."""
    days = panel['signal_days']
    cal = panel['calendar']
    index = {d: i for i, d in enumerate(cal)}
    panel_rows: dict[str, list[dict]] = {}
    for i, day in enumerate(days):
        ci = index[day]
        rows = []
        for sid, amount in panel['universe'][day].items():
            tri = panel['tri'].get(sid, {})
            if day not in tri:
                continue
            signals = {}
            for name, L in SIGNALS.items():
                if ci >= L:
                    value = momentum(tri, day, cal[ci - L])
                    if value is not None:
                        signals[name] = value
            if ci >= 252 and ci >= 21:
                value = momentum(tri, day, cal[ci - 252])
                base = momentum(tri, day, cal[ci - 21])
                if value is not None and base is not None:
                    signals['MOM_252_21'] = (1 + value) / (1 + base) - 1
            if not signals:
                continue
            futures = {}
            for h in HORIZONS:
                if i + h < len(days):
                    value = future_return(tri, day, days[i + h])
                    if value is not None:
                        futures[h] = value
            rows.append(dict(sid=sid, amount=amount, signals=signals, futures=futures))
        panel_rows[day] = rows
    return panel_rows


def restrict_universe(panel: dict, top_m: int) -> dict:
    """A copy of the panel keeping only the top_m companies by fund amount on each signal day."""
    out = dict(panel)
    out['universe'] = {day: dict(sorted(secs.items(), key=lambda kv: (-kv[1], kv[0]))[:top_m])
                       for day, secs in panel['universe'].items()}
    return out


def size_momentum(panel: dict, signal: str = 'MOM_90', horizon: int = 13) -> dict:
    """Three size terciles x five momentum quintiles: equal-weight future return, annualised."""
    rows_by_day = build_rows(panel)
    scale = 52.0 / horizon
    sizes = ('large', 'mid', 'small')
    cells = {p: {size: [[] for _ in range(5)] for size in sizes} for p in ('all', 'A', 'B')}
    spreads = {p: {size: [] for size in sizes} for p in ('all', 'A', 'B')}
    for day, rows in rows_by_day.items():
        pairs = [(r['sid'], r['signals'][signal], r['futures'][horizon], r['amount'])
                 for r in rows if signal in r['signals'] and horizon in r['futures']]
        if len(pairs) < 30:
            continue
        pairs.sort(key=lambda p: (-p[3], p[0]))
        n = len(pairs)
        period = _period(day)
        for size, lo, hi in (('large', 0, n // 3), ('mid', n // 3, 2 * n // 3), ('small', 2 * n // 3, n)):
            part = sorted(pairs[lo:hi], key=lambda p: (p[1], p[0]))
            groups = []
            for g in range(5):
                members = part[g * len(part) // 5:(g + 1) * len(part) // 5]
                value = sum(m[2] for m in members) / len(members)
                groups.append(value)
                cells['all'][size][g].append(value)
                if period in ('A', 'B'):
                    cells[period][size][g].append(value)
            spreads['all'][size].append(groups[-1] - groups[0])
            if period in ('A', 'B'):
                spreads[period][size].append(groups[-1] - groups[0])
    out = dict(signal=signal, horizon=horizon, cells={}, spread={}, n={})
    for period in ('all', 'A', 'B'):
        out['cells'][period] = {size: [sum(v) / len(v) * scale if v else None for v in cells[period][size]]
                                for size in sizes}
        out['spread'][period] = {size: sum(v) / len(v) * scale if v else None
                                 for size, v in spreads[period].items()}
        out['n'][period] = {size: len(v) for size, v in spreads[period].items()}
    return out


def compute(panel: dict) -> dict:
    """All signal x horizon statistics. Returns plain dicts ready for JSON and the markdown report."""
    days = panel['signal_days']
    panel_rows = build_rows(panel)
    names = list(SIGNALS) + ['MOM_252_21']
    ic_series: dict[tuple[str, int], list[tuple[str, float]]] = defaultdict(list)
    q_series: dict[tuple[str, int], list[tuple[str, list[float], float]]] = defaultdict(list)
    d_series: dict[tuple[str, int], list[tuple[str, list[float], float]]] = defaultdict(list)
    size_series: dict[tuple[str, int], list[tuple[str, dict]]] = defaultdict(list)
    top_members: dict[str, list[tuple[str, set]]] = defaultdict(list)

    for day in days:
        rows = panel_rows[day]
        if not rows:
            continue
        for name in names:
            have = [r for r in rows if name in r['signals']]
            if len(have) < 10:
                continue
            ranked = sorted(have, key=lambda r: (r['signals'][name], r['sid']))
            cut = max(1, len(ranked) // 10)
            top_members[name].append((day, {r['sid'] for r in ranked[-cut:]}))
            for h in HORIZONS:
                pairs = [(r['signals'][name], r['futures'][h], r['amount'], r['sid'])
                         for r in have if h in r['futures']]
                if len(pairs) < 10:
                    continue
                sig = [p[0] for p in pairs]
                fut = [p[1] for p in pairs]
                ic = spearman(sig, fut)
                if ic is not None:
                    ic_series[(name, h)].append((day, ic))
                q = _bucket(sig, fut, 5)
                q_series[(name, h)].append((day, q, sum(fut) / len(fut)))
                d = _bucket(sig, fut, 10)
                d_series[(name, h)].append((day, d, sum(fut) / len(fut)))
                by_amount = sorted(pairs, key=lambda p: (-p[2], p[3]))
                n = len(by_amount)
                terciles = {}
                for label, lo, hi in (('large', 0, n // 3), ('mid', n // 3, 2 * n // 3),
                                      ('small', 2 * n // 3, n)):
                    part = by_amount[lo:hi]
                    if len(part) < 10:
                        continue
                    psig = [p[0] for p in part]
                    pfut = [p[1] for p in part]
                    bucket = _bucket(psig, pfut, 5)
                    terciles[label] = dict(ic=spearman(psig, pfut), spread=bucket[-1] - bucket[0])
                if terciles:
                    size_series[(name, h)].append((day, terciles))

    ic = {}
    for name in names:
        ic[name] = {}
        for h in HORIZONS:
            series = ic_series[(name, h)]
            values = [v for _, v in series]
            if not values:
                continue
            yearly = defaultdict(list)
            for day, value in series:
                yearly[day[:4]].append(value)
            by_period = {}
            for label, (lo, hi) in (('A', PERIOD_A), ('B', PERIOD_B)):
                part = [v for d, v in series if lo <= d <= hi]
                by_period[label] = (sum(part) / len(part) if part else None,
                                    newey_west_t(part, h) if part else None)
            ic[name][h] = dict(mean=sum(values) / len(values), std=_std(values), n=len(values),
                               t=newey_west_t(values, h), positive=sum(1 for v in values if v > 0) / len(values),
                               yearly={y: sum(v) / len(v) for y, v in sorted(yearly.items())},
                               A=by_period['A'][0], A_t=by_period['A'][1],
                               B=by_period['B'][0], B_t=by_period['B'][1])

    def group_block(series: list[tuple[str, list[float], float]], k: int, h: int) -> dict:
        if not series:
            return {}
        scale = 52.0 / h

        def agg(part, label):
            if not part:
                return {}
            groups = [sum(day[1][g] for day in part) / len(part) * scale for g in range(k)]
            excess = [sum(day[1][g] - day[2] for day in part) / len(part) * scale for g in range(k)]
            spreads = [day[1][-1] - day[1][0] for day in part]
            return dict(label=label, n=len(part), groups=groups, excess=excess,
                        spread=sum(spreads) / len(spreads) * scale,
                        t=newey_west_t(spreads, h),
                        mono=spearman([float(i + 1) for i in range(k)], groups))
        out = agg(series, 'all')
        for label, (lo, hi) in (('A', PERIOD_A), ('B', PERIOD_B)):
            out[label] = agg([x for x in series if lo <= x[0] <= hi], label)
        return out

    quintile = {name: {h: group_block(q_series[(name, h)], 5, h) for h in HORIZONS} for name in names}
    decile = {name: {h: group_block(d_series[(name, h)], 10, h) for h in HORIZONS} for name in names}

    size = {}
    for name in names:
        size[name] = {}
        for h in HORIZONS:
            series = size_series[(name, h)]
            block = {}
            for label, (lo, hi) in (('all', ('', '9999')), ('A', PERIOD_A), ('B', PERIOD_B)):
                part = [x for x in series if lo <= x[0] <= hi]
                item = {}
                for tercile in ('large', 'mid', 'small'):
                    vals = [x[1][tercile] for x in part if tercile in x[1]]
                    item[tercile] = dict(ic=sum(v['ic'] for v in vals) / len(vals) if vals else None,
                                         spread=sum(v['spread'] for v in vals) / len(vals) * 52.0 / h
                                         if vals else None,
                                         n=len(vals))
                block[label] = item
            size[name][h] = block

    stability = {}
    for name in names:
        series = top_members[name]
        shares = []
        for (_, prev), (_, cur) in zip(series, series[1:]):
            if prev:
                shares.append(len(prev & cur) / len(prev))
        stability[name] = dict(mean=sum(shares) / len(shares) if shares else None, n=len(shares))

    return dict(signals=names, horizons=list(HORIZONS), ic=ic, quintile=quintile, decile=decile, size=size,
                stability=stability, signal_days=len(days), securities=len(panel['tri']))


def _std(values: list[float]) -> float:
    if len(values) < 2:
        return float('nan')
    mean = sum(values) / len(values)
    return math.sqrt(sum((v - mean) ** 2 for v in values) / (len(values) - 1))


def run_study(db_path=None) -> dict:
    return compute(build_panel(db_path))
