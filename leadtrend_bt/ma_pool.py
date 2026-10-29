"""Pooled moving-average portfolios: hold the names whose SMA 50/200 is long, on the pool in force that week.

Five portfolios share one rule and one universe, rebuilt every Friday from the quarterly pool version
effective on that day:

    all_equal        every pool name at 1/M
    long_plus_cash   long names at 1/M, the rest in cash
    long_full        long names at 1/K, fully invested
    random_same_count a random draw of K pool names, 100 seeds
    voo              buy and hold

Between signal days the share counts are fixed and the weights drift with each security's total return.
Registered in research/pool_ma_v1/spec.md. Development segment only: `_check_end` refuses anything after
2024-06-28.
"""
from __future__ import annotations

import numpy as np

from leadtrend_bt import engine, metrics, trend
from leadtrend_db import api

LAST_ALLOWED_END = '2024-06-28'
START = '2014-02-28'
FIRST_DAY = '2012-12-03'
CAPITAL = 100_000.0
SEEDS = 100
FAST, SLOW = 50, 200
BENCH = 'BENCH:VOO'


def _check_end(end: str) -> str:
    if end > LAST_ALLOWED_END:
        raise ValueError(f'end {end} is after {LAST_ALLOWED_END}: the reserved segment must not be used')
    if end <= START:
        raise ValueError('end must be after the start')
    return end


def priced(pool, values, i: int) -> np.ndarray:
    """Pool columns that already have a total return on session `i`; a name with none is not investable yet."""
    return np.array([j for j in pool if np.isfinite(values[i, j])], dtype=int)


class Market:
    """The stored calendar, the total-return matrix and the long flag of every security."""

    def __init__(self, days, sids, values, long, pools, diagnostics):
        self.days = list(days)
        self.sids = list(sids)
        self.index = {day: i for i, day in enumerate(self.days)}
        self.column = {sid: j for j, sid in enumerate(self.sids)}
        self.values = values
        self.long = long
        self.pools = pools
        self.diagnostics = diagnostics


def load_market(db_path=None, start: str = START, end: str = LAST_ALLOWED_END, scope=None) -> Market:
    """Pools per signal day, total-return matrix over the full stored history, and the SMA 50/200 flag."""
    _check_end(end)
    signal_days = api.friday_signal_days(start, end, db_path)
    calendar = api.trading_days(FIRST_DAY, end, db_path)
    raw_pools, union = {}, set()
    for day in signal_days:
        pool = api.pool_at(day, db_path, scope)
        representatives = engine.Backtest._representatives(api.pool_amounts(pool['version_id'], db_path, scope),
                                                           api.pool_company_amounts(pool['version_id'], db_path, scope))
        raw_pools[day] = [row['security_id'] for row in representatives]
        union.update(raw_pools[day])
    sids = sorted(union)
    series = api.tri_series(sids, FIRST_DAY, end, db_path)
    matrix = trend.matrix({sid: {day: value for day, value, _ in series[sid]} for sid in sids}, calendar, sids)
    values = trend.carry_forward(matrix)
    long = trend.bullish(values, 'sma', FAST, SLOW)
    column = {sid: j for j, sid in enumerate(sids)}
    pools, missing = {}, 0
    for day in signal_days:
        i = calendar.index(day)
        kept = priced([column[sid] for sid in raw_pools[day]], values, i)
        missing += len(raw_pools[day]) - len(kept)
        pools[day] = np.sort(kept)
    diagnostics = dict(missing_tri_cells=missing,
                       missing_tri_securities=len({sid for day in signal_days for sid in raw_pools[day]
                                                   if not np.isfinite(values[calendar.index(day), column[sid]])}),
                       pool_versions=len({api.pool_at(day, db_path, scope)['version_id'] for day in signal_days}))
    market = Market(calendar, sids, values, long, pools, diagnostics)
    market.db_path, market.scope = db_path, scope
    return market


# -- weights -----------------------------------------------------------------------------------------------------

def equal_targets(market: Market, signal_days: list[str]) -> dict[int, np.ndarray]:
    out = {}
    for day in signal_days:
        i = market.index[day]
        pool = market.pools[day]
        weights = np.zeros(len(market.sids))
        if len(pool):
            weights[pool] = 1.0 / len(pool)
        out[i] = weights
    return out


def long_targets(market: Market, signal_days: list[str], full: bool) -> dict[int, np.ndarray]:
    """`full=False`: 1/M each, the rest cash; `full=True`: 1/K each."""
    out = {}
    for day in signal_days:
        i = market.index[day]
        pool = market.pools[day]
        weights = np.zeros(len(market.sids))
        if len(pool):
            chosen = pool[market.long[i, pool]]
            if len(chosen):
                weights[chosen] = 1.0 / (len(chosen) if full else len(pool))
        out[i] = weights
    return out


