"""The first portfolio: the ten heaviest names in the pool, tilted into high-beta names while the market is in a zone.

Every Friday, using only data up to that close:

    EW50    every representative in the pool that week, 1/M
    EW10    the ten companies ranked 1-10 in that week's pool version, 1/10
    CW10    the same ten, weighted by their company amount
    TILT_A  while the market is in a zone (level 2 or above), the high-beta half of the ten at 1/5
    TILT_B  the same tilt, but a concentrated state runs 52 weeks from the start of each market episode
    TILT_C  (boll_pf_v2) as TILT_B, but each episode only arms the tilt; it starts, and its 52 weeks count, from the
            first armed week in which VOO's weekly mid line allows a buy (not falling, or a prediction point)

The high-beta half is the names whose 52-week beta to VOO is strictly above the median of the ten. Between
signal days the share counts are fixed and the weights drift with each security's total return, exactly as in
`ma_pool.simulate`. Registered in research/boll_pf_v1/spec.md; development segment only.
"""
from __future__ import annotations

import numpy as np

from leadtrend_bt import boll_weekly, engine, ma_pool
from leadtrend_db import api

SCOPE = 'top50_v1'
DEFAULT_DB = boll_weekly.DEFAULT_DB
START, END = ma_pool.START, ma_pool.LAST_ALLOWED_END
BENCH = ma_pool.BENCH
CAPITAL = ma_pool.CAPITAL
TOP = 10
BETA_WEEKS = 52
HOLD_WEEKS = 52
ZONE_LEVEL = 2
LINES = ('VOO', 'EW50', 'EW10', 'CW10', 'TILT_A', 'TILT_B')
TILTS = ('TILT_A', 'TILT_B')
LINES_V2 = LINES + ('TILT_C',)
TILTS_V2 = TILTS + ('TILT_C',)


# -- weekly inputs ------------------------------------------------------------------------------------------------

def ranked_members(version_id: str, db_path=None, scope: str = SCOPE) -> list[dict]:
    """One representative security per company, with the company's amount and its rank in the scope."""
    conn = api.connect(db_path) if db_path else api.connect()
    ranks = {row[0]: row[1] for row in conn.execute(
        'select issuer_id, historical_rank from scope_members where version_id=? and scope=?', (version_id, scope))}
    reps = engine.Backtest._representatives(api.pool_amounts(version_id, db_path, scope),
                                            api.pool_company_amounts(version_id, db_path, scope))
    return [dict(security_id=row['security_id'], issuer_id=row['issuer_id'],
                 amount=float(row['company_amount_cents']), rank=ranks.get(row['issuer_id']))
            for row in reps]


def beta(returns, bench_returns, k: int, window: int = BETA_WEEKS) -> float:
    """Covariance over variance of the last `window` weekly returns ending at week `k`; NaN if short or gappy."""
    if k < window:
        return float('nan')
    y = np.asarray(returns[k - window + 1:k + 1], dtype=float)
    x = np.asarray(bench_returns[k - window + 1:k + 1], dtype=float)
    if not (np.all(np.isfinite(y)) and np.all(np.isfinite(x))):
        return float('nan')
    xm, ym = x - x.mean(), y - y.mean()
    var = float((xm * xm).mean())
    return float((xm * ym).mean() / var) if var > 0 else float('nan')


def high_beta_half(betas: dict) -> list[int]:
    """Columns whose beta is strictly above the median of the known betas; empty when fewer than two are known."""
    known = {column: float(value) for column, value in betas.items()
             if value is not None and np.isfinite(value)}
    if len(known) < 2:
        return []
    median = float(np.median(list(known.values())))
    return sorted(column for column, value in known.items() if value > median)


def concentrated(levels, window: int = HOLD_WEEKS) -> list[bool]:
    """TILT_B's state: starts at each market episode's first week, ends once 52 weeks have passed and the zone has ended."""
    levels = list(levels)
    out, on, anchor = [], False, None
    for k, level in enumerate(levels):
        if level >= ZONE_LEVEL:
            if not on or levels[k - 1] < ZONE_LEVEL:
                anchor = k
            on = True
        elif on and k - anchor >= window:
            on = False
        out.append(on)
    return out


def voo_permission(weeks: list[str], db_path=None, end: str = END) -> list[bool]:
    """Whether VOO's weekly mid line allows a buy each week: s >= 0, or a prediction point (s < 0, s + a >= 0).

    The same VOO price closes as `boll_weekly.market_zone`; a week with no VOO close of its own uses the last one.
    """
    bars = api.benchmark_bars('1900-01-01', end, db_path or DEFAULT_DB)
    days = sorted(bars)
    voo_weeks, closes = boll_weekly.weekly_closes(days, [bars[day]['close'] for day in days])
    s, a = boll_weekly.derivatives(boll_weekly.hann_mid(closes))
    allowed = dict(zip(voo_weeks, ((s >= 0) | boll_weekly.is_prediction(s, a)).tolist()))
    out, j = [], -1
    for week in weeks:
        while j + 1 < len(voo_weeks) and voo_weeks[j + 1] <= week:
            j += 1
        out.append(bool(allowed[voo_weeks[j]]) if j >= 0 else False)
    return out


def armed_concentrated(levels, permit, window: int = HOLD_WEEKS) -> tuple[list[bool], list[bool]]:
    """TILT_C's state and its armed weeks.

    An episode's first week arms the tilt; the first armed week with `permit` starts (or renews) it and its 52-week
    count; it ends once 52 weeks have passed since that week and the market has left its zone.
    """
    levels, permit = list(levels), list(permit)
    out, armed_weeks, on, armed, anchor = [], [], False, False, None
    for k, level in enumerate(levels):
        if level >= ZONE_LEVEL and (k == 0 or levels[k - 1] < ZONE_LEVEL):
            armed = True
        armed_weeks.append(armed)
        if armed and permit[k]:
            armed, on, anchor = False, True, k
        elif on and level < ZONE_LEVEL and k - anchor >= window:
            on = False
        out.append(on)
    return out, armed_weeks


def runs(flags, weeks: list[str]) -> list[dict]:
    """Uninterrupted runs of a weekly boolean."""
    out, start = [], None
    flags = list(flags)
    for k, flag in enumerate(flags):
        if flag and start is None:
            start = k
        elif not flag and start is not None:
            out.append(dict(start=weeks[start], end=weeks[k - 1], weeks=k - start))
            start = None
    if start is not None:
        out.append(dict(start=weeks[start], end=weeks[-1], weeks=len(flags) - start))
    return out


def load(db_path=None, scope: str = SCOPE, start: str = START, end: str = END) -> dict:
    """Weekly pools, the top ten of each week with their betas, and the market's zone level."""
    db_path = db_path or DEFAULT_DB
    market = ma_pool.load_market(db_path, start, end, scope)
    weeks = [day for day in api.friday_signal_days(start, end, db_path) if day in market.index]
    rows = [market.index[day] for day in weeks]
    closes = market.values[rows]
    returns = np.full(closes.shape, np.nan)
    returns[1:] = closes[1:] / closes[:-1] - 1
    bench = api.tri_series([BENCH], start, end, db_path)[BENCH]
    by_day = {day: value for day, value, _ in bench}
    bench_closes, last = [], np.nan
    for day in weeks:
        last = by_day.get(day, last)
        bench_closes.append(last)
    bench_closes = np.asarray(bench_closes, dtype=float)
    bench_returns = np.full(bench_closes.size, np.nan)
    bench_returns[1:] = bench_closes[1:] / bench_closes[:-1] - 1

    zone = boll_weekly.market_zone(db_path, quarter_n=8, width=0.5, end=end)
    levels = [int(zone['level'].get(day, 0)) for day in weeks]
    permit = voo_permission(weeks, db_path, end)
    concentrated_c, armed = armed_concentrated(levels, permit)

    versions: dict[str, list[dict]] = {}
    top: dict[str, list[tuple]] = {}
    betas: dict[str, dict[int, float]] = {}
    for k, day in enumerate(weeks):
        version = api.pool_at(day, db_path, scope)['version_id']
        if version not in versions:
            versions[version] = ranked_members(version, db_path, scope)
        members = sorted((row for row in versions[version] if row['rank'] and row['rank'] <= TOP),
                         key=lambda row: row['rank'])
        i = market.index[day]
        kept = []
        for row in members:
            column = market.column.get(row['security_id'])
            if column is not None and np.isfinite(market.values[i, column]):
                kept.append((column, row['amount'], int(row['rank'])))
        top[day] = kept
        betas[day] = {column: beta(returns[:, column], bench_returns, k) for column, _, _ in kept}

    return dict(market=market, weeks=weeks, top=top, betas=betas, levels=levels,
                concentrated=concentrated(levels), versions=len(versions),
                permit=permit, armed=armed, concentrated_c=concentrated_c,
                bench_closes=bench_closes)


# -- weights -----------------------------------------------------------------------------------------------------