def random_targets(market: Market, signal_days: list[str], seed: int) -> dict[int, np.ndarray]:
    rng = np.random.default_rng(seed)
    out = {}
    for day in signal_days:
        i = market.index[day]
        pool = market.pools[day]
        weights = np.zeros(len(market.sids))
        count = int(market.long[i, pool].sum()) if len(pool) else 0
        if count:
            chosen = rng.choice(pool, size=count, replace=False)
            weights[chosen] = 1.0 / count
        out[i] = weights
    return out


# -- simulation --------------------------------------------------------------------------------------------------

def simulate(market: Market, start: str, targets: dict[int, np.ndarray], capital: float = CAPITAL) -> dict:
    """Daily equity from `start`'s close; weights are reset at each target day's close, shares otherwise fixed."""
    start_i = market.index[start]
    values = market.values
    shares = np.zeros(len(market.sids))
    cash = capital
    days, equity, exposure, holdings, turnover = [], [], [], [], []
    previous = np.zeros(len(market.sids) + 1)
    previous[-1] = 1.0
    for i in range(start_i, len(market.days)):
        price = values[i]
        position = float(shares @ np.nan_to_num(price))
        total = cash + position
        days.append(market.days[i])
        equity.append(total)
        exposure.append(position / total if total else 0.0)
        holdings.append(int(np.count_nonzero(shares)))
        if i in targets:
            weights = targets[i]
            safe = np.where((weights > 0) & np.isfinite(price) & (price > 0), weights, 0.0)
            shares = np.where(safe > 0, safe * total / np.where(price > 0, price, 1.0), 0.0)
            cash = total - float(shares @ np.nan_to_num(price))
            cash_weight = cash / total if total else 0.0
            current = np.append(safe, cash_weight)
            turnover.append(0.5 * float(np.abs(current - previous).sum()))
            previous = current
        else:
            turnover.append(0.0)
    return dict(days=days, equity=np.array(equity), exposure=float(np.mean(exposure)),
                holdings=float(np.mean(holdings)), turnover_total=float(np.sum(turnover)))


def portfolio_metrics(simulation: dict) -> dict:
    row = metrics.series_metrics(simulation['days'], [float(v) for v in simulation['equity']], {})
    row['average_exposure'] = simulation['exposure']
    row['average_holdings'] = simulation['holdings']
    years = max(row['sessions'], 1) / metrics.TRADING_DAYS
    row['annual_turnover'] = simulation['turnover_total'] / years if years else float('nan')
    row['cagr_over_dd'] = (row['cagr'] / abs(row['max_drawdown'])) if row['max_drawdown'] else float('nan')
    return row


def run(market: Market, signal_days: list[str], start: str = START, capital: float = CAPITAL,
        seeds: int = SEEDS, scope=None) -> dict:
    """The four deterministic portfolios and the random band."""
    if scope is not None and getattr(market, 'scope', None) != scope:
        raise ValueError('market scope does not match requested scope')
    out = {}
    plans = dict(all_equal=equal_targets(market, signal_days),
                 long_plus_cash=long_targets(market, signal_days, full=False),
                 long_full=long_targets(market, signal_days, full=True))
    for name, targets in plans.items():
        simulation = simulate(market, start, targets, capital)
        out[name] = portfolio_metrics(simulation)
        out[name]['equity'] = simulation['equity']

    bench = api.tri_series([BENCH], start, market.days[-1], getattr(market, 'db_path', None))
    series = {day: value for day, value, _ in bench[BENCH]}
    first = series.get(start)
    if first is None:
        raise ValueError(f'{BENCH} has no total return on {start}')
    days = [day for day in market.days if day >= start]
    voo_simulation = dict(days=days, equity=np.array([series[d] / first * capital for d in days]),
                          exposure=1.0, holdings=1.0, turnover_total=0.0)
    out['voo'] = portfolio_metrics(voo_simulation)
    out['voo']['equity'] = voo_simulation['equity']

    random_rows, random_equity = [], []
    for seed in range(seeds):
        simulation = simulate(market, start, random_targets(market, signal_days, seed), capital)
        row = portfolio_metrics(simulation)
        row['seed'] = seed
        random_rows.append(row)
        random_equity.append(simulation['equity'])
    return dict(portfolios=out, random=random_rows, random_equity=random_equity)


def quantiles(rows: list[dict], key: str) -> dict:
    values = [row[key] for row in rows if row.get(key) is not None and np.isfinite(row[key])]
    if not values:
        return dict(median=None, p05=None, p95=None)
    return dict(median=float(np.median(values)), p05=float(np.percentile(values, 5)),
                p95=float(np.percentile(values, 95)))