def targets(book: dict, lines=LINES) -> tuple[dict[str, dict[int, np.ndarray]], dict[str, dict[int, bool]]]:
    """Weekly target weights for the five investable lines, and whether the tilt was actually in force."""
    market = book['market']
    size = len(market.sids)
    out: dict[str, dict[int, np.ndarray]] = {name: {} for name in lines if name != 'VOO'}
    applied: dict[str, dict[int, bool]] = {name: {} for name in TILTS_V2 if name in lines}
    for k, day in enumerate(book['weeks']):
        i = market.index[day]
        pool = market.pools[day]
        equal = np.zeros(size)
        if len(pool):
            equal[pool] = 1.0 / len(pool)
        out['EW50'][i] = equal

        members = book['top'][day]
        columns = [column for column, _, _ in members]
        ten = np.zeros(size)
        if columns:
            ten[columns] = 1.0 / len(columns)
        out['EW10'][i] = ten

        amounts = np.asarray([amount for _, amount, _ in members], dtype=float)
        capped = np.zeros(size)
        if columns and amounts.sum() > 0:
            capped[columns] = amounts / amounts.sum()
        out['CW10'][i] = capped

        half = high_beta_half({column: book['betas'][day].get(column, np.nan) for column in columns})
        tilt = np.zeros(size)
        if half:
            tilt[half] = 1.0 / len(half)
        out['TILT_A'][i] = tilt if (book['levels'][k] >= ZONE_LEVEL and half) else ten
        out['TILT_B'][i] = tilt if (book['concentrated'][k] and half) else ten
        applied['TILT_A'][i] = bool(book['levels'][k] >= ZONE_LEVEL and half)
        applied['TILT_B'][i] = bool(book['concentrated'][k] and half)
        if 'TILT_C' in out:
            out['TILT_C'][i] = tilt if (book['concentrated_c'][k] and half) else ten
            applied['TILT_C'][i] = bool(book['concentrated_c'][k] and half)
    return out, applied


def voo_line(market, start: str, capital: float = CAPITAL) -> dict:
    """Buy and hold the benchmark, the same construction as `ma_pool.run` uses for its VOO row."""
    bench = api.tri_series([BENCH], start, market.days[-1], getattr(market, 'db_path', None))[BENCH]
    series = {day: value for day, value, _ in bench}
    first = series.get(start)
    if first is None:
        raise ValueError(f'{BENCH} has no total return on {start}')
    days = [day for day in market.days if day >= start]
    return dict(days=days, equity=np.array([series[day] / first * capital for day in days]),
                exposure=1.0, holdings=1.0, turnover_total=0.0)


# -- results -----------------------------------------------------------------------------------------------------

def run(db_path=None, scope: str = SCOPE, start: str = START, end: str = END, capital: float = CAPITAL,
        lines=LINES) -> dict:
    """All six lines (seven with `LINES_V2`): metrics, yearly returns and, for the two tilts, the concentrated segments."""
    book = load(db_path, scope, start, end)
    market = book['market']
    plans, applied = targets(book, lines)
    simulations = {name: ma_pool.simulate(market, start, targets_week, capital)
                   for name, targets_week in plans.items()}
    simulations['VOO'] = voo_line(market, start, capital)
    rows = {name: ma_pool.portfolio_metrics(sim) for name, sim in simulations.items()}
    voo = rows['VOO']
    for name, row in rows.items():
        row['excess_cagr'] = row['cagr'] - voo['cagr']
    return dict(book=book, simulations=simulations, metrics=rows, applied=applied,
                yearly=yearly(rows), segments=segment_rows(book, simulations, plans, applied),
                tickers=tickers(db_path))


def yearly(rows: dict) -> dict:
    """{line: {year: return}} plus the gap to VOO, both in percent-free decimals."""
    out = {}
    for name, row in rows.items():
        out[name] = dict(row['annual_returns'])
    gap = {name: {year: value - out['VOO'].get(year, float('nan')) for year, value in years.items()}
           for name, years in out.items()}
    return dict(returns=out, excess=gap)


def tickers(db_path=None) -> dict:
    conn = api.connect(db_path) if db_path else api.connect()
    return {row[0]: row[1] for row in conn.execute('select security_id, current_ticker from strategy_securities')}


def segment_rows(book: dict, simulations: dict, plans: dict, applied: dict) -> dict:
    """For each tilt: every concentrated run, its gain against EW10 over the same weeks, and the names held."""
    market = book['market']
    equity = {name: dict(zip(sim['days'], sim['equity'])) for name, sim in simulations.items()}
    names = tickers(getattr(market, 'db_path', None))
    out = {}
    flags_by_line = dict(TILT_A=[level >= ZONE_LEVEL for level in book['levels']],
                         TILT_B=book['concentrated'], TILT_C=book['concentrated_c'])
    for name in TILTS_V2:
        if name not in plans:
            continue
        flags = flags_by_line[name]
        held_by_week = []
        for k, day in enumerate(book['weeks']):
            weights = plans[name][market.index[day]]
            on = applied[name][market.index[day]]
            held_by_week.append({market.sids[column] for column in np.flatnonzero(weights > 0)} if on else set())
        rows = []
        for run_row in runs(flags, book['weeks']):
            start, end = run_row['start'], run_row['end']
            first, last = equity[name][start], equity[name][end]
            base_first, base_last = equity['EW10'][start], equity['EW10'][end]
            ratio = (last / first) / (base_last / base_first) - 1
            span = [k for k, day in enumerate(book['weeks']) if start <= day <= end]
            # one row per ticker: a company can have two security ids whose current ticker is the same
            held = sorted({names.get(sid, sid) for k in span for sid in held_by_week[k]})
            rows.append(dict(start=start, end=end, weeks=run_row['weeks'],
                             tilt_return=last / first - 1, ew10_return=base_last / base_first - 1,
                             excess=ratio, holdings=held))
        out[name] = rows
    return out
